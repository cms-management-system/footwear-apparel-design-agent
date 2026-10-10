"""Dual-mode personnel and single-image boundaries. All images/providers here are synthetic."""

import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from test_managed_stream_access import managed_access as managed_access

from app import config, models
from app.agent import runner
from app.agent.managed_store import ImageReservation, IndependentProject, ManagedReceipt, sha256
from app.agent.providers import Provider
from app.agent.store import Record, transaction
from app.design_auth import session_context, token_hash
from app.main import app


@pytest.fixture
def client(managed_access, monkeypatch):
    monkeypatch.setattr(Provider, "_post", lambda *a, **k: pytest.fail("test must never call a paid provider"))
    instance = TestClient(app)
    instance.cookies.set(config.get_config().design_session_cookie, managed_access["tokens"]["alice"])
    yield instance
    instance.close()


def headers(fixture, action="create-independent-001", revision=None, username="alice"):
    result = {
        "X-Design-Context": session_context(token_hash(fixture["tokens"][username])),
        "X-Design-Action-Id": action,
    }
    if revision is not None:
        result["X-Design-Revision"] = str(revision)
    return result


def new_project(client, fixture, **extra):
    data = {
        "title": "自主蓝色鞋履",
        "design_object": "运动鞋",
        "initial_prompt": "轻便浅蓝休闲鞋",
        "output_kind": "effect_image",
        **extra,
    }
    return client.post("/api/design-projects", json=data, headers=headers(fixture))


def assume(client, fixture, username):
    client.cookies.clear()
    client.cookies.set(config.get_config().design_session_cookie, fixture["tokens"][username])


def grant(monkeypatch, tmp_path, pid, subject="alice", source_mode="independent"):
    path = tmp_path / "image-grant.json"
    path.write_text(
        json.dumps(
            {
                "grant_id": "synthetic-grant-001",
                "project_id": pid,
                "source_mode": source_mode,
                "subject": subject,
                "scope_id": config.get_config().design_scope_id,
                "expires_at": int(time.time()) + 600,
                "max_calls": 1,
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv("DESIGN_IMAGE_ONLY_ENABLED", "true")
    monkeypatch.setenv("DESIGN_IMAGE_AUTHORIZATION_FILE", str(path))
    monkeypatch.setenv("IMAGE_API_KEY", "synthetic-never-send")
    monkeypatch.setenv("IMAGE_MODEL", "synthetic-image-alias")
    monkeypatch.setenv("AGENT_IMAGE_CALL_MAX_FEN", "50")
    config.get_config.cache_clear()
    return path


def queue(client, fixture, created, action="single-image-action-001", revision=1):
    return client.post(
        f"/api/project/{created['project_id']}/design-text-to-image",
        json={
            "prompt_id": created["prompt"]["id"],
            "authorized": True,
            "idempotency_key": action,
        },
        headers=headers(fixture, action, revision),
    )


class ImageProvider:
    image_url = "https://synthetic.example.test/v1"
    image_model = "synthetic-image-alias"
    image_fen = 50

    def __init__(self, error=None):
        self.calls, self.error = 0, error
        self.last_receipt = {"model": None, "request_id": None, "usage": {}, "actual_cost_fen": None}

    def require(self, mode):
        assert mode == "image_only"

    def render_direct(self, prompt, images):
        self.calls += 1
        self.prompt = prompt
        with models.session() as db:
            task = db.scalar(
                select(Record).where(Record.kind == "task", Record.payload["mode"].as_string() == "image_only")
            )
            assert task.payload["steps"][0]["status"] == "pending" and task.payload["image_calls"] == 1
        if self.error:
            raise self.error
        target = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(target, "PNG")
        self.raw = target.getvalue()
        return self.raw


def run(id, provider):
    with transaction() as db:
        db.get(Record, id).status = "running"
    runner.run_task(id, provider)


def test_create_exact_replay_owner_and_no_fake_upstream(client, managed_access):
    first = new_project(client, managed_access)
    assert first.status_code == 201, first.text
    data = first.json()
    assert data["revision"] == 1 and data["source_mode"] == "independent" and data["owner_subject"] == "alice"
    assert new_project(client, managed_access).content == first.content
    assert new_project(client, managed_access, initial_prompt="另一个输入").status_code == 409
    workspace = client.get(f"/api/project/{data['project_id']}/design-workspace").json()
    assert workspace["source_binding"] is None and workspace["prompts"] == [data["prompt"]]
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(IndependentProject)) == 1
        assert db.scalar(select(func.count()).select_from(models.DesignHandoff)) == 6  # Fixture upstream only.
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind == "task")) == 0
        prompt = db.get(Record, data["prompt"]["id"])
        raw = base64.b64decode(prompt.payload["original_body_base64"])
        assert sha256(raw) == data["prompt"]["digest"]
        assert json.loads(raw)["positive_prompt"] == "轻便浅蓝休闲鞋"
    recovery = client.get("/api/design-operations/create-independent-001")
    assert recovery.status_code == 200 and recovery.json()["result"] == data


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_subject": "bob"},
        {"scope_id": "other"},
        {"approval": True},
        {"design_object": " "},
        {"output_kind": "3d"},
    ],
)
def test_create_rejects_client_authority_and_bad_input(client, managed_access, extra):
    assert new_project(client, managed_access, **extra).status_code == 422
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(IndependentProject)) == 0


