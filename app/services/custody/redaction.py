from __future__ import annotations

import hashlib
import re

_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_LONG_SECRET_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_PHONE_PATTERN = re.compile(r"\+?\d{10,15}")
_MAX_MESSAGE_LENGTH = 200


def signature_fingerprint(signature: str) -> str | None:
    value = signature.strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    message = _BEARER_PATTERN.sub("Bearer ***", message)
    message = _LONG_SECRET_PATTERN.sub("***", message)
    message = _PHONE_PATTERN.sub("***", message)
    if len(message) > _MAX_MESSAGE_LENGTH:
        message = f"{message[:_MAX_MESSAGE_LENGTH]}..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
