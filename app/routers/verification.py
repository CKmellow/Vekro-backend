import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.orm import Session

from app.core.auth_context import CurrentUser, get_client_identifier
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.anchor_batch import AnchorBatch
from app.models.transaction import Transaction
from app.models.user import User, UserRole
from app.schemas.anchoring import (
    LiabilitiesProofResponse,
    PublicBatchResponse,
    SellerTrackRecordResponse,
    TransactionVerificationResponse,
)
from app.services.anchoring.canonical import hex32
from app.services.anchoring.chain_config import SUPPORTED_CHAINS
from app.services.anchoring.liabilities import liabilities_proof
from app.services.anchoring.track_record import seller_track_record
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
    "/transactions/{transaction_id}/liabilities-proof",
    response_model=LiabilitiesProofResponse,
    summary="Proof the order's held money is counted in Vekro's published total (buyer, admin)",
    description=(
        "The order's leaf, salt and Merkle sum proof in the latest liabilities snapshot, the "
        "snapshot's root and total, and the anchored liabilities_snapshot event with its batch "
        "proof. The browser checks the leaf is this order's (order_commit), folds the proof (no "
        "negative or overflowing sums), compares the root and total with the anchored payload, "
        "and reads the batch root from the AnchorRegistry. 404 when the order is not held now."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_404_NOT_FOUND: {
            "description": "Not the caller's order, or no held money for it in the latest snapshot."
        },
    },
)
def get_liabilities_proof(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> LiabilitiesProofResponse:
    transaction = db.get(Transaction, transaction_id)
    allowed = transaction is not None and (
        current_user.role == UserRole.ADMIN or current_user.id == transaction.buyer_id
    )
    proof = liabilities_proof(db, transaction_id) if allowed else None
    if proof is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No proof for this order."
        )
    return LiabilitiesProofResponse.model_validate(proof)


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
    _check_public_quota(request)
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


@router.get(
    "/sellers/{seller_id}/track-record",
    response_model=SellerTrackRecordResponse,
    summary="A seller's verifiable track record (public)",
    description=(
        "Each completed sale as a public, anchored event (seller, outcome, amount band; no buyer "
        "or order) with its Merkle proof, so anyone can check the count against the "
        "AnchorRegistry. Rate-limited per client."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No such seller."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many lookups. See Retry-After."},
    },
)
def get_seller_track_record(
    seller_id: uuid.UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> SellerTrackRecordResponse:
    _check_public_quota(request)
    seller = db.get(User, seller_id)
    if seller is None or not seller.has_store:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Seller not found.")
    record = seller_track_record(db, seller_id, get_settings())
    return SellerTrackRecordResponse.model_validate(record)


def _check_public_quota(request: Request) -> None:
    try:
        check_lookup_quota("anchoring:" + get_client_identifier(request))
    except PublicLookupRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many lookups. Please wait a moment.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
