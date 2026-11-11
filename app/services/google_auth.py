"""Continue with Google (#324): OpenID Connect, authorization-code flow with PKCE, state and nonce.

- **start**: a single-use ``state`` (stored as a hash, bound to the browser by an HttpOnly cookie,
  10 minutes), a ``nonce`` and a PKCE verifier (S256) are kept server-side. The browser goes to
  Google with the state, the nonce and the code challenge only.
- **callback**: the state must match the cookie and an unused, unexpired row (it is used up at once,
  so a replayed or forged callback fails). The code is exchanged with the verifier, and the ID token
  is verified here: RS256 against Google's published keys, issuer, audience, expiry, nonce, and a
  verified e-mail. Google's tokens are dropped: only ``sub`` and the e-mail (for display) are kept.
- **accounts**: a sign-in matches ``google_sub`` only, **never the e-mail**. So an admin can sign in
  with Google only after linking it from their own signed-in session. An unknown Google account
  becomes a *pending seller sign-up*: no account exists until a phone is verified by SMS code, so
  nobody can transact unverified. Buyers link Google from settings while signed in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_phone
from app.core.security import hash_password
from app.models.oauth import GoogleSignup, OAuthState
from app.models.user import User, UserRole
from app.services.phone_auth import consume_phone_code, request_phone_code

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
STATE_TTL = timedelta(minutes=10)
SIGNUP_TTL = timedelta(minutes=30)
CLOCK_LEEWAY_SECONDS = 60
JWKS_MAX_AGE_SECONDS = 3600
STATE_COOKIE = "vekro_google_state"
SIGNUP_COOKIE = "vekro_google_signup"

logger = logging.getLogger("app.auth.google")

# (status code, JSON body); injectable so tests never reach Google.
HttpPost = Callable[[str, dict[str, str]], tuple[int, dict[str, Any]]]
HttpGet = Callable[[str], tuple[int, dict[str, Any]]]


class GoogleAuthError(Exception):
    """``code`` is what the web app shows: not_configured, denied, state, token, id_token,
    taken, inactive, signup_expired, phone_taken, password_needed, not_linked."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.link_return_to: str | None = None  # set when a link from settings failed


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str | None


@dataclass(frozen=True)
class CallbackResult:
    kind: str  # "signed_in", "linked" or "signup"
    user: User | None = None
    signup_token: str | None = None
    return_to: str | None = None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)) without padding."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def safe_return_to(value: str | None) -> str | None:
    """Only a path on this site (never //host or a scheme), so the redirect cannot leave Vekro."""
    if not value or len(value) > 200 or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(c) < 32 for c in value):
        return None
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _require_configured(settings) -> None:
    if not settings.google_enabled:
        raise GoogleAuthError("not_configured", "Google sign-in is not set up.")


# --- start -------------------------------------------------------------------------------------


def start_authorization(
    db: Session,
    settings,
    *,
    mode: str,
    user: User | None = None,
    return_to: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """The Google URL to send the browser to, and the state to bind to it with a cookie."""
    _require_configured(settings)
    if mode not in {"login", "link"} or (mode == "link") != (user is not None):
        raise ValueError("link needs the signed-in user; login must not have one")
    current = now or datetime.now(UTC)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 characters, within RFC 7636's 43-128
    db.add(
        OAuthState(
            id=uuid.uuid4(),
            state_hash=_sha256(state),
            nonce=nonce,
            code_verifier=verifier,
            mode=mode,
            user_id=user.id if user else None,
            return_to=safe_return_to(return_to),
            created_at=current,
            expires_at=current + STATE_TTL,
        )
    )
    db.commit()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}", state


# --- callback ----------------------------------------------------------------------------------


