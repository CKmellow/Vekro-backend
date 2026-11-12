"""#324: Continue with Google: OIDC code flow with PKCE, state and nonce; accounts by google_sub.

A fake Google signs real RS256 ID tokens with a test key, serves its JWKS, and checks that the code
exchange carries the PKCE verifier matching the challenge sent to the consent screen.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from app.core.security import hash_password
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.oauth import GoogleSignup, OAuthState
from app.models.phone_code import PhoneCode
from app.models.store_event import StoreEvent
from app.models.user import PartyRole, User, UserRole
from app.models.user_session import UserSession
from app.services import google_auth, phone_auth
from app.services.google_auth import (
    SIGNUP_COOKIE,
    STATE_COOKIE,
    GoogleAuthError,
    complete_callback,
    complete_signup,
    pending_signup,
    pkce_challenge,
    request_signup_code,
    start_authorization,
    unlink_google,
)
from app.services.phone_auth import PhoneCodeInvalidError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.accounts import account_fields

CLIENT_ID = "client-123.apps.googleusercontent.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def _sign(claims: dict[str, Any], *, key=KEY, kid="k1", alg="RS256") -> str:
    head = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(signature)}"


class FakeGoogle:
    """Token endpoint plus JWKS. ``claims`` overrides tweak the next ID token."""

    def __init__(self) -> None:
        self.sub = "google-sub-111"
        self.email = "wanjiru@example.com"
        self.overrides: dict[str, Any] = {}
        self.key = KEY
        self.challenge: str | None = None
        self.exchanges: list[dict[str, str]] = []
        self.nonce: str | None = None

    def authorize(self, url: str) -> tuple[str, str]:
        query = parse_qs(urlsplit(url).query)
        self.challenge = query["code_challenge"][0]
        self.nonce = query["nonce"][0]
        return query["state"][0], query["nonce"][0]

    def post(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.exchanges.append(dict(data))
        # PKCE: Google refuses a verifier that does not hash to the challenge it was shown.
        if pkce_challenge(data["code_verifier"]) != self.challenge:
            return 400, {"error": "invalid_grant"}
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "sub": self.sub,
            "email": self.email,
            "email_verified": True,
            "name": "Wanjiru Kamau",
            "iat": now,
            "exp": now + 3600,
            "nonce": self.nonce,
        } | self.overrides
        return 200, {"id_token": _sign(claims, key=self.key), "access_token": "ya29.secret-access"}

    def get(self, url: str) -> tuple[int, dict[str, Any]]:
        return 200, {"keys": [_jwk(KEY, "k1")]}


@pytest.fixture(autouse=True)
def _google(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "google_redirect_uri", "")
    monkeypatch.setattr(settings, "frontend_url", "https://app.vekro.me")
    google_auth._JWKS.update({"keys": {}, "fetched": 0.0})
    phone_auth._CLIENT_BUCKETS.clear()
    phone_auth._DECOY_BUCKETS.clear()
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)
    fake = FakeGoogle()
    monkeypatch.setattr(google_auth, "_http_post", fake.post)
    monkeypatch.setattr(google_auth, "_http_get", fake.get)
    return fake


@pytest.fixture
def factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [User, UserSession, PhoneCode, OAuthState, GoogleSignup, StoreEvent]
    Base.metadata.create_all(engine, tables=[cast(Table, t.__table__) for t in tables])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def db(factory) -> Session:
    return factory()


def _user(db: Session, phone: str, role: UserRole, **extra) -> User:
    user = User(
        name=f"{role.value} user",
        phone=phone,
        **account_fields(role),
        password_hash=hash_password("Correct-Horse-9"),
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        **extra,
    )
    db.add(user)
    db.commit()
    return user


def _login(db: Session, fake: FakeGoogle, *, cookie: str | None = "same", code="auth-code", **kw):
    url, state = start_authorization(db, get_settings(), mode="login", **kw)
    url_state, _ = fake.authorize(url)
    assert url_state == state
    return complete_callback(
        db,
        get_settings(),
        state=state,
        cookie_state=state if cookie == "same" else cookie,
        code=code,
        error=None,
    )


# --- the authorization request ---


def test_the_request_carries_state_nonce_and_an_s256_challenge_of_a_server_side_verifier(
    db, _google
) -> None:
    url, state = start_authorization(db, get_settings(), mode="login")
    query = parse_qs(urlsplit(url).query)
    row = db.execute(select(OAuthState)).scalar_one()

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == ["https://app.vekro.me/api/auth/google/callback"]
    assert query["response_type"] == ["code"] and query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [pkce_challenge(row.code_verifier)]
    assert query["nonce"] == [row.nonce]
    # The verifier never leaves the server; the state is stored only as a hash.
    assert row.code_verifier not in url
    assert (
        row.state_hash == hashlib.sha256(state.encode()).hexdigest() and state not in row.state_hash
    )
    assert 43 <= len(row.code_verifier) <= 128


def test_the_code_exchange_sends_the_pkce_verifier_and_a_wrong_one_is_refused(db, _google) -> None:
    owner = _user(db, "+254711000001", PartyRole.SELLER, google_sub=_google.sub)
    result = _login(db, _google)
    assert result.kind == "signed_in" and result.user.id == owner.id
    sent = _google.exchanges[-1]
    assert pkce_challenge(sent["code_verifier"]) == _google.challenge
    assert sent["grant_type"] == "authorization_code"

    _google.challenge = "something-else"  # Google would refuse this verifier
    url, state = start_authorization(db, get_settings(), mode="login")
    _google.authorize(url)
    _google.challenge = "something-else"
    with pytest.raises(GoogleAuthError) as refused:
        complete_callback(db, get_settings(), state=state, cookie_state=state, code="c", error=None)
    assert refused.value.code == "token"


# --- state: replayed and forged callbacks ---


def test_a_replayed_callback_is_refused(db, _google) -> None:
    _user(db, "+254711000001", PartyRole.SELLER, google_sub=_google.sub)
    url, state = start_authorization(db, get_settings(), mode="login")
    _google.authorize(url)
    first = complete_callback(
        db, get_settings(), state=state, cookie_state=state, code="c", error=None
    )
    assert first.kind == "signed_in"
    with pytest.raises(GoogleAuthError) as replay:
        complete_callback(db, get_settings(), state=state, cookie_state=state, code="c", error=None)
    assert replay.value.code == "state"


@pytest.mark.parametrize(
    "cookie",
    [None, "", "another-browsers-state"],
    ids=["no cookie", "empty cookie", "cookie of another request"],
)
def test_a_callback_whose_state_is_not_this_browsers_is_refused(db, _google, cookie) -> None:
    with pytest.raises(GoogleAuthError) as refused:
        _login(db, _google, cookie=cookie)
    assert refused.value.code == "state"
    assert _google.exchanges == []  # the code is never even exchanged


def test_a_forged_state_is_refused(db, _google) -> None:
    with pytest.raises(GoogleAuthError) as refused:
        complete_callback(
            db, get_settings(), state="forged", cookie_state="forged", code="c", error=None
        )
    assert refused.value.code == "state"


def test_an_expired_state_is_refused(db, _google) -> None:
    url, state = start_authorization(
        db, get_settings(), mode="login", now=datetime.now(UTC) - timedelta(minutes=11)
    )
    _google.authorize(url)
    with pytest.raises(GoogleAuthError) as refused:
        complete_callback(db, get_settings(), state=state, cookie_state=state, code="c", error=None)
    assert refused.value.code == "state"


# --- the ID token ---


def test_a_token_with_another_nonce_is_refused(db, _google) -> None:
    _user(db, "+254711000001", PartyRole.SELLER, google_sub=_google.sub)
    _google.overrides = {"nonce": "a-nonce-from-another-request"}
    with pytest.raises(GoogleAuthError) as refused:
        _login(db, _google)
    assert refused.value.code == "id_token" and "nonce" in str(refused.value)


@pytest.mark.parametrize(
    ("override", "check"),
    [
        ({"aud": "someone-elses-client"}, "audience"),
        ({"iss": "https://evil.example"}, "issuer"),
        ({"exp": int(time.time()) - 3600}, "expiry"),
        ({"email_verified": False}, "email"),
        ({"sub": ""}, "subject"),
    ],
)
def test_id_token_claims_are_checked(db, _google, override, check) -> None:
    _google.overrides = override
    with pytest.raises(GoogleAuthError) as refused:
        _login(db, _google)
    assert refused.value.code == "id_token" and check in str(refused.value)


def test_a_token_signed_by_another_key_is_refused(db, _google) -> None:
    _google.key = OTHER_KEY
    with pytest.raises(GoogleAuthError) as refused:
        _login(db, _google)
    assert refused.value.code == "id_token" and "signature" in str(refused.value)


def test_access_denied_comes_back_as_denied(db, _google) -> None:
    url, state = start_authorization(db, get_settings(), mode="login")
    with pytest.raises(GoogleAuthError) as refused:
        complete_callback(
            db, get_settings(), state=state, cookie_state=state, code=None, error="access_denied"
        )
    assert refused.value.code == "denied"


# --- accounts ---


def test_an_unlinked_admin_is_never_signed_in_by_google(db, _google) -> None:
    admin = _user(db, "+254700000220", UserRole.ADMIN, google_email=_google.email)
    result = _login(db, _google)
    # Same e-mail, but no linked subject: no admin session, only a seller sign-up offer.
    assert result.kind == "signup" and result.user is None
    signup = pending_signup(db, result.signup_token)
    request_signup_code(db, signup, "+254711000099", sender_provider=lambda: None)
    code = db.execute(select(PhoneCode)).scalar_one()
    assert code.purpose == "signup"
    db.refresh(admin)
    assert admin.google_sub is None and admin.role == UserRole.ADMIN


def test_an_admin_who_linked_google_can_sign_in_with_it(db, _google) -> None:
    admin = _user(db, "+254700000220", UserRole.ADMIN)
    url, state = start_authorization(
        db, get_settings(), mode="link", user=admin, return_to="/admin"
    )
    _google.authorize(url)
    linked = complete_callback(
        db, get_settings(), state=state, cookie_state=state, code="c", error=None
    )
    assert linked.kind == "linked" and linked.return_to == "/admin"
    signed_in = _login(db, _google)
    assert signed_in.kind == "signed_in" and signed_in.user.role == UserRole.ADMIN


def test_a_matching_email_never_links_an_account(db, _google) -> None:
    buyer = _user(db, "+254711000002", PartyRole.BUYER, google_email=_google.email)
    seller = _user(
        db, "+254711000003", PartyRole.SELLER, google_sub="another-sub", google_email=_google.email
    )
    result = _login(db, _google)
    assert result.kind == "signup"
    db.refresh(buyer)
    db.refresh(seller)
    assert buyer.google_sub is None and seller.google_sub == "another-sub"


def test_linking_a_google_account_already_linked_elsewhere_is_refused(db, _google) -> None:
    _user(db, "+254711000004", PartyRole.SELLER, google_sub=_google.sub)
    buyer = _user(db, "+254711000005", PartyRole.BUYER)
    url, state = start_authorization(
        db, get_settings(), mode="link", user=buyer, return_to="/buyer/orders"
    )
    _google.authorize(url)
    with pytest.raises(GoogleAuthError) as refused:
        complete_callback(db, get_settings(), state=state, cookie_state=state, code="c", error=None)
    assert refused.value.code == "taken" and refused.value.link_return_to == "/buyer/orders"
    db.refresh(buyer)
    assert buyer.google_sub is None


def test_a_buyer_links_google_and_only_sub_and_email_are_kept(db, _google) -> None:
    buyer = _user(db, "+254711000006", PartyRole.BUYER)
    url, state = start_authorization(db, get_settings(), mode="link", user=buyer)
    _google.authorize(url)
    complete_callback(db, get_settings(), state=state, cookie_state=state, code="c", error=None)
    db.refresh(buyer)
    assert (buyer.google_sub, buyer.google_email) == (_google.sub, _google.email)
    # Google's access token is never stored anywhere.
    for row in db.execute(select(OAuthState)).scalars():
        assert "ya29" not in json.dumps({c: str(getattr(row, c)) for c in row.__table__.c.keys()})
    assert "ya29" not in json.dumps({c: str(getattr(buyer, c)) for c in buyer.__table__.c.keys()})


def test_return_to_never_leaves_the_site(db, _google) -> None:
    for bad in ["https://evil.example/", "//evil.example", "\\\\evil", "javascript:alert(1)"]:
        start_authorization(db, get_settings(), mode="login", return_to=bad)
    assert {r.return_to for r in db.execute(select(OAuthState)).scalars()} == {None}


# --- member sign-up: no account until the phone is verified ---


def test_a_new_google_user_becomes_a_member_only_after_the_sms_code(db, _google) -> None:
    result = _login(db, _google)
    assert result.kind == "signup"
    assert db.execute(select(User)).first() is None  # nothing to transact with yet

    signup = pending_signup(db, result.signup_token)
    issued = request_signup_code(db, signup, "0711000007", sender_provider=lambda: None)
    with pytest.raises(PhoneCodeInvalidError):
        complete_signup(
            db, signup, phone="0711000007", code="000000" if issued.code != "000000" else "111111"
        )
    assert db.execute(select(User)).first() is None

    member = complete_signup(db, signup, phone="0711000007", code=issued.code, name="Amina Otieno")
    # A member with no store (M28): selling starts later from the buyer area.
    assert member.role == UserRole.MEMBER and not member.has_store
    assert member.name == "Amina Otieno" and member.phone == "+254711000007"
    assert member.google_sub == _google.sub and member.has_password is False
    # Only the subject and the display e-mail are kept from Google.
    assert member.google_email == _google.email
    assert db.execute(select(GoogleSignup)).first() is None
    # The same Google account now signs straight in.
    assert _login(db, _google).kind == "signed_in"


@pytest.mark.parametrize("role", [PartyRole.BUYER, PartyRole.SELLER, UserRole.ADMIN])
def test_a_number_with_an_account_is_never_signed_up_or_linked(db, _google, role) -> None:
    owner = _user(db, "+254711000008", role)
    signup = pending_signup(db, _login(db, _google).signup_token)
    with pytest.raises(GoogleAuthError) as refused:
        request_signup_code(db, signup, "+254711000008", sender_provider=lambda: None)
    assert refused.value.code == "phone_taken"
    assert str(refused.value) == (
        "This number already has a Vekro account. Sign in with password or SMS code, then "
        "link Google in Profile & security."
    )
    # Even with a code for that number, completing is refused and nothing is linked.
    with pytest.raises(GoogleAuthError):
        complete_signup(db, signup, phone="+254711000008", code="123456")
    db.refresh(owner)
    assert owner.google_sub is None
    assert db.execute(select(User)).scalars().all() == [owner]


def test_an_abandoned_signup_creates_nothing(db, _google) -> None:
    signup = pending_signup(db, _login(db, _google).signup_token)
    request_signup_code(db, signup, "0711000013", sender_provider=lambda: None)
    # The person never enters the right code: no account, no link.
    assert db.execute(select(User)).first() is None
    with pytest.raises(PhoneCodeInvalidError):
        complete_signup(db, signup, phone="0711000013", code="000001")
    assert db.execute(select(User)).first() is None
    # And once the sign-up has expired it can't be resumed.
    with pytest.raises(GoogleAuthError) as expired:
        pending_signup(db, "not-a-live-token")
    assert expired.value.code == "signup_expired"


def test_unlinking_needs_another_way_in(db, _google) -> None:
    google_only = _user(db, "+254711000009", PartyRole.SELLER, google_sub="s9", has_password=False)
    with pytest.raises(GoogleAuthError) as refused:
        unlink_google(db, google_only)
    assert refused.value.code == "password_needed" and google_only.google_sub == "s9"
    buyer = _user(
        db, "+254711000010", PartyRole.BUYER, google_sub="s10", google_email="b@example.com"
    )
    unlink_google(db, buyer)
    assert buyer.google_sub is None and buyer.google_email is None


# --- the HTTP routes ---


@pytest.fixture
def client(factory, monkeypatch):
    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    import app.main as app_main
    from app.services.auth import resolve_active_session

    def load(token: str):  # the session middleware reads the same test database
        session = factory()
        try:
            return resolve_active_session(session, token)
        finally:
            session.close()

    monkeypatch.setattr(app_main, "_load_session_context", load)
    monkeypatch.setattr("app.routers.google_auth.run_in_background", lambda job: job())
    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", True)
    app.dependency_overrides[get_db] = override
    yield TestClient(app, base_url="https://app.vekro.me", follow_redirects=False)
    app.dependency_overrides.pop(get_db, None)


def _start(client: TestClient, fake: FakeGoogle) -> str:
    started = client.get("/auth/google/start")
    assert started.status_code == 302
    state, _ = fake.authorize(started.headers["location"])
    cookie = started.cookies.get(STATE_COOKIE)
    assert cookie == state
    assert "httponly" in started.headers["set-cookie"].lower()
    return state


def test_routes_sign_in_by_google_and_a_replay_is_turned_away(client, factory, _google) -> None:
    _user(factory(), "+254711000011", PartyRole.SELLER, google_sub=_google.sub)
    state = _start(client, _google)
    done = client.get("/auth/google/callback", params={"state": state, "code": "c"})
    assert done.status_code == 302 and done.headers["location"] == "/login?google=signed_in"
    assert get_settings().session_cookie_name in done.cookies
    assert client.get("/auth/me").json()["role"] == "seller"

    client.cookies.set(STATE_COOKIE, state)
    replay = client.get("/auth/google/callback", params={"state": state, "code": "c"})
    assert replay.headers["location"] == "/login?google=state"


def test_routes_report_access_denied_for_a_non_test_user(client, _google) -> None:
    state = _start(client, _google)
    denied = client.get("/auth/google/callback", params={"state": state, "error": "access_denied"})
    assert denied.headers["location"] == "/login?google=denied"


def test_routes_complete_a_member_signup_and_block_everything_before_it(client, _google) -> None:
    state = _start(client, _google)
    done = client.get("/auth/google/callback", params={"state": state, "code": "c"})
    assert done.headers["location"] == "/register/google" and SIGNUP_COOKIE in done.cookies
    assert client.get("/auth/google/signup").json()["email"] == _google.email

    # Before the phone is verified there is no session: nothing a seller does is allowed.
    assert client.get("/auth/me").status_code == 401
    assert client.post("/listings", json={"title": "Basket", "price": "100"}).status_code == 401

    sent = client.post("/auth/google/signup/request-code", json={"phone": "0711000012"})
    assert sent.status_code == 202
    code = sent.json()["dev_code"]
    wrong_code = "000000" if code != "000000" else "111111"
    wrong = client.post(
        "/auth/google/signup/complete", json={"phone": "0711000012", "code": wrong_code}
    )
    assert wrong.status_code == 422 and client.get("/auth/me").status_code == 401
    created = client.post(
        "/auth/google/signup/complete", json={"phone": "0711000012", "code": code}
    )
    assert created.status_code == 200
    me = client.get("/auth/me").json()
    assert me["role"] == "buyer" and me["store"]["status"] == "none"
    assert me["google_email"] == _google.email
    assert me["has_password"] is False


def test_linking_needs_a_signed_in_session(client) -> None:
    assert client.post("/account/google/link", json={}).status_code == 401
    assert client.delete("/account/google").status_code == 401


def test_a_link_comes_back_to_the_settings_tab_it_started_from(client, factory, _google) -> None:
    from app.routers.google_auth import _with_result

    assert _with_result("/seller/settings?tab=security", "linked") == (
        "/seller/settings?tab=security&google=linked"
    )
    assert _with_result("/buyer/orders", "taken") == "/buyer/orders?google=taken"