def test_permissions_manager_readonly_and_other_employee_hidden(client, managed_access):
    created = new_project(client, managed_access).json()
    pid = created["project_id"]
    assume(client, managed_access, "bob")
    assert client.get(f"/api/project/{pid}/design-workspace").status_code == 404
    assert pid not in [p["project_id"] for p in client.get("/api/design-projects").json()["items"]]
    assume(client, managed_access, "lead")
    assert client.get(f"/api/project/{pid}/design-workspace").status_code == 200
    assert new_project(client, managed_access).status_code == 409  # Wrong context before role guard.
    response = client.post(
        "/api/design-projects",
        json={"title": "代建", "design_object": "鞋", "initial_prompt": "蓝鞋", "output_kind": "effect_image"},
        headers=headers(managed_access, username="lead"),
    )
    assert response.status_code == 403
    assert (
        client.post(
            f"/api/project/{pid}/design-prompts", json={}, headers=headers(managed_access, revision=1, username="lead")
        ).status_code
        == 403
    )
    assert client.get("/api/design-projects?limit=1&offset=0").json()["total"] >= 3
    with models.session() as db, db.begin():
        db.get(IndependentProject, pid).scope_id = "other-scope"
    assert client.get(f"/api/project/{pid}/design-workspace").status_code == 404


def test_prompt_edit_immutable_revision_and_zero_provider(client, managed_access):
    created = new_project(client, managed_access).json()
    path = f"/api/project/{created['project_id']}/design-prompts"
    body = {
        "expected_prompt_id": created["prompt"]["id"],
        "positive_prompt": "轻便白色鞋履",
        "avoid_items": ["厚底"],
        "output_kind": "design_draft",
        "base_version_id": None,
        "edit_region": "",
    }
    first = client.post(path, json=body, headers=headers(managed_access, "save-prompt-001", 1))
    assert first.status_code == 201, first.text
    assert first.json()["prompt"]["derived_from_prompt_id"] == created["prompt"]["id"]
    assert client.post(path, json=body, headers=headers(managed_access, "save-prompt-001", 1)).content == first.content
    assert client.post(path, json=body, headers=headers(managed_access, "save-prompt-002", 1)).status_code == 409
    assert (
        client.post(
            path,
            json={**body, "base_version_id": "invented", "edit_region": ""},
            headers=headers(managed_access, "save-prompt-003", 2),
        ).status_code
        == 422
    )
    workspace = client.get(f"/api/project/{created['project_id']}/design-workspace").json()
    assert len(workspace["prompts"]) == 2 and workspace["prompts"][0] == created["prompt"]


def test_no_grant_or_wrong_owner_cannot_queue(client, managed_access, monkeypatch, tmp_path):
    created = new_project(client, managed_access).json()
    assert queue(client, managed_access, created).status_code == 409
    grant(monkeypatch, tmp_path, created["project_id"], subject="bob")
    assert queue(client, managed_access, created).status_code == 409
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(ImageReservation)) == 0


