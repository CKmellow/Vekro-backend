"""Turn a Daraja STK callback into a confirmed payment, and only then into funding (issue #224).

Daraja's callback carries no signature, so it is a trigger, never proof. For the prompt Vekro
itself sent (matched by ``CheckoutRequestID``), an STK query must report ``ResultCode`` 0 before
the transaction locks and the escrow funding is posted, in production too. A callback claiming a
different amount from the order's is refused and audited. A pending or unreachable query leaves
the order waiting; ``python -m app.cli confirm-mpesa-payments`` (or the next callback) settles it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_prompt import PaymentPrompt, PaymentPromptStatus
from app.models.transaction import Transaction, TransactionStatus
from app.schemas.transaction import PaymentCallbackRequest
from app.services.audit import ACTOR_PROVIDER, record_money_audit_event
from app.services.daraja import DarajaClient, DarajaUnavailableError
from app.services.payment_prompts import resolve_open_prompt

_logger = logging.getLogger("app.payments.daraja")

RAIL = "daraja"
RECONCILE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class DarajaCallback:
    checkout_request_id: str
    result_code: int | None
    amount: Decimal | None
    receipt: str | None


def masked_checkout(checkout_request_id: str) -> str:
    """Daraja's CheckoutRequestID ends with the payer's subscriber number: never log it whole."""
    return checkout_request_id[:20] + "…" if len(checkout_request_id) > 20 else "…"


def parse_callback(body: Any) -> DarajaCallback | None:
    """The fields Vekro uses from Daraja's ``Body.stkCallback``; None for anything else."""
    try:
        callback = body["Body"]["stkCallback"]
        checkout = str(callback["CheckoutRequestID"]).strip()
    except (KeyError, TypeError):
        return None
    if not checkout:
        return None
    items = (callback.get("CallbackMetadata") or {}).get("Item") or []
    meta = {str(item.get("Name")): item.get("Value") for item in items if isinstance(item, dict)}
    amount = None
    if meta.get("Amount") is not None:
        try:
            amount = Decimal(str(meta["Amount"]))
        except InvalidOperation:
            amount = None
    code = callback.get("ResultCode")
    return DarajaCallback(
        checkout_request_id=checkout,
        result_code=int(code) if str(code).lstrip("-").isdigit() else None,
        amount=amount,
        receipt=str(meta["MpesaReceiptNumber"]) if meta.get("MpesaReceiptNumber") else None,
    )


def handle_callback(db: Session, body: Any, *, client: DarajaClient) -> str:
    callback = parse_callback(body)
    if callback is None:
        _logger.warning("daraja_callback_unreadable")
        return "unreadable"
    prompt = db.execute(
        select(PaymentPrompt).where(
            PaymentPrompt.checkout_request_id == callback.checkout_request_id
        )
    ).scalar()
    if prompt is None:
        _logger.warning(
            "daraja_callback_unknown_checkout id=%s", masked_checkout(callback.checkout_request_id)
        )
        return "unknown_checkout"
    transaction = db.get(Transaction, prompt.transaction_id)
    if transaction is None:  # pragma: no cover - prompts belong to transactions
        return "unknown_checkout"
    return confirm_payment(
        db,
        transaction,
        prompt,
        client=client,
        claimed_amount=callback.amount,
        receipt=callback.receipt,
    )


def confirm_payment(
    db: Session,
    transaction: Transaction,
    prompt: PaymentPrompt,
    *,
    client: DarajaClient,
    claimed_amount: Decimal | None = None,
    receipt: str | None = None,
) -> str:
    from app.services.transaction import confirm_payment_callback

    if transaction.status != TransactionStatus.AWAITING_PAYMENT:
        return "already_processed"
    if claimed_amount is not None and claimed_amount != Decimal(transaction.amount):
        record_money_audit_event(
            db,
            action="daraja_callback_amount_mismatch",
            actor_type=ACTOR_PROVIDER,
            reason="Callback amount differs from the order; ignored.",
            transaction_id=transaction.id,
            provider_reference=prompt.checkout_request_id,
            rail_name=RAIL,
            amount=claimed_amount,
            currency="KES",
            details={"expected": str(transaction.amount)},
        )
        db.commit()
        _logger.warning(
            "daraja_callback_amount_mismatch transaction_id=%s claimed=%s expected=%s",
            transaction.id,
            claimed_amount,
            transaction.amount,
        )
        return "amount_mismatch"
    if not prompt.checkout_request_id:
        return "unknown_checkout"
    try:
        result = client.stk_query(prompt.checkout_request_id)
    except DarajaUnavailableError as exc:
        _logger.warning("daraja_query_unavailable transaction_id=%s error=%s", transaction.id, exc)
        return "unconfirmed"
    if result.state == "pending":
        return "pending"
    if result.state == "failed":
        if resolve_open_prompt(db, transaction.id, result_code=result.result_code or 1):
            db.commit()
        return "failed"
    confirm_payment_callback(
        db,
        PaymentCallbackRequest(
            transaction_id=transaction.id,
            result_code=0,
            result_desc=result.result_desc[:255] or None,
            checkout_request_id=prompt.checkout_request_id,
            provider_reference=receipt,
        ),
        confirmed_by=RAIL,
    )
    return "confirmed"


def confirm_pending_payments(
    db: Session, *, client: DarajaClient, now: datetime | None = None
) -> dict[uuid.UUID, str]:
    """Settle prompts whose callback never arrived: query each open one from the last day."""
    since = (now or datetime.now(UTC)) - RECONCILE_WINDOW
    rows = db.execute(
        select(Transaction, PaymentPrompt)
        .join(PaymentPrompt, PaymentPrompt.transaction_id == Transaction.id)
        .where(
            Transaction.status == TransactionStatus.AWAITING_PAYMENT,
            PaymentPrompt.status == PaymentPromptStatus.PENDING.value,
            PaymentPrompt.checkout_request_id.is_not(None),
            PaymentPrompt.sent_at >= since,
        )
        .order_by(PaymentPrompt.sent_at)
    ).all()
    return {
        transaction.id: confirm_payment(db, transaction, prompt, client=client)
        for transaction, prompt in rows
    }
