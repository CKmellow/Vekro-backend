import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.orm import Session

from app.core.auth_context import CurrentUser, get_client_identifier
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.anchor_batch import AnchorBatch
from app.models.transaction import Transaction
from app.models.user import UserRole
from app.schemas.anchoring import PublicBatchResponse, TransactionVerificationResponse
from app.services.anchoring.canonical import hex32
from app.services.anchoring.chain_config import SUPPORTED_CHAINS
from app.services.anchoring.verification import build_transaction_verification
from app.services.public_order import PublicLookupRateLimitedError, check_lookup_quota

router = APIRouter(tags=["Verification"])


@router.get(
    "/transactions/{transaction_id}/verification",
    response_model=TransactionVerificationResponse,
    summary="Verification data for an order (buyer, seller or admin)",
    description=(
        "Everything needed to check the order's tamper-evident record independently: each event's "
        "canonical payload, salt, hashes and Merkle proof in chain order, and the live status and "
        "amount. Data only: the verdict is the verifier's, after reading each batch root from the "
        "AnchorRegistry. See docs/frontend-verification-contract.md."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_404_NOT_FOUND: {
            "description": "Transaction not found, or the caller is not a party to it."
        },
    },
)
def get_transaction_verification(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> TransactionVerificationResponse:
    transaction = db.get(Transaction, transaction_id)
    allowed = transaction is not None and (
        current_user.role == UserRole.ADMIN
        or current_user.id in (transaction.buyer_id, transaction.seller_id)
    )
    if transaction is None or not allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found.")
    bundle = build_transaction_verification(db, transaction, get_settings())
    return TransactionVerificationResponse.model_validate(bundle)


@router.get(
    "/anchoring/batches/{batch_id}",
    response_model=PublicBatchResponse,
    summary="An anchored batch (public)",
    description=(
        "A batch's root, sequence range, status and anchoring transaction. No personal data. "
        "Rate-limited per client."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No such batch."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many lookups. See Retry-After."},
    },
)
def get_public_batch(
    batch_id: Annotated[int, Path(ge=1)],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PublicBatchResponse:
    try:
        check_lookup_quota("anchoring:" + get_client_identifier(request))
    except PublicLookupRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many lookups. Please wait a moment.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    batch = db.get(AnchorBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
    return PublicBatchResponse(
        batch_id=batch.id,
        root=hex32(batch.root),
        prev_root=hex32(batch.prev_root),
        first_seq=batch.first_seq,
        last_seq=batch.last_seq,
        leaf_count=batch.leaf_count,
        status=batch.status.value,
        tx_hash=("0x" + batch.tx_hash.hex()) if batch.tx_hash else None,
        block_number=batch.block_number,
        confirmed_at=batch.confirmed_at,
        chain_id=batch.chain_id,
        network=SUPPORTED_CHAINS.get(batch.chain_id),
        registry_address=batch.registry_address,
    )
