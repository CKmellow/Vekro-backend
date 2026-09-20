import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.settings import get_settings
from app.db.session import _get_session_factory
from app.routers.auth import router as auth_router
from app.routers.health import router as health_router
from app.routers.listings import router as listings_router
from app.routers.protected import router as protected_router
from app.services.auth import is_valid_csrf_for_session, resolve_active_session


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()
    yield


app = FastAPI(title="Vekro Backend", lifespan=lifespan)

settings = get_settings()
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
audit_logger = logging.getLogger("app.auth.audit")


def _set_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )

    if settings.environment.strip().lower() == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")


def _load_session_context(session_token: str):
    db = _get_session_factory()()
    try:
        return resolve_active_session(db, session_token)
    finally:
        db.close()


def _security_error_response(
    status_code: int,
    detail: str,
    *,
    clear_cookies: bool = False,
) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    if clear_cookies:
        _clear_auth_cookies(response)
    _set_security_headers(response)
    return response


@app.middleware("http")
async def session_security_middleware(request: Request, call_next):
    request.state.current_user = None
    request.state.current_session = None

    session_token = request.cookies.get(settings.session_cookie_name)
    session_context = None
    invalid_session_cookie = False

    if session_token:
        session_context = _load_session_context(session_token)
        if session_context is None:
            invalid_session_cookie = True
            audit_logger.warning(
                "session_invalid_or_expired method=%s path=%s client_ip=%s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
        else:
            request.state.current_user = session_context.user
            request.state.current_session = session_context.session

    if request.method.upper() in STATE_CHANGING_METHODS and session_token:
        if session_context is None:
            audit_logger.warning(
                "request_rejected_invalid_session method=%s path=%s client_ip=%s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return _security_error_response(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid or expired session.",
                clear_cookies=True,
            )

        csrf_cookie_token = request.cookies.get(settings.csrf_cookie_name)
        csrf_header_token = request.headers.get(settings.csrf_header_name)
        if not csrf_cookie_token or not csrf_header_token:
            audit_logger.warning(
                "csrf_missing method=%s path=%s client_ip=%s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return _security_error_response(
                status.HTTP_403_FORBIDDEN,
                "CSRF token missing.",
            )

        if not hmac.compare_digest(csrf_cookie_token, csrf_header_token):
            audit_logger.warning(
                "csrf_mismatch method=%s path=%s client_ip=%s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return _security_error_response(
                status.HTTP_403_FORBIDDEN,
                "CSRF token mismatch.",
            )

        if not is_valid_csrf_for_session(csrf_header_token, session_context.session):
            audit_logger.warning(
                "csrf_invalid method=%s path=%s client_ip=%s",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return _security_error_response(
                status.HTTP_403_FORBIDDEN,
                "CSRF token invalid.",
                clear_cookies=True,
            )

    response = await call_next(request)
    if invalid_session_cookie:
        _clear_auth_cookies(response)
    _set_security_headers(response)
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list or [settings.frontend_url],
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(health_router)
app.include_router(listings_router)
app.include_router(protected_router)
