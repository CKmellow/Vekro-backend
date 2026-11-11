"""Continue with Google (#324): routes. The flow and its rules are in app/services/google_auth.py.

The callback is reached through the web app's /api proxy ({FRONTEND_URL}/api/auth/google/callback),
so every redirect here is a path on the same site.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier, get_current_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User
from app.routers.auth import _open_session
from app.schemas.auth import (
    GoogleLinkRequest,
    GoogleLinkResponse,
    GoogleSignupCodeRequest,
    GoogleSignupCompleteRequest,
    GoogleSignupResponse,
    GoogleStatusResponse,
    LoginResponse,
    PhoneCodeRequestResponse,
)
from app.services.auth import create_session
from app.services.google_auth import (
    SIGNUP_COOKIE,
    SIGNUP_TTL,
    STATE_COOKIE,
    STATE_TTL,
    GoogleAuthError,
    complete_callback,
    complete_signup,
    pending_signup,
    request_signup_code,
    safe_return_to,
    start_authorization,
    unlink_google,
)
from app.services.notification_delivery import run_in_background
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneCodeRateLimitedError,
)

router = APIRouter(tags=["Auth"])
logger = logging.getLogger("app.auth.google")

_STATUS_FOR = {
    "not_configured": status.HTTP_404_NOT_FOUND,
    "signup_expired": status.HTTP_410_GONE,
    "phone_taken": status.HTTP_409_CONFLICT,
    "taken": status.HTTP_409_CONFLICT,
    "password_needed": status.HTTP_409_CONFLICT,
    "not_linked": status.HTTP_409_CONFLICT,
}


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    settings = get_settings()
    # Lax: the browser sends it on Google's top-level redirect back to the callback.
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _login_redirect(code: str) -> str:
    return f"/login?{urlencode({'google': code})}"


@router.get(
    "/auth/google/status",
    response_model=GoogleStatusResponse,
    summary="Whether Continue with Google is offered",
)
def google_status() -> GoogleStatusResponse:
    return GoogleStatusResponse(enabled=get_settings().google_enabled)


@router.get(
    "/auth/google/start",
    summary="Start Continue with Google (browser navigation)",
    description=(
        "Redirects to Google's consent screen with a single-use state, a nonce and a PKCE S256 "
        "challenge; the state is also bound to this browser by an HttpOnly cookie."
    ),
    status_code=status.HTTP_302_FOUND,
)
def google_start(
    db: Annotated[Session, Depends(get_db)], return_to: str | None = None
) -> RedirectResponse:
    try:
        url, state = start_authorization(db, get_settings(), mode="login", return_to=return_to)
    except GoogleAuthError as exc:
        return RedirectResponse(_login_redirect(exc.code), status_code=status.HTTP_302_FOUND)
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    _set_cookie(response, STATE_COOKIE, state, int(STATE_TTL.total_seconds()))
    return response


@router.get(
    "/auth/google/callback",
    summary="Google's redirect back (OIDC callback)",
    description=(
        "Checks the state against this browser's cookie (single use), exchanges the code with the "
        "PKCE verifier, verifies the ID token (signature, issuer, audience, expiry, nonce, verified"
        " e-mail) and signs in by google_sub only, links it, or starts a seller sign-up."
    ),
    status_code=status.HTTP_302_FOUND,
)
def google_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    try:
        result = complete_callback(
            db,
            settings,
            state=state,
            cookie_state=request.cookies.get(STATE_COOKIE),
            code=code,
            error=error,
        )
    except GoogleAuthError as exc:
        logger.warning("google_callback_refused code=%s", exc.code)
        target = (
            f"{exc.link_return_to}?{urlencode({'google': exc.code})}"
            if exc.link_return_to
            else _login_redirect(exc.code)
        )
        response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
        response.delete_cookie(STATE_COOKIE, path="/")
        return response

    if result.kind == "signup":
        response = RedirectResponse("/register/google", status_code=status.HTTP_302_FOUND)
        _set_cookie(
            response, SIGNUP_COOKIE, result.signup_token or "", int(SIGNUP_TTL.total_seconds())
        )
    elif result.kind == "linked":
        target = f"{result.return_to or '/'}?{urlencode({'google': 'linked'})}"
        response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    else:
        assert result.user is not None
        params = {"google": "signed_in"}
        if result.return_to:
            params["returnTo"] = result.return_to
        response = RedirectResponse(
            f"/login?{urlencode(params)}", status_code=status.HTTP_302_FOUND
        )
        authenticated = create_session(
            db, result.user, datetime.now(UTC), user_agent=request.headers.get("user-agent")
        )
        _open_session(response, authenticated)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post(
    "/account/google/link",
    response_model=GoogleLinkResponse,
    summary="Start linking Google to the signed-in account",
    description=(
        "Signed in only (CSRF). Returns Google's URL; the callback links this Google account to "
        "this user. Never links by matching e-mail."
    ),
    responses={status.HTTP_404_NOT_FOUND: {"description": "Google sign-in is not set up."}},
)
def google_link(
    payload: GoogleLinkRequest,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> GoogleLinkResponse:
    try:
        url, state = start_authorization(
            db,
            get_settings(),
            mode="link",
            user=current_user,
            return_to=safe_return_to(payload.return_to),
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=_STATUS_FOR.get(exc.code, 400), detail=str(exc)) from exc
    _set_cookie(response, STATE_COOKIE, state, int(STATE_TTL.total_seconds()))
    return GoogleLinkResponse(authorization_url=url)


@router.delete(
    "/account/google",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink Google from the signed-in account",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Not linked, or Google is the only way in (set a password first)."
        }
    },
)
def google_unlink(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        unlink_google(db, current_user)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=_STATUS_FOR.get(exc.code, 400), detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _signup(request: Request, db: Session):
    try:
        return pending_signup(db, request.cookies.get(SIGNUP_COOKIE))
    except GoogleAuthError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc


@router.get(
    "/auth/google/signup",
    response_model=GoogleSignupResponse,
    summary="The Google identity waiting for a verified phone",
    responses={status.HTTP_410_GONE: {"description": "No pending sign-up, or it expired."}},
)
def google_signup(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> GoogleSignupResponse:
    signup = _signup(request, db)
    return GoogleSignupResponse(email=signup.google_email, name=signup.google_name)


@router.post(
    "/auth/google/signup/request-code",
    response_model=PhoneCodeRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send the seller's phone a sign-up code by SMS",
    responses={
        status.HTTP_409_CONFLICT: {"description": "The number already has an account."},
        status.HTTP_410_GONE: {"description": "No pending sign-up, or it expired."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many codes. See Retry-After."},
    },
)
def google_signup_request_code(
    payload: GoogleSignupCodeRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PhoneCodeRequestResponse:
    signup = _signup(request, db)
    try:
        issued = request_signup_code(
            db,
            signup,
            payload.phone,
            client_identifier=get_client_identifier(request),
            dispatch=run_in_background,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=_STATUS_FOR.get(exc.code, 400), detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except PhoneCodeRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many codes. Please wait before asking again.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    return PhoneCodeRequestResponse(
        resend_after_seconds=issued.resend_after_seconds,
        expires_at=issued.expires_at,
        dev_code=issued.code if get_settings().phone_code_dev_echo else None,
    )


@router.post(
    "/auth/google/signup/complete",
    response_model=LoginResponse,
    summary="Create the seller account once the phone is verified",
    description=(
        "Checks the SMS code, then creates the seller with this phone and the Google account, and "
        "signs in (same cookies as /auth/login). No account exists before this succeeds."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"description": "The number or the Google account is taken."},
        status.HTTP_410_GONE: {"description": "The sign-up or the code expired."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The code doesn't match."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many wrong tries."},
    },
)
def google_signup_complete(
    payload: GoogleSignupCompleteRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    signup = _signup(request, db)
    try:
        user = complete_signup(
            db, signup, phone=payload.phone, code=payload.code, name=payload.name
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=_STATUS_FOR.get(exc.code, 400), detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except PhoneCodeRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many tries. Please wait before trying again.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except PhoneCodeExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="That code has expired."
        ) from exc
    except PhoneCodeInvalidError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="That code doesn't match."
        ) from exc
    authenticated = create_session(
        db, user, datetime.now(UTC), user_agent=request.headers.get("user-agent")
    )
    response.delete_cookie(SIGNUP_COOKIE, path="/")
    return _open_session(response, authenticated)
