"""M21 A6: the verification bundle, the per-transaction verifier, the full audit, the scripts and
the tamper demo. Every verdict is covered, including the case only the full audit can catch."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.anchor_batch import AnchorBatch, AnchorBatchStatus
from app.models.anchor_event import AnchorEvent
from app.models.transaction import Transaction
from app.models.user import User, UserRole
from app.routers.verification import get_transaction_verification
from app.services.anchoring.batcher import run_once
from app.services.anchoring.chain import ChainUnavailableError
from app.services.anchoring.full_audit import run_full_audit
from app.services.anchoring.verification import build_transaction_verification
from app.services.anchoring.verify import verify_bundle
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event
from app.services.transaction import confirm_payment_callback, dispatch_transaction
from fastapi import HTTPException
from fastapi.testclient import TestClient
from scripts import audit_anchoring, demo_tamper, verify_transaction
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.anchoring_fakes import FakeRegistry
from tests.test_anchoring_batcher import CONFIG, T0, _db
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _seed_transaction_context,
)


def _dispatched_order(db: Session, suffix: str) -> tuple[User, User, Transaction]:
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    return buyer, seller, transaction


def _anchor_all(db: Session, chain: FakeRegistry) -> None:
    while True:
        result = run_once(db, chain, CONFIG, now=T0)
        if result.action == "idle":
            return
        chain.mine(2)
        assert run_once(db, chain, CONFIG, now=T0).action == "confirmed"


def _bundle(db: Session, transaction: Transaction) -> dict[str, Any]:
    db.expire_all()
    fresh = db.get(Transaction, transaction.id)
    assert fresh is not None
    bundle = build_transaction_verification(db, fresh, get_settings())
    return json.loads(json.dumps(bundle, default=str))


@pytest.fixture
def anchored() -> tuple[Session, FakeRegistry, Transaction, User, User]:
    db = _db()
    chain = FakeRegistry()
    buyer, seller, transaction = _dispatched_order(db, "v1")
    _anchor_all(db, chain)
    return db, chain, transaction, buyer, seller


def test_an_anchored_order_verifies_against_the_registry(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    bundle = _bundle(db, transaction)
    assert bundle["current"]["status"] == transaction.status.value
    assert all(event["anchor"] for event in bundle["events"])
    assert {e["event_type"] for e in bundle["events"]} >= {
        "transaction_created",
        "transaction_transition",
        "ledger_entry",
    }

    report = verify_bundle(bundle, chain.root_of)
    assert report.verdict == "VERIFIED", report.findings
    assert report.anchored == report.total == len(bundle["events"])


def test_events_not_yet_anchored_are_partially_pending(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    record_money_audit_event(
        db,
        action="note_for_test",
        actor_type=ACTOR_SYSTEM,
        transaction_id=transaction.id,
        occurred_at=datetime.now(UTC),
    )
    db.commit()
    report = verify_bundle(_bundle(db, transaction), chain.root_of)
    assert report.verdict == "PARTIALLY_PENDING"
    assert report.pending == 1

    # Batched but not on-chain yet still counts as pending.
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    assert verify_bundle(_bundle(db, transaction), chain.root_of).verdict == "PARTIALLY_PENDING"


def test_an_edited_payload_in_the_bundle_is_tampered(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    bundle = _bundle(db, transaction)
    ledger = next(e for e in bundle["events"] if e["event_type"] == "ledger_entry")
    ledger["payload"] = ledger["payload"].replace('"amount":"', '"amount":"9')
    report = verify_bundle(bundle, chain.root_of)
    assert report.verdict == "TAMPERED"
    assert "leaf_mismatch" in {finding.code for finding in report.findings}


def test_a_different_root_on_chain_is_tampered(anchored) -> None:
    db, _chain, transaction, _buyer, _seller = anchored
    report = verify_bundle(_bundle(db, transaction), lambda _batch: b"\x05" * 32)
    assert report.verdict == "TAMPERED"
    assert {finding.code for finding in report.findings} == {"root_mismatch"}


def test_an_unreachable_registry_is_unavailable(anchored) -> None:
    db, _chain, transaction, _buyer, _seller = anchored

    def down(_batch: int) -> bytes:
        raise ChainUnavailableError("RPC unreachable")

    assert verify_bundle(_bundle(db, transaction), down).verdict == "UNAVAILABLE"
    assert verify_bundle({"events": []}, down).verdict == "UNAVAILABLE"


def test_deleting_a_middle_event_breaks_the_chain(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    demo_tamper.tamper(db, "delete-middle-event", transaction.reference)

    report = verify_bundle(_bundle(db, transaction), chain.root_of)
    assert report.verdict == "TAMPERED"
    assert "chain_broken" in {finding.code for finding in report.findings}
    audit = run_full_audit(db, chain.root_of)
    assert audit.verdict == "TAMPERED"
    assert {"chain_broken", "batch_event_missing", "seq_gap"} <= {f.code for f in audit.findings}


def test_a_status_set_without_a_transition_is_tampered(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    demo_tamper.tamper(db, "change-status", transaction.reference)

    report = verify_bundle(_bundle(db, transaction), chain.root_of)
    assert report.verdict == "TAMPERED"
    assert {finding.code for finding in report.findings} == {"status_mismatch"}
    assert "status_mismatch" in {f.code for f in run_full_audit(db, chain.root_of).findings}


def test_an_edited_ledger_row_is_caught_by_the_full_audit(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    assert run_full_audit(db, chain.root_of).verdict == "CLEAN"
    demo_tamper.tamper(db, "edit-ledger", transaction.reference)

    audit = run_full_audit(db, chain.root_of)
    assert audit.verdict == "TAMPERED"
    assert "source_row_changed" in {finding.code for finding in audit.findings}


def test_deleting_the_last_event_is_caught_only_by_the_full_audit(anchored) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    # The latest event is an audit note, so no status depends on it.
    record_money_audit_event(
        db,
        action="note_for_test",
        actor_type=ACTOR_SYSTEM,
        transaction_id=transaction.id,
        occurred_at=datetime.now(UTC),
    )
    db.commit()
    _anchor_all(db, chain)
    demo_tamper.tamper(db, "delete-last-event", transaction.reference)

    assert verify_bundle(_bundle(db, transaction), chain.root_of).verdict == "VERIFIED"
    audit = run_full_audit(db, chain.root_of)
    assert audit.verdict == "TAMPERED"
    codes = {finding.code for finding in audit.findings}
    assert {"batch_event_missing", "seq_gap", "batch_root_mismatch", "event_missing"} <= codes


def test_the_full_audit_checks_on_chain_roots_and_reports_an_unreachable_chain(anchored) -> None:
    db, _chain, _transaction, _buyer, _seller = anchored
    forged = run_full_audit(db, lambda _batch: b"\x09" * 32)
    assert "onchain_root_mismatch" in {finding.code for finding in forged.findings}

    def down(_batch: int) -> bytes:
        raise ChainUnavailableError("RPC unreachable")

    assert run_full_audit(db, down).verdict == "UNAVAILABLE"
    assert run_full_audit(db, None).verdict == "CLEAN"


def test_parties_and_admins_read_the_bundle_and_strangers_get_404(anchored) -> None:
    db, _chain, transaction, buyer, seller = anchored
    admin = User(name="Support", phone="+254700000999", role=UserRole.ADMIN, password_hash="x")
    stranger = User(name="Other", phone="+254700000998", role=UserRole.BUYER, password_hash="x")
    for viewer in (buyer, seller, admin):
        response = get_transaction_verification(transaction.id, viewer, db)
        assert response.reference == transaction.reference
        assert response.events and response.events[0].event_type == "transaction_created"
    with pytest.raises(HTTPException) as error:
        get_transaction_verification(transaction.id, stranger, db)
    assert error.value.status_code == 404


def test_the_verification_endpoint_needs_a_session() -> None:
    response = TestClient(app).get(
        "/transactions/00000000-0000-4000-8000-000000000000/verification"
    )
    assert response.status_code == 401


@pytest.fixture
def batch_db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[cast(Table, AnchorBatch.__table__), cast(Table, AnchorEvent.__table__)]
    )
    db = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_the_public_batch_read_has_no_personal_data(batch_db: Session, monkeypatch) -> None:
    batch_db.add(
        AnchorBatch(
            id=1,
            root=b"\x01" * 32,
            prev_root=bytes(32),
            first_seq=1,
            last_seq=4,
            leaf_count=4,
            status=AnchorBatchStatus.CONFIRMED,
            chain_id=84532,
            registry_address="0x" + "ab" * 20,
            tx_hashes="",
            attempts=1,
            tx_hash=b"\x02" * 32,
            block_number=99,
        )
    )
    batch_db.commit()
    client = TestClient(app)
    body = client.get("/anchoring/batches/1").json()
    assert body == {
        "batch_id": 1,
        "root": "0x" + "01" * 32,
        "prev_root": "0x" + "00" * 32,
        "first_seq": 1,
        "last_seq": 4,
        "leaf_count": 4,
        "status": "confirmed",
        "tx_hash": "0x" + "02" * 32,
        "block_number": 99,
        "confirmed_at": None,
        "chain_id": 84532,
        "network": "base-sepolia",
        "registry_address": "0x" + "ab" * 20,
    }
    assert client.get("/anchoring/batches/2").status_code == 404

    from app.routers import verification
    from app.services.public_order import PublicLookupRateLimitedError

    def limited(_client: str) -> None:
        raise PublicLookupRateLimitedError(12)

    monkeypatch.setattr(verification, "check_lookup_quota", limited)
    response = client.get("/anchoring/batches/1")
    assert response.status_code == 429 and response.headers["Retry-After"] == "12"


def test_the_verify_script_reads_a_downloaded_bundle(anchored, tmp_path: Path, capsys) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_bundle(db, transaction)))

    assert verify_transaction.main(["--bundle", str(receipt)], root_of=chain.root_of) == 0
    assert capsys.readouterr().out.startswith("VERIFIED " + transaction.reference)

    demo_tamper.tamper(db, "change-status", transaction.reference)
    code = verify_transaction.main(
        ["--transaction-id", str(transaction.id)],
        root_of=chain.root_of,
        load_bundle=lambda _id, _database=None: _bundle(db, transaction),
    )
    assert code == 1
    assert "status_mismatch" in capsys.readouterr().out


def test_the_audit_script_exit_codes(anchored, capsys) -> None:
    db, chain, transaction, _buyer, _seller = anchored
    assert audit_anchoring.main([], session=lambda: db, root_of=chain.root_of) == 0
    demo_tamper.tamper(db, "edit-ledger", transaction.reference)
    assert audit_anchoring.main(["--no-chain"], session=lambda: db) == 1
    assert "source_row_changed" in capsys.readouterr().out


def _tamper_settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "environment": "development",
        "database_url": "postgresql://vekro:pw@127.0.0.1:5432/vekro_base_sepolia",
        "alembic_database_url": "",
        "anchor_registry_address": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def deployments(tmp_path: Path) -> Path:
    directory = tmp_path / "deployments"
    directory.mkdir()
    (directory / "anvil.json").write_text(json.dumps({"chainId": 31337, "registry": ANVIL_REG}))
    (directory / "base-sepolia.json").write_text(
        json.dumps({"chainId": 84532, "registry": PUBLIC_REG})
    )
    return directory


ANVIL_REG = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
PUBLIC_REG = "0x8B3Fd6B4b31a24a6922A14Beb892BD695054de68"


class _Calls:
    def __init__(self, db: Session | None = None) -> None:
        self.db = db
        self.copies: list[tuple[str, str, bool]] = []
        self.targets: list[Any] = []

    def copy(self, _source: Any, from_db: str, copy: str, *, fresh: bool) -> None:
        self.copies.append((from_db, copy, fresh))

    def session_for(self, target: Any) -> Session:
        self.targets.append(target)
        assert self.db is not None, "the demo opened a database it should have refused"
        return self.db


def _run_tamper(args: list[str], calls: _Calls, deployments: Path, monkeypatch, **settings) -> int:
    import app.core.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: _tamper_settings(**settings))
    return demo_tamper.main(
        args, session_for=calls.session_for, copy_database=calls.copy, deployments=deployments
    )


def test_the_tamper_demo_refuses_without_the_flag_and_in_production(
    anchored, deployments: Path, monkeypatch
) -> None:
    db, _chain, transaction, _buyer, _seller = anchored
    args = ["change-status", "--reference", transaction.reference, "--from-db", "vekro_demo"]
    calls = _Calls()
    assert _run_tamper(args, calls, deployments, monkeypatch) == 2
    assert (
        _run_tamper(
            [*args, demo_tamper.CONFIRM_FLAG],
            calls,
            deployments,
            monkeypatch,
            environment="production",
        )
        == 2
    )
    assert calls.copies == [] and calls.targets == []
    db.expire_all()
    status = db.execute(
        select(Transaction.status).where(Transaction.id == transaction.id)
    ).scalar_one()
    assert status == transaction.status


@pytest.mark.parametrize("registry", [PUBLIC_REG, PUBLIC_REG.lower(), PUBLIC_REG.upper()])
def test_the_tamper_demo_refuses_while_settings_name_a_public_registry(
    registry: str, deployments: Path, monkeypatch, capsys
) -> None:
    # #279: the demo was once pointed at the database that owns the Base Sepolia registry.
    calls = _Calls()
    args = ["change-status", "--reference", "VK-X", "--from-db", "vekro_base_sepolia"]
    code = _run_tamper(
        [*args, demo_tamper.CONFIRM_FLAG],
        calls,
        deployments,
        monkeypatch,
        anchor_registry_address=registry,
    )
    assert code == 2
    assert calls.copies == [] and calls.targets == []
    err = capsys.readouterr().err
    assert "public registry in contracts/deployments/base-sepolia.json" in err
    assert "pw" not in err  # the database line never shows credentials
    assert "database vekro_base_sepolia (from DATABASE_URL" in err


def test_the_tamper_demo_fails_closed_without_the_deployment_list(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls = _Calls()
    args = ["edit-ledger", "--reference", "VK-X", "--from-db", "vekro", demo_tamper.CONFIRM_FLAG]
    assert _run_tamper(args, calls, tmp_path / "missing", monkeypatch) == 2
    assert "no public registry list" in capsys.readouterr().err
    assert calls.copies == [] and calls.targets == []


def test_the_tamper_demo_refuses_sqlite_and_odd_names(deployments: Path, monkeypatch) -> None:
    calls = _Calls()
    base = ["edit-ledger", "--reference", "VK-X", demo_tamper.CONFIRM_FLAG, "--from-db"]
    assert _run_tamper([*base, "x"], calls, deployments, monkeypatch, database_url="sqlite://") == 2
    assert _run_tamper([*base, 'vekro"; DROP'], calls, deployments, monkeypatch) == 2
    assert calls.copies == [] and calls.targets == []


def test_the_tamper_demo_copies_first_and_edits_only_the_copy(
    anchored, deployments: Path, monkeypatch, capsys
) -> None:
    db, _chain, transaction, _buyer, _seller = anchored
    calls = _Calls(db)
    args = ["change-status", "--reference", transaction.reference, demo_tamper.CONFIRM_FLAG]
    # The normal settings may load ALEMBIC_DATABASE_URL, which wins over DATABASE_URL.
    code = _run_tamper(
        [*args, "--from-db", "vekro_base_sepolia"],
        calls,
        deployments,
        monkeypatch,
        alembic_database_url="postgresql://vekro:pw@127.0.0.1:5432/vekro_base_sepolia",
        anchor_registry_address=ANVIL_REG,
    )
    assert code == 0
    assert calls.copies == [("vekro_base_sepolia", "vekro_base_sepolia_tamper_copy", False)]
    (target,) = calls.targets
    assert target.name == "vekro_base_sepolia_tamper_copy"
    assert target.url.endswith("/vekro_base_sepolia_tamper_copy")
    err = capsys.readouterr().err
    assert "database vekro_base_sepolia (from ALEMBIC_DATABASE_URL" in err
    assert "Tampering database vekro_base_sepolia_tamper_copy only." in err

    # An existing copy is edited again without another copy.
    again = _Calls(db)
    code = _run_tamper(
        [*args, "--from-db", "vekro_base_sepolia_tamper_copy"], again, deployments, monkeypatch
    )
    assert code == 0
    assert again.copies == []
    assert [t.name for t in again.targets] == ["vekro_base_sepolia_tamper_copy"]


def test_the_refusal_rules() -> None:
    deployments = Path(__file__).resolve().parents[1] / "contracts" / "deployments"
    ok = {"environment": "development", "registry_address": "", "dialect": "postgresql"}
    assert demo_tamper.refusal(target="vekro_tamper_copy", deployments=deployments, **ok) is None
    assert "not a tamper copy" in str(
        demo_tamper.refusal(target="vekro_base_sepolia", deployments=deployments, **ok)
    )
    public = demo_tamper.public_registries(deployments)
    assert PUBLIC_REG.lower() in public  # the retired Base Sepolia pilot stays listed
    assert ANVIL_REG.lower() not in public


def test_the_database_target_names_the_setting_and_hides_credentials() -> None:
    from app.db.session import database_target

    plain = database_target(_tamper_settings())
    assert (plain.setting, plain.name) == ("DATABASE_URL", "vekro_base_sepolia")
    preferred = database_target(
        _tamper_settings(alembic_database_url="postgresql://a:secretpw@h:5432/vekro")
    )
    assert preferred.name == "vekro" and "used before DATABASE_URL" in preferred.describe()
    other = preferred.with_name("vekro_x_tamper_copy")
    assert other.url == "postgresql+psycopg://a:secretpw@h:5432/vekro_x_tamper_copy"
    assert "secretpw" not in other.describe() and "vekro_x_tamper_copy" in other.describe()


def test_a_resolved_case_audits_clean_until_its_decision_is_edited(monkeypatch) -> None:
    from decimal import Decimal

    from app.models.dispute import AdminDecision, Dispute
    from app.services.dispute import force_resolve_dispute_case
    from sqlalchemy import update

    from tests.test_anchor_events import _admin, _funded_dispute_case

    db = _db()
    chain = FakeRegistry()
    admin = _admin(db)
    _buyer, _seller, transaction = _funded_dispute_case(db, monkeypatch, "93")
    dispute = db.execute(select(Dispute)).scalar_one()
    force_resolve_dispute_case(
        db,
        dispute.id,
        decision=AdminDecision.SPLIT,
        reason="Both are partly right.",
        actor_id=admin.id,
        seller_share=Decimal("0.6"),
    )
    _anchor_all(db, chain)
    assert run_full_audit(db, chain.root_of).verdict == "CLEAN"
    assert verify_bundle(_bundle(db, transaction), chain.root_of).verdict == "VERIFIED"

    # The dispute row is mutable working state: an edit is caught against the anchored decision.
    db.execute(update(Dispute).where(Dispute.id == dispute.id).values(split_ratio=Decimal("0.9")))
    db.commit()
    audit = run_full_audit(db, chain.root_of)
    assert [finding.code for finding in audit.findings] == ["admin_decision_changed"]


@pytest.mark.parametrize(
    "script", ["verify_transaction.py", "audit_anchoring.py", "demo_tamper.py"]
)
def test_the_scripts_run_from_any_directory_without_pythonpath(script: str, tmp_path: Path) -> None:
    # docs/anchoring.md and docs/hosting.md run them as `python scripts/<name>.py` (#279).
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