def test_single_call_saves_original_and_replay_without_text_checks(client, managed_access, monkeypatch, tmp_path):
    created = new_project(client, managed_access).json()
    grant(monkeypatch, tmp_path, created["project_id"])
    assert Provider().capabilities()["image_only"] and not Provider().capabilities()["understand"]
    first = queue(client, managed_access, created)
    assert first.status_code == 202, first.text
    task_id = first.json()["task_id"]
    provider = ImageProvider()
    run(task_id, provider)
    task = client.get(f"/api/design-tasks/{task_id}").json()
    assert task["status"] == "succeeded" and task["image_calls"] == 1 and task["reasoning_calls"] == 0
    version_id = task["version_ids"][0]
    workspace = client.get(f"/api/project/{created['project_id']}/design-workspace").json()
    version = workspace["versions"][0]
    assert version["generation_mode"] == "model_generated" and version["check_status"] == "not_checked"
    provenance = version["generation_provenance"]
    assert provenance["actual_cost_fen"] is None and provenance["response_model"] is None
    assert base64.b64decode(provenance["execution_prompt_base64"]) == provider.prompt.encode()
    assert client.get(f"/api/design-versions/{version_id}/image").content == provider.raw
    assert queue(client, managed_access, created).content == first.content
    assert queue(client, managed_access, created, "different-image-action-001", 2).status_code == 409
    selected = client.post(
        f"/api/design-versions/{version_id}/confirm", headers=headers(managed_access, "select-image-001", 2)
    )
    assert selected.status_code == 200 and selected.json()["confirmation_origin"] == "manual_selection"
    assert selected.json()["review"]["goal"]["status"] == "unknown"
    models.reset_engine_for_tests()
    assert client.get(f"/api/design-versions/{version_id}/image").content == provider.raw
    assert provider.calls == 1
    assume(client, managed_access, "bob")
    assert client.get(f"/api/design-versions/{version_id}/image").status_code == 404
    assert client.get(f"/api/design-tasks/{task_id}").status_code == 404


def test_unknown_call_is_durable_and_never_resumed(client, managed_access, monkeypatch, tmp_path):
    created = new_project(client, managed_access).json()
    grant(monkeypatch, tmp_path, created["project_id"])
    task_id = queue(client, managed_access, created).json()["task_id"]
    provider = ImageProvider(TimeoutError("synthetic unknown"))
    run(task_id, provider)
    task = client.get(f"/api/design-tasks/{task_id}").json()
    assert task["status"] == "interrupted" and task["outcome"] == "unknown"
    assert task["steps"][0]["status"] == "unknown" and provider.calls == 1
    resume = client.post(
        f"/api/design-tasks/{task_id}/resume", headers=headers(managed_access, "resume-unknown-001", 2)
    )
    assert resume.status_code == 409 and resume.json()["error"]["code"] == "UNKNOWN_CALL"
    assert queue(client, managed_access, created, "unknown-other-key-001", 2).status_code == 409
    models.reset_engine_for_tests()
    with transaction() as db:
        runner.recover(db)
    assert client.get(f"/api/design-tasks/{task_id}").json()["status"] == "interrupted"
    assert provider.calls == 1


def test_restart_after_dispatch_marks_unknown_without_network(client, managed_access, monkeypatch, tmp_path):
    created = new_project(client, managed_access).json()
    grant(monkeypatch, tmp_path, created["project_id"])
    task_id = queue(client, managed_access, created).json()["task_id"]
    with transaction() as db:
        task = db.get(Record, task_id)
        task.status = "running"
        task.payload = {
            **task.payload,
            "steps": [{"status": "pending", "tool": "generate_image_only"}],
            "image_calls": 1,
        }
    models.reset_engine_for_tests()
    with transaction() as db:
        runner.recover(db)
    task = client.get(f"/api/design-tasks/{task_id}").json()
    assert task["status"] == "interrupted" and task["steps"][0]["status"] == "unknown"
    assert queue(client, managed_access, created, "after-restart-key-001", 2).status_code == 409


