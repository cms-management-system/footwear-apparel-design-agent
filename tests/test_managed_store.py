"""Persistence checks only: synthetic data, fresh SQLite files, no HTTP/providers."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.managed_store import (
    ManagedAction,
    ManagedAttempt,
    ManagedAudit,
    ManagedBase,
    ManagedOutbox,
    ManagedReceipt,
    action_key,
    canonical_bytes,
    sha256,
)

RAW_ENVELOPE = '{ "模拟": true, "version": "v1", "prompt": "合成设计要求" }\n'.encode()
RAW_ACTION = b'{ "action": "approve", "expected_revision": 4 }\n'
RAW_EVENT = '{"event_id":"synthetic-event-1","模拟":true}\n'.encode()


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'managed-synthetic.sqlite3'}"
    engine = create_engine(url, connect_args={"timeout": 10})
    ManagedBase.metadata.create_all(engine)
    yield engine, url
    engine.dispose()


def receipt(**overrides):
    values = {
        "handoff_id": "synthetic-handoff-1",
        "source_instance_id": "synthetic-product",
        "target_instance_id": "synthetic-design",
        "scope_id": "synthetic-scope",
        "request_id": "synthetic-request-1",
        "package_id": "synthetic-package-1",
        "package_version": "v1",
        "envelope_bytes": RAW_ENVELOPE,
        "envelope_sha256": sha256(RAW_ENVELOPE),
        "proof": {"synthetic": True, "version": "v1", "approver": "synthetic-manager"},
        "receipt": {"id": "synthetic-receipt-1", "received": True, "unknown": None},
    }
    return ManagedReceipt(**(values | overrides))


def action(**overrides):
    values = {
        "action_key": action_key("synthetic-scope", "synthetic-manager", "synthetic-action-1"),
        "action_id": "synthetic-action-1",
        "username": "synthetic-manager",
        "auth_context_id": "synthetic-context-1",
        "method": "POST",
        "path": "/api/design-handoffs/synthetic-handoff-1/review",
        "raw_body": RAW_ACTION,
        "status_code": 200,
        "response": {"status": "approved", "revision": 5, "synthetic": True},
        "handoff_id": "synthetic-handoff-1",
    }
    return ManagedAction(**(values | overrides))


def outbox(event_id="synthetic-event-1"):
    return ManagedOutbox(
        event_id=event_id,
        handoff_id="synthetic-handoff-1",
        source_instance_id="synthetic-design",
        target_instance_id="synthetic-product",
        scope_id="synthetic-scope",
        raw_body=RAW_EVENT,
        body_sha256=sha256(RAW_EVENT),
        status="unconfirmed",
    )


def test_exact_bytes_history_and_pending_delivery_survive_new_connection(store):
    engine, url = store
    with Session(engine) as db, db.begin():
        db.add_all([
            receipt(),
            action(),
            ManagedAudit(
                handoff_id="synthetic-handoff-1", revision=4, kind="submitted", actor="synthetic-designer",
                payload={"version_id": "synthetic-version-1", "image_sha256": "a" * 64},
            ),
            ManagedAudit(
                handoff_id="synthetic-handoff-1", revision=5, kind="approved", actor="synthetic-manager",
                payload={"version_id": "synthetic-version-1", "note": "仅合成技术夹具审批"},
            ),
            outbox(),
            outbox("synthetic-historical-event"),
            ManagedAttempt(
                event_id="synthetic-event-1", action_id="synthetic-send-1", actor="synthetic-manager",
                status="unconfirmed", error_code="RECEIPT_NOT_OBSERVED",
            ),
        ])
    engine.dispose()
    reopened = create_engine(url)
    try:
        with Session(reopened) as db:
            saved = db.get(ManagedReceipt, "synthetic-handoff-1")
            assert saved.envelope_bytes == RAW_ENVELOPE
            assert saved.envelope_sha256 == sha256(RAW_ENVELOPE)
            assert saved.proof == {"synthetic": True, "version": "v1", "approver": "synthetic-manager"}
            assert saved.receipt == {"id": "synthetic-receipt-1", "received": True, "unknown": None}
            assert (saved.revision, saved.priority, saved.due_date) == (1, "P2", None)
            assert saved.created_at.endswith("+00:00")
            saved_action = db.get(
                ManagedAction, action_key("synthetic-scope", "synthetic-manager", "synthetic-action-1")
            )
            assert saved_action.raw_body == RAW_ACTION
            assert saved_action.auth_context_id == "synthetic-context-1"
            assert saved_action.response == {"status": "approved", "revision": 5, "synthetic": True}
            assert saved_action.status_code == 200
            history = list(db.scalars(select(ManagedAudit).order_by(ManagedAudit.revision)))
            assert [(row.kind, row.revision, row.actor) for row in history] == [
                ("submitted", 4, "synthetic-designer"), ("approved", 5, "synthetic-manager"),
            ]
            assert len({row.id for row in history}) == 2
            assert history[0].payload["image_sha256"] == "a" * 64
            assert db.scalar(select(func.count()).select_from(ManagedOutbox)) == 2
            pending = db.get(ManagedOutbox, "synthetic-event-1")
            assert pending.raw_body == RAW_EVENT and pending.body_sha256 == sha256(RAW_EVENT)
            assert pending.status == "unconfirmed" and pending.receipt is None
            attempt = db.scalar(select(ManagedAttempt))
            assert attempt.status == "unconfirmed" and attempt.error_code == "RECEIPT_NOT_OBSERVED"
            assert attempt.finished_at is None and attempt.started_at.endswith("+00:00")
    finally:
        reopened.dispose()


@pytest.mark.parametrize("changed", [
    {"request_id": "different-request"},
    {"package_id": "different-package", "package_version": "v2"},
])
def test_receipt_conflicts_reject_reused_package_version_or_request_without_overwrite(store, changed):
    engine, _ = store
    with Session(engine) as db, db.begin():
        db.add(receipt())
    with Session(engine) as db, pytest.raises(IntegrityError), db.begin():
        db.add(receipt(handoff_id="different-handoff", envelope_bytes=b"different-body", **changed))
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(ManagedReceipt)) == 1
        assert db.get(ManagedReceipt, "synthetic-handoff-1").envelope_bytes == RAW_ENVELOPE


@pytest.mark.parametrize("field", ["source_instance_id", "target_instance_id", "scope_id"])
def test_receipt_identity_is_isolated_by_source_target_and_scope(store, field):
    engine, _ = store
    with Session(engine) as db, db.begin():
        db.add(receipt())
        db.add(receipt(handoff_id="different-handoff", **{field: "different-identity"}))
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(ManagedReceipt)) == 2


def test_concurrent_receipt_inserts_keep_only_one_original(store):
    engine, _ = store
    barrier = Barrier(2)

    def insert(index):
        barrier.wait(timeout=10)
        try:
            with Session(engine) as db, db.begin():
                db.add(receipt(handoff_id=f"synthetic-race-{index}"))
            return "created"
        except IntegrityError:
            return "identity_conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert, (1, 2)))
    assert sorted(outcomes) == ["created", "identity_conflict"]
    with Session(engine) as db:
        rows = list(db.scalars(select(ManagedReceipt)))
        assert len(rows) == 1 and rows[0].envelope_bytes == RAW_ENVELOPE


def test_same_action_identity_cannot_overwrite_original_with_byte_different_request(store):
    engine, _ = store
    altered = RAW_ACTION + b" "
    assert json.loads(altered) == json.loads(RAW_ACTION)
    assert sha256(altered) != sha256(RAW_ACTION)
    with Session(engine) as db, db.begin():
        db.add(action())
    with Session(engine) as db, pytest.raises(IntegrityError), db.begin():
        db.add(action(raw_body=altered, response={"status": "changed"}))
    with Session(engine) as db:
        original = db.get(ManagedAction, action_key("synthetic-scope", "synthetic-manager", "synthetic-action-1"))
        assert original.raw_body == RAW_ACTION and original.response["status"] == "approved"
    # Authorization and a 409 response remain route responsibilities.
    assert action_key("a:b", "c", "d") != action_key("a", "b:c", "d")
    assert action_key("scope", "user-a", "action-1") != action_key("scope", "user-b", "action-1")
    assert action_key("scope-a", "user", "action-1") != action_key("scope-b", "user", "action-1")


def test_local_canonical_encoding_does_not_replace_received_wire_bytes():
    assert canonical_bytes({"b": 2, "a": "合成"}) == canonical_bytes({"a": "合成", "b": 2})
    assert canonical_bytes(json.loads(RAW_ENVELOPE)) != RAW_ENVELOPE
    with pytest.raises(ValueError):
        canonical_bytes({"invalid_json_number": float("nan")})


def test_migration_is_additive_and_idempotent_in_synthetic_database(tmp_path):
    path = Path(__file__).resolve().parents[1] / "migrations/versions/003_managed_handoff.py"
    spec = importlib.util.spec_from_file_location("managed_handoff_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "003_managed_handoff" and module.down_revision == "002_cms_link"
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic-migration.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE existing_synthetic_history (id INTEGER PRIMARY KEY, raw_body BLOB)"))
            connection.execute(
                text("INSERT INTO existing_synthetic_history (id, raw_body) VALUES (1, :body)"),
                {"body": RAW_ENVELOPE},
            )
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                module.upgrade()
            assert set(ManagedBase.metadata.tables).issubset(inspect(connection).get_table_names())
            original = connection.execute(text("SELECT raw_body FROM existing_synthetic_history")).scalar_one()
            assert original == RAW_ENVELOPE
        with pytest.raises(RuntimeError, match="禁止自动删除"):
            module.downgrade()
    finally:
        engine.dispose()
