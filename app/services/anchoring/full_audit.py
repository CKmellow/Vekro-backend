"""Full anchoring audit from the source tables (M21 A6, admin and operator only).

Where the per-transaction verifier sees one chain, this sees everything:

- batches: event count equals ``leaf_count``, seq is gapless across batches, roots recompute, each
  batch links to the previous one, and the registry holds each confirmed root;
- chains: every event's leaf recomputes and follows its predecessor, per transaction and global;
- sources: every ledger row, money audit row and transaction has its event, every event's source
  row still exists, and source-backed payloads rebuild byte for byte from their rows;
- live state: transaction status and amount, admin decisions (``admin_ref`` resolved with the
  HMAC secret), and escrow totals against the anchored ledger rows (design 5.7).

Deleting a transaction's latest event leaves its chain looking valid; only this audit catches it
(through the batch count and the seq gap, or the status check).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.anchor_batch import AnchorBatch, AnchorBatchStatus
from app.models.anchor_event import AnchorEvent
from app.models.dispute import Dispute
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry, LedgerEntrySide
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import Transaction
from app.services.anchoring import payloads
from app.services.anchoring.admin_ref import admin_ref, configured_keys
from app.services.anchoring.canonical import (
    ZERO_HASH,
    CanonicalEncodingError,
    canonical_json,
    leaf_hash,
)
from app.services.anchoring.chain import ChainUnavailableError
from app.services.anchoring.merkle import merkle_root
from app.services.anchoring.recorder import (
    AUDIT_TABLE,
    LEDGER_TABLE,
    SELLER_SALES_SOURCE,
    TRANSACTIONS_TABLE,
)
from app.services.transitions import SALE_STATUSES

AuditVerdict = Literal["CLEAN", "TAMPERED", "UNAVAILABLE"]

RELEASE_ACCOUNT = "SELLER_PAYABLE"
REFUND_ACCOUNT = "BUYER_REFUNDABLE"
FUNDING_ACCOUNT = "ESCROW_HELD"


@dataclass(frozen=True)
class AuditFinding:
    code: str
    detail: str
    transaction_id: str | None = None


@dataclass
class AuditReport:
    verdict: AuditVerdict = "CLEAN"
    findings: list[AuditFinding] = field(default_factory=list)
    events: int = 0
    batches: int = 0

    def add(self, code: str, detail: str, transaction_id: Any = None) -> None:
        self.findings.append(
            AuditFinding(code, detail, str(transaction_id) if transaction_id else None)
        )


def run_full_audit(db: Session, root_of: Callable[[int], bytes] | None = None) -> AuditReport:
    report = AuditReport()
    events = list(db.execute(select(AnchorEvent).order_by(AnchorEvent.id)).scalars())
    report.events = len(events)
    try:
        _audit_batches(db, report, events, root_of)
    except ChainUnavailableError as exc:
        report.verdict = "UNAVAILABLE"
        report.add("chain_unavailable", str(exc))
        return report
    bodies = _audit_chains(report, events)
    _audit_sources(db, report, events, bodies)
    _audit_live_state(db, report, events, bodies)
    _audit_liabilities(db, report, events, bodies)
    _audit_reserves(report, events, bodies)
    if report.findings:
        report.verdict = "TAMPERED"
    return report


def _audit_batches(
    db: Session,
    report: AuditReport,
    events: list[AnchorEvent],
    root_of: Callable[[int], bytes] | None,
) -> None:
    by_batch: dict[int, list[AnchorEvent]] = defaultdict(list)
    for event in events:
        if event.batch_id is not None:
            by_batch[int(event.batch_id)].append(event)
    batches = list(db.execute(select(AnchorBatch).order_by(AnchorBatch.id)).scalars())
    report.batches = len(batches)
    prev_root, next_seq = ZERO_HASH, 1
    for batch in batches:
        members = sorted(by_batch.pop(batch.id, []), key=lambda item: item.leaf_index or 0)
        if len(members) != batch.leaf_count:
            report.add(
                "batch_event_missing",
                f"Batch {batch.id} has {len(members)} of {batch.leaf_count} events.",
            )
        seqs = [int(item.seq or 0) for item in members]
        expected = list(range(batch.first_seq, batch.last_seq + 1))
        if seqs != expected[: len(seqs)] or len(seqs) != len(expected):
            missing = sorted(set(expected) - set(seqs))
            report.add("seq_gap", f"Batch {batch.id} is missing seq {missing[:10]}.")
        if not members or merkle_root([bytes(item.leaf_hash) for item in members]) != batch.root:
            report.add("batch_root_mismatch", f"Batch {batch.id} no longer has its root.")
        if batch.prev_root != prev_root or batch.first_seq != next_seq:
            report.add("batch_chain_broken", f"Batch {batch.id} does not follow the previous one.")
        if root_of is not None and batch.status == AnchorBatchStatus.CONFIRMED:
            if root_of(batch.id) != batch.root:
                report.add("onchain_root_mismatch", f"Registry root for batch {batch.id} differs.")
        prev_root, next_seq = batch.root, batch.last_seq + 1
    for batch_id in by_batch:
        report.add("unknown_batch", f"Events point at batch {batch_id}, which is not recorded.")


def _audit_chains(report: AuditReport, events: list[AnchorEvent]) -> dict[int, dict[str, Any]]:
    heads: dict[Any, bytes] = {}
    bodies: dict[int, dict[str, Any]] = {}
    for event in events:
        key = event.transaction_id
        prev = heads.get(key, ZERO_HASH)
        if bytes(event.prev_event_hash) != prev:
            report.add(
                "chain_broken",
                f"Event {event.id} ({event.event_type}) does not follow its predecessor.",
                key,
            )
        if leaf_hash(bytes(event.salt), bytes(event.prev_event_hash), event.payload) != bytes(
            event.leaf_hash
        ):
            report.add("leaf_mismatch", f"Event {event.id} hash does not match its data.", key)
        try:
            body = json.loads(event.payload)
            if canonical_json(body) != event.payload:
                report.add("non_canonical_payload", f"Event {event.id} payload changed form.", key)
            bodies[event.id] = body
        except (ValueError, CanonicalEncodingError):
            report.add("non_canonical_payload", f"Event {event.id} payload is not JSON.", key)
        heads[key] = bytes(event.leaf_hash)
    return bodies


def _audit_sources(
    db: Session,
    report: AuditReport,
    events: list[AnchorEvent],
    bodies: dict[int, dict[str, Any]],
) -> None:
    keys = configured_keys()
    by_source = {
        (event.source_table, event.source_id): event
        for event in events
        if event.source_table and event.source_id
    }
    escrow_txn = dict(db.execute(select(Escrow.id, Escrow.transaction_id)).tuples().all())

    def compare(
        event: AnchorEvent, rebuilt: dict[str, Any] | list[dict[str, Any]], label: str
    ) -> None:
        candidates = rebuilt if isinstance(rebuilt, list) else [rebuilt]
        if all(canonical_json(item) != event.payload for item in candidates):
            report.add(
                "source_row_changed",
                f"{label} no longer matches what was anchored.",
                event.transaction_id,
            )

    for entry in db.execute(select(LedgerEntry)).scalars():
        event = by_source.pop((LEDGER_TABLE, str(entry.id)), None)
        transaction_id = escrow_txn.get(entry.escrow_id)
        if event is None:
            report.add("event_missing", f"Ledger row {entry.id} has no event.", transaction_id)
            continue
        backfilled = bool(bodies.get(event.id, {}).get("backfilled"))
        compare(
            event,
            payloads.ledger_entry_payload(entry, transaction_id, backfilled=backfilled),
            f"Ledger row {entry.id}",
        )
    for audit in db.execute(select(MoneyAuditEvent)).scalars():
        event = by_source.pop((AUDIT_TABLE, str(audit.id)), None)
        if event is None:
            report.add("event_missing", f"Audit row {audit.id} has no event.", audit.transaction_id)
            continue
        backfilled = bool(bodies.get(event.id, {}).get("backfilled"))
        # An admin's actor_ref was made with the key that was newest then (#295).
        compare(
            event,
            [
                payloads.money_audit_payload(audit, backfilled=backfilled, admin_keys=[key])
                for key in keys
            ],
            f"Audit row {audit.id}",
        )
    for transaction in db.execute(select(Transaction)).scalars():
        event = by_source.pop((TRANSACTIONS_TABLE, str(transaction.id)), None)
        if event is None:
            has_snapshot = any(
                item.transaction_id == transaction.id
                and item.event_type == payloads.BACKFILL_SNAPSHOT
                for item in events
            )
            if not has_snapshot:
                report.add("event_missing", "Transaction has no creation event.", transaction.id)
    transactions = {str(item.id): item for item in db.execute(select(Transaction)).scalars()}
    for (table, source_id), event in by_source.items():
        if table == SELLER_SALES_SOURCE:
            _audit_sale(
                report, transactions.get(source_id), bodies.get(event.id, {}), source_id, keys
            )
            continue
        report.add(
            "source_missing",
            f"{table} row {source_id} was anchored but no longer exists.",
            event.transaction_id,
        )
    recorded_sales = {source_id for (table, source_id) in by_source if table == SELLER_SALES_SOURCE}
    for transaction_id, transaction in transactions.items():
        if transaction.status in SALE_STATUSES and transaction_id not in recorded_sales:
            report.add(
                "sale_event_missing",
                "A completed sale has no track-record event (run anchor-sales-backfill).",
                transaction_id,
            )


def _audit_sale(
    report: AuditReport,
    transaction: Transaction | None,
    body: dict[str, Any],
    source_id: str,
    keys: list[str],
) -> None:
    """A public sale must be a real completed sale of the seller it names, under its own ref
    (made with whichever configured key was newest when the sale was recorded, #295)."""
    if (
        transaction is None
        or transaction.status not in SALE_STATUSES
        or body.get("seller_id") != str(transaction.seller_id)
        or body.get("sale_ref") not in {payloads.sale_ref(transaction.id, keys=[k]) for k in keys}
        or body.get("amount_band") != payloads.amount_band(transaction.amount)
    ):
        report.add(
            "sale_record_mismatch",
            "A track-record sale does not match a completed sale of that seller.",
            source_id,
        )


def _audit_live_state(
    db: Session,
    report: AuditReport,
    events: list[AnchorEvent],
    bodies: dict[int, dict[str, Any]],
) -> None:
    keys = configured_keys()
    expected_status: dict[Any, str] = {}
    expected_amount: dict[Any, str] = {}
    decisions: dict[str, dict[str, Any]] = {}
    for event in events:
        body = bodies.get(event.id, {})
        kind = body.get("type")
        if kind == payloads.TRANSACTION_TRANSITION:
            expected_status[event.transaction_id] = body.get("to")
        elif kind == payloads.BACKFILL_SNAPSHOT:
            expected_status[event.transaction_id] = body.get("status")
            expected_amount[event.transaction_id] = body.get("amount")
        elif kind == payloads.TRANSACTION_CREATED:
            expected_amount[event.transaction_id] = body.get("amount")
        elif kind == payloads.ADMIN_DECISION:
            decisions[str(body.get("dispute_id"))] = body

    for transaction in db.execute(select(Transaction)).scalars():
        status = expected_status.get(transaction.id)
        if status is not None and transaction.status.value != status:
            report.add(
                "status_mismatch",
                f"Status {transaction.status.value!r} differs from the last anchored {status!r}.",
                transaction.id,
            )
        amount = expected_amount.get(transaction.id)
        if amount is not None and payloads.money(transaction.amount) != amount:
            report.add("amount_mismatch", "Amount differs from the anchored one.", transaction.id)

    for dispute in db.execute(select(Dispute)).scalars():
        decision = decisions.get(str(dispute.id))
        if decision is None:
            continue
        live = {
            "decision": getattr(dispute.admin_decision, "value", dispute.admin_decision),
            "reason": dispute.admin_reason,
            "seller_share": payloads.ratio(dispute.split_ratio),
        }
        # The anchored admin_ref may be under an older key (#295).
        admin_refs = {admin_ref(dispute.decided_by_user_id, keys=[key]) for key in keys}
        if (
            any(decision.get(key) != value for key, value in live.items())
            or decision.get("admin_ref") not in admin_refs
        ):
            report.add(
                "admin_decision_changed",
                "The admin decision differs from the anchored one.",
                dispute.transaction_id,
            )

    totals: dict[Any, dict[str, Decimal]] = defaultdict(
        lambda: {"funded": Decimal("0"), "released": Decimal("0"), "refunded": Decimal("0")}
    )
    for entry in db.execute(select(LedgerEntry)).scalars():
        if entry.side != LedgerEntrySide.DEBIT:
            continue
        bucket = totals[entry.escrow_id]
        if entry.account_code == FUNDING_ACCOUNT:
            bucket["funded"] += entry.amount
        elif entry.account_code == RELEASE_ACCOUNT:
            bucket["released"] += entry.amount
        elif entry.account_code == REFUND_ACCOUNT:
            bucket["refunded"] += entry.amount
    for escrow in db.execute(select(Escrow)).scalars():
        ledger = totals[escrow.id]
        live = {
            "funded": escrow.funded_amount or Decimal("0"),
            "released": escrow.released_amount or Decimal("0"),
            "refunded": escrow.refunded_amount or Decimal("0"),
        }
        for name, value in live.items():
            if Decimal(value).quantize(Decimal("0.01")) != ledger[name].quantize(Decimal("0.01")):
                report.add(
                    "escrow_total_mismatch",
                    f"Escrow {name} total {value} differs from the ledger {ledger[name]}.",
                    escrow.transaction_id,
                )


def _audit_liabilities(
    db: Session, report: AuditReport, events: list[AnchorEvent], bodies: dict[int, dict[str, Any]]
) -> None:
    """Each anchored liabilities snapshot against its stored leaves (M24 B2): editing an amount or
    dropping an order after the fact changes the rebuilt root, total or count."""
    from sqlalchemy import inspect as sa_inspect

    from app.models.liabilities import LiabilitiesLeaf
    from app.services.anchoring.sum_tree import SumTree, leaf

    if not sa_inspect(db.get_bind()).has_table(LiabilitiesLeaf.__tablename__):
        return
    for event in events:
        if event.event_type != payloads.LIABILITIES_SNAPSHOT:
            continue
        body = bodies.get(event.id) or {}
        rows = list(
            db.execute(
                select(LiabilitiesLeaf)
                .where(LiabilitiesLeaf.snapshot_id == body.get("snapshot_id"))
                .order_by(LiabilitiesLeaf.leaf_index)
            ).scalars()
        )
        if rows:
            root = SumTree([leaf(r.salt, r.order_commit, int(r.amount_cents)) for r in rows]).root
            rebuilt = (payloads.hex32(root.hash), payloads.money(Decimal(root.sum) / 100))
        else:
            rebuilt = (None, payloads.money(Decimal(0)))
        if (body.get("root"), body.get("total"), body.get("leaf_count")) != (*rebuilt, len(rows)):
            report.add(
                "liabilities_mismatch",
                f"Liabilities snapshot {body.get('snapshot_id')} no longer matches its anchored "
                "root, total or count.",
            )


def _audit_reserves(
    report: AuditReport, events: list[AnchorEvent], bodies: dict[int, dict[str, Any]]
) -> None:
    for event in events:
        if event.event_type != payloads.RESERVES_RECONCILIATION:
            continue
        body = bodies.get(event.id) or {}
        attested, liability = body.get("attested_total"), body.get("liability_total")
        if attested is None:
            consistent = body.get("difference") is None
        else:
            consistent = body.get("difference") == payloads.money(
                Decimal(attested) - Decimal(liability)
            )
        if not consistent:
            report.add(
                "reserves_inconsistent",
                f"Reserves reconciliation event {event.id} has a difference that does not add up.",
            )
