from __future__ import annotations

import hmac

from app.services.custody.loop_auth import LOOP_SIGNING_TEST_VECTOR, build_loop_signature


def run_rails_selftest() -> None:
    vector = LOOP_SIGNING_TEST_VECTOR
    actual = build_loop_signature(
        secret=vector.secret,
        timestamp=vector.timestamp,
        nonce=vector.nonce,
        payload=vector.payload,
    )
    if not hmac.compare_digest(actual, vector.expected_signature):
        raise RuntimeError(
            "LOOP signing self-test failed: expected " f"{vector.expected_signature}, got {actual}."
        )


def main() -> None:
    run_rails_selftest()
    print("LOOP rails self-test passed.")


if __name__ == "__main__":
    main()
