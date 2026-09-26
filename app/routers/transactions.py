import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_buyer_user, require_seller_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.transaction import (
    BuyerReconfirmationRequest,
    CreateTransactionRequest,
    OtpGiveRequest,
    PaymentCallbackRequest,
    PaymentCallbackResponse,
    ReportFunctionalIssueRequest,
    SellerResolutionActionRequest,
    TransactionResponse,
)
from app.services.transaction import (
    InvalidTransactionOtpError,
    ListingNotFoundForTransactionError,
    PaymentCallbackResult,
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

router = APIRouter(prefix="/transactions", tags=["Transactions"])


@router.post(
    "",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create transaction (buyer only)",
    description=(
        "Create an escrow transaction from a listing as the authenticated buyer. "
        "New transactions start in awaiting_payment state."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not a buyer."},
        status.HTTP_404_NOT_FOUND: {"description": "Listing not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Business-rule or payload validation failed."
        },
    },
)
def create_buyer_transaction(
    payload: CreateTransactionRequest,
    current_user: Annotated[User, Depends(require_buyer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TransactionResponse:
    try:
        transaction = create_transaction(db, buyer=current_user, payload=payload)
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
    "/payment-callback",
    response_model=PaymentCallbackResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm payment callback and lock transaction",
    description=(
        "Provider callback endpoint that transitions awaiting_payment transactions to locked "
        "on successful result_code and safely handles duplicate/non-success callbacks."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Callback acknowledged with no state transition."
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
        "from at_door_pending_inspection."
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
) -> TransactionResponse:
    try:
        transaction = withhold_buyer_delivery_otp(
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
