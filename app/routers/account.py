from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier, get_current_user, require_seller_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.account import (
    DeviceSessionResponse,
    NotificationPreferences,
    PasswordChange,
    PasswordChangeResponse,
    PasswordReset,
    ResetCodeRequest,
    ResetCodeResponse,
    RevokedResponse,
)
from app.services.account_security import (
    CurrentSessionError,
    SessionNotFoundError,
    WrongPasswordError,
    change_password,
    list_sessions,
    request_reset_code,
    reset_password,
    revoke_other_sessions,
    revoke_session,
)
from app.services.notification_delivery import run_in_background
from app.services.notification_prefs import preferences, set_preferences
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneCodeRateLimitedError,
)

router = APIRouter(tags=["Account"])
AUTH = {status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."}}


def _session_id(request: Request) -> UUID:
    session = getattr(request.state, "current_session", None)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return session.id


def _user(db: Session, current: User) -> User:
    user = db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def _limited(exc: PhoneCodeRateLimitedError) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many tries. Please wait before trying again.",
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@router.post(
    "/auth/password/change",
    response_model=PasswordChangeResponse,
    summary="Change the password",
    description="Needs the current password. Every other device is signed out.",
    responses={**AUTH, status.HTTP_400_BAD_REQUEST: {"description": "Current password is wrong."}},
)
def post_change_password(
    payload: PasswordChange,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasswordChangeResponse:
    try:
        revoked = change_password(
            db,
            _user(db, current_user),
            current_password=payload.current_password,
            new_password=payload.new_password,
            keep_session=_session_id(request),
        )
    except WrongPasswordError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Your current password doesn't match."
        ) from exc
    return PasswordChangeResponse(signed_out_devices=revoked)


@router.post(
    "/auth/password/reset/request-code",
    response_model=ResetCodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Text a password reset code",
    description=(
        "Always answers 202, so it doesn't reveal which numbers have accounts; only an account's "
        "number gets the SMS."
    ),
    responses={status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many codes."}},
)
def post_reset_code(
    payload: ResetCodeRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> ResetCodeResponse:
    settings = get_settings()
    try:
        issued = request_reset_code(
            db,
            payload.phone,
            client_identifier=get_client_identifier(request),
            dispatch=run_in_background,
        )
    except PhoneCodeRateLimitedError as exc:
        raise _limited(exc) from exc
    return ResetCodeResponse(
        resend_after_seconds=settings.phone_code_resend_seconds,
        dev_code=issued.code if issued and settings.phone_code_dev_echo else None,
    )


@router.post(
    "/auth/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password with the SMS code",
    description="Every device is signed out; log in again with the new password.",
    responses={
        status.HTTP_410_GONE: {"description": "No active code."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The code doesn't match."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many wrong tries."},
    },
)
def post_reset(payload: PasswordReset, db: Annotated[Session, Depends(get_db)]) -> Response:
    try:
        reset_password(db, payload.phone, payload.code, payload.new_password)
    except PhoneCodeInvalidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That code doesn't match."
        ) from exc
    except PhoneCodeExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="That code has expired.") from exc
    except PhoneCodeRateLimitedError as exc:
        raise _limited(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/auth/sessions",
    response_model=list[DeviceSessionResponse],
    summary="Signed-in devices",
    description="This device first, then the most recently used.",
    responses=AUTH,
)
def get_sessions(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[DeviceSessionResponse]:
    return [
        DeviceSessionResponse(**vars(s))
        for s in list_sessions(db, current_user.id, _session_id(request))
    ]


@router.delete(
    "/auth/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out one other device",
    responses={
        **AUTH,
        status.HTTP_404_NOT_FOUND: {"description": "No such active session of yours."},
        status.HTTP_409_CONFLICT: {"description": "That's this device; use log out."},
    },
)
def delete_session(
    session_id: UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        revoke_session(db, current_user.id, session_id, _session_id(request))
    except CurrentSessionError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Use log out for this device."
        ) from exc
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/auth/sessions/revoke-others",
    response_model=RevokedResponse,
    summary="Sign out every other device",
    responses=AUTH,
)
def post_revoke_others(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RevokedResponse:
    return RevokedResponse(revoked=revoke_other_sessions(db, current_user.id, _session_id(request)))


@router.get(
    "/me/notification-preferences",
    response_model=NotificationPreferences,
    summary="Notification preferences (sellers)",
    responses={**AUTH, status.HTTP_403_FORBIDDEN: {"description": "Not a seller."}},
)
def get_preferences(
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationPreferences:
    return NotificationPreferences.model_validate(preferences(_user(db, current_user)))


@router.put(
    "/me/notification-preferences",
    response_model=NotificationPreferences,
    summary="Save notification preferences (sellers)",
    description="Support decisions in the app can't be turned off; that value is ignored.",
    responses={**AUTH, status.HTTP_403_FORBIDDEN: {"description": "Not a seller."}},
)
def put_preferences(
    payload: NotificationPreferences,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationPreferences:
    user = _user(db, current_user)
    set_preferences(user, payload.model_dump())
    db.commit()
    return NotificationPreferences.model_validate(preferences(user))
