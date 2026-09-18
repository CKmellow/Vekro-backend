from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth import RegisterUserRequest, RegisterUserResponse
from app.services.auth import PhoneAlreadyRegisteredError, register_user

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/register",
    response_model=RegisterUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register buyer or seller",
)
def register(
    payload: RegisterUserRequest,
    db: Annotated[Session, Depends(get_db)],
) -> RegisterUserResponse:
    try:
        created_user = register_user(db, payload)
    except PhoneAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone number is already registered.",
        ) from exc

    return RegisterUserResponse.model_validate(created_user)
