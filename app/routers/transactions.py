from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_buyer_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.transaction import CreateTransactionRequest, TransactionResponse
from app.services.transaction import (
    ListingNotFoundForTransactionError,
    TransactionValidationError,
    create_transaction,
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
