"""Canonical event payloads and leaf hashes for anchoring (M21, docs/anchoring-design.md section 4).

Payloads are RFC 8785 (JCS) JSON text. They are built only from str, int, bool, None, list and
dict, so the number rules of JCS never come into play: money, ratios and anything fractional are
decimal strings, and a float is refused. The leaf is

    keccak256(0x00 || salt || prev_event_hash || utf8(payload))

with a 32-byte random salt per event and the previous leaf of the same transaction (32 zero bytes
for the first). The golden vectors in tests/fixtures/anchoring/vectors.json pin all of this for
the frontend verifier.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from Crypto.Hash import keccak

HASH_SIZE = 32
ZERO_HASH = bytes(HASH_SIZE)
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

# Integers outside this range lose precision in JavaScript, so a verifier could not trust them.
MAX_SAFE_INTEGER = 2**53 - 1

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalEncodingError(ValueError):
    pass


def keccak256(*parts: bytes) -> bytes:
    digest = keccak.new(digest_bits=256)
    for part in parts:
        digest.update(part)
    return digest.digest()


def new_salt() -> bytes:
    return secrets.token_bytes(HASH_SIZE)


def leaf_hash(salt: bytes, prev_event_hash: bytes, payload: str) -> bytes:
    if len(salt) != HASH_SIZE or len(prev_event_hash) != HASH_SIZE:
        raise CanonicalEncodingError("salt and prev_event_hash must be 32 bytes.")
    return keccak256(LEAF_PREFIX, salt, prev_event_hash, _utf8(payload))


def canonical_json(value: Any) -> str:
    """RFC 8785 text for ``value``. Raises CanonicalEncodingError for anything JCS would encode in
    a way that could differ between implementations (floats, unsafe integers, lone surrogates)."""
    parts: list[str] = []
    _encode(value, parts)
    return "".join(parts)


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalEncodingError(f"Integer {value} is outside the safe range.")
        out.append(str(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise CanonicalEncodingError("Object keys must be strings.")
            items.append(key)
        # JCS orders keys by their UTF-16 code units; big-endian UTF-16 bytes sort the same way.
        items.sort(key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        out.append("{")
        for index, key in enumerate(items):
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise CanonicalEncodingError(
            f"{type(value).__name__} cannot be canonicalized; use a decimal string."
        )


def _string(value: str) -> str:
    _utf8(value)  # rejects lone surrogates
    escaped = []
    for char in value:
        if char in _SHORT_ESCAPES:
            escaped.append(_SHORT_ESCAPES[char])
        elif ord(char) < 0x20:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("Text must be valid Unicode (no lone surrogates).") from exc


# Field encoders, shared by the per-event serializers so every payload uses one format.


def money(value: Decimal | str | int | None) -> str | None:
    """A 2-place decimal string ("500.00"). Refuses values with more precision than cents."""
    if value is None:
        return None
    return _fixed(value, Decimal("0.01"))


def ratio(value: Decimal | str | None) -> str | None:
    """A 4-place decimal string ("0.5000"), as stored in disputes.split_ratio."""
    if value is None:
        return None
    return _fixed(value, Decimal("0.0001"))


def _fixed(value: Decimal | str | int, exponent: Decimal) -> str:
    if isinstance(value, float):
        raise CanonicalEncodingError("Floats are never encoded; pass a Decimal or a string.")
    try:
        number = Decimal(str(value))
        fixed = number.quantize(exponent)
    except InvalidOperation as exc:
        raise CanonicalEncodingError(f"{value!r} is not a decimal number.") from exc
    if fixed != number:
        raise CanonicalEncodingError(f"{value!r} has more precision than {exponent}.")
    return str(fixed)


def timestamp(value: datetime | None) -> str | None:
    """UTC ISO-8601 truncated to seconds with a Z suffix. Naive datetimes are taken as UTC, which
    is how SQLite returns the app's timezone-aware columns."""
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def uuid_text(value: uuid.UUID | str | None) -> str | None:
    if value is None:
        return None
    return str(uuid.UUID(str(value)))


def hex32(value: bytes) -> str:
    return "0x" + value.hex()


def text_keccak(value: str | None) -> str | None:
    """Binds free text written by a party without copying it into the payload (decision D4)."""
    if value is None:
        return None
    return hex32(keccak256(_utf8(value)))


def plain_json(value: Any) -> Any:
    """A JSON-ish structure (such as a JSONB ``details`` column) made safe for canonical_json:
    floats and Decimals become decimal strings, tuples become lists, keys stay strings."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return str(Decimal(repr(value)))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain_json(item) for item in value]
    raise CanonicalEncodingError(f"{type(value).__name__} is not JSON data.")
