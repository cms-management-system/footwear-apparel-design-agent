"""Internal workflow tests with prevalidated *synthetic* source fixtures.

No source approval verification, real product bridge, model generation, or business
human review is claimed. Local outbox freeze doubles isolate transaction behavior.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import time
from contextlib import ExitStack
from importlib import import_module

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from app import config, models
from app.agent import assets
from app.agent.managed_store import ManagedAction, ManagedAudit, ManagedOutbox, ManagedReceipt
from app.agent.store import Record, create, head, records, transaction
from app.design_auth import cookie_name, password_hash, token_hash
from app.main import app
from tests.managed_fixtures import make_delivery


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture()
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    password = "synthetic-workflow-password-123"
    password_digest = password_hash(password)
    settings = {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'workflow-synthetic.sqlite3'}",
        "ASSETS_DIR": str(tmp_path / "synthetic-assets"),
        "AGENT_WORKER_ENABLED": "false",
        "DESIGN_INTEGRATION_MODE": "managed",
        "DESIGN_AUTH_REQUIRED": "true",
        "DESIGN_INSTANCE_ID": "synthetic-design-workflow",
        "DESIGN_SCOPE_ID": "synthetic-workflow-scope",
        "DESIGN_ALLOWED_ORIGINS": "http://127.0.0.1:3092",
        "DESIGN_PAID_PROVIDERS_ENABLED": "false",
        "DESIGN_MANAGER_USERNAME": "lead",
        "DESIGN_MANAGER_PASSWORD_HASH": password_digest,
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DESIGN_SESSION_COOKIE_NAME", raising=False)
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    envelope = (
        b" \n"
        + make_delivery(
            source_instance_id="synthetic-product-workflow",
            target_instance_id="synthetic-design-workflow",
            scope_id="synthetic-workflow-scope",
            prompt_overrides={"positive_prompt": "完整合成提示词；" * 800},
            content_suffix=b"\n ",
        )
        + b"\n "
    )
    source_delivery = json.loads(envelope)
    source = json.loads(base64.b64decode(source_delivery["content_base64"]))
    source_requirement = json.loads(base64.b64decode(source["requirement_base64"]))
    source_prompt = json.loads(base64.b64decode(source["prompt_base64"]))
    frozen = []
    network = []

    def reject_network(*args, **kwargs):
        network.append("unexpected-network-client")
        raise AssertionError("Internal workflow must not construct a network client")

    def freeze_local(db, row, meta, audit, submission=None):
        kind = "design_approved" if submission else "clarification"
        event_id = f"synthetic-{kind}-{audit.id}"
        event_body = encoded(
            {
                "synthetic": True,
                "fixture_layer": "local-freeze-double",
                "event_id": event_id,
                "kind": kind,
                "handoff_id": row.id,
                "review_revision": audit.revision,
                "submission_id": submission.id if submission else None,
                "payload": copy.deepcopy(audit.payload),
            }
        )
        body = encoded(
            {
                "synthetic": True,
                "fixture_layer": "local-freeze-double",
                "content_base64": base64.b64encode(event_body).decode("ascii"),
                "content_digest": digest(event_body),
            }
        )
        db.add(
            ManagedOutbox(
                event_id=event_id,
                handoff_id=row.id,
                source_instance_id=meta.target_instance_id,
                target_instance_id=meta.source_instance_id,
                scope_id=meta.scope_id,
                raw_body=body,
                body_sha256=digest(body),
                status="prepared",
            )
        )
        db.flush()
        frozen.append((kind, event_id))

    # Only freezing is replaced; startup still exercises the real recovery hook.
    bridge = import_module("app.agent.managed_bridge")
    monkeypatch.setattr(bridge, "freeze_result", freeze_local)
    monkeypatch.setattr(bridge, "freeze_clarification", freeze_local)
    try:
        with ExitStack() as stack:
            manager = stack.enter_context(TestClient(app))
            clients = {"lead": manager, "alice": TestClient(app), "bob": TestClient(app)}
            for client in (clients["alice"], clients["bob"]):
                stack.callback(client.close)
            with models.session() as db:
                for name in ("alice", "bob"):
                    db.add(
                        models.DesignerUser(
                            username=name,
                            display_name=f"合成{name}",
                            password_hash=password_digest,
                            role="designer",
                            active=True,
                        )
                    )
                for name in clients:
                    token = f"synthetic-workflow-session-{name}"
                    db.add(
                        models.DesignerSession(
                            token_hash=token_hash(token), username=name, expires_at=int(time.time()) + 3600
                        )
                    )
                    clients[name].cookies.set(cookie_name(), token)
                handoff_id = source["package_id"] + "@v1"
                db.add(
                    models.DesignHandoff(
                        id=handoff_id, package_id=source["package_id"], version="v1", snapshot=source, status="new"
                    )
                )
                db.add(
                    ManagedReceipt(
                        handoff_id=handoff_id,
                        source_instance_id="synthetic-product-workflow",
                        target_instance_id="synthetic-design-workflow",
                        scope_id="synthetic-workflow-scope",
                        request_id="synthetic-source-request-1",
                        package_id=source["package_id"],
                        package_version="v1",
                        envelope_bytes=envelope,
                        envelope_sha256=digest(envelope),
                        proof={
                            "synthetic": True,
                            "verification_method": "prevalidated_internal_only_not_network",
                            "package": source,
                            "requirement": source_requirement,
                            "prompt": source_prompt,
                            "content_base64": source_delivery["content_base64"],
                            "approval": source["approval"],
                        },
                        receipt={
                            "synthetic": True,
                            "design_receive_id": "synthetic-receive-1",
                            "content_digest": source_delivery["content_digest"],
                        },
                    )
                )
                db.commit()
            for client in clients.values():
                identity = client.get("/api/design-auth/me")
                assert identity.status_code == 200, identity.text
                client.headers["X-Design-Context"] = identity.json()["auth_context_id"]
            monkeypatch.setattr("httpx.Client", reject_network)
            monkeypatch.setattr("httpx.AsyncClient", reject_network)
            yield {
                "clients": clients,
                "id": handoff_id,
                "source": source,
                "envelope": envelope,
                "frozen": frozen,
                "network": network,
                "password": password,
            }
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def act(w, actor, endpoint, data, action_id, *, raw=None, handoff_id=None):
    return w["clients"][actor].post(
        f"/api/design-handoffs/{handoff_id or w['id']}/{endpoint}",
        content=encoded(data) if raw is None else raw,
        headers={"Content-Type": "application/json", "X-Design-Action-Id": action_id},
    )


def success(response):
    assert response.status_code == 200, response.text
    return response.json()


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code, response.text


def accepted(w):
    return success(act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "accept-first-001"))


def assigned(w):
    accepted(w)
    return success(
        act(
            w,
            "lead",
            "decision",
            {"action": "assign", "expected_revision": 2, "assignee": "alice", "priority": "P1"},
            "assign-first-001",
        )
    )


def version_fixture(w, *, current=True, confirmed=True, pid=None):
    if pid is None:
        with models.session() as db:
            pid = db.get(models.DesignHandoff, w["id"]).project_id
    image = Image.new("RGB", (256, 128), "blue")
    ImageDraw.Draw(image).text((12, 45), "SYNTHETIC TEST ONLY", fill="white")
    raw = io.BytesIO()
    image.save(raw, "PNG")
    saved = assets.save_image(raw.getvalue())
    with transaction() as db:
        spec = records(db, pid, "spec")[-1]
        row = create(
            db,
            pid,
            "version",
            {
                "spec_id": spec.id,
                "image": saved,
                "generation_mode": "synthetic_fixture",
                "review": {"synthetic": True, "checks": []},
            },
            "confirmed" if confirmed else "ready_for_review",
        )
        if current:
            h = head(db, pid)
            h.payload = {**h.payload, "confirmed_version_id": row.id}
        return row.id, copy.deepcopy(row.payload)


def submitted(w):
    assigned(w)
    version_id, payload = version_fixture(w)
    result = success(act(w, "alice", "submit", {"expected_revision": 3, "version_id": version_id}, "submit-first-001"))
    return result, version_id, payload


def counts():
    with models.session() as db:
        return {
            "actions": db.query(ManagedAction).count(),
            "audits": db.query(ManagedAudit).count(),
            "outbox": db.query(ManagedOutbox).count(),
            "projects": db.query(models.Project).count(),
        }


def test_original_action_replay_returns_original_snapshot_after_later_state(workflow):
    w = workflow
    raw = b'{ "action":"accept", "expected_revision":1 }\n'
    first = success(act(w, "lead", "decision", {}, "exact-accept-001", raw=raw))
    success(
        act(
            w, "lead", "decision", {"action": "assign", "expected_revision": 2, "assignee": "alice"}, "exact-assign-001"
        )
    )
    before = counts()
    repeated = success(act(w, "lead", "decision", {}, "exact-accept-001", raw=raw))
    assert repeated == first and repeated["status"] == "accepted" and repeated["revision"] == 2
    assert counts() == before
    with models.session() as db:
        action = db.scalar(select(ManagedAction).where(ManagedAction.action_id == "exact-accept-001"))
        assert action.raw_body == raw
        assert db.get(models.DesignHandoff, w["id"]).status == "assigned"
    assert w["network"] == []


def test_semantically_identical_trailing_space_is_action_conflict(workflow):
    raw = encoded({"action": "accept", "expected_revision": 1})
    success(act(workflow, "lead", "decision", {}, "raw-body-conflict", raw=raw))
    before = counts()
    error(act(workflow, "lead", "decision", {}, "raw-body-conflict", raw=raw + b" "), 409, "IDEMPOTENCY_CONFLICT")
    assert counts() == before


def test_stale_revision_never_writes_a_new_action_or_assignment(workflow):
    accepted(workflow)
    before = counts()
    error(
        act(
            workflow,
            "lead",
            "decision",
            {"action": "assign", "expected_revision": 1, "assignee": "alice"},
            "stale-revision-001",
        ),
        409,
        "STATE_CONFLICT",
    )
    assert counts() == before


def test_new_session_cannot_replay_old_action_but_owner_can_read_it(workflow):
    w = workflow
    old = accepted(w)
    client = w["clients"]["lead"]
    token = "synthetic-replacement-manager-session"
    with models.session() as db:
        db.add(
            models.DesignerSession(token_hash=token_hash(token), username="lead", expires_at=int(time.time()) + 3600)
        )
        db.commit()
    client.cookies.set(cookie_name(), token)
    raw = {"action": "accept", "expected_revision": 1}
    error(act(w, "lead", "decision", raw, "accept-first-001"), 409, "AUTH_CONTEXT_CHANGED")
    client.headers["X-Design-Context"] = success(client.get("/api/design-auth/me"))["auth_context_id"]
    error(act(w, "lead", "decision", raw, "accept-first-001"), 409, "AUTH_CONTEXT_CHANGED")
    restored = success(client.get("/api/design-operations/accept-first-001"))
    assert restored["result"] == old
    error(w["clients"]["bob"].get("/api/design-operations/accept-first-001"), 404, "NOT_FOUND")
    assert counts()["actions"] == 1


def test_accept_first_assign_binds_only_current_assignee_and_preserves_full_source(workflow):
    w = workflow
    result = assigned(w)
    assert result["status"] == "assigned" and result["revision"] == 3 and result["assignee"] == "alice"
    assert result["package"] == w["source"]
    assert 4000 < len(json.loads(base64.b64decode(result["package"]["prompt_base64"]))["positive_prompt"]) <= 10000
    alice = success(w["clients"]["alice"].get("/api/design-handoffs"))
    assert alice["total"] == 1 and alice["items"][0]["id"] == w["id"]
    assert success(w["clients"]["bob"].get("/api/design-handoffs"))["total"] == 0
    error(w["clients"]["bob"].get(f"/api/design-handoffs/{w['id']}"), 404, "NOT_FOUND")
    error(w["clients"]["bob"].get(f"/api/project/{result['project_id']}/design-workspace"), 404, "NOT_FOUND")
    with models.session() as db:
        assert db.get(models.DesignProjectAccess, result["project_id"]).username == "alice"
        assert db.get(ManagedReceipt, w["id"]).envelope_bytes == w["envelope"]
        source = db.scalar(select(Record).where(Record.project_id == result["project_id"], Record.kind == "source"))
        assert source.payload["package"] == w["source"]


@pytest.mark.parametrize("assignee", ["alice", "bob"])
def test_assigned_task_cannot_be_assigned_again_even_with_reason(workflow, assignee):
    w = workflow
    assigned(w)
    before = counts()
    error(
        act(
            w,
            "lead",
            "decision",
            {"action": "assign", "expected_revision": 3, "assignee": assignee, "note": "合成改派请求"},
            "no-reassignment-001",
        ),
        409,
        "STATE_CONFLICT",
    )
    assert counts() == before
    assert success(w["clients"]["lead"].get(f"/api/design-handoffs/{w['id']}"))["assignee"] == "alice"


@pytest.mark.parametrize(
    "endpoint,data",
    [
        ("decision", {"action": "accept", "expected_revision": 3}),
        ("review", {"action": "approve", "expected_revision": 3, "submitted_version_id": "synthetic", "note": "合成"}),
    ],
)
def test_designer_cannot_perform_manager_actions(workflow, endpoint, data):
    assigned(workflow)
    error(act(workflow, "alice", endpoint, data, "designer-management-001"), 403, "ROLE_FORBIDDEN")


def test_wrong_assignee_and_manager_cannot_submit(workflow):
    assigned(workflow)
    version, _ = version_fixture(workflow)
    body = {"expected_revision": 3, "version_id": version}
    error(act(workflow, "bob", "submit", body, "wrong-designer-submit"), 404, "NOT_FOUND")
    error(act(workflow, "lead", "submit", body, "manager-cannot-submit"), 403, "ROLE_FORBIDDEN")


def test_submission_requires_explicit_current_confirmed_version(workflow):
    w = workflow
    assigned(w)
    previous, _ = version_fixture(w)
    current, payload = version_fixture(w)
    error(
        act(w, "alice", "submit", {"expected_revision": 3, "version_id": previous}, "stale-version-submit"),
        409,
        "STATE_CONFLICT",
    )
    result = success(
        act(w, "alice", "submit", {"expected_revision": 3, "version_id": current}, "current-version-submit")
    )
    assert result["status"] == "review" and result["submitted_version_id"] == current
    snap = result["submission"]
    assert snap["version_payload"] == payload and snap["image"]["sha256"] == payload["image"]["sha256"]
    assert snap["image"]["generation_mode"] == "synthetic_fixture"
    assert snap["source_envelope_sha256"] == digest(w["envelope"])


@pytest.mark.parametrize("failure", ["unconfirmed", "missing", "busy", "digest"])
def test_invalid_submission_preserves_assigned_state_and_history(workflow, failure):
    w = workflow
    task = assigned(w)
    version, payload = version_fixture(w, confirmed=failure != "unconfirmed")
    expected = "STATE_CONFLICT"
    status = 409
    if failure == "missing":
        version, expected, status = "nonexistent-version", "NOT_FOUND", 404
    elif failure == "busy":
        with transaction() as db:
            create(db, task["project_id"], "task", {"synthetic": True}, "queued")
        expected = "TASK_ACTIVE"
    elif failure == "digest":
        path = assets.file_path(payload["image"])
        path.write_bytes(path.read_bytes() + b"synthetic-tamper")
        expected = "ASSET_DIGEST_MISMATCH"
    before = counts()
    error(
        act(w, "alice", "submit", {"expected_revision": 3, "version_id": version}, "invalid-submission-001"),
        status,
        expected,
    )
    assert counts() == before
    state = success(w["clients"]["lead"].get(f"/api/design-handoffs/{w['id']}"))
    assert state["status"] == "assigned" and state["revision"] == 3 and state["submission"] is None


def test_review_freezes_locally_without_network_and_survives_database_reopen(workflow):
    w = workflow
    submitted_result, version, _ = submitted(w)
    raw = encoded(
        {
            "action": "approve",
            "expected_revision": 4,
            "submitted_version_id": version,
            "note": "合成技术审查，不是真实业务人审",
        }
    )
    approved = success(act(w, "lead", "review", {}, "approve-local-only-001", raw=raw))
    assert approved["status"] == "approved" and approved["revision"] == 5
    assert approved["review"]["reviewer"] == "lead"
    assert approved["submission"] == submitted_result["submission"]
    assert approved["delivery"]["status"] == "prepared" and approved["delivery"]["receipt"] is None
    assert w["frozen"] == [("design_approved", approved["delivery"]["event_id"])]
    assert w["network"] == []
    before = counts()
    repeated = success(act(w, "lead", "review", {}, "approve-local-only-001", raw=raw))
    assert repeated == approved and counts() == before and len(w["frozen"]) == 1
    models.reset_engine_for_tests()  # Actual disk reopen; not a process-restart claim.
    restored = success(w["clients"]["lead"].get(f"/api/design-handoffs/{w['id']}"))
    assert restored["status"] == "approved" and restored["submission"] == approved["submission"]
    assert restored["review"] == approved["review"] and restored["delivery"] == approved["delivery"]
    with models.session() as db:
        outbox = db.get(ManagedOutbox, approved["delivery"]["event_id"])
        assert digest(outbox.raw_body) == outbox.body_sha256 and outbox.status == "prepared"
        assert db.get(ManagedReceipt, w["id"]).envelope_bytes == w["envelope"]


@pytest.mark.parametrize("change", ["submitted_id", "version_payload", "image_bytes"])
def test_review_rejects_changed_submission_basis_without_audit_or_outbox(workflow, change):
    w = workflow
    _, version, payload = submitted(w)
    expected = "STATE_CONFLICT"
    submitted_id = version
    if change == "submitted_id":
        submitted_id = "wrong-submitted-version"
    elif change == "version_payload":
        with transaction() as db:
            row = db.get(Record, version)
            row.payload = {**row.payload, "synthetic_tamper": True}
    else:
        path = assets.file_path(payload["image"])
        path.write_bytes(path.read_bytes() + b"synthetic-change-after-submit")
        expected = "ASSET_DIGEST_MISMATCH"
    before = counts()
    error(
        act(
            w,
            "lead",
            "review",
            {"action": "approve", "expected_revision": 4, "submitted_version_id": submitted_id, "note": "合成检查"},
            "reject-mutated-review",
        ),
        409,
        expected,
    )
    assert counts() == before and w["frozen"] == [] and w["network"] == []


def test_return_preserves_source_and_freezes_independent_local_clarification(workflow):
    w = workflow
    original = copy.deepcopy(w["source"])
    returned = success(
        act(
            w,
            "lead",
            "decision",
            {"action": "return", "expected_revision": 1, "note": "合成澄清：请核对约束版本"},
            "return-original-source",
        )
    )
    assert returned["status"] == "returned" and returned["project_id"] is None
    assert returned["package"] == original and returned["delivery"]["status"] == "prepared"
    with models.session() as db:
        assert db.get(models.DesignHandoff, w["id"]).snapshot == original
        assert db.get(ManagedReceipt, w["id"]).envelope_bytes == w["envelope"]
    assert len(w["frozen"]) == 1 and w["frozen"][0][0] == "clarification" and w["network"] == []


def test_revision_request_keeps_old_submission_history(workflow):
    w = workflow
    old, version, _ = submitted(w)
    revised = success(
        act(
            w,
            "lead",
            "review",
            {
                "action": "revise",
                "expected_revision": 4,
                "submitted_version_id": version,
                "note": "合成退回：请创建新版本",
            },
            "review-revision-001",
        )
    )
    assert revised["status"] == "assigned" and revised["submitted_version_id"] is None and revised["revision"] == 5
    assert revised["submission"] == old["submission"]
    history = success(w["clients"]["lead"].get(f"/api/design-handoffs/{w['id']}"))["history"]
    assert [event["kind"] for event in history] == ["accept", "assign", "submitted", "revise"]
    assert history[-2]["payload"] == old["submission"] and w["frozen"] == []


def test_approved_submission_and_source_snapshots_cannot_be_overwritten_by_engine(workflow):
    w = workflow
    submitted_result, version, payload = submitted(w)
    approved = success(
        act(
            w,
            "lead",
            "review",
            {"action": "approve", "expected_revision": 4, "submitted_version_id": version, "note": "合成审查"},
            "approve-freeze-001",
        )
    )
    response = w["clients"]["alice"].post(
        f"/api/design-versions/{version}/check-corrections",
        json={"constraint_id": "c_product_0", "status": "pass", "evidence": "合成测试尝试修改已经提交的设计版本"},
        headers={"X-Design-Action-Id": "immutable-review-correction", "X-Design-Revision": "5"},
    )
    assert response.status_code == 409, response.text
    current = success(w["clients"]["lead"].get(f"/api/design-handoffs/{w['id']}"))
    assert current["submission"] == submitted_result["submission"] and current["review"] == approved["review"]
    assert current["package"] == w["source"]
    with models.session() as db:
        assert db.get(Record, version).payload == payload
        assert db.get(ManagedReceipt, w["id"]).envelope_bytes == w["envelope"]


def test_transaction_revalidates_revoked_session_before_mutating(workflow, monkeypatch):
    from app.agent import managed_workflow

    w = workflow
    original = managed_workflow.decide

    def revoke_before_transaction(request, handoff_id, raw):
        with models.session() as db:
            db.query(models.DesignerSession).filter_by(username="lead").delete()
            db.commit()
        return original(request, handoff_id, raw)

    monkeypatch.setattr(managed_workflow, "decide", revoke_before_transaction)
    error(
        act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "revoked-during-request"),
        401,
        "LOGIN_REQUIRED",
    )
    assert counts() == {"actions": 0, "audits": 0, "outbox": 0, "projects": 0}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"action":"accept","action":"accept","expected_revision":1}',
        b'{"action":"accept","expected_revision":1,"expected_revision":1}',
        b'{"action":"accept","expected_revision":true}',
        b'{"action":"accept","expected_revision":1.0}',
        b'{"action":"accept","expected_revision":"1"}',
    ],
)
def test_duplicate_json_and_coerced_revision_are_rejected_without_mutation(workflow, raw):
    before = counts()
    error(act(workflow, "lead", "decision", {}, "strict-revision-input", raw=raw), 422, "INVALID_INPUT")
    assert counts() == before
    with models.session() as db:
        assert db.get(models.DesignHandoff, workflow["id"]).status == "new"
        assert db.get(ManagedReceipt, workflow["id"]).revision == 1
