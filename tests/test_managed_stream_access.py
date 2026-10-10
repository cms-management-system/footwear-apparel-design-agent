"""Synthetic transaction/SSE access checks; no service or provider is started."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi import Request
from sqlalchemy import select

from app import config, models
from app.agent import access_context, events, routes
from app.agent.access_context import bind_request, reset_request, suspend_request
from app.agent.managed_store import ManagedBase, ManagedReceipt, sha256
from app.agent.store import AgentBase, AgentError, Record, create, head, transaction
from app.design_auth import session_context, token_hash


@pytest.fixture
def managed_access(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    for key, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'synthetic-stream.sqlite3'}",
        "ASSETS_DIR": str(tmp_path / "synthetic-assets"),
        "DESIGN_INTEGRATION_MODE": "managed",
        "DESIGN_AUTH_REQUIRED": "true",
        "DESIGN_INSTANCE_ID": "synthetic-design-stream",
        "DESIGN_SCOPE_ID": "synthetic-stream-scope",
        "DESIGN_PAID_PROVIDERS_ENABLED": "false",
        "AGENT_WORKER_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    AgentBase.metadata.create_all(models.engine())
    ManagedBase.metadata.create_all(models.engine())
    projects = {}
    prompt = "完整合成设计提示词：" + "颜色与版型保持明确；" * 1200
    tokens = {name: f"synthetic-stream-cookie-{name}" for name in ("alice", "bob", "lead")}
    try:
        with models.session() as db, db.begin():
            for name, cookie in tokens.items():
                db.add(models.DesignerUser(
                    username=name, password_hash="synthetic-unused-password-hash", display_name=f"合成{name}",
                    role="manager" if name == "lead" else "designer", active=True,
                ))
                db.add(models.DesignerSession(
                    token_hash=token_hash(cookie), username=name, expires_at=int(time.time()) + 3600,
                ))
            for name in ("alice", "bob", "orphan", "foreign-scope", "foreign-target", "mismatch"):
                project = models.Project(name=f"合成项目-{name}")
                db.add(project)
                db.flush()
                head(db, project.id)
                asset = create(db, project.id, "asset", {"synthetic": True}, "ready")
                projects[name] = {"pid": project.id, "asset": asset.id}
                assignee = "bob" if name in {"bob", "mismatch"} else "alice"
                db.add(models.DesignProjectAccess(
                    project_id=project.id, username="alice" if name == "mismatch" else assignee,
                ))
                row = models.DesignHandoff(
                    id=f"synthetic-{name}", package_id=f"synthetic-package-{name}", version="v1",
                    snapshot={"synthetic": True}, status="assigned", assignee=assignee, project_id=project.id,
                )
                db.add(row)
                if name != "orphan":
                    raw = json.dumps({"synthetic": True, "package_id": row.package_id}).encode()
                    db.add(ManagedReceipt(
                        handoff_id=row.id, source_instance_id="synthetic-product",
                        target_instance_id="foreign" if name == "foreign-target" else "synthetic-design-stream",
                        scope_id="foreign" if name == "foreign-scope" else "synthetic-stream-scope",
                        request_id=f"request-{name}", package_id=row.package_id, package_version="v1",
                        envelope_bytes=raw, envelope_sha256=sha256(raw),
                        proof={
                            "package": {"id": row.package_id, "synthetic": True},
                            "prompt": {"text": prompt, "version": "v3"},
                            "requirement": {"text": "合成的完整产品需求", "version": "v2"},
                        },
                        receipt={"id": f"receipt-{name}", "synthetic": True, "content_digest": "a" * 64},
                    ))
        yield {"projects": projects, "tokens": tokens, "prompt": prompt}
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def request_for(fixture, username="alice", *, path=None, method="GET", expected_context=None):
    cookie = fixture["tokens"][username]
    headers = [(b"cookie", f"{config.get_config().design_session_cookie}={cookie}".encode())]
    if method not in {"GET", "HEAD"}:
        context = session_context(token_hash(cookie)) if expected_context is None else expected_context
        headers.append((b"x-design-context", context.encode()))

    async def connected():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({
        "type": "http", "method": method,
        "path": path or f"/api/project/{fixture['projects']['alice']['pid']}/design-workspace",
        "headers": headers, "scheme": "http", "server": ("testserver", 80), "query_string": b"",
    }, receive=connected)


@contextmanager
def bound(request):
    token = bind_request(request)
    try:
        yield
    finally:
        reset_request(token)


def error_code(error, code, status):
    assert error.value.code == code
    assert error.value.status == status


def test_context_contains_only_hash_and_fresh_transaction_uses_complete_source(managed_access):
    fixture = managed_access
    request = request_for(fixture)
    with bound(request):
        context = access_context._request_access.get()
        assert context.token_hash == token_hash(fixture["tokens"]["alice"])
        assert fixture["tokens"]["alice"] not in repr(context)
        snapshot = events.workspace_snapshot(fixture["projects"]["alice"]["pid"])
    assert access_context._request_access.get() is None
    source = snapshot["source_binding"]
    assert source["prompt"]["text"] == fixture["prompt"] and len(source["prompt"]["text"]) > 4000
    assert source["requirement"] == {"text": "合成的完整产品需求", "version": "v2"}
    assert source["receipt"] == {"id": "receipt-alice", "synthetic": True, "content_digest": "a" * 64}
    assert source["package"] == {"id": "synthetic-package-alice", "synthetic": True}
    assert source["content_digest"] == "a" * 64
    assert source["envelope_sha256"] != source["content_digest"]
    assert source["revision"] == snapshot["revision"] == 1
    assert source["status"] == "assigned"


def test_workspace_and_source_are_one_snapshot_during_concurrent_update(managed_access, monkeypatch):
    fixture = managed_access
    pid = fixture["projects"]["alice"]["pid"]
    original_workspace = events.workspace
    updating, committed = Event(), Event()

    def update_source_and_engine():
        with models.session() as db:
            updating.set()
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            db.get(ManagedReceipt, "synthetic-alice").revision = 2
            db.get(models.DesignHandoff, "synthetic-alice").status = "review"
            head(db, pid).payload = {"synthetic_revision": 2}
            db.commit()
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = []

        def workspace_during_update(project_id):
            if not pending:
                pending.append(pool.submit(update_source_and_engine))
                assert updating.wait(2)
                assert not committed.wait(0.05)
            return original_workspace(project_id)

        monkeypatch.setattr(events, "workspace", workspace_during_update)
        with bound(request_for(fixture)):
            first = events.workspace_snapshot(pid)
        pending[0].result(timeout=5)
    with bound(request_for(fixture)):
        second = events.workspace_snapshot(pid)
    assert first["revision"] == first["source_binding"]["revision"] == 1
    assert first["source_binding"]["status"] == "assigned" and first["head"] == {}
    assert second["revision"] == second["source_binding"]["revision"] == 2
    assert second["source_binding"]["status"] == "review"
    assert second["head"] == {"synthetic_revision": 2}


def test_managed_snapshot_without_request_context_rejects_before_reading_workspace(managed_access, monkeypatch):
    monkeypatch.setattr(
        events, "workspace", lambda _: pytest.fail("must not read a workspace without request identity"),
    )
    with pytest.raises(AgentError) as error:
        events.workspace_snapshot(managed_access["projects"]["alice"]["pid"])
    error_code(error, "LOGIN_REQUIRED", 401)


@pytest.mark.parametrize("name", ["bob", "orphan", "foreign-scope", "foreign-target", "mismatch"])
def test_project_and_asset_recheck_require_receipt_scope_target_and_both_ownerships(managed_access, name):
    fixture = managed_access
    obj = fixture["projects"][name]
    for path in (f"/api/project/{obj['pid']}/design-workspace", f"/api/design-assets/{obj['asset']}/image"):
        with bound(request_for(fixture, path=path)), pytest.raises(AgentError) as error:
            with transaction():
                pytest.fail("must reject before reading project data")
        error_code(error, "NOT_FOUND", 404)


def test_project_list_uses_authoritative_receipts_and_assignee_access(managed_access):
    fixture = managed_access
    request = request_for(fixture, path="/api/design-projects")
    with bound(request):
        result = routes.projects(request)
        assert [(item["project_id"], item["title"]) for item in result["items"]] == [
            (fixture["projects"]["alice"]["pid"], "合成项目-alice")
        ]
        assert result["total"] == 1 and result["items"][0]["source_mode"] == "upstream"
    manager = request_for(fixture, "lead", path="/api/design-projects")
    with bound(manager):
        result = routes.projects(manager)
        assert {item["project_id"] for item in result["items"]} == {
            fixture["projects"][name]["pid"] for name in ("alice", "bob", "mismatch")
        }


@pytest.mark.parametrize("state", ["review", "approved", "returned"])
def test_reviewed_or_nonworking_task_blocks_engine_writes_without_affecting_reads(managed_access, state):
    fixture = managed_access
    with models.session() as db, db.begin():
        db.get(models.DesignHandoff, "synthetic-alice").status = state
    pid = fixture["projects"]["alice"]["pid"]
    request = request_for(fixture, method="POST", path=f"/api/project/{pid}/design-specs")
    with bound(request), pytest.raises(AgentError) as error:
        with transaction() as db:
            create(db, pid, "spec", {"intent": "must never be saved"})
    error_code(error, "STATE_CONFLICT", 409)
    with bound(request_for(fixture)):
        assert events.workspace_snapshot(pid)["specs"] == []


def test_manager_can_read_but_cannot_write_and_context_must_match(managed_access):
    fixture = managed_access
    pid = fixture["projects"]["alice"]["pid"]
    with bound(request_for(fixture, "lead")):
        assert events.workspace_snapshot(pid)["project"]["id"] == pid
    with bound(request_for(fixture, "lead", method="POST")), pytest.raises(AgentError) as error:
        with transaction():
            pytest.fail("manager cannot mutate the design engine")
    error_code(error, "ROLE_FORBIDDEN", 403)
    with bound(request_for(fixture, method="POST", expected_context="old-session")), pytest.raises(AgentError) as error:
        with transaction():
            pytest.fail("old context cannot mutate")
    error_code(error, "AUTH_CONTEXT_CHANGED", 409)


@pytest.mark.parametrize("change", ["expired", "revoked", "inactive", "reassigned"])
def test_permission_changes_after_request_binding_are_rechecked_before_work(managed_access, change):
    fixture = managed_access
    with bound(request_for(fixture)):
        with models.session() as db, db.begin():
            record = db.get(models.DesignerSession, token_hash(fixture["tokens"]["alice"]))
            if change == "expired":
                record.expires_at = 0
            elif change == "revoked":
                db.delete(record)
            elif change == "inactive":
                db.get(models.DesignerUser, "alice").active = False
            else:
                db.get(models.DesignHandoff, "synthetic-alice").assignee = "bob"
                db.get(models.DesignProjectAccess, fixture["projects"]["alice"]["pid"]).username = "bob"
        with pytest.raises(AgentError) as error:
            with transaction():
                pytest.fail("revocation is authoritative at the start of the transaction")
    error_code(
        error, "NOT_FOUND" if change == "reassigned" else "LOGIN_REQUIRED", 404 if change == "reassigned" else 401,
    )


def test_session_expiring_during_transaction_rolls_back_pending_engine_write(managed_access, monkeypatch):
    fixture = managed_access
    pid = fixture["projects"]["alice"]["pid"]
    request = request_for(fixture, method="POST", path=f"/api/project/{pid}/design-specs")
    current_time = time.time()
    with bound(request), pytest.raises(AgentError) as error:
        with transaction() as db:
            create(db, pid, "spec", {"intent": "rollback after expiry"})
            monkeypatch.setattr(access_context.time, "time", lambda: current_time + 7200)
    error_code(error, "LOGIN_REQUIRED", 401)
    with models.session() as db:
        assert list(db.scalars(select(Record).where(Record.kind == "spec"))) == []


def test_system_recording_can_suspend_expired_request_then_restore_guard(managed_access):
    fixture = managed_access
    with models.session() as db, db.begin():
        db.get(models.DesignerSession, token_hash(fixture["tokens"]["alice"])).expires_at = 0
    with bound(request_for(fixture)):
        suspended = suspend_request()
        try:
            with transaction() as db:
                create(db, fixture["projects"]["alice"]["pid"], "message", {"text": "synthetic system receipt"})
        finally:
            reset_request(suspended)
        with pytest.raises(AgentError) as error:
            with transaction():
                pytest.fail("personnel context must be restored")
    error_code(error, "LOGIN_REQUIRED", 401)
    with transaction() as db:
        assert len(list(db.scalars(select(Record).where(Record.kind == "message")))) == 1


@pytest.mark.parametrize("change", ["revoked", "reassigned"])
def test_sse_stops_with_error_and_no_second_workspace_after_revocation(managed_access, change):
    fixture = managed_access
    pid = fixture["projects"]["alice"]["pid"]
    request = request_for(fixture, path=f"/api/project/{pid}/design-events")

    async def collect():
        stream = events.stream(pid, request, duration=5)
        first = await anext(stream)
        assert first.startswith("event: workspace\n")
        with models.session() as db, db.begin():
            if change == "revoked":
                db.delete(db.get(models.DesignerSession, token_hash(fixture["tokens"]["alice"])))
            else:
                db.get(models.DesignHandoff, "synthetic-alice").assignee = "bob"
                db.get(models.DesignProjectAccess, pid).username = "bob"
        remaining = [frame async for frame in stream]
        assert access_context._request_access.get() is None
        return remaining

    remaining = asyncio.run(collect())
    assert len(remaining) == 1 and remaining[0].startswith("event: error\n")
    data = json.loads(remaining[0].split("data: ", 1)[1])
    assert data["error"]["code"] == ("LOGIN_REQUIRED" if change == "revoked" else "NOT_FOUND")
    assert data["error"]["retryable"] is False and len(data["request_id"]) == 32
    assert "request_id" not in data["error"]
    assert "source_binding" not in remaining[0] and fixture["prompt"] not in remaining[0]
