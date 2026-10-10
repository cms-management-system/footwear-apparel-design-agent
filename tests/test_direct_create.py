"""2.0.1 bounded executions on new disposable objects; every external POST is forbidden."""

import asyncio
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from sqlalchemy import func, select
from test_dual_entry import ImageProvider, assume, headers
from test_dual_entry import client as client
from test_dual_entry import managed_access as managed_access
from test_managed_stream_access import request_for

from app import config, models
from app.agent import assets, direct_create, events, execution_quota, runner, workbench
from app.agent.managed_store import (
    CreationGrantBinding,
    ExecutionReservation,
    ImageReservation,
    IndependentProject,
    ManagedAction,
    ProviderSlot,
)
from app.agent.store import AgentError, Record, change, create, head, transaction

ACTION = "direct-creation-synthetic-001"
BODY = {
    "entry_mode": "idea",
    "text": "  设计一款奶白运动鞋\n柔和圆头  ",
    "output_kind": "effect_image",
    "intent": "generate_image",
    "authorized": True,
}
BLANK = {"entry_mode": "blank", "text": "", "output_kind": "effect_image", "intent": "none", "authorized": False}


def direct(client, fixture, body=None, action=ACTION, username="alice"):
    return client.post(
        "/api/design-projects/direct", json=body or BODY, headers=headers(fixture, action, username=username)
    )


@pytest.fixture
def configured(managed_access, monkeypatch, tmp_path):
    p = tmp_path / "private-creation-grant.json"
    g = {
        "authorized": True,
        "grant_id": "synthetic-direct-grant-001",
        "subject": "alice",
        "scope_id": config.get_config().design_scope_id,
        "target_instance_id": config.get_config().design_instance_id,
        "creation_action_id": ACTION,
        "expires_at": int(time.time()) + 600,
        "stages": {"text": {"max_calls": 1, "max_cost_fen": 50}, "image": {"max_calls": 1, "max_cost_fen": 50}},
    }
    p.write_text(json.dumps(g))
    p.chmod(0o600)
    for k, v in {
        "DESIGN_CREATION_AUTHORIZATION_FILE": str(p),
        "DESIGN_PAID_PROVIDERS_ENABLED": "true",
        "AGENT_ENABLED": "true",
        "MODEL_BASE_URL": "https://synthetic.invalid/v1",
        "MODEL_ID": "synthetic-gemini-alias",
        "MODEL_API_KEY": "synthetic-never-send",
        "AGENT_REASONING_CALL_MAX_FEN": "50",
        "DESIGN_IMAGE_ONLY_ENABLED": "true",
        "IMAGE_BASE_URL": "https://synthetic.invalid/v1",
        "IMAGE_API_KEY": "synthetic-never-send",
        "IMAGE_MODEL": "synthetic-image-alias",
        "AGENT_IMAGE_CALL_MAX_FEN": "50",
    }.items():
        monkeypatch.setenv(k, v)
    config.get_config.cache_clear()
    return p


def alter(p, **updates):
    g = json.loads(p.read_text())
    g.update(updates)
    p.write_text(json.dumps(g))
    p.chmod(0o600)
    return g


class DecisionProvider(ImageProvider):
    text_model = "synthetic-gemini-alias"
    text_url = "https://synthetic.invalid/v1"
    reasoning_fen = 50
    vision_model = vision_key = vision_url = "synthetic-image-understanding"

    def __init__(self, *, resolved="generate_image", error=None, during=None, decision=None):
        super().__init__()
        self.text_calls = 0
        self.text_error = error
        self.during = during
        self.decision = decision or {
            "resolved_intent": resolved,
            "answer": "合成模型回复：已整理创意",
            "positive_prompt": "合成奶白运动鞋，柔和圆头，完整二维效果示意",
            "avoid_items": ["不增加文字标志"],
            "base_version_id": None,
            "edit_region": "",
        }

    def require(self, mode):
        assert mode in {"understand", "image_only"}

    def capabilities(self):
        return {"understand": True, "image_only": True}

    def creation_decision(self, context, images):
        self.text_calls += 1
        self.context = context
        with models.session() as db:
            r = db.scalar(select(ExecutionReservation).where(ExecutionReservation.stage == "text"))
            assert r.status == "sent"
            assert db.get(ProviderSlot, 1).status == "sent"
        if self.during:
            self.during()
        if self.text_error:
            raise self.text_error
        self.last_receipt = {
            "model": "synthetic-response-gemini",
            "request_id": "synthetic-request-001",
            "usage": {"total_tokens": 13},
            "actual_cost_fen": None,
        }
        return self.decision


def execute(task_id, provider):
    with transaction() as db:
        task = db.get(Record, task_id)
        if task.status == "queued":
            task.status = "running"
    runner.run_task(task_id, provider)


def run_state(client, run_id):
    r = client.get(f"/api/design-creation-runs/{run_id}")
    assert r.status_code == 200, r.text
    return r.json()


def state(client, pid):
    return client.get(f"/api/project/{pid}/design-workspace").json()


