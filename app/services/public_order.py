"""Public order summary (frontend gap #10): the order behind a link, without personal data."""

from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.transaction import Transaction
from app.services.listing_content import UNPAID_STATUSES, listing_status
from app.services.transaction_list import SEGMENT_STATUSES, OrderSegment

LOOKUPS_PER_MINUTE = 30
_BUCKETS: dict[str, deque[datetime]] = {}
_LOCK = Lock()

StateGroup = Literal["unpaid", "paid", "closed"]


class PublicLookupRateLimitedError(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many order lookups")
        self.retry_after_seconds = max(int(retry_after_seconds), 1)


def check_lookup_quota(client_identifier: str, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    window = timedelta(minutes=1)
    with _LOCK:
        bucket = _BUCKETS.setdefault(client_identifier, deque())
        while bucket and bucket[0] <= current - window:
            bucket.popleft()
        if len(bucket) >= LOOKUPS_PER_MINUTE:
            raise PublicLookupRateLimitedError((bucket[0] + window - current).total_seconds())
        bucket.append(current)


def state_group(transaction: Transaction) -> StateGroup:
    if transaction.status in UNPAID_STATUSES:
        return "unpaid"
    if transaction.status in SEGMENT_STATUSES[OrderSegment.COMPLETED]:
        return "closed"
    return "paid"


def find_public_order(db: Session, key: str) -> tuple[Transaction, Listing, str] | None:
    """The order for an SMS link id or a reference, with its listing and listing status."""
    text = key.strip()
    try:
        condition = Transaction.id == uuid.UUID(text)
    except ValueError:
        condition = Transaction.reference == text.upper()
    row = db.execute(
        select(Transaction, Listing)
        .join(Listing, Listing.id == Transaction.listing_id)
        .where(condition)
    ).one_or_none()
    if row is None:
        return None
    transaction, listing = row
    return transaction, listing, listing_status(db, listing.id)
