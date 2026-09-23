import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_buyer_user, require_seller_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.transaction import (
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
    withhold_buyer_delivery_otp,
)

router = APIRouter(prefix="/transactions", tags=["Transactions"])


@router.post(
    "",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create transaction (buyer only)",
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