def turn(client, fixture, pid, action="new-turn-synthetic-001", **overrides):
    w = state(client, pid)
    return client.post(
        f"/api/project/{pid}/design-messages",
        json={
            "text": "这款的圆头有什么特点？",
            "intent": "auto",
            "authorized": True,
            "idempotency_key": action,
            "expected_prompt_id": w["current_prompt_id"],
            "expected_spec_id": w["head"].get("spec_id"),
            **overrides,
        },
        headers=headers(fixture, action, w["revision"]),
    )


def test_atomic_create_raw_text_ids_digest_replay_and_context(client, managed_access, configured):
    r = direct(client, managed_access)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["revision"] == 1 and d["run_status"] == "queued"
    assert direct(client, managed_access).content == r.content
    assert direct(client, managed_access, {**BODY, "text": "另一个输入"}).status_code == 409
    bad = headers(managed_access)
    bad["X-Design-Context"] = "different-context"
    assert client.post("/api/design-projects/direct", json=BODY, headers=bad).status_code == 409
    w = state(client, d["project_id"])
    assert len(w["messages"]) == 1 and w["messages"][0]["text"] == BODY["text"]
    assert w["messages"][0]["id"] == d["message_id"]
    assert w["current_prompt_id"] is None and w["specs"] == [] and w["source_binding"] is None
    op = client.get(f"/api/design-operations/{ACTION}").json()
    assert op["result"] == d
    with models.session() as db:
        i = db.get(Record, d["intent_id"]).payload["intent"]
        assert i["digest"] == execution_quota.sha256(
            execution_quota.canonical_bytes({k: v for k, v in i.items() if k != "digest"})
        )
        assert "resolved_intent" not in i
        assert db.scalar(select(func.count()).select_from(IndependentProject)) == 1
        assert db.scalar(select(func.count()).select_from(ExecutionReservation)) == 1


def test_blank_then_first_input_works_without_manual_spec(client, managed_access, configured):
    d = direct(client, managed_access, BLANK).json()
    assert all(d[x] is None for x in ["message_id", "intent_id", "run_id", "run_status"])
    w = state(client, d["project_id"])
    assert not w["messages"] and not w["specs"] and not w["prompts"]
    with models.session() as db:
        assert not list(db.scalars(select(ExecutionReservation)))
    r = turn(client, managed_access, d["project_id"], intent="generate_image", text=BODY["text"])
    assert r.status_code == 201, r.text
    run = run_state(client, r.json()["run_id"])
    assert run["status"] == "queued"


@pytest.mark.parametrize(
    "patch",
    [
        {"text": " "},
        {"text": "x" * 4001},
        {"authorized": False},
        {"authorized": 1},
        {"authorized": "true"},
        {"entry_mode": "form"},
        {"intent": "none"},
        {"output_kind": "3d"},
        {"title": "must not accept"},
    ],
)
def test_invalid_creation_is_all_or_nothing(client, managed_access, patch):
    r = direct(client, managed_access, {**BODY, **patch})
    assert r.status_code == 422
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(IndependentProject)) == 0


@pytest.mark.parametrize("patch", [{"text": "x"}, {"intent": "discuss"}, {"authorized": True}])
def test_blank_rejects_hidden_execution(client, managed_access, patch):
    assert direct(client, managed_access, {**BLANK, **patch}).status_code == 422


def test_unavailable_persists_blocked_user_not_fake_assistant(client, managed_access):
    d = direct(client, managed_access).json()
    assert d["run_status"] == "blocked"
    w = state(client, d["project_id"])
    assert [m["role"] for m in w["messages"]] == ["user", "system"]
    assert not w["tasks"] and not w["versions"]