def consume_state(
    db: Session, state: str | None, cookie_state: str | None, *, now: datetime | None = None
) -> OAuthState:
    """The state must equal this browser's cookie and an unused, unexpired request. It is used up
    here, before anything else, so a replay fails even if a later step does."""
    current = now or datetime.now(UTC)
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise GoogleAuthError("state", "The sign-in state does not match this browser.")
    row = db.execute(
        select(OAuthState).where(OAuthState.state_hash == _sha256(state))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or _aware(row.expires_at) <= current:
        raise GoogleAuthError("state", "This sign-in link has expired or was already used.")
    row.used_at = current
    db.commit()
    return row


def _http_post(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:  # pragma: no cover
    import httpx2 as httpx

    response = httpx.post(url, data=data, timeout=10.0)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def _http_get(url: str) -> tuple[int, dict[str, Any]]:  # pragma: no cover
    import httpx2 as httpx

    response = httpx.get(url, timeout=10.0)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def exchange_code(settings, code: str, verifier: str, *, post: HttpPost | None = None) -> str:
    """Trade the code (plus the PKCE verifier) for an ID token. Other tokens are dropped."""
    if not code:
        raise GoogleAuthError("token", "No authorization code.")
    status, body = (post or _http_post)(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "redirect_uri": settings.google_callback_url,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    id_token = body.get("id_token") if status == 200 else None
    if not isinstance(id_token, str) or not id_token:
        # Google's error code only (for example invalid_grant); never the request or the secret.
        logger.warning("google_token_exchange_failed status=%s error=%s", status, body.get("error"))
        raise GoogleAuthError("token", "Google did not accept the sign-in.")
    return id_token


_JWKS: dict[str, Any] = {"keys": {}, "fetched": 0.0}


def _signing_key(kid: str, get: HttpGet | None) -> rsa.RSAPublicKey:
    fresh = time.monotonic() - _JWKS["fetched"] < JWKS_MAX_AGE_SECONDS
    if kid not in _JWKS["keys"] or not fresh:
        status, body = (get or _http_get)(JWKS_URI)
        if status != 200:
            raise GoogleAuthError("id_token", "Google's signing keys are unavailable.")
        _JWKS["keys"] = {k["kid"]: k for k in body.get("keys", []) if k.get("kty") == "RSA"}
        _JWKS["fetched"] = time.monotonic()
    jwk = _JWKS["keys"].get(kid)
    if jwk is None:
        raise GoogleAuthError("id_token", "Unknown signing key.")
    numbers = rsa.RSAPublicNumbers(
        int.from_bytes(_b64url_decode(jwk["e"]), "big"),
        int.from_bytes(_b64url_decode(jwk["n"]), "big"),
    )
    return numbers.public_key()


def verify_id_token(
    token: str, *, settings, nonce: str, now: datetime | None = None, get: HttpGet | None = None
) -> GoogleIdentity:
    current = (now or datetime.now(UTC)).timestamp()
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError) as exc:
        raise GoogleAuthError("id_token", "Malformed ID token.") from exc
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise GoogleAuthError("id_token", "Unexpected ID token algorithm.")
    key = _signing_key(header["kid"], get)
    try:
        key.verify(
            signature,
            f"{header_b64}.{payload_b64}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise GoogleAuthError("id_token", "Bad ID token signature.") from exc

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    checks = {
        "issuer": claims.get("iss") in ISSUERS,
        "audience": settings.google_client_id in audiences
        and (not isinstance(audience, list) or claims.get("azp") == settings.google_client_id),
        "expiry": isinstance(claims.get("exp"), int | float)
        and claims["exp"] > current - CLOCK_LEEWAY_SECONDS,
        "issued": isinstance(claims.get("iat"), int | float)
        and claims["iat"] <= current + CLOCK_LEEWAY_SECONDS,
        "nonce": isinstance(claims.get("nonce"), str)
        and hmac.compare_digest(claims["nonce"], nonce),
        "subject": isinstance(claims.get("sub"), str) and bool(claims["sub"]),
        "email": isinstance(claims.get("email"), str)
        and claims.get("email_verified") in (True, "true"),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        logger.warning("google_id_token_rejected checks=%s", ",".join(failed))
        raise GoogleAuthError("id_token", f"ID token failed: {', '.join(failed)}.")
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    return GoogleIdentity(sub=claims["sub"], email=claims["email"].strip().lower(), name=name)


def complete_callback(
    db: Session,
    settings,
    *,
    state: str | None,
    cookie_state: str | None,
    code: str | None,
    error: str | None,
    now: datetime | None = None,
    post: HttpPost | None = None,
    get: HttpGet | None = None,
) -> CallbackResult:
    _require_configured(settings)
    current = now or datetime.now(UTC)
    row = consume_state(db, state, cookie_state, now=current)
    try:
        return _finish_callback(
            db, settings, row, code=code, error=error, now=current, post=post, get=get
        )
    except GoogleAuthError as exc:
        if row.mode == "link":
            exc.link_return_to = row.return_to or "/"
        raise


def _finish_callback(
    db: Session,
    settings,
    row: OAuthState,
    *,
    code: str | None,
    error: str | None,
    now: datetime,
    post: HttpPost | None,
    get: HttpGet | None,
) -> CallbackResult:
    current = now
    if error:
        # access_denied: the person cancelled, or the consent screen is in Testing and the account
        # is not one of its test users.
        raise GoogleAuthError("denied" if error == "access_denied" else "token", error)
    id_token = exchange_code(settings, code or "", row.code_verifier, post=post)
    identity = verify_id_token(id_token, settings=settings, nonce=row.nonce, now=current, get=get)

    owner = db.execute(select(User).where(User.google_sub == identity.sub)).scalar_one_or_none()
    if row.mode == "link":
        user = db.get(User, row.user_id) if row.user_id else None
        if user is None or not user.is_active:
            raise GoogleAuthError("state", "The account that started linking is gone.")
        if owner is not None and owner.id != user.id:
            raise GoogleAuthError(
                "taken", "This Google account is linked to another Vekro account."
            )
        user.google_sub = identity.sub
        user.google_email = identity.email
        user.google_linked_at = current
        db.commit()
        logger.info("google_linked user_id=%s role=%s", user.id, user.role.value)
        return CallbackResult("linked", user=user, return_to=row.return_to)

    if owner is not None:
        if not owner.is_active:
            raise GoogleAuthError("inactive", "This account is disabled.")
        # Only a linked subject signs in, so an admin gets here only after linking it themselves.
        logger.info("google_sign_in user_id=%s role=%s", owner.id, owner.role.value)
        return CallbackResult("signed_in", user=owner, return_to=row.return_to)

    # Unknown Google account: never matched by e-mail. It may become a seller, after an SMS code.
    token = secrets.token_urlsafe(32)
    db.add(
        GoogleSignup(
            id=uuid.uuid4(),
            token_hash=_sha256(token),
            google_sub=identity.sub,
            google_email=identity.email,
            google_name=(identity.name or "")[:120] or None,
            created_at=current,
            expires_at=current + SIGNUP_TTL,
        )
    )
    db.commit()
    return CallbackResult("signup", signup_token=token)


# --- seller sign-up: a phone verified by SMS before any account exists ------------------------


def pending_signup(db: Session, token: str | None, *, now: datetime | None = None) -> GoogleSignup:
    current = now or datetime.now(UTC)
    row = (
        db.execute(
            select(GoogleSignup).where(GoogleSignup.token_hash == _sha256(token))
        ).scalar_one_or_none()
        if token
        else None
    )
    if row is None or _aware(row.expires_at) <= current:
        raise GoogleAuthError("signup_expired", "Start again with Continue with Google.")
    return row


def _normalized(phone: str) -> str:
    try:
        number = normalize_phone(phone)
    except ValueError as exc:
        raise ValueError("Enter a Kenyan mobile number.") from exc
    if not number:
        raise ValueError("Enter a Kenyan mobile number.")
    return number


def request_signup_code(db: Session, signup: GoogleSignup, phone: str, **kwargs):
    number = _normalized(phone)
    if db.execute(select(User.id).where(User.phone == number)).first() is not None:
        raise GoogleAuthError(
            "phone_taken",
            "This number already has an account. Log in, then link Google in settings.",
        )
    return request_phone_code(db, number, purpose="signup", **kwargs)


def complete_signup(
    db: Session,
    signup: GoogleSignup,
    *,
    phone: str,
    code: str,
    name: str | None = None,
    now: datetime | None = None,
) -> User:
    """Create the seller only now, with the SMS-verified phone. Raises the phone-code errors."""
    current = now or datetime.now(UTC)
    number = _normalized(phone)
    if db.execute(select(User.id).where(User.phone == number)).first() is not None:
        raise GoogleAuthError("phone_taken", "This number already has an account.")
    if db.execute(select(User.id).where(User.google_sub == signup.google_sub)).first():
        raise GoogleAuthError("taken", "This Google account is already linked.")
    consume_phone_code(db, number, code, "signup", now=current)
    display_name = (name or signup.google_name or "").strip()[:120] or "Vekro seller"
    user = User(
        id=uuid.uuid4(),
        name=display_name,
        phone=number,
        role=UserRole.SELLER,
        # Nobody knows this password; "Forgot password" sets a real one (has_password).
        password_hash=hash_password(secrets.token_urlsafe(32)),
        has_password=False,
        mpesa_phone=number,
        google_sub=signup.google_sub,
        google_email=signup.google_email,
        google_linked_at=current,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=current,
        updated_at=current,
    )
    db.add(user)
    db.delete(signup)
    db.commit()
    logger.info("google_seller_signup user_id=%s", user.id)
    return user


def unlink_google(db: Session, user: User) -> None:
    if not user.google_sub:
        raise GoogleAuthError("not_linked", "Google is not linked.")
    if user.has_password is False:
        raise GoogleAuthError(
            "password_needed",
            "Google is your only way in. Set a password with Forgot password first.",
        )
    user.google_sub = None
    user.google_email = None
    user.google_linked_at = None
    db.commit()
    logger.info("google_unlinked user_id=%s", user.id)
