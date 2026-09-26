import logging
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier, get_current_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, RegisterUserRequest, RegisterUserResponse
from app.services.auth import (
    AccountLockedError,
    InvalidCredentialsError,
    PhoneAlreadyRegisteredError,
    TooManyLoginAttemptsError,
    authenticate_and_create_session,
    invalidate_session,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger("app.auth.audit")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


@router.post(
    "/register",
    response_model=RegisterUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register buyer or seller",
    description=(
        "Create a buyer or seller account with validated phone and password fields. "
        "Registration currently supports only buyer and seller roles."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"description": "Phone number is already registered."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Request payload failed validation."
        },
    },
)
def register(
    payload: RegisterUserRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> RegisterUserResponse:
    try:
        created_user = register_user(db, payload)
    except PhoneAlreadyRegisteredError as exc:
        logger.warning(
            "register_conflict phone=%s client_ip=%s",
            payload.phone,
            get_client_identifier(request),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone number is already registered.",
        ) from exc

    logger.info(
        "register_success user_id=%s phone=%s role=%s client_ip=%s",
        created_user.id,
        created_user.phone,
        created_user.role,
        get_client_identifier(request),
    )
    return RegisterUserResponse.model_validate(created_user)


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Login user and create session",
    description=(
        "Authenticate a user, issue server-side session state, and set session + CSRF cookies. "
        "Rate limiting and account lockout protections are enforced."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Invalid phone or password."},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Too many login attempts or account temporarily locked."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Request payload failed validation."
        },
    },
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    try:
        authenticated = authenticate_and_create_session(
            db,
            payload,
            client_identifier=get_client_identifier(request),
        )
    except TooManyLoginAttemptsError as exc:
        logger.warning(
            "login_rate_limited phone=%s client_ip=%s retry_after=%s",
            payload.phone,
            get_client_identifier(request),
            exc.retry_after_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except AccountLockedError as exc:
        logger.warning(
            "login_account_locked phone=%s client_ip=%s retry_after=%s",
            payload.phone,
            get_client_identifier(request),
            exc.retry_after_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except InvalidCredentialsError as exc:
        logger.warning(
            "login_invalid_credentials phone=%s client_ip=%s",
            payload.phone,
            get_client_identifier(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid phone or password.",
        ) from exc

    settings = get_settings()
    session_samesite = cast(
        Literal["lax", "strict", "none"],
        settings.session_cookie_samesite,
    )
    csrf_samesite = cast(
        Literal["lax", "strict", "none"],
        settings.csrf_cookie_samesite,
    )

    response.set_cookie(
        key=settings.session_cookie_name,
        value=authenticated.session_token,
        max_age=settings.session_cookie_max_age_seconds,
        expires=settings.session_cookie_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=session_samesite,
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=authenticated.csrf_token,
        max_age=settings.session_cookie_max_age_seconds,
        expires=settings.session_cookie_max_age_seconds,
        httponly=False,
        secure=settings.csrf_cookie_secure,
        samesite=csrf_samesite,
        path="/",
    )

    logger.info(
        "login_success user_id=%s phone=%s client_ip=%s",
        authenticated.user.id,
        authenticated.user.phone,
        get_client_identifier(request),
    )

    return LoginResponse(
        id=authenticated.user.id,
        name=authenticated.user.name,
        phone=authenticated.user.phone,
        role=authenticated.user.role,
        mpesa_phone=authenticated.user.mpesa_phone,
        mpesa_account_name=authenticated.user.mpesa_account_name,
        is_active=authenticated.user.is_active,
        created_at=authenticated.user.created_at,
        session_expires_at=authenticated.expires_at,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout user and revoke session",
    description=(
        "Revoke the active server-side session when present and clear authentication cookies."
    ),
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "Session revoked (or no active session)."},
        status.HTTP_401_UNAUTHORIZED: {"description": "Invalid or expired session."},
        status.HTTP_403_FORBIDDEN: {"description": "CSRF token missing, mismatched, or invalid."},
    },
)
def logout(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    settings = get_settings()
    session_token = request.cookies.get(settings.session_cookie_name)
    invalidated = invalidate_session(db, session_token)

    response.delete_cookie(key=settings.session_cookie_name, path="/")
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    logger.info(
        "logout_result invalidated=%s client_ip=%s",
        invalidated,
        get_client_identifier(request),
    )
    return response


@router.get(
    "/me",
    response_model=LoginResponse,
    summary="Get current authenticated user",
    description="Return the currently authenticated user profile based on active session context.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
    },
)
def me(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> LoginResponse:
    current_session = getattr(request.state, "current_session", None)
    if current_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    return LoginResponse(
        id=current_user.id,
        name=current_user.name,
        phone=current_user.phone,
        role=current_user.role,
        mpesa_phone=current_user.mpesa_phone,
        mpesa_account_name=current_user.mpesa_account_name,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        session_expires_at=current_session.expires_at,
    )