def test_model_freeze_single_image_and_auto_canvas_original_bytes(client, managed_access, configured):
    d = direct(client, managed_access).json()
    run = run_state(client, d["run_id"])
    p = DecisionProvider()
    execute(run["text_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["stage"] == "image" and run["status"] == "running"
    w = state(client, d["project_id"])
    prompt = w["prompts"][0]
    assert prompt["confirmation_mode"] == "user_intent" and prompt["execution_intent_id"] == d["intent_id"]
    assert w["head"]["spec_id"] == run["spec_id"] and w["execution_brief_state"] == "ready"
    assert w["briefs"][0]["model_task_id"] == run["text_task_id"]
    execute(run["image_task_id"], p)
    done = run_state(client, d["run_id"])
    assert done["status"] == "succeeded" and done["canvas_placement"] == "placed"
    assert p.text_calls == 1 and p.calls == 1
    image = client.get(f"/api/design-versions/{done['version_id']}/image")
    assert image.content == p.raw
    canvas = client.get(f"/api/project/{d['project_id']}/design-canvas").json()
    assert canvas["layout_revision"] == 1
    assert canvas["layout"]["nodes"][0]["id"] == "generated_" + done["version_id"]
    assert len(done["steps"]) == 2 and done["steps"][0]["response_model"] == "synthetic-response-gemini"
    assert done["steps"][0]["actual_cost_fen"] is None
    execute(run["text_task_id"], p)
    execute(run["image_task_id"], p)
    assert p.text_calls == 1 and p.calls == 1
    cap = client.get(f"/api/design-agent/capabilities?project_id={d['project_id']}").json()["direct_creation"]
    assert cap["remaining"] == {"text_calls": 0, "image_calls": 0}


@pytest.mark.parametrize(
    "resolved,requested,expected",
    [
        ("discuss", "auto", "succeeded"),
        ("generate_image", "discuss", "succeeded"),
        ("needs_input", "generate_image", "needs_input"),
    ],
)
def test_discussion_or_question_never_calls_image(client, managed_access, configured, resolved, requested, expected):
    if requested == "auto":
        blank = direct(client, managed_access, BLANK).json()
        response = turn(client, managed_access, blank["project_id"], intent="auto")
        d = {"project_id": blank["project_id"], "run_id": response.json()["run_id"]}
    else:
        d = direct(client, managed_access, {**BODY, "intent": requested}).json()
    p = DecisionProvider(resolved=resolved)
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["status"] == expected and run["image_task_id"] is None
    assert p.text_calls == 1 and p.calls == 0 and not state(client, d["project_id"])["prompts"]


def test_new_message_supersedes_before_freeze(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider(during=lambda: turn(client, managed_access, d["project_id"], text="先不要出图"))
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["status"] == "superseded"
    assert not run["image_task_id"] and not state(client, d["project_id"])["prompts"]
    assert p.text_calls == 1 and p.calls == 0


@pytest.mark.parametrize("fault", ["schema", "unknown", "denied"])
def test_single_text_failure_no_repair_or_resume(client, managed_access, configured, fault):
    d = direct(client, managed_access).json()
    task = run_state(client, d["run_id"])["text_task_id"]
    error = AgentError("CALL_OUTCOME_UNKNOWN" if fault == "unknown" else "PROVIDER_ACCESS_DENIED", "synthetic")
    p = DecisionProvider(
        error=None if fault == "schema" else error,
        decision={"resolved_intent": "generate_image"} if fault == "schema" else None,
    )
    execute(task, p)
    execute(task, p)
    run = run_state(client, d["run_id"])
    assert run["status"] == ("unknown" if fault == "unknown" else "failed") and not run["image_task_id"]
    assert p.text_calls == 1 and p.calls == 0
    with transaction() as db:
        runner.recover(db)
    assert run_state(client, d["run_id"])["status"] == run["status"]


def test_new_grant_not_consumed_by_old_image_reservation(client, managed_access, configured):
    with transaction() as db:
        db.add(
            ImageReservation(
                grant_id="old-consumed-003", task_id="old-task", project_id=3, subject="alice", authorization={}
            )
        )
    d = direct(client, managed_access).json()
    assert d["run_status"] == "queued"
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    assert run_state(client, d["run_id"])["image_task_id"]
    with models.session() as db:
        assert db.get(ImageReservation, "old-consumed-003").task_id == "old-task"


def test_same_grant_cross_run_limits_and_new_grant_can_discuss(client, managed_access, configured):
    d = direct(client, managed_access, {**BODY, "intent": "discuss"}).json()
    p = DecisionProvider(resolved="discuss")
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    r = turn(client, managed_access, d["project_id"])
    assert r.status_code == 201
    assert run_state(client, r.json()["run_id"])["reason"] == "CALL_LIMIT_EXHAUSTED"
    g = json.loads(configured.read_text())
    g.pop("creation_action_id")
    g.update(project_id=d["project_id"], grant_id="synthetic-second-text-grant")
    configured.write_text(json.dumps(g))
    configured.chmod(0o600)
    r = turn(client, managed_access, d["project_id"], action="second-authorized-turn")
    run = run_state(client, r.json()["run_id"])
    assert run["status"] == "queued"
    # Stub asserts first reservation status, so use a non-inspecting decision method for this separate second grant.
    p.creation_decision = lambda context, images: {"resolved_intent": "discuss", "answer": "独立授权的合成回复"}
    execute(run["text_task_id"], p)
    assert run_state(client, run["id"])["status"] == "succeeded"


@pytest.mark.parametrize("change_grant", ["cost", "calls", "expired", "actor", "scope", "mode"])
def test_invalid_grant_prevents_dispatch(client, managed_access, configured, change_grant):
    g = json.loads(configured.read_text())
    if change_grant == "cost":
        g["stages"]["text"]["max_cost_fen"] = 49
    elif change_grant == "calls":
        g["stages"]["text"]["max_calls"] = 0
    elif change_grant == "expired":
        g["expires_at"] = int(time.time()) - 1
    elif change_grant == "actor":
        g["subject"] = "bob"
    elif change_grant == "scope":
        g["scope_id"] = "foreign"
    else:
        configured.chmod(0o644)
    if change_grant != "mode":
        configured.write_text(json.dumps(g))
        configured.chmod(0o600)
    d = direct(client, managed_access).json()
    assert d["run_status"] == "blocked"
    with models.session() as db:
        assert not list(db.scalars(select(ExecutionReservation)))


def test_expired_after_queue_zero_calls_and_audited_release(client, managed_access, configured):
    d = direct(client, managed_access).json()
    alter(configured, expires_at=int(time.time()) - 1)
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    assert p.text_calls == 0
    with models.session() as db:
        assert db.scalar(select(ExecutionReservation)).status == "failed_not_sent"


def test_atomic_replay_under_concurrent_create(client, managed_access, configured):
    def actual(_):
        return direct(client, managed_access)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(actual, range(2)))
    assert [r.status_code for r in responses] == [201, 201] and responses[0].content == responses[1].content
    with models.session() as db:
        assert db.scalar(select(func.count()).select_from(IndependentProject)) == 1


def test_provider_unknown_slot_blocks_next_grant_and_archive(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider(error=AgentError("CALL_OUTCOME_UNKNOWN", "synthetic"))
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    g = json.loads(configured.read_text())
    g["grant_id"] = "new-grant-after-unknown"
    configured.write_text(json.dumps(g))
    r = turn(client, managed_access, d["project_id"])
    task = run_state(client, r.json()["run_id"])["text_task_id"]
    second = DecisionProvider()
    execute(task, second)
    assert second.text_calls == 0
    w = state(client, d["project_id"])
    a = client.post(
        f"/api/design-projects/{d['project_id']}/archive",
        json={"expected_revision": w["revision"], "confirm": True},
        headers=headers(managed_access, "archive-after-unknown"),
    )
    assert a.status_code == 409 and a.json()["error"]["code"] == "PROJECT_BUSY"


def test_rename_archive_receipt_and_all_resource_guards(client, managed_access):
    d = direct(client, managed_access, BLANK).json()
    pid = d["project_id"]
    h = headers(managed_access, "rename-new-object-001")
    r = client.patch(f"/api/design-projects/{pid}", json={"expected_revision": 1, "title": "新合成名称"}, headers=h)
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert (
        client.patch(
            f"/api/design-projects/{pid}", json={"expected_revision": 1, "title": "新合成名称"}, headers=h
        ).content
        == r.content
    )
    assert (
        client.patch(
            f"/api/design-projects/{pid}",
            json={"expected_revision": 1, "title": "过时名称"},
            headers=headers(managed_access, "stale-rename-action"),
        ).status_code
        == 409
    )
    a_headers = headers(managed_access, "archive-new-object-001")
    b = {"expected_revision": 2, "confirm": True}
    a = client.post(f"/api/design-projects/{pid}/archive", json=b, headers=a_headers)
    assert a.status_code == 200, a.text
    assert client.post(f"/api/design-projects/{pid}/archive", json=b, headers=a_headers).content == a.content
    for suffix in ["design-workspace", "design-canvas", "design-versions", "design-events"]:
        assert client.get(f"/api/project/{pid}/{suffix}").status_code == 404
    assert pid not in [x["project_id"] for x in client.get("/api/design-projects").json()["items"]]
    assert client.get("/api/design-operations/archive-new-object-001").json()["result"] == a.json()
    assert client.get(f"/api/design-operations/{ACTION}").status_code == 404
    assert (
        client.post(
            f"/api/design-projects/{pid}/archive", json=b, headers=headers(managed_access, "another-archive-action")
        ).status_code
        == 404
    )
    with models.session() as db:
        assert db.get(models.Project, pid).name == "新合成名称"
        assert db.get(IndependentProject, pid).status == "archived"
        assert len(list(db.scalars(select(ManagedAction)))) == 3
        assert db.get(Record, f"head_{pid}")


@pytest.mark.parametrize("username,code", [("bob", 404), ("lead", 403)])
def test_foreign_or_manager_cannot_rename_archive(client, managed_access, username, code):
    d = direct(client, managed_access, BLANK).json()
    assume(client, managed_access, username)
    for method, path, body in [
        ("PATCH", f"/api/design-projects/{d['project_id']}", {"expected_revision": 1, "title": "禁止"}),
        ("POST", f"/api/design-projects/{d['project_id']}/archive", {"expected_revision": 1, "confirm": True}),
    ]:
        r = client.request(
            method, path, json=body, headers=headers(managed_access, "unauthorized-" + method, username=username)
        )
        assert r.status_code == code, r.text


def test_owner_only_create_and_upstream_menu_forbidden(client, managed_access):
    assume(client, managed_access, "lead")
    assert direct(client, managed_access, BLANK, username="lead").status_code == 403
    assume(client, managed_access, "alice")
    pid = managed_access["projects"]["alice"]["pid"]
    assert (
        client.patch(
            f"/api/design-projects/{pid}",
            json={"expected_revision": 1, "title": "不得改A"},
            headers=headers(managed_access, "upstream-rename-001"),
        ).status_code
        == 403
    )


def test_unsent_running_recovered_without_new_task(client, managed_access, configured):
    d = direct(client, managed_access).json()
    task = run_state(client, d["run_id"])["text_task_id"]
    with transaction() as db:
        db.get(Record, task).status = "running"
        runner.recover(db)
    with models.session() as db:
        assert db.get(Record, task).status == "queued"
    p = DecisionProvider(resolved="discuss")
    execute(task, p)
    assert p.text_calls == 1


def test_dispatch_crash_unknown_not_resent(client, managed_access, configured):
    d = direct(client, managed_access).json()
    run_id = d["run_id"]
    task_id = run_state(client, run_id)["text_task_id"]
    with transaction() as db:
        run = db.get(Record, run_id)
        task = db.get(Record, task_id)
        task.status = run.status = "running"
        reserve = direct_create.reservation(db, run, "text")
        execution_quota.dispatch(db, reserve, run, 50)
        change(task, steps=[{"id": reserve.payload["attempt_id"], "status": "pending"}])
        runner.recover(db)
    assert run_state(client, run_id)["status"] == "unknown"
    p = DecisionProvider()
    execute(task_id, p)
    assert p.text_calls == 0


def test_canvas_merges_latest_viewport_and_nodes_never_old_full_layout(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    with transaction() as db:
        create(
            db,
            d["project_id"],
            "canvas",
            {
                "schema_version": "design-canvas/1",
                "project_id": d["project_id"],
                "layout_revision": 7,
                "layout": {"nodes": [], "viewport": {"x": 500, "y": -20, "zoom": 2}},
                "updated_at": None,
                "updated_by": "alice",
            },
            "saved",
            id=f"canvas_{d['project_id']}",
        )
    execute(run_state(client, d["run_id"])["image_task_id"], p)
    c = client.get(f"/api/project/{d['project_id']}/design-canvas").json()
    assert c["layout_revision"] == 8 and c["layout"]["viewport"] == {"x": 500, "y": -20, "zoom": 2}
    with transaction() as db:
        run = db.get(Record, d["run_id"])
        v = db.get(Record, run.payload["version_id"])
        assert direct_create.place_canvas(db, run, v) == "placed"
        assert workbench.canvas_document(db, d["project_id"])["layout_revision"] == 8


def test_creation_transaction_rolls_back_when_receipt_commit_fails(client, managed_access, configured, monkeypatch):
    from app.agent.routes import EngineAction

    def stop(self, response):
        raise AgentError("INTERNAL_ERROR", "synthetic receipt failure", 500)

    monkeypatch.setattr(EngineAction, "finish", stop)
    assert direct(client, managed_access).status_code == 500
    with models.session() as db:
        assert not list(db.scalars(select(IndependentProject)))
        assert not list(db.scalars(select(ExecutionReservation)))
        assert not list(
            db.scalars(select(Record).where(Record.kind.in_(["message", "creation_run", "creation_intent"])))
        )


def test_cancel_only_proven_unsent_release_and_no_paid_dispatch(client, managed_access, configured):
    d = direct(client, managed_access).json()
    task = run_state(client, d["run_id"])["text_task_id"]
    r = client.post(
        f"/api/design-tasks/{task}/cancel", json={}, headers=headers(managed_access, "cancel-before-sent", 1)
    )
    assert r.status_code == 200, r.text
    assert run_state(client, d["run_id"])["status"] == "cancelled"
    p = DecisionProvider()
    execute(task, p)
    assert p.text_calls == 0
    with models.session() as db:
        assert db.scalar(select(ExecutionReservation)).status == "failed_not_sent"
        assert db.scalar(select(Record).where(Record.kind == "reservation_audit"))


def test_task_counter_refusal_before_provider(client, managed_access, configured):
    d = direct(client, managed_access).json()
    task = run_state(client, d["run_id"])["text_task_id"]
    with transaction() as db:
        change(db.get(Record, task), max_reasoning_calls=0)
    p = DecisionProvider()
    execute(task, p)
    assert p.text_calls == 0 and run_state(client, d["run_id"])["reason"] == "CALL_LIMIT_EXHAUSTED"


@pytest.mark.parametrize("change_actor", ["inactive", "scope", "source"])
def test_worker_rechecks_permission_and_binding_before_text(client, managed_access, configured, change_actor):
    d = direct(client, managed_access).json()
    with transaction() as db:
        if change_actor == "inactive":
            db.get(models.DesignerUser, "alice").active = False
        elif change_actor == "scope":
            db.get(IndependentProject, d["project_id"]).scope_id = "foreign"
        else:
            change(head(db, d["project_id"]), current_prompt_id="new-source")
    p = DecisionProvider()
    with models.session() as db:
        task = db.get(Record, d["run_id"]).payload["text_task_id"]
    execute(task, p)
    assert p.text_calls == 0


def test_new_message_after_freeze_prevents_unsent_image(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    task = run_state(client, d["run_id"])["image_task_id"]
    assert turn(client, managed_access, d["project_id"], text="先不要生成").status_code == 201
    execute(task, p)
    assert p.calls == 0 and run_state(client, d["run_id"])["status"] == "superseded"
    with models.session() as db:
        assert (
            db.scalar(select(ExecutionReservation).where(ExecutionReservation.stage == "image")).status
            == "failed_not_sent"
        )


def test_late_image_preserves_frozen_input_without_overwriting_new_head(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    task = run_state(client, d["run_id"])["image_task_id"]
    original = p.render_direct

    def late(prompt, images):
        assert turn(client, managed_access, d["project_id"], text="先讨论材料").status_code == 201
        return original(prompt, images)

    p.render_direct = late
    execute(task, p)
    run = run_state(client, d["run_id"])
    assert run["status"] == "succeeded" and run["version_id"]
    w = state(client, d["project_id"])
    assert w["head"].get("version_id") is None and w["execution_brief_state"] == "needs_confirmation"
    assert w["versions"][0]["prompt_id"] == run["prompt_id"] and p.calls == 1


def test_fake_model_base_is_rejected_not_silently_new_image(client, managed_access, configured):
    d = direct(client, managed_access).json()
    decision = {
        "resolved_intent": "generate_image",
        "answer": "合成修改",
        "positive_prompt": "完整设计",
        "base_version_id": "invented-version",
        "edit_region": "鞋头",
    }
    p = DecisionProvider(decision=decision)
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["status"] == "needs_input" and run["reason"] == "CONTEXT_MISMATCH"
    assert not state(client, d["project_id"])["prompts"] and p.calls == 0


@pytest.mark.parametrize("case", ["count", "bounds"])
def test_canvas_limits_keep_successful_image_without_regeneration(client, managed_access, configured, case):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    data = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(data, "PNG")
    with transaction() as db:
        image = assets.save_original_image(data.getvalue())
        nodes = []
        for i in range(200 if case == "count" else 1):
            version = create(db, d["project_id"], "version", {"image": image, "synthetic": True}, "candidate")
            nodes.append(
                {
                    "id": f"old_{i}",
                    "kind": "version",
                    "ref_id": version.id,
                    "x": 100000 if case == "bounds" else i * 10,
                    "y": 1,
                    "width": 100,
                    "height": 100,
                }
            )
        create(
            db,
            d["project_id"],
            "canvas",
            {
                "schema_version": "design-canvas/1",
                "project_id": d["project_id"],
                "layout_revision": 3,
                "layout": {"nodes": nodes, "viewport": {"x": 3, "y": 2, "zoom": 1}},
            },
            "saved",
            id=f"canvas_{d['project_id']}",
        )
    execute(run_state(client, d["run_id"])["image_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["status"] == "succeeded" and run["canvas_placement"] == "limit_reached"
    assert run["version_id"] and p.calls == 1
    c = client.get(f"/api/project/{d['project_id']}/design-canvas").json()
    assert c["layout_revision"] == 3 and len(c["layout"]["nodes"]) == len(nodes)


def test_archived_completed_resources_and_history_are_retained_but_unreadable(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    execute(run_state(client, d["run_id"])["image_task_id"], p)
    run = run_state(client, d["run_id"])
    w = state(client, d["project_id"])
    body = {"expected_revision": w["revision"], "confirm": True}
    a = client.post(
        f"/api/design-projects/{d['project_id']}/archive",
        json=body,
        headers=headers(managed_access, "archive-finished-new-object"),
    )
    assert a.status_code == 200, a.text
    for path in [
        f"/api/design-creation-runs/{run['id']}",
        f"/api/design-tasks/{run['text_task_id']}",
        f"/api/design-tasks/{run['image_task_id']}",
        f"/api/design-versions/{run['version_id']}/image",
        f"/api/design-versions/{run['version_id']}/delivery",
    ]:
        assert client.get(path).status_code == 404
    with models.session() as db:
        version = db.get(Record, run["version_id"])
        assert assets.file_path(version.payload["image"]).read_bytes() == p.raw
        assert db.get(Record, run["prompt_id"]) and db.get(Record, run["spec_id"])
        assert len(list(db.scalars(select(ExecutionReservation)))) == 2


def test_open_sse_rejects_next_event_after_archive(client, managed_access):
    d = direct(client, managed_access, BLANK).json()
    pid = d["project_id"]

    async def collect():
        request = request_for(managed_access, path=f"/api/project/{pid}/design-events")
        stream = events.stream(pid, request, duration=3)
        assert (await anext(stream)).startswith("event: workspace")
        r = client.post(
            f"/api/design-projects/{pid}/archive",
            json={"expected_revision": 1, "confirm": True},
            headers=headers(managed_access, "archive-during-read-sse"),
        )
        assert r.status_code == 200
        return [x async for x in stream]

    result = asyncio.run(collect())
    assert len(result) == 1 and "NOT_FOUND" in result[0] and result[0].startswith("event: error")


def test_new_understand_task_obeys_call_and_cost_limits(managed_access):

    with transaction() as db:
        pid = managed_access["projects"]["alice"]["pid"]
        change(head(db, pid), spec_id="synthetic-spec")
        p = DecisionProvider()
        task = create(
            db,
            pid,
            "task",
            {
                "mode": "understand",
                "spec_id": "synthetic-spec",
                "provider": p.capabilities(),
                "reasoning_calls": 1,
                "image_calls": 0,
                "max_reasoning_calls": 1,
                "max_image_calls": 0,
                "max_cost_fen": 50,
                "reserved_cost_fen": 50,
                "steps": [],
            },
            "running",
        )
        with pytest.raises(AgentError, match="次数已用完"):
            runner.event(db, task, "plan", p)
        change(task, reasoning_calls=0, reserved_cost_fen=1)
        with pytest.raises(AgentError, match="费用上限"):
            runner.event(db, task, "plan", p)
        assert not task.payload["steps"]


@pytest.fixture
def next_grant(configured):
    g = json.loads(configured.read_text())
    g.pop("creation_action_id")
    g["binding_mode"] = "next_creation"
    configured.write_text(json.dumps(g))
    return configured


def test_natural_action_claim_and_pipeline_without_prearranged_id(client, managed_access, next_grant):
    before = client.get("/api/design-agent/capabilities").json()["direct_creation"]
    assert before["available"] and before["remaining"] == {"text_calls": 1, "image_calls": 1}
    action = "natural-browser-generated-action-449"
    r = direct(client, managed_access, action=action)
    assert r.status_code == 201 and r.json()["run_status"] == "queued", r.text
    d = r.json()
    assert direct(client, managed_access, action=action).content == r.content
    with models.session() as db:
        binding = db.get(CreationGrantBinding, "synthetic-direct-grant-001")
        assert binding.project_id == d["project_id"] and binding.creation_action_id == action
        assert binding.run_id == d["run_id"] and binding.intent_id == d["intent_id"]
        assert binding.grant_sha256 == execution_quota.sha256(
            execution_quota.canonical_bytes(json.loads(next_grant.read_text()))
        )
    assert not client.get("/api/design-agent/capabilities").json()["direct_creation"]["available"]
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    execute(run_state(client, d["run_id"])["image_task_id"], p)
    assert run_state(client, d["run_id"])["status"] == "succeeded" and p.text_calls == 1 and p.calls == 1


def test_next_grant_blank_discuss_and_blank_later_chat_do_not_claim(client, managed_access, next_grant):
    blank = direct(client, managed_access, BLANK, action="blank-non-consuming-next").json()
    discussion = direct(client, managed_access, {**BODY, "intent": "discuss"}, action="discussion-no-next-claim").json()
    assert discussion["run_status"] == "blocked"
    r = turn(client, managed_access, blank["project_id"], intent="generate_image", text=BODY["text"])
    assert run_state(client, r.json()["run_id"])["status"] == "blocked"
    with models.session() as db:
        assert not list(db.scalars(select(CreationGrantBinding)))
        assert not list(db.scalars(select(ExecutionReservation)))
    assert direct(client, managed_access, action="later-natural-creation").json()["run_status"] == "queued"


def test_next_grant_different_concurrent_actions_only_one_claim(client, managed_access, next_grant):
    def send(action):
        return direct(client, managed_access, action=action)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, ["natural-action-parallel-001", "natural-action-parallel-002"]))
    assert all(r.status_code == 201 for r in responses)
    data = [r.json() for r in responses]
    assert sorted(x["run_status"] for x in data) == ["blocked", "queued"]
    assert next(x for x in data if x["run_status"] == "blocked")["reason"] == "GRANT_BINDING_CONFLICT"
    with models.session() as db:
        assert len(list(db.scalars(select(CreationGrantBinding)))) == 1
        assert len(list(db.scalars(select(ExecutionReservation)))) == 1
        assert len(list(db.scalars(select(IndependentProject)))) == 2


@pytest.mark.parametrize("case", ["actor", "expired", "unready", "budget"])
def test_next_grant_ineligible_action_does_not_bind(client, managed_access, next_grant, monkeypatch, case):
    if case == "actor":
        alter(next_grant, subject="bob")
    elif case == "expired":
        alter(next_grant, expires_at=int(time.time()) - 1)
    elif case == "unready":
        monkeypatch.setenv("MODEL_API_KEY", "")
    else:
        alter(
            next_grant,
            stages={"text": {"max_calls": 1, "max_cost_fen": 50}, "image": {"max_calls": 1, "max_cost_fen": 49}},
        )
    d = direct(client, managed_access).json()
    assert d["run_status"] == "blocked"
    with models.session() as db:
        assert not list(db.scalars(select(CreationGrantBinding)))
        assert not list(db.scalars(select(ExecutionReservation)))


def test_next_grant_creation_rollback_includes_claim(client, managed_access, next_grant, monkeypatch):
    from app.agent.routes import EngineAction

    def fail(self, response):
        raise AgentError("INTERNAL_ERROR", "synthetic commit failure", 500)

    monkeypatch.setattr(EngineAction, "finish", fail)
    assert direct(client, managed_access).status_code == 500
    with models.session() as db:
        assert not list(db.scalars(select(CreationGrantBinding)))
        assert not list(db.scalars(select(IndependentProject)))


def test_next_grant_cancel_or_expiry_never_releases_binding(client, managed_access, next_grant):
    d = direct(client, managed_access).json()
    task = run_state(client, d["run_id"])["text_task_id"]
    assert (
        client.post(
            f"/api/design-tasks/{task}/cancel", json={}, headers=headers(managed_access, "cancel-next-claimed", 1)
        ).status_code
        == 200
    )
    second = direct(client, managed_access, action="another-new-after-cancel").json()
    assert second["run_status"] == "blocked" and second["reason"] == "GRANT_BINDING_CONFLICT"
    with transaction() as db:
        runner.recover(db)
    with models.session() as db:
        assert db.get(CreationGrantBinding, "synthetic-direct-grant-001").project_id == d["project_id"]


def test_bound_grant_cannot_expand_and_worker_refuses_changed_sha(client, managed_access, next_grant):
    d = direct(client, managed_access).json()
    alter(
        next_grant,
        stages={"text": {"max_calls": 2, "max_cost_fen": 100}, "image": {"max_calls": 2, "max_cost_fen": 100}},
    )
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    assert p.text_calls == 0 and run_state(client, d["run_id"])["status"] == "blocked"
    assert direct(client, managed_access, action="cannot-expand-old-next").json()["reason"] == "GRANT_BINDING_CONFLICT"


def test_committed_claim_without_reservation_cannot_retarget_fixed_modes(
    client, managed_access, next_grant, monkeypatch
):
    original = json.loads(next_grant.read_text())
    real_grant = execution_quota.grant
    reads = 0

    def expires_on_capability_reread():
        nonlocal reads
        reads += 1
        return None if reads == 2 else real_grant()

    with monkeypatch.context() as patch:
        patch.setattr(execution_quota, "grant", expires_on_capability_reread)
        claimed = direct(client, managed_access).json()
    assert claimed["run_status"] == "blocked"
    with models.session() as db:
        binding = db.get(CreationGrantBinding, original["grant_id"])
        assert binding.project_id == claimed["project_id"] and binding.run_id == claimed["run_id"]
        frozen_sha = binding.grant_sha256
        assert not list(db.scalars(select(ExecutionReservation)))

    target = managed_access["projects"]["alice"]["pid"]
    for mode in ("fixed_action", "fixed_project"):
        action = f"retarget-committed-claim-{mode}"
        changed = {**original, "binding_mode": mode}
        changed["stages"] = {
            "text": {"max_calls": 2, "max_cost_fen": 100},
            "image": {"max_calls": 2, "max_cost_fen": 100},
        }
        changed["creation_action_id" if mode == "fixed_action" else "project_id"] = (
            action if mode == "fixed_action" else target
        )
        next_grant.write_text(json.dumps(changed))
        assert execution_quota.grant() == changed  # Valid file, rejected because the ID is already frozen.
        assert not client.get("/api/design-agent/capabilities").json()["direct_creation"]["available"]
        if mode == "fixed_action":
            result = direct(client, managed_access, action=action).json()
            assert result["run_status"] == "blocked"
        else:
            response = turn(client, managed_access, target, action=action)
            assert response.status_code == 201, response.text
            result = run_state(client, response.json()["run_id"])
            assert result["status"] == "blocked"
        assert result["reason"] == "CREATION_AUTHORIZATION_REQUIRED"
    with models.session() as db:
        binding = db.get(CreationGrantBinding, original["grant_id"])
        assert binding.grant_sha256 == frozen_sha and binding.project_id == claimed["project_id"]
        assert not list(db.scalars(select(ExecutionReservation)))


def test_explicit_design_draft_survives_model_freeze(client, managed_access, configured):
    d = direct(client, managed_access, {**BODY, "output_kind": "design_draft"}).json()
    p = DecisionProvider()
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    assert p.context["output_kind"] == "design_draft"
    with models.session() as db:
        intent = db.get(Record, d["intent_id"])
        run = db.get(Record, d["run_id"])
        prompt = db.get(Record, run.payload["prompt_id"])
        brief = db.get(Record, prompt.payload["prompt"]["source_brief_id"])
        spec = db.get(Record, run.payload["spec_id"])
        assert intent.payload["intent"]["output_kind"] == run.payload["output_kind"] == "design_draft"
        assert prompt.payload["prompt"]["output_kind"] == brief.payload["brief"]["output_kind"] == "design_draft"
        assert spec.payload["output_kind"] == "design_draft"
    assert p.text_calls == 1 and p.calls == 0


def test_generation_text_message_cannot_claim_unfinished_image(client, managed_access, configured):
    d = direct(client, managed_access).json()
    p = DecisionProvider()
    p.decision["answer"] = "我已经生成了展示板。请查看设计效果。"
    execute(run_state(client, d["run_id"])["text_task_id"], p)
    run = run_state(client, d["run_id"])
    assert run["status"] == "running" and run["stage"] == "image"
    with models.session() as db:
        assistant = next(
            r
            for r in db.scalars(select(Record).where(Record.project_id == d["project_id"], Record.kind == "message"))
            if r.payload.get("role") == "assistant"
        )
        assert "已经生成" not in assistant.payload["text"] and "请查看设计效果" not in assistant.payload["text"]
        assert "尚未完成" in assistant.payload["text"]
        assert db.get(Record, run["text_task_id"]).payload["outcome"] == p.decision["answer"]
    assert p.text_calls == 1 and p.calls == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"binding_mode": "fixed_project"},
        {"binding_mode": "fixed_action"},
        {"binding_mode": "next_creation", "project_id": 1},
        {"binding_mode": "next_creation", "creation_action_id": ACTION},
        {"binding_mode": "all_users"},
    ],
)
def test_grant_mode定位字段_strict(next_grant, patch):
    alter(next_grant, **patch)
    assert execution_quota.grant() is None


def test_fixed_modes_compatible_and_provider_slot_is_persistent_single_dispatch(managed_access, configured):
    g = execution_quota.grant()
    assert execution_quota.binding_mode(g) == "fixed_action"
    alter(configured, binding_mode="fixed_action")
    assert execution_quota.grant()
    g = json.loads(configured.read_text())
    g.pop("creation_action_id")
    g.update(binding_mode="fixed_project", project_id=managed_access["projects"]["alice"]["pid"])
    configured.write_text(json.dumps(g))
    assert execution_quota.grant()

    def reserve(attempt):
        try:
            with transaction() as db:
                execution_quota.acquire_slot(db, attempt)
            return "sent"
        except AgentError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["synthetic-text-attempt", "synthetic-image-attempt"]))
    assert sorted(results) == ["PROVIDER_BUSY", "sent"]
    with models.session() as db:
        assert db.get(ProviderSlot, 1).status == "sent"
