from __future__ import annotations

import hashlib
import re

_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_ECONFIRM_KEY_PATTERN = re.compile(r"ek_[A-Za-z0-9]+")
_LONG_SECRET_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_PHONE_PATTERN = re.compile(r"\+?\d{10,15}")
_MAX_MESSAGE_LENGTH = 200

# Structured-payload redaction: keys whose VALUE is always secret regardless of
# its length (confirmation codes are short alphanumerics, so the long-secret
# regex would miss them).
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "confirmationcode",
        "code",
        "apikey",
        "apisecret",
        "authorization",
        "token",
        "secret",
        "password",
        "signature",
    }
)
_PII_KEY_NAMES = frozenset({"phone", "phonenumber", "msisdn", "payerphone", "receiverphone"})


def signature_fingerprint(signature: str) -> str | None:
    value = signature.strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    message = _BEARER_PATTERN.sub("Bearer ***", message)
    message = _ECONFIRM_KEY_PATTERN.sub("ek_***", message)
    message = _LONG_SECRET_PATTERN.sub("***", message)
    message = _PHONE_PATTERN.sub("***", message)
    if len(message) > _MAX_MESSAGE_LENGTH:
        message = f"{message[:_MAX_MESSAGE_LENGTH]}..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _norm_key(key: str) -> str:
    return key.strip().lower().replace("-", "").replace("_", "")


def redact_structure(value: object) -> object:
    """Return a deep copy of a JSON-ish structure safe to log.

    Masks values under secret/PII key names (confirmation codes, api keys,
    phones, ...), masks eConfirm `ek_` keys and bearer tokens found in free
    strings, and leaves everything else intact.
    """
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            norm = _norm_key(str(key))
            if norm in _SENSITIVE_KEY_NAMES:
                out[key] = "***"
            elif norm in _PII_KEY_NAMES:
                out[key] = "***"
            else:
                out[key] = redact_structure(item)
        return out
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, str):
        text = _BEARER_PATTERN.sub("Bearer ***", value)
        text = _ECONFIRM_KEY_PATTERN.sub("ek_***", text)
        return text
    return value