def test_upstream_prompt_keeps_full_source_and_single_image_can_freeze_result(
    client,
    managed_access,
    monkeypatch,
    tmp_path,
):
    from app.agent.managed_protocol import decode_delivery
    from app.agent.managed_store import ManagedAudit, ManagedOutbox
    from tests.managed_fixtures import SOURCE_INSTANCE, make_delivery

    cfg = config.get_config()
    decoded = decode_delivery(
        make_delivery(target_instance_id=cfg.design_instance_id, scope_id=cfg.design_scope_id),
        source_instance_id=SOURCE_INSTANCE,
        target_instance_id=cfg.design_instance_id,
        scope_id=cfg.design_scope_id,
    )
    pid = managed_access["projects"]["alice"]["pid"]
    with models.session() as db, db.begin():
        row = db.get(models.DesignHandoff, "synthetic-alice")
        row.package_id = decoded["package"]["package_id"]
        row.version = decoded["package"]["version"]
        meta = db.get(ManagedReceipt, row.id)
        meta.package_id = row.package_id
        meta.package_version = row.version
        meta.proof = {key: decoded[key] for key in ("package", "requirement", "prompt")}
        meta.receipt = {
            **meta.receipt,
            "content_digest": decoded["delivery"]["content_digest"],
            "design_receive_id": "DR-SYNTHETIC-UPSTREAM-1",
        }
        db.add(ManagedAudit(handoff_id=row.id, revision=1, kind="assign", actor="lead", payload={}))
    source = decoded["prompt"]
    body = {
        "expected_prompt_id": None,
        "positive_prompt": source["positive_prompt"],
        "avoid_items": source["avoid_items"],
        "output_kind": "effect_image",
        "base_version_id": None,
        "edit_region": "",
    }
    path = f"/api/project/{pid}/design-prompts"
    bad = client.post(path, json={**body, "avoid_items": []}, headers=headers(managed_access, "bad-upstream-001", 1))
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "SOURCE_CONSTRAINT_CONFLICT"
    saved = client.post(path, json=body, headers=headers(managed_access, "save-upstream-001", 1))
    assert saved.status_code == 201, saved.text
    workspace = client.get(f"/api/project/{pid}/design-workspace").json()
    assert workspace["source_binding"]["prompt"] == source
    assert workspace["specs"][0]["product_source"]["prompt"] == source
    created = {"project_id": pid, "prompt": saved.json()["prompt"]}
    grant(monkeypatch, tmp_path, pid, source_mode="upstream")
    queued = queue(client, managed_access, created, revision=2)
    assert queued.status_code == 202, queued.text
    provider = ImageProvider()
    run(queued.json()["task_id"], provider)
    task = client.get(f"/api/design-tasks/{queued.json()['task_id']}").json()
    assert task["status"] == "succeeded", task
    version_id = task["version_ids"][0]
    assert source["positive_prompt"] in provider.prompt
    assert (
        client.post(
            f"/api/design-versions/{version_id}/confirm", headers=headers(managed_access, "select-upstream-001", 3)
        ).status_code
        == 200
    )
    submitted = client.post(
        "/api/design-handoffs/synthetic-alice/submit",
        json={
            "expected_revision": 4,
            "version_id": version_id,
        },
        headers=headers(managed_access, "submit-upstream-001"),
    )
    assert submitted.status_code == 200, submitted.text
    assume(client, managed_access, "lead")
    reviewed = client.post(
        "/api/design-handoffs/synthetic-alice/review",
        json={
            "expected_revision": 5,
            "action": "approve",
            "submitted_version_id": version_id,
            "note": "合成技术账户审查，实际图效果仍待用户核验",
        },
        headers=headers(managed_access, "review-upstream-001", username="lead"),
    )
    assert reviewed.status_code == 200, reviewed.text
    with models.session() as db:
        outbox = db.scalar(select(ManagedOutbox))
        event = json.loads(base64.b64decode(json.loads(outbox.raw_body)["content_base64"]))
        result = event["result"]
        assert result["generation_mode"] == "model_generated"
        assert len(result["generation_provenance"]) == 15
        assert result["generation_provenance"]["source_product_prompt_digest"] == event["prompt_digest"]
        projection = json.loads(base64.b64decode(result["design_version_base64"]))
        assert projection["generation_provenance"] == result["generation_provenance"]
        assert result["assets"][0]["sha256"] == sha256(provider.raw)
    assert provider.calls == 1


@pytest.mark.parametrize("change", ["inactive", "scope"])
def test_permission_revoked_after_queue_prevents_dispatch(client, managed_access, monkeypatch, tmp_path, change):
    created = new_project(client, managed_access).json()
    grant(monkeypatch, tmp_path, created["project_id"])
    task_id = queue(client, managed_access, created).json()["task_id"]
    with models.session() as db, db.begin():
        if change == "inactive":
            db.get(models.DesignerUser, "alice").active = False
        else:
            db.get(IndependentProject, created["project_id"]).scope_id = "another-scope"
    provider = ImageProvider()
    run(task_id, provider)
    with models.session() as db:
        task = db.get(Record, task_id)
        assert task.status == "failed" and task.payload["image_calls"] == 0
        assert task.payload["steps"] == []
    assert provider.calls == 0
