import hmac
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import CurrentUser, require_buyer_user, require_seller_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.transaction import TransactionStatus
from app.models.user import User, UserRole
from app.schemas.payment_prompt import PaymentInstructionResponse, TransactionCreatedResponse
from app.schemas.transaction import (
    BuyerReconfirmationRequest,
    CreateTransactionRequest,
    OtpGiveRequest,
    OtpWithholdRequest,
    PaymentCallbackRequest,
    PaymentCallbackResponse,
    ReportFunctionalIssueRequest,
    SellerResolutionActionRequest,
    TransactionResponse,
)
from app.schemas.transaction_detail import (
    TimelineEventResponse,
    TransactionDetailResponse,
    TransactionListResponse,
)
from app.services.payment_prompts import (
    PaymentResendInitiationError,
    PaymentResendInvalidStateError,
    PaymentResendNotFoundError,
    PaymentResendTooSoonError,
    latest_payment_prompt,
    prompt_state,
    resend_payment_prompt,
)
from app.services.transaction import (
    InvalidTransactionOtpError,
    ListingNotFoundForTransactionError,
    PaymentCallbackResult,
    PaymentInitiationError,
    TransactionArrivalForbiddenError,
    TransactionArrivalInvalidStateError,
    TransactionBuyerActionForbiddenError,
    TransactionBuyerActionInvalidStateError,
    TransactionDispatchForbiddenError,
    TransactionDispatchInvalidStateError,
    TransactionNotFoundForArrivalError,
    TransactionNotFoundForBuyerActionError,
    TransactionNotFoundForCallbackError,
    TransactionNotFoundForDispatchError,
    TransactionValidationError,
    apply_seller_resolution_action,
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    create_transaction,
    dispatch_transaction,
    mark_buyer_sent_back,
    mark_seller_received,
    mark_transaction_arrived,
    report_functional_issue,
    submit_buyer_reconfirmation,
    submit_buyer_resolution_confirmation,
    submit_seller_resolution_confirmation,
    withhold_buyer_delivery_otp,
)
from app.services.transaction_list import (
    OrderListForbiddenError,
    OrderSegment,
    OrderSort,
    list_orders_for_viewer,
)
from app.services.transaction_payout import seller_payout
from app.services.transaction_read import TransactionNotVisibleError, get_transaction_for_viewer
from app.services.transaction_timeline import get_transaction_timeline

router = APIRouter(prefix="/transactions", tags=["Transactions"])
audit_logger = logging.getLogger("app.transactions.audit")

PAYMENT_CALLBACK_SECRET_HEADER = "X-Payment-Callback-Secret"


def require_payment_callback_auth(
    x_payment_callback_secret: Annotated[
        str | None,
        Header(
            alias=PAYMENT_CALLBACK_SECRET_HEADER,
            description="Shared secret; required when PAYMENT_CALLBACK_SECRET is set.",
        ),
    ] = None,
) -> None:
    """Authenticate the payment callback (issue #164).

    With PAYMENT_CALLBACK_SECRET set, the header must match it (constant-time).
    Without it, the callback is refused in production and allowed elsewhere so the
    local simulated rail and end-to-end tests keep working.
    """
    settings = get_settings()
    expected = settings.payment_callback_secret.strip()
    if expected:
        provided = (x_payment_callback_secret or "").strip()
        if provided and hmac.compare_digest(provided.encode(), expected.encode()):
            return
        audit_logger.warning("payment_callback_rejected reason=bad_or_missing_secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payment callback authentication failed.",
        )
    if settings.environment.strip().lower() == "production":
        audit_logger.warning("payment_callback_rejected reason=secret_not_configured")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payment callback authentication failed.",
        )


@router.post(
    "",
    response_model=TransactionCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create transaction (buyer only)",
    description=(
        "Create an escrow transaction from a listing as the authenticated buyer. "
        "New transactions start in awaiting_payment state. The response carries the payment "
        "instruction: an STK prompt sent to the buyer's phone, or a checkout URL to redirect to."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": (
                "Transaction created, but payment initiation transport failed "
                "(payment.status is failed_to_initiate). Continue with this transaction id "
                "and offer a resend."
            )
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not a buyer."},
        status.HTTP_404_NOT_FOUND: {"description": "Listing not found."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "Payment initiation failed before a partial-success response " "could be returned."
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Business-rule or payload validation failed."
        },
    },
)
def create_buyer_transaction(
    payload: CreateTransactionRequest,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> TransactionCreatedResponse:
    try:
        transaction = create_transaction(db, buyer=current_user, payload=payload)
    except ListingNotFoundForTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found.",
        ) from exc
    except PaymentInitiationError as exc:
        if exc.transaction is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["X-Payment-Initiation-Status"] = "failed"
        return _created_response(db, exc.transaction)
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return _created_response(db, transaction)


