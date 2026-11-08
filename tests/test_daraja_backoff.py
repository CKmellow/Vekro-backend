"""#243: the callback is the primary path; the fallback watcher backs off so the sandbox's STK
query rate limit (HTTP 429, docs/daraja-observed.md section 4) is never hit in a class demo."""

import random
from typing import Any

from app.models.payment_prompt import PaymentPromptStatus
from app.models.transaction import TransactionStatus
from app.services.daraja_confirmation import Backoff, next_delay, watch_pending_payments
from app.services.payment_prompts import resolve_open_prompt

from tests.test_daraja import FakeDaraja, _awaiting, _db

NO_JITTER = Backoff(jitter=0.0)
PENDING = (500, {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
LIMITED = (429, {})
PAID = (200, {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."})


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: list[Any] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in self.on_sleep:
            hook(self.now)


def _scripted(fake: FakeDaraja, clock: FakeClock, answers: list[tuple[int, dict]]) -> list[float]:
    """Daraja answers in order (the last one repeats); returns the clock time of each query."""
    times: list[float] = []

    def answer() -> tuple[int, dict]:
        times.append(clock())
        return answers[min(len(times) - 1, len(answers) - 1)]

    fake.query = answer
    return times


def _watch(db, fake, clock, seconds, backoff=NO_JITTER):
    return watch_pending_payments(
        db,
        client=fake.client(),
        duration_seconds=seconds,
        backoff=backoff,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
    )


def test_backs_off_from_5s_doubling_to_a_30s_cap_until_paid() -> None:
    db = _db()
    transaction, _prompt = _awaiting(db, "81")
    fake, clock = FakeDaraja(), FakeClock()
    times = _scripted(fake, clock, [LIMITED, LIMITED, PENDING, PAID])

    outcomes = _watch(db, fake, clock, 120)

    assert times == [5.0, 15.0, 35.0, 65.0]
    assert outcomes[transaction.id] == "confirmed"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED


def test_jitter_stays_within_bounds_and_the_cap_holds() -> None:
    backoff, rng = Backoff(), random.Random(1)
    delay = None
    for _ in range(200):
        delay = next_delay(delay, backoff, rng)
        assert 5.0 * 0.8 <= delay <= 30.0 * 1.2
    assert next_delay(None, backoff, rng) <= 5.0 * 1.2


def test_never_two_queries_closer_than_the_minimum_gap() -> None:
    db = _db()
    _awaiting(db, "82")
    _awaiting(db, "83")
    fake, clock = FakeDaraja(), FakeClock()
    times = _scripted(fake, clock, [PAID])

    _watch(db, fake, clock, 60)

    assert times == [5.0, 7.0]


def test_a_rate_limited_or_pending_prompt_is_never_failed_and_the_budget_ends_the_watch() -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "84")
    fake, clock = FakeDaraja(), FakeClock()
    times = _scripted(fake, clock, [LIMITED])

    outcomes = _watch(db, fake, clock, 100)

    assert times == [5.0, 15.0, 35.0, 65.0, 95.0]
    assert clock() >= 100
    assert outcomes[transaction.id] == "unconfirmed"
    db.refresh(prompt)
    db.refresh(transaction)
    assert prompt.status == PaymentPromptStatus.PENDING.value
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT


def test_a_prompt_the_callback_settles_first_is_never_queried() -> None:
    db = _db()
    transaction, _prompt = _awaiting(db, "85")
    fake, clock = FakeDaraja(), FakeClock()
    times = _scripted(fake, clock, [PAID])

    def callback_settles(now: float) -> None:
        if now >= 3 and resolve_open_prompt(db, transaction.id, result_code=1032):
            db.commit()

    clock.on_sleep.append(callback_settles)
    _watch(db, fake, clock, 30)

    assert times == []


def test_prompts_that_appear_while_watching_are_picked_up() -> None:
    db = _db()
    fake, clock = FakeDaraja(), FakeClock()
    times = _scripted(fake, clock, [PAID])
    created: list[Any] = []

    def new_order(now: float) -> None:
        if now >= 10 and not created:
            created.append(_awaiting(db, "86"))
            db.commit()

    clock.on_sleep.append(new_order)
    outcomes = _watch(db, fake, clock, 60)

    ((transaction, _prompt),) = created
    assert outcomes == {transaction.id: "confirmed"}
    assert len(times) == 1 and 15.0 <= times[0] <= 20.0


def test_stop_when_idle_returns_once_nothing_is_open() -> None:
    db = _db()
    transaction, _prompt = _awaiting(db, "87")
    fake, clock = FakeDaraja(), FakeClock()
    _scripted(fake, clock, [PENDING, PAID])

    outcomes = watch_pending_payments(
        db,
        client=fake.client(),
        duration_seconds=600,
        backoff=NO_JITTER,
        clock=clock,
        sleep=clock.sleep,
        stop_when_idle=True,
    )

    assert outcomes == {transaction.id: "confirmed"}
    assert clock() == 15.0
