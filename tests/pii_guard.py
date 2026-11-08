"""Personal data that must never appear in an anchored payload (#241).

Both parties can read every payload of their order, so a payload may name people only by UUID
or keyed ref. This finds phone numbers (+254 / 254 / 07 / 01 forms, and any run of 9 or more
digits, which also catches Daraja checkout IDs that end with the payer's number) and e-mail
addresses. UUIDs and hexadecimal digests (16+ characters with at least one letter: hashes, keyed
refs) are removed before scanning, so an all-digit provider ID is still caught.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RUN = re.compile(r"(?:0x)?[0-9a-f]{16,}", re.I)


def _without_identifiers(text: str) -> str:
    text = _UUID.sub(" ", text)
    return _HEX_RUN.sub(lambda m: " " if re.search(r"[a-f]", m.group(), re.I) else m.group(), text)


_PATTERNS = {
    "phone": re.compile(r"(?:\+?254|(?<!\d)0)[17]\d{8}(?!\d)"),
    "digit run": re.compile(r"\d{9,}"),
    "email": re.compile(r"[^\s@\"]+@[^\s@\"]+\.[A-Za-z]{2,}"),
}


def _values(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _values(item, f"{path}[{index}]")
    else:
        yield path, value


def pii_findings(payload: str | dict[str, Any]) -> list[str]:
    data = json.loads(payload) if isinstance(payload, str) else payload
    findings = []
    for path, value in _values(data):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) and abs(value) >= 100_000_000:
            findings.append(f"{path}: integer {value} looks like a phone number")
            continue
        if not isinstance(value, str):
            continue
        text = _without_identifiers(value)
        for name, pattern in _PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path}: {name} in {value!r}")
    return findings