def _payment_instruction(db: Session, transaction_id: uuid.UUID):
    prompt = latest_payment_prompt(db, transaction_id)
    return PaymentInstructionResponse.from_state(prompt_state(prompt)) if prompt else None


def _created_response(db: Session, transaction) -> TransactionCreatedResponse:
    payment = _payment_instruction(db, transaction.id)
    if payment is None:  # pragma: no cover - create_transaction always records a prompt
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment prompt was not recorded.",
        )
    return TransactionCreatedResponse(
        **TransactionResponse.model_validate(transaction).model_dump(),
        payment=payment,
    )


@router.get(
    "",
    response_model=TransactionListResponse,
    status_code=status.HTTP_200_OK,
    summary="List my orders (buyer or seller)",
    description=(
        "The caller's own orders: orders they bought (buyers) or sold (sellers). Filter by list "
        "segment, by status (repeatable) and by reference or listing title; sort and page. "
        "`counts` and `status_counts` cover all the caller's orders, before filters."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Only buyers and sellers have orders."},
    },
)
def list_my_transactions(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    segment: Annotated[OrderSegment | None, Query(description="List segment.")] = None,
    status_filter: Annotated[
        list[TransactionStatus] | None,
        Query(alias="status", description="Only these statuses (repeat the parameter)."),
    ] = None,
    q: Annotated[
        str | None, Query(max_length=64, description="Reference or listing title contains.")
    ] = None,
    sort: Annotated[OrderSort, Query()] = OrderSort.NEWEST,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TransactionListResponse:
    try:
        result = list_orders_for_viewer(
            db,
            viewer=current_user,
            segment=segment,
            statuses=status_filter,
            q=q,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    except OrderListForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return TransactionListResponse.from_page(result)


@router.get(
    "/{transaction_id}",
    response_model=TransactionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get one transaction (buyer, seller or admin)",
    description=(
        "Read a transaction as its buyer, its seller or an admin: state, listing summary, "
        "masked parties and the server deadline for the current state. Clients poll this to "
        "learn state changes; anyone else gets 404 so existence is not leaked."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_404_NOT_FOUND: {
            "description": "Transaction not found, or the caller is not a party to it."
        },
    },
)
def get_transaction(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> TransactionDetailResponse:
    try:
        view = get_transaction_for_viewer(db, transaction_id=transaction_id, viewer=current_user)
    except TransactionNotVisibleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc

    return TransactionDetailResponse.from_view(
        view,
        payment=_payment_instruction(db, view.transaction.id),
        payout=(
            seller_payout(db, view.transaction) if view.viewer_role != UserRole.BUYER else None
        ),
    )


@router.get(
    "/{transaction_id}/timeline",
    response_model=list[TimelineEventResponse],
    status_code=status.HTTP_200_OK,
    summary="Get an order's timeline (buyer, seller or admin)",
    description=(
        "The order's history, oldest first, in plain words for the caller's role. Built from the "
        "caller's own notifications; message bodies and payloads (which can hold the "
        "delivery code) are never returned."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_404_NOT_FOUND: {
            "description": "Transaction not found, or the caller is not a party to it."
        },
    },
)
def get_timeline(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> list[TimelineEventResponse]:
    try:
        events = get_transaction_timeline(db, transaction_id=transaction_id, viewer=current_user)
    except TransactionNotVisibleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    return [
        TimelineEventResponse(kind=e.kind, at=e.at, title=e.title, description=e.description)
        for e in events
    ]


@router.post(
    "/{transaction_id}/payment/resend",
    response_model=PaymentInstructionResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend the payment prompt (buyer only)",
    description=(
        "Send a new STK prompt for an unpaid transaction, for example after the buyer cancelled "
        "or missed it. At most one prompt per cooldown (see resend_available_at)."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "A prompt was recorded but sending it failed (failed_to_initiate)."
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not a buyer."},
        status.HTTP_404_NOT_FOUND: {
            "description": "Transaction not found, or the caller is not its buyer."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not awaiting payment."
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Too soon after the last prompt; see the Retry-After header."
        },
    },
)
def resend_transaction_payment_prompt(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> PaymentInstructionResponse:
    try:
        prompt = resend_payment_prompt(db, transaction_id=transaction_id, buyer=current_user)
    except PaymentResendNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except PaymentResendInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except PaymentResendTooSoonError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except PaymentResendInitiationError as exc:
        response.status_code = status.HTTP_202_ACCEPTED
        return PaymentInstructionResponse.from_state(prompt_state(exc.prompt))

    return PaymentInstructionResponse.from_state(prompt_state(prompt))


@router.post(
    "/payment-callback",
    response_model=PaymentCallbackResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm payment callback and lock transaction",
    description=(
        "Provider callback endpoint that transitions awaiting_payment transactions to locked "
        "on successful result_code and safely handles duplicate/non-success callbacks. "
        "Requires the X-Payment-Callback-Secret header when PAYMENT_CALLBACK_SECRET is set; "
        "without a configured secret it is refused in production. eConfirm transactions are "
        "never locked here (their funding is confirmed by polling eConfirm)."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Callback acknowledged with no state transition."
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Wrong or missing callback secret, or none configured in production."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Request payload failed validation."
        },
    },
)
def payment_confirmation_callback(
    payload: PaymentCallbackRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    _auth: Annotated[None, Depends(require_payment_callback_auth)],
) -> PaymentCallbackResponse:
    try:
        callback_result: PaymentCallbackResult = confirm_payment_callback(db, payload=payload)
    except TransactionNotFoundForCallbackError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc

    if not callback_result.transitioned and not callback_result.duplicate:
        response.status_code = status.HTTP_202_ACCEPTED

    return PaymentCallbackResponse(
        transaction_id=callback_result.transaction.id,
        status=callback_result.transaction.status,
        transitioned=callback_result.transitioned,
        duplicate=callback_result.duplicate,
        detail=callback_result.detail,
    )


@router.post(
    "/{transaction_id}/dispatch",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Dispatch locked transaction (seller only)",
    description=(
        "Seller-owned transition from locked to out_for_delivery. "
        "Rejects non-owner access and invalid prior states."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to dispatch this transaction."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for dispatch."
        },
    },
)
def dispatch_locked_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = dispatch_transaction(
            db,
            transaction_id=transaction_id,
            seller=current_user,
        )
    except TransactionNotFoundForDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionDispatchForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionDispatchInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/arrival",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark delivery arrival (seller only)",
    description=(
        "Seller-owned transition from out_for_delivery to at_door_pending_inspection. "
        "Generates a delivery OTP for buyer confirmation."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to mark arrival for this transaction."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for arrival confirmation."
        },
    },
)
def mark_delivery_arrival(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = mark_transaction_arrived(
            db,
            transaction_id=transaction_id,
            seller=current_user,
        )
    except TransactionNotFoundForArrivalError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionArrivalForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionArrivalInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/otp-give",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm delivery OTP (buyer only)",
    description=(
        "Buyer confirmation step for delivery handoff. "
        "Transitions to released for non-serialized listings or hold_24h for serialized listings."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to confirm OTP for this transaction."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction or listing not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid OTP or transaction not in OTP-confirmable state."
        },
    },
)
def confirm_delivery_otp(
    transaction_id: uuid.UUID,
    payload: OtpGiveRequest,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = confirm_buyer_delivery_otp(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
            otp_code=payload.otp_code,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except InvalidTransactionOtpError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ListingNotFoundForTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found.",
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/otp-withhold",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Withhold delivery OTP and trigger return-refund (buyer only)",
    description=(
        "Buyer action that withholds OTP and applies return/refund path transitions "
        "from at_door_pending_inspection. An optional body gives the reason and a note "
        "for the seller."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to withhold OTP for this transaction."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for OTP-withhold flow."
        },
    },
)
def withhold_delivery_otp(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
    payload: OtpWithholdRequest | None = None,
) -> TransactionResponse:
    try:
        transaction = withhold_buyer_delivery_otp(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
            reason=payload.reason if payload else None,
            note=payload.note if payload else None,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/report-functional-issue",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Report functional issue during hold window (buyer only)",
    description=(
        "Buyer dispute-intake endpoint for serialized transactions in hold_24h state. "
        "Routes category other directly to admin escalation path."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Authenticated user is not allowed to report an issue " "for this transaction."
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction or listing not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction state, listing type, or issue category is invalid."
        },
    },
)
def report_functional_issue_for_transaction(
    transaction_id: uuid.UUID,
    payload: ReportFunctionalIssueRequest,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = report_functional_issue(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
            category=payload.category,
            description=payload.description,
            evidence=payload.evidence,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ListingNotFoundForTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found.",
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/buyer-sent-back",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark buyer sent-back action in dispute flow (buyer only)",
    description=(
        "Buyer transition from disputed_functional to return_in_transit in functional dispute flow."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to update sent-back state."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for buyer sent-back action."
        },
    },
)
def mark_buyer_sent_back_for_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = mark_buyer_sent_back(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/seller-received",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark seller received return in dispute flow (seller only)",
    description=(
        "Seller transition from return_in_transit to return_received in functional dispute flow."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to update seller-received state."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for seller-received action."
        },
    },
)
def mark_seller_received_for_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = mark_seller_received(
            db,
            transaction_id=transaction_id,
            seller=current_user,
        )
    except TransactionNotFoundForDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionDispatchForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionDispatchInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/seller-resolution-action",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Apply seller dispute resolution action (seller only)",
    description=(
        "Seller remediation action endpoint from return_received state. "
        "Supports refund_issued, repair_shipped, and replacement_shipped (policy-gated)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to apply resolution action."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction or listing not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Action is invalid or transaction is not eligible for seller resolution."
        },
    },
)
def apply_seller_resolution_action_for_transaction(
    transaction_id: uuid.UUID,
    payload: SellerResolutionActionRequest,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = apply_seller_resolution_action(
            db,
            transaction_id=transaction_id,
            seller=current_user,
            action=payload.action,
            notes=payload.notes,
        )
    except TransactionNotFoundForDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionDispatchForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionDispatchInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ListingNotFoundForTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found.",
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/buyer-reconfirmation",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit buyer reconfirmation response (buyer only)",
    description=(
        "Buyer response to seller remediation outcome from awaiting_buyer_reconfirmation. "
        "Reject path retries once before escalated_admin_review."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to submit reconfirmation."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not eligible for buyer reconfirmation."
        },
    },
)
def submit_buyer_reconfirmation_for_transaction(
    transaction_id: uuid.UUID,
    payload: BuyerReconfirmationRequest,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = submit_buyer_reconfirmation(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
            accepted=payload.accepted,
            notes=payload.notes,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/confirm-resolved-buyer",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Buyer confirmation for mutual resolution closure",
    description=(
        "Buyer-side mutual confirmation update for resolved transactions. "
        "Closure requires both buyer and seller confirmations."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to confirm buyer resolution state."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not in a resolved state or validation failed."
        },
    },
)
def confirm_resolved_buyer_for_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = submit_buyer_resolution_confirmation(
            db,
            transaction_id=transaction_id,
            buyer=current_user,
        )
    except TransactionNotFoundForBuyerActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionBuyerActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionBuyerActionInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)


@router.post(
    "/{transaction_id}/confirm-resolved-seller",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Seller confirmation for mutual resolution closure",
    description=(
        "Seller-side mutual confirmation update for resolved transactions. "
        "Closure requires both buyer and seller confirmations."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user is not allowed to confirm seller resolution state."
        },
        status.HTTP_404_NOT_FOUND: {"description": "Transaction not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Transaction is not in a resolved state or validation failed."
        },
    },
)
def confirm_resolved_seller_for_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = submit_seller_resolution_confirmation(
            db,
            transaction_id=transaction_id,
            seller=current_user,
        )
    except TransactionNotFoundForDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        ) from exc
    except TransactionDispatchForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except TransactionDispatchInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TransactionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return TransactionResponse.model_validate(transaction)
