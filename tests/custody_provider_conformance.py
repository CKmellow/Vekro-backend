from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.ports import CustodyProvider


@dataclass(frozen=True)
class ProviderConformanceVector:
    success_funding_reference: str
    unknown_funding_reference: str
    failed_funding_reference: str
    success_release_purpose: str
    idempotent_release_purpose: str
    unknown_release_purpose: str
    failed_refund_purpose: str


def run_provider_conformance(
    provider: CustodyProvider,
    *,
    vector: ProviderConformanceVector,
) -> None:
    capabilities = provider.capabilities()
    assert isinstance(capabilities.holds_funds_structurally, bool)
    assert isinstance(capabilities.supports_split_payout, bool)
    assert isinstance(capabilities.supports_partial_release, bool)

    open_request = OpenEscrowRequest(
        transaction_id=str(uuid.uuid4()),
        buyer_id=str(uuid.uuid4()),
        seller_id=str(uuid.uuid4()),
        amount=Decimal("500.00"),
        currency="KES",
    )
    escrow_record = provider.open_escrow(open_request)

    assert escrow_record.escrow_reference
    assert escrow_record.transaction_id == open_request.transaction_id
    assert escrow_record.amount == Decimal("500.00")

    funding_success = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("500.00"),
            phone_number="+254700111222",
            account_reference=vector.success_funding_reference,
        )
    )
    funding_unknown = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("10.00"),
            phone_number="+254700111222",
            account_reference=vector.unknown_funding_reference,
        )
    )
    funding_failed = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("15.00"),
            phone_number="+254700111222",
            account_reference=vector.failed_funding_reference,
        )
    )

    assert funding_success.outcome == CollectionOutcome.SUCCEEDED
    assert funding_unknown.outcome == CollectionOutcome.UNKNOWN
    assert funding_failed.outcome == CollectionOutcome.FAILED_DEFINITE

    release_success = provider.release(
        PayoutRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("80.00"),
            destination_phone="+254700111333",
            purpose=vector.success_release_purpose,
        )
    )
    assert release_success.outcome == PayoutOutcome.SUCCEEDED

    idempotent_request = PayoutRequest(
        escrow_reference=escrow_record.escrow_reference,
        amount=Decimal("35.00"),
        destination_phone="+254700111333",
        purpose=vector.idempotent_release_purpose,
    )
    idempotent_first = provider.release(idempotent_request)
    idempotent_second = provider.release(idempotent_request)

    assert idempotent_first.outcome == PayoutOutcome.SUCCEEDED
    assert idempotent_second.outcome == PayoutOutcome.SUCCEEDED
    assert idempotent_second.provider_reference == idempotent_first.provider_reference

    release_unknown = provider.release(
        PayoutRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("20.00"),
            destination_phone="+254700111333",
            purpose=vector.unknown_release_purpose,
        )
    )
    refund_failed = provider.refund(
        PayoutRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=Decimal("25.00"),
            destination_phone="+254700111222",
            purpose=vector.failed_refund_purpose,
        )
    )

    assert release_unknown.outcome == PayoutOutcome.UNKNOWN
    assert refund_failed.outcome == PayoutOutcome.FAILED_DEFINITE

    status = provider.get_status(escrow_record.escrow_reference)
    assert status.escrow_reference == escrow_record.escrow_reference
    assert status.funded_amount >= Decimal("0.00")
    assert status.released_amount >= Decimal("0.00")
    assert status.refunded_amount >= Decimal("0.00")