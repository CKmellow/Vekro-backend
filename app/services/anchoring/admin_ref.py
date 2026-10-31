"""admin_ref: how an admin is named in anchored payloads (decision D5).

Buyers and sellers can read their transaction's payloads, so an admin's UUID never appears in
one. Payloads carry ``admin_ref = hex(HMAC-SHA256(key, utf8(lowercase admin uuid)))`` instead.
The admin-only full audit resolves a ref by computing it for each admin.

``ANCHOR_ADMIN_REF_SECRET`` is a comma-separated list, newest first: new events use the first key,
and resolution tries every key, so rotating the secret never breaks older payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from collections.abc import Iterable

MIN_KEY_LENGTH = 32
# Used only outside production when no secret is configured. Not a secret.
DEVELOPMENT_KEY = "vekro-development-admin-ref-key-not-a-secret"

_logger = logging.getLogger("app.anchoring")
_warned = False


def parse_keys(config: str) -> list[str]:
    return [key.strip() for key in (config or "").split(",") if key.strip()]


def is_valid_secret_config(config: str) -> bool:
    keys = parse_keys(config)
    return bool(keys) and all(len(key) >= MIN_KEY_LENGTH for key in keys)


def configured_keys(settings=None) -> list[str]:
    """The keys in use: the configured ones, or the development key outside production."""
    global _warned
    if settings is None:
        from app.core.settings import get_settings

        settings = get_settings()
    keys = parse_keys(settings.anchor_admin_ref_secret)
    if keys:
        return keys
    if (settings.environment or "").strip().lower() == "production":
        raise RuntimeError("ANCHOR_ADMIN_REF_SECRET is required in production.")
    if not _warned:
        _logger.warning(
            "anchor_admin_ref_secret_missing using the development key; set "
            "ANCHOR_ADMIN_REF_SECRET before anchoring anything real"
        )
        _warned = True
    return [DEVELOPMENT_KEY]


def admin_ref(admin_id: uuid.UUID | str | None, *, keys: list[str] | None = None) -> str | None:
    if admin_id is None:
        return None
    key = (keys or configured_keys())[0]
    message = str(uuid.UUID(str(admin_id))).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def resolve_admin_ref(
    ref: str, admin_ids: Iterable[uuid.UUID], *, keys: list[str] | None = None
) -> uuid.UUID | None:
    """The admin whose ref this is, under any configured key; None when nobody matches."""
    candidates = list(admin_ids)
    for key in keys or configured_keys():
        for admin_id in candidates:
            if hmac.compare_digest(admin_ref(admin_id, keys=[key]) or "", ref):
                return admin_id
    return None
