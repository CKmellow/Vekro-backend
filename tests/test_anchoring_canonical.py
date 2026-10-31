"""Canonical payload encoding and leaf hashing (M21 A2)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app.services.anchoring.canonical import (
    MAX_SAFE_INTEGER,
    ZERO_HASH,
    CanonicalEncodingError,
    canonical_json,
    keccak256,
    leaf_hash,
    money,
    new_salt,
    plain_json,
    ratio,
    text_keccak,
    timestamp,
    uuid_text,
)
from hypothesis import given, settings
from hypothesis import strategies as st

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER)
    | st.text(),
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(st.text(max_size=8), children, max_size=5),
    max_leaves=25,
)


def test_keccak_is_ethereum_keccak_not_sha3() -> None:
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_keys_sort_by_utf16_code_units() -> None:
    # Code point order would put U+FFFF before U+1F600; UTF-16 puts the surrogate pair first.
    assert canonical_json({"￿": 1, "\U0001f600": 2, "b": 3, "a": 4}) == (
        '{"a":4,"b":3,"\U0001f600":2,"￿":1}'
    )


def test_string_escapes_follow_jcs() -> None:
    assert canonical_json('"\\\b\f\n\r\t\x00\x1f\x7f é') == (
        '"\\"\\\\\\b\\f\\n\\r\\t\\u0000\\u001f\x7f é"'
    )


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        0.0,
        Decimal("1.00"),
        MAX_SAFE_INTEGER + 1,
        -MAX_SAFE_INTEGER - 1,
        {1: "x"},
        b"x",
        "\ud800",
    ],
)
def test_refuses_values_that_could_encode_differently(value) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json(value)


@settings(max_examples=150, deadline=None)
@given(json_values)
def test_canonical_text_round_trips_and_is_a_fixed_point(value) -> None:
    text = canonical_json(value)
    assert json.loads(text) == value
    assert canonical_json(json.loads(text)) == text
    assert "\n" not in text  # no whitespace between tokens; newlines inside strings are escaped


def test_money_and_ratio_are_fixed_place_strings() -> None:
    assert money(Decimal("500")) == "500.00"
    assert money("1500.5") == "1500.50"
    assert money(7) == "7.00"
    assert ratio(Decimal("0.5")) == "0.5000"
    assert money(None) is None
    for bad in (Decimal("1.005"), "abc", 1.5, "NaN"):
        with pytest.raises(CanonicalEncodingError):
            money(bad)


def test_timestamps_are_utc_seconds_with_z() -> None:
    nairobi = timezone(timedelta(hours=3))
    assert timestamp(datetime(2026, 10, 31, 12, 0, 5, 999999, tzinfo=nairobi)) == (
        "2026-10-31T09:00:05Z"
    )
    assert timestamp(datetime(2026, 10, 31, 9, 0, 5, 123)) == "2026-10-31T09:00:05Z"  # naive: UTC
    assert timestamp(None) is None


def test_uuids_are_lowercase_canonical() -> None:
    assert uuid_text("5B0E6C1E1F6A4B8E9C2D3F4A5B6C7D8E") == "5b0e6c1e-1f6a-4b8e-9c2d-3f4a5b6c7d8e"


def test_text_keccak_binds_free_text() -> None:
    assert text_keccak(None) is None
    assert text_keccak("a") != text_keccak("a ")
    digest = text_keccak("a") or ""
    assert digest.startswith("0x") and len(digest) == 66


def test_plain_json_makes_details_canonicalizable() -> None:
    details = {"ratio": 0.1, "amount": Decimal("2.50"), "ids": ("a", 1), "big": 2**60}
    assert canonical_json(plain_json(details)) == (
        '{"amount":"2.50","big":"1152921504606846976","ids":["a",1],"ratio":"0.1"}'
    )


def test_leaf_is_domain_separated_keccak_of_its_parts() -> None:
    salt = keccak256(b"salt")
    payload = '{"v":1}'
    assert leaf_hash(salt, ZERO_HASH, payload) == keccak256(
        b"\x00" + salt + ZERO_HASH + payload.encode()
    )
    with pytest.raises(CanonicalEncodingError):
        leaf_hash(b"short", ZERO_HASH, payload)


@settings(max_examples=100, deadline=None)
@given(
    salt=st.binary(min_size=32, max_size=32),
    prev=st.binary(min_size=32, max_size=32),
    payload=st.text(min_size=1),
    part=st.sampled_from(["salt", "prev"]),
    bit=st.integers(min_value=0, max_value=255),
)
def test_any_single_bit_change_in_salt_or_prev_changes_the_leaf(
    salt, prev, payload, part, bit
) -> None:
    original = leaf_hash(salt, prev, payload)
    target = bytearray(salt if part == "salt" else prev)
    target[bit // 8] ^= 1 << (bit % 8)
    if part == "salt":
        changed = leaf_hash(bytes(target), prev, payload)
    else:
        changed = leaf_hash(salt, bytes(target), payload)
    assert changed != original


@settings(max_examples=100, deadline=None)
@given(payload=st.text(min_size=1), position=st.integers(min_value=0), char=st.characters())
def test_any_payload_change_changes_the_leaf(payload, position, char) -> None:
    index = position % len(payload)
    edited = payload[:index] + char + payload[index + 1 :]
    if edited == payload:
        return
    salt = keccak256(b"s")
    assert leaf_hash(salt, ZERO_HASH, edited) != leaf_hash(salt, ZERO_HASH, payload)


def test_salts_are_fresh_32_bytes() -> None:
    assert len(new_salt()) == 32 and new_salt() != new_salt()
