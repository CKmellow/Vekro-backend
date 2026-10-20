import re

PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
PHONE_VALIDATION_ERROR = "Phone number must contain 7-15 digits and optional leading +."


def _canonicalize_kenyan_phone(raw_digits: str) -> str | None:
    if raw_digits.startswith("254") and len(raw_digits) == 12:
        return f"+{raw_digits}"

    if raw_digits.startswith("0") and len(raw_digits) == 10 and raw_digits[1] in {"7", "1"}:
        return f"+254{raw_digits[1:]}"

    if len(raw_digits) == 9 and raw_digits[0] in {"7", "1"}:
        return f"+254{raw_digits}"

    return None


def normalize_phone(value: str | None, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(PHONE_VALIDATION_ERROR)

    normalized = re.sub(r"[\s\-()]", "", value.strip())
    had_plus_prefix = normalized.startswith("+")
    raw_digits = normalized[1:] if had_plus_prefix else normalized

    if not raw_digits.isdigit():
        raise ValueError(PHONE_VALIDATION_ERROR)

    canonical_kenyan = _canonicalize_kenyan_phone(raw_digits)
    if canonical_kenyan is not None:
        return canonical_kenyan

    normalized_value = f"+{raw_digits}" if had_plus_prefix else raw_digits
    if not PHONE_PATTERN.fullmatch(normalized_value):
        raise ValueError(PHONE_VALIDATION_ERROR)

    return normalized_value


def mask_phone(value: str | None) -> str | None:
    """Masks a phone for display to the other party, e.g. +254712345678 -> +254 712 *** 678."""
    if not value:
        return None
    if value.startswith("+254") and len(value) == 13:
        return f"{value[:4]} {value[4:7]} *** {value[-3:]}"
    if len(value) <= 4:
        return "***"
    return f"{value[:3]} *** {value[-3:]}"
