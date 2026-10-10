"""Original engine writes share one durable managed operation transaction."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from test_managed_stream_access import managed_access as managed_access

from app import config, models
from app.agent import routes
from app.agent.managed_store import ManagedAction, ManagedAudit, ManagedReceipt
from app.agent.providers import Provider
from app.agent.store import Record
from app.design_auth import session_context, token_hash
from app.main import app

SPEC = '{"intent":"合成规格，非真实生成","expected_spec_id":null}'.encode()


@pytest.fixture(autouse=True)
def complete_synthetic_source(managed_access):
    with models.session() as db, db.begin():
        for meta in db.scalars(select(ManagedReceipt)):
            proof = meta.proof
            meta.proof = {
                **proof,
                "package": {**proof["package"], "version_group_id": "synthetic-group",
                            "requirement_digest": "b" * 64, "prompt_digest": "c" * 64,
                            "requirement_base64": "e30=", "prompt_base64": "e30=",
                            "approval": {"synthetic": True}},
                "requirement": {**proof["requirement"], "constraints": []},
                "prompt": {**proof["prompt"], "avoid_items": []},
            }
            meta.receipt = {**meta.receipt, "content_digest": meta.envelope_sha256}


@pytest.fixture
def client(managed_access):
    instance = TestClient(app)
    instance.cookies.set(config.get_config().design_session_cookie, managed_access["tokens"]["alice"])
    yield instance
    instance.close()


def headers(fixture, action="synthetic-engine-001", revision=1, *, username="alice", **overrides):
    result = {
        "X-Design-Context": session_context(token_hash(fixture["tokens"][username])),
        "X-Design-Action-Id": action,
        "X-Design-Revision": str(revision),
        "Content-Type": "application/json",
    }
    return result | overrides


def spec_path(fixture):
    return f"/api/project/{fixture['projects']['alice']['pid']}/design-specs"


def counts():
    with models.session() as db:
        return {
            "specs": db.scalar(select(func.count()).select_from(Record).where(Record.kind == "spec")),
            "actions": db.scalar(select(func.count()).select_from(ManagedAction)),
            "audits": db.scalar(select(func.count()).select_from(ManagedAudit)),
            "revision": db.get(ManagedReceipt, "synthetic-alice").revision,
        }


def test_exact_action_response_replays_after_state_changes_and_engine_reconnect(client, managed_access):
    request_headers = headers(managed_access)
    first = client.post(spec_path(managed_access), content=SPEC, headers=request_headers)
    assert first.status_code == 201, first.text
    assert first.json()["base_revision"] == 1 and first.json()["revision"] == 2
    assert first.headers["X-Design-Revision"] == "2"
    spec_id = first.json()["id"]
    confirmation = client.post(
        f"/api/design-specs/{spec_id}/confirm", content=b"",
        headers=headers(managed_access, "synthetic-confirm-001", 2),
    )
    assert confirmation.status_code == 200 and confirmation.json()["revision"] == 3
    with models.session() as db, db.begin():
        db.get(models.DesignHandoff, "synthetic-alice").status = "review"
    models.reset_engine_for_tests()
    replay = client.post(spec_path(managed_access), content=SPEC, headers=request_headers)
    assert (replay.status_code, replay.content, replay.headers["X-Design-Revision"]) == (
        first.status_code, first.content, first.headers["X-Design-Revision"],
    )
    assert counts() == {"specs": 1, "actions": 2, "audits": 2, "revision": 3}
    operation = client.get("/api/design-operations/synthetic-engine-001")
    assert operation.status_code == 200 and operation.json()["result"] == first.json()
    blocked = client.post(
        f"/api/design-specs/{spec_id}/confirm", content=b"", headers=headers(managed_access, "new-confirm-action", 3),
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "STATE_CONFLICT"


@pytest.mark.parametrize("conflict", ["bytes", "revision", "target"])
def test_reused_action_rejects_any_changed_raw_identity(client, managed_access, conflict):
    path, body, request_headers = spec_path(managed_access), SPEC, headers(managed_access)
    assert client.post(path, content=body, headers=request_headers).status_code == 201
    if conflict == "bytes":
        body += b" "
    elif conflict == "revision":
        request_headers["X-Design-Revision"] = "2"
    else:
        path += "?different=1"
    response = client.post(path, content=body, headers=request_headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert counts() == {"specs": 1, "actions": 1, "audits": 1, "revision": 2}


def test_distinct_action_with_old_revision_is_rejected_before_business_write(client, managed_access):
    first = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    body = json.dumps({"intent": "新的合成规格", "expected_spec_id": first.json()["id"]}).encode()
    response = client.post(
        spec_path(managed_access), content=body, headers=headers(managed_access, "different-action", 1),
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "STATE_CONFLICT"
    assert counts() == {"specs": 1, "actions": 1, "audits": 1, "revision": 2}


def test_exception_before_action_commit_rolls_back_engine_and_revision(client, managed_access, monkeypatch):
    def fail(_operation, _response):
        raise RuntimeError("synthetic failure before action commit")

    monkeypatch.setattr(routes.EngineAction, "finish", fail)
    failed = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    assert failed.status_code == 500
    assert counts() == {"specs": 0, "actions": 0, "audits": 0, "revision": 1}


def test_commit_then_lost_response_can_replay_without_second_write(client, managed_access, monkeypatch):
    finish, saved = routes.EngineAction.finish, {}

    def lost(operation, response):
        result = finish(operation, response)
        saved["content"], saved["status"] = result.body, result.status_code
        raise RuntimeError("synthetic lost response after durable commit")

    monkeypatch.setattr(routes.EngineAction, "finish", lost)
    assert client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access)).status_code == 500
    assert counts() == {"specs": 1, "actions": 1, "audits": 1, "revision": 2}
    retry = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    assert retry.status_code == saved["status"] and retry.content == saved["content"]
    assert counts()["specs"] == 1


def test_session_revoked_before_commit_rolls_back_engine_and_action(client, managed_access, monkeypatch):
    finish = routes.EngineAction.finish

    def revoke(operation, response):
        record = operation.db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"]))
        record.expires_at = 0
        return finish(operation, response)

    monkeypatch.setattr(routes.EngineAction, "finish", revoke)
    response = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    assert response.status_code == 401 and response.json()["error"]["code"] == "LOGIN_REQUIRED"
    assert counts() == {"specs": 0, "actions": 0, "audits": 0, "revision": 1}


def test_new_session_queries_old_operation_but_cannot_reexecute_old_action(client, managed_access):
    first = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    assert first.status_code == 201
    new_cookie = "synthetic-fresh-alice-session"
    with models.session() as db, db.begin():
        prior = db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"]))
        db.add(models.DesignerSession(token_hash=token_hash(new_cookie), username="alice", expires_at=prior.expires_at))
    client.cookies.clear()
    client.cookies.set(config.get_config().design_session_cookie, new_cookie)
    new_headers = headers(managed_access, **{"X-Design-Context": session_context(token_hash(new_cookie))})
    retry = client.post(spec_path(managed_access), content=SPEC, headers=new_headers)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "AUTH_CONTEXT_CHANGED"
    operation = client.get("/api/design-operations/synthetic-engine-001")
    assert operation.status_code == 200 and operation.json()["result"] == first.json()
    assert counts()["actions"] == 1


@pytest.mark.parametrize("same_action", [True, False])
def test_concurrent_engine_actions_serialize_without_nested_sqlite_lock(managed_access, same_action):
    barrier = Barrier(2)

    def submit(index):
        instance = TestClient(app)
        instance.cookies.set(config.get_config().design_session_cookie, managed_access["tokens"]["alice"])
        try:
            barrier.wait(timeout=5)
            action_id = "concurrent-action" if same_action else f"concurrent-action-{index}"
            return instance.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access, action_id))
        finally:
            instance.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, (1, 2)))
    assert sorted(response.status_code for response in responses) == ([201, 201] if same_action else [201, 409])
    if same_action:
        assert responses[0].content == responses[1].content
    assert counts() == {"specs": 1, "actions": 1, "audits": 1, "revision": 2}


def test_upload_original_binary_and_name_query_are_preserved(client, managed_access):
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, "PNG")
    raw = image.getvalue()
    path = f"/api/project/{managed_access['projects']['alice']['pid']}/design-assets?name=synthetic.png"
    request_headers = headers(managed_access, **{"Content-Type": "image/png"})
    first = client.post(path, content=raw, headers=request_headers)
    assert first.status_code == 201, first.text
    assert client.post(path, content=raw, headers=request_headers).content == first.content
    conflict = client.post(path.replace("synthetic.png", "other.png"), content=raw, headers=request_headers)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with models.session() as db:
        action = db.scalar(select(ManagedAction))
        assert action.raw_body == raw and action.path.endswith("?name=synthetic.png")
    assert len(list((config.get_config().assets_dir / "design-agent").glob("*.png"))) == 1


def test_chat_turn_has_durable_operation_even_with_model_gate_closed(client, managed_access):
    path = f"/api/project/{managed_access['projects']['alice']['pid']}/design-messages"
    raw = json.dumps({
        "text": "仅保存合成设计要求", "expected_spec_id": None, "expected_prompt_id": None,
        "idempotency_key": "synthetic-engine-001",
    }).encode()
    first = client.post(path, content=raw, headers=headers(managed_access))
    assert first.status_code == 201 and first.json()["task_id"] is None
    assert client.post(path, content=raw, headers=headers(managed_access)).content == first.content
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind == "task")) == 0
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind == "message")) == 2
    assert counts()["actions"] == 1


def test_queued_task_replay_is_frozen_even_if_task_completed(client, managed_access, monkeypatch):
    first = client.post(spec_path(managed_access), content=SPEC, headers=headers(managed_access))
    # Explicit synthetic queue capability only; worker remains disabled and no provider is called.
    monkeypatch.setattr(Provider, "require", lambda self, mode: None)
    monkeypatch.setattr(Provider, "capabilities", lambda self: {"understand": True, "synthetic": True})
    raw = json.dumps({
        "spec_id": first.json()["id"], "mode": "understand", "authorized": True,
        "idempotency_key": "synthetic-queue-1",
    }).encode()
    path = f"/api/project/{managed_access['projects']['alice']['pid']}/design-tasks"
    request_headers = headers(managed_access, "synthetic-task-action", 2)
    queued = client.post(path, content=raw, headers=request_headers)
    assert queued.status_code == 202, queued.text
    with models.session() as db, db.begin():
        db.get(Record, queued.json()["id"]).status = "completed"
    models.reset_engine_for_tests()
    retry = client.post(path, content=raw, headers=request_headers)
    assert retry.status_code == 202 and retry.content == queued.content and retry.json()["status"] == "queued"


@pytest.mark.parametrize("missing", ["X-Design-Action-Id", "X-Design-Revision"])
def test_missing_action_contract_is_rejected_without_write(client, managed_access, missing):
    request_headers = headers(managed_access)
    request_headers.pop(missing)
    response = client.post(spec_path(managed_access), content=SPEC, headers=request_headers)
    assert response.status_code == 422
    error = response.json()
    assert len(error["request_id"]) == 32 and "request_id" not in error["error"]
    assert error["error"]["retryable"] is False
    assert counts() == {"specs": 0, "actions": 0, "audits": 0, "revision": 1}


def test_sync_technical_flat_is_explicitly_disabled_before_provider(client, managed_access, monkeypatch):
    with models.session() as db, db.begin():
        pid = managed_access["projects"]["alice"]["pid"]
        version = Record(id="synthetic-version-flat", project_id=pid, kind="version", status="confirmed", payload={},
                         dedupe="synthetic-version-flat")
        db.add(version)
    monkeypatch.setattr(routes.technical_flat, "generate", lambda _: pytest.fail("must not call sync generation"))
    response = client.post(
        "/api/design-versions/synthetic-version-flat/technical-flat", content=b"", headers=headers(managed_access),
    )
    assert response.status_code == 503 and response.json()["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert counts()["revision"] == 1 and counts()["actions"] == 0


def test_oversized_body_is_rejected_before_parser_or_action_creation(client, managed_access):
    response = client.post(
        spec_path(managed_access), content=b"x" * (256 * 1024 + 1),
        headers=headers(managed_access, **{"Content-Type": "multipart/form-data; boundary=synthetic"}),
    )
    assert response.status_code == 413
    assert counts() == {"specs": 0, "actions": 0, "audits": 0, "revision": 1}
