import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_buyer_user, require_seller_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.transaction import (
    CreateTransactionRequest,
    PaymentCallbackRequest,
    PaymentCallbackResponse,
    TransactionResponse,
)
from app.services.transaction import (
    ListingNotFoundForTransactionError,
    PaymentCallbackResult,
    TransactionDispatchForbiddenError,
    TransactionDispatchInvalidStateError,
    TransactionNotFoundForCallbackError,
    TransactionNotFoundForDispatchError,
    TransactionValidationError,
    confirm_payment_callback,
    create_transaction,
    dispatch_transaction,
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
