"""design-workspace/1.0.1 on disposable synthetic databases, zero external calls."""

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image
from sqlalchemy import select
from test_dual_entry import assume, headers, new_project
from test_dual_entry import client as client
from test_dual_entry import managed_access as managed_access

from app import config, models
from app.agent import access_context, assets, routes, runner
from app.agent.managed_store import IndependentProject, ManagedAction, ManagedAudit, canonical_bytes, sha256
from app.agent.providers import Provider
from app.agent.store import AgentError, Record, create, head, transaction
from app.design_auth import session_context, token_hash


@pytest.fixture
def owned(client, managed_access):
    created = new_project(client, managed_access).json()
    pid = created["project_id"]
    raw = io.BytesIO()
    Image.new("RGB", (64, 128), "blue").save(raw, "PNG")
    with transaction() as db:
        image = assets.save_image(raw.getvalue())
        asset = create(db, pid, "asset", {**image, "name": "合成参考图"}, "ready")
        version = create(
            db,
            pid,
            "version",
            {"image": image, "synthetic": True, "spec_id": head(db, pid).payload["spec_id"]},
            "candidate",
        )
        second = create(db, pid, "asset", {**assets.save_image(raw.getvalue()), "name": "合成第二参考图"}, "ready")
    return {**created, "asset_id": asset.id, "version_id": version.id, "other_asset_id": second.id}


def snapshot(client, owned):
    return client.get(f"/api/project/{owned['project_id']}/design-workspace").json()


def layout(owned, revision=0):
    return {
        "schema_version": "design-canvas/1",
        "expected_layout_revision": revision,
        "layout": {
            "nodes": [
                {
                    "id": "node_01",
                    "kind": "version",
                    "ref_id": owned["version_id"],
                    "x": 12,
                    "y": -20,
                    "width": 100,
                    "height": 200,
                }
            ],
            "viewport": {"x": 24, "y": 48, "zoom": 1.25},
        },
    }


def canvas(client, fixture, owned, data=None, action="layout-save-001", **extra):
    return client.put(
        f"/api/project/{owned['project_id']}/design-canvas",
        json=data or layout(owned),
        headers=headers(fixture, action, **extra),
    )


def message(client, fixture, owned, action="discussion-message-001", **overrides):
    state = snapshot(client, owned)
    body = {
        "text": "讨论原版图，先不要生成",
        "expected_spec_id": state["head"].get("spec_id"),
        "expected_prompt_id": state["current_prompt_id"],
        "selected_context": {"version_id": owned["version_id"]},
        "idempotency_key": action,
        **overrides,
    }
    return client.post(
        f"/api/project/{owned['project_id']}/design-messages",
        json=body,
        headers=headers(fixture, action, state["revision"]),
    )


def brief_body(client, owned, **overrides):
    state = snapshot(client, owned)
    return {
        "expected_prompt_id": state["current_prompt_id"],
        "expected_spec_id": state["head"].get("spec_id"),
        "message_id": None,
        "selected_context": None,
        "positive_prompt": "人工整理：浅蓝轻便鞋履",
        "avoid_items": ["厚底"],
        "output_kind": "effect_image",
        "base_version_id": None,
        "edit_region": "",
        **overrides,
    }


def brief(client, fixture, owned, action="manual-brief-save-001", **overrides):
    state = snapshot(client, owned)
    return client.post(
        f"/api/project/{owned['project_id']}/design-briefs",
        json=brief_body(client, owned, **overrides),
        headers=headers(fixture, action, state["revision"]),
    )


def confirmation_body(candidate):
    return {
        "expected_prompt_id": candidate["based_on_prompt_id"],
        "expected_spec_id": candidate["based_on_spec_id"],
        "candidate_id": candidate["id"],
        **{
            k: candidate[k] for k in ("positive_prompt", "avoid_items", "output_kind", "base_version_id", "edit_region")
        },
    }


def confirm(client, fixture, owned, candidate, action="confirm-brief-save-001", **overrides):
    state = snapshot(client, owned)
    return client.post(
        f"/api/project/{owned['project_id']}/design-prompts",
        json={**confirmation_body(candidate), **overrides},
        headers=headers(fixture, action, state["revision"]),
    )


def image_attempt(client, fixture, owned, action="guard-image-attempt-001"):
    state = snapshot(client, owned)
    return client.post(
        f"/api/project/{owned['project_id']}/design-text-to-image",
        json={"prompt_id": state["current_prompt_id"], "idempotency_key": action, "authorized": True},
        headers=headers(fixture, action, state["revision"]),
    )


def test_layout_defaults_readonly_save_replay_restore_and_business_unchanged(client, managed_access, owned):
    before = snapshot(client, owned)
    listing = client.get("/api/design-projects").json()
    path = f"/api/project/{owned['project_id']}/design-canvas"
    default = client.get(path)
    assert default.status_code == 200 and default.json()["layout_revision"] == 0
    assert default.json()["updated_at"] is None
    with models.session() as db:
        assert db.get(Record, f"canvas_{owned['project_id']}") is None
    saved = canvas(client, managed_access, owned)
    assert saved.status_code == 200, saved.text
    assert saved.json()["layout_revision"] == 1 and saved.json()["base_layout_revision"] == 0
    assert saved.json()["revision_domain"] == "layout" and "revision" not in saved.json()
    assert "X-Design-Revision" not in saved.headers
    assert saved.headers["cache-control"] == "private, no-store"
    assert canvas(client, managed_access, owned).content == saved.content
    assert snapshot(client, owned) == before
    assert client.get("/api/design-projects").json() == listing
    recovery = client.get("/api/design-operations/layout-save-001").json()
    assert recovery["revision_domain"] == "layout" and recovery["result"] == saved.json()
    models.reset_engine_for_tests()
    document = client.get(path).json()
    assert document["layout"] == saved.json()["layout"] and document["layout_revision"] == 1
    assert canvas(client, managed_access, owned).content == saved.content
    with models.session() as db:
        assert db.get(IndependentProject, owned["project_id"]).revision == 1
        assert len(list(db.scalars(select(ManagedAudit).where(ManagedAudit.kind == "layout_mutation")))) == 1


def test_layout_changed_bytes_target_and_old_revision_reject_without_overwrite(client, managed_access, owned):
    saved = canvas(client, managed_access, owned)
    changed = layout(owned)
    changed["layout"]["viewport"]["x"] += 1
    assert canvas(client, managed_access, owned, changed).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    other = canvas(client, managed_access, owned, action="other-layout-save-001")
    assert other.status_code == 409 and other.json()["error"]["code"] == "LAYOUT_CONFLICT"
    same = client.put(
        f"/api/project/{owned['project_id']}/design-canvas?changed=1",
        json=layout(owned),
        headers=headers(managed_access, "layout-save-001"),
    )
    assert same.status_code == 409 and same.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert canvas(client, managed_access, owned).content == saved.content


@pytest.mark.parametrize(
    "change",
    [
        "bool",
        "nan",
        "infinity",
        "zero_zoom",
        "zoom_over",
        "size",
        "position",
        "node_extra",
        "layout_extra",
        "viewport_extra",
        "body_extra",
        "duplicate_id",
        "duplicate_ref",
        "too_many",
        "schema",
        "revision_bool",
        "bad_id",
        "blank_ref",
    ],
)
def test_layout_strict_boundaries(client, managed_access, owned, change):
    data = layout(owned)
    node, view = data["layout"]["nodes"][0], data["layout"]["viewport"]
    if change in {"bool", "nan", "infinity"}:
        node["x"] = {"bool": True, "nan": float("nan"), "infinity": float("inf")}[change]
    elif change == "zero_zoom":
        view["zoom"] = 0
    elif change == "zoom_over":
        view["zoom"] = 4.01
    elif change == "size":
        node["width"] = 23
    elif change == "position":
        node["y"] = -100001
    elif change == "node_extra":
        node["url"] = "https://example.test/fake.png"
    elif change == "layout_extra":
        data["layout"]["selected"] = node["id"]
    elif change == "viewport_extra":
        view["secret"] = True
    elif change == "body_extra":
        data["owner"] = "bob"
    elif change == "duplicate_id":
        data["layout"]["nodes"].append({**node, "kind": "asset", "ref_id": owned["asset_id"]})
    elif change == "duplicate_ref":
        data["layout"]["nodes"].append({**node, "id": "another"})
    elif change == "too_many":
        data["layout"]["nodes"] *= 201
    elif change == "schema":
        data["schema_version"] = "tldraw/arbitrary"
    elif change == "revision_bool":
        data["expected_layout_revision"] = False
    elif change == "bad_id":
        node["id"] = "<script>"
    elif change == "blank_ref":
        node["ref_id"] = " "
    response = client.put(
        f"/api/project/{owned['project_id']}/design-canvas",
        content=json.dumps(data).encode(),
        headers={**headers(managed_access, "layout-invalid-001"), "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    with models.session() as db:
        assert db.get(Record, f"canvas_{owned['project_id']}") is None


def test_layout_concurrent_saves_one_commit(client, managed_access, owned):
    barrier = Barrier(2)

    def write(i):
        barrier.wait(timeout=5)
        return canvas(client, managed_access, owned, action=f"parallel-layout-{i}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert client.get(f"/api/project/{owned['project_id']}/design-canvas").json()["layout_revision"] == 1


def test_layout_transaction_failure_rolls_back_all(client, managed_access, owned, monkeypatch):
    original = routes.EngineAction.finish

    def fail(operation, response):
        if operation.layout:
            raise AgentError("INTERNAL_ERROR", "合成提交失败", 500)
        return original(operation, response)

    monkeypatch.setattr(routes.EngineAction, "finish", fail)
    assert canvas(client, managed_access, owned).status_code == 500
    with models.session() as db:
        assert db.get(Record, f"canvas_{owned['project_id']}") is None
        assert not list(db.scalars(select(ManagedAction).where(ManagedAction.action_id == "layout-save-001")))
        assert not list(db.scalars(select(ManagedAudit).where(ManagedAudit.kind == "layout_mutation")))


@pytest.mark.parametrize("change", ["cross_project", "non_image", "missing"])
def test_layout_object_authorization(client, managed_access, owned, change):
    data = layout(owned)
    node = data["layout"]["nodes"][0]
    node["kind"] = "asset"
    node["ref_id"] = (
        managed_access["projects"]["bob"]["asset"]
        if change == "cross_project"
        else (managed_access["projects"]["alice"]["asset"] if change == "non_image" else "missing")
    )
    assert canvas(client, managed_access, owned, data).status_code == 404


@pytest.mark.parametrize("identity,status", [("bob", 404), ("lead", 403), ("anonymous", 401)])
def test_layout_personnel_permissions(client, managed_access, owned, identity, status):
    if identity == "anonymous":
        client.cookies.clear()
    else:
        assume(client, managed_access, identity)
    response = canvas(client, managed_access, owned, username="alice" if identity == "anonymous" else identity)
    assert response.status_code == status


@pytest.mark.parametrize("status", ["review", "approved"])
def test_upstream_readonly_layout_and_new_business_routes(client, managed_access, status):
    pid = managed_access["projects"]["alice"]["pid"]
    with models.session() as db, db.begin():
        db.get(models.DesignHandoff, "synthetic-alice").status = status
    assert client.get(f"/api/project/{pid}/design-canvas").json()["layout_revision"] == 0
    for method, route in [("put", "canvas"), ("post", "messages"), ("post", "briefs"), ("post", "prompts")]:
        data = (
            {
                "schema_version": "design-canvas/1",
                "expected_layout_revision": 0,
                "layout": {"nodes": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            }
            if route == "canvas"
            else {}
        )
        response = getattr(client, method)(
            f"/api/project/{pid}/design-{route}", json=data, headers=headers(managed_access, f"readonly-{route}-001", 1)
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "STATE_CONFLICT"
    assert snapshot(client, {"project_id": pid})["project_context"]["allowed_actions"] == []


def test_message_freezes_context_sets_confirmation_and_never_changes_prompt_spec(client, managed_access, owned):
    before = snapshot(client, owned)
    saved = message(client, managed_access, owned)
    assert saved.status_code == 201, saved.text
    data = saved.json()
    assert data["task_id"] is None and data["brief_id"] is None and data["reply_state"] == "unavailable"
    assert data["reason"] == "CAPABILITY_UNAVAILABLE"
    assert data["selected_context"] == {"version_id": owned["version_id"], "asset_id": owned["asset_id"]}
    state = snapshot(client, owned)
    assert state["head"]["spec_id"] == before["head"]["spec_id"]
    assert state["current_prompt_id"] == before["current_prompt_id"]
    assert state["prompts"] == before["prompts"] and state["specs"] == before["specs"]
    assert state["latest_user_message_id"] == data["id"] and state["execution_brief_state"] == "needs_confirmation"
    assert [m["role"] for m in state["messages"]] == ["user", "system"]
    assert state["messages"][-1]["reply_to"] == data["id"]
    assert state["tasks"] == [] and state["briefs"] == []
    rejected = image_attempt(client, managed_access, owned)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "EXECUTION_BRIEF_CONFLICT"
    models.reset_engine_for_tests()
    assert snapshot(client, owned) == state
    recovered = client.get("/api/design-operations/discussion-message-001").json()["result"]
    assert recovered == data


@pytest.mark.parametrize(
    "context,code,status",
    [
        ({"version_id": "missing"}, "NOT_FOUND", 404),
        ({"asset_id": "missing"}, "NOT_FOUND", 404),
        ({}, "INVALID_INPUT", 422),
        ({"version_id": "OWN", "asset_id": "OTHER"}, "CONTEXT_MISMATCH", 422),
        ({"version_id": "OWN", "unknown": True}, "INVALID_INPUT", 422),
    ],
)
def test_message_context_validation(client, managed_access, owned, context, code, status):
    context = {
        k: owned["version_id"] if v == "OWN" else owned["other_asset_id"] if v == "OTHER" else v
        for k, v in context.items()
    }
    rejected = message(client, managed_access, owned, selected_context=context)
    assert rejected.status_code == status and rejected.json()["error"]["code"] == code
    assert snapshot(client, owned)["messages"] == []


def test_message_required_heads_action_and_cross_project_asset(client, managed_access, owned):
    base = {"text": "合成消息", "expected_spec_id": None, "idempotency_key": "message-missing-head-001"}
    path = f"/api/project/{owned['project_id']}/design-messages"
    assert client.post(path, json=base, headers=headers(managed_access, base["idempotency_key"], 1)).status_code == 422
    assert (
        message(client, managed_access, owned, expected_prompt_id="stale").json()["error"]["code"] == "PROMPT_CONFLICT"
    )
    assert message(client, managed_access, owned, expected_spec_id=None).json()["error"]["code"] == "STALE_SOURCE"
    assert message(client, managed_access, owned, idempotency_key="different-action-001").status_code == 422
    assert (
        message(
            client, managed_access, owned, selected_context={"asset_id": managed_access["projects"]["bob"]["asset"]}
        ).status_code
        == 404
    )


def test_provider_config_and_client_flag_cannot_enable_unapproved_text(client, managed_access, owned, monkeypatch):
    monkeypatch.setenv("DESIGN_PAID_PROVIDERS_ENABLED", "true")
    config.get_config.cache_clear()
    monkeypatch.setattr(Provider, "capabilities", lambda _: {"understand": True, "image_only": False})
    saved = message(client, managed_access, owned, authorized=True)
    assert saved.status_code == 201 and saved.json()["reply_state"] == "unavailable"
    assert snapshot(client, owned)["tasks"] == []


def test_candidate_immutable_confirmation_binds_spec_and_latest_message(client, managed_access, owned):
    discussion = message(client, managed_access, owned).json()
    before = snapshot(client, owned)
    response = brief(client, managed_access, owned, message_id=discussion["id"])
    assert response.status_code == 201, response.text
    candidate = response.json()["brief"]
    assert candidate["origin"] == "manual" and candidate["model_task_id"] is None
    assert candidate["source_binding_digest"] is None and candidate["based_on_latest_message_id"] == discussion["id"]
    assert candidate["status"] == "candidate"
    assert candidate["digest"] == sha256(
        canonical_bytes({k: v for k, v in candidate.items() if k not in {"digest", "status"}})
    )
    state = snapshot(client, owned)
    assert state["head"] == before["head"] and state["specs"] == before["specs"]
    bad = confirm(client, managed_access, owned, candidate, positive_prompt="内容变化")
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "BRIEF_CONFLICT"
    confirmed = confirm(client, managed_access, owned, candidate)
    assert confirmed.status_code == 201, confirmed.text
    result = confirmed.json()
    state = snapshot(client, owned)
    assert state["head"]["spec_id"] == result["spec_id"]
    assert state["head"]["current_prompt_id"] == result["prompt"]["id"]
    assert result["source_brief_id"] == candidate["id"] == result["prompt"]["source_brief_id"]
    assert result["prompt"]["confirmed_through_message_id"] == discussion["id"]
    assert state["execution_brief_state"] == "ready" and state["briefs"][0]["status"] == "confirmed"
    with models.session() as db:
        prompt = db.get(Record, result["prompt"]["id"])
        spec = db.get(Record, result["spec_id"])
        assert prompt.payload["spec_id"] == spec.id and spec.status == "confirmed"
        assert spec.payload["spec"]["intent"] == candidate["positive_prompt"]
        assert any(c["text"] == "厚底" and c["kind"] == "forbidden" for c in spec.payload["spec"]["constraints"])
        assert db.get(Record, candidate["id"]).payload["brief"] == {k: v for k, v in candidate.items() if k != "status"}
    models.reset_engine_for_tests()
    recovered = client.get("/api/design-operations/confirm-brief-save-001").json()["result"]
    assert recovered == result
    next_message = message(client, managed_access, owned, action="discussion-message-002")
    assert next_message.status_code == 201 and snapshot(client, owned)["execution_brief_state"] == "needs_confirmation"
    assert image_attempt(client, managed_access, owned).json()["error"]["code"] == "EXECUTION_BRIEF_CONFLICT"


def test_candidate_stale_on_new_message_without_head_drift(client, managed_access, owned):
    candidate = brief(client, managed_access, owned).json()["brief"]
    assert message(client, managed_access, owned).status_code == 201
    rejected = confirm(client, managed_access, owned, candidate)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "BRIEF_CONFLICT"
    assert snapshot(client, owned)["briefs"][0]["status"] == "candidate"


@pytest.mark.parametrize("change", ["message", "base", "region", "origin", "blank", "avoid_long"])
def test_candidate_input_and_identity(client, managed_access, owned, change):
    overrides = {
        "message": {"message_id": "missing"},
        "base": {"base_version_id": owned["version_id"], "edit_region": "鞋面"},
        "region": {"base_version_id": owned["version_id"], "edit_region": ""},
        "origin": {"origin": "model"},
        "blank": {"positive_prompt": " "},
        "avoid_long": {"avoid_items": ["字" * 10001]},
    }[change]
    response = brief(client, managed_access, owned, **overrides)
    assert response.status_code == (404 if change == "message" else 422)
    assert snapshot(client, owned)["briefs"] == []


def test_edit_candidate_tracks_base_context_and_confirmation(client, managed_access, owned):
    candidate = brief(
        client,
        managed_access,
        owned,
        selected_context={"version_id": owned["version_id"]},
        base_version_id=owned["version_id"],
        edit_region="鞋面颜色",
    ).json()["brief"]
    assert candidate["selected_context"]["asset_id"] == owned["asset_id"]
    confirmed = confirm(client, managed_access, owned, candidate).json()
    assert confirmed["prompt"]["base_version_id"] == owned["version_id"]
    spec = next(s for s in snapshot(client, owned)["specs"] if s["id"] == confirmed["spec_id"])
    assert spec["spec"]["base_version_id"] == owned["version_id"] and spec["spec"]["edit_region"] == "鞋面颜色"


@pytest.mark.parametrize("drift", ["head", "confirmed", "intent", "source"])
def test_execution_guard_rejects_inconsistent_source_before_quota_or_provider(client, managed_access, owned, drift):
    with models.session() as db, db.begin():
        h = head(db, owned["project_id"])
        spec = db.get(Record, h.payload["spec_id"])
        if drift == "head":
            h.payload = {**h.payload, "spec_id": "missing-spec"}
        elif drift == "confirmed":
            spec.status = "draft"
        elif drift == "intent":
            spec.payload = {**spec.payload, "spec": {**spec.payload["spec"], "intent": "异内容"}}
        else:
            prompt = db.get(Record, h.payload["current_prompt_id"])
            prompt.payload = {**prompt.payload, "source_identity_digest": "f" * 64}
    rejected = image_attempt(client, managed_access, owned)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "EXECUTION_BRIEF_CONFLICT"
    assert snapshot(client, owned)["tasks"] == []


def test_layout_can_save_after_business_revision_without_conflict(client, managed_access, owned):
    assert canvas(client, managed_access, owned).status_code == 200
    assert message(client, managed_access, owned).status_code == 201
    assert canvas(client, managed_access, owned, layout(owned, 1), action="layout-after-message-001").status_code == 200
    assert snapshot(client, owned)["revision"] == 2


def test_old_session_can_read_recovery_but_not_replay_write(client, managed_access, owned):
    assert canvas(client, managed_access, owned).status_code == 200
    token = "synthetic-new-conversation-session"
    with models.session() as db, db.begin():
        db.add(
            models.DesignerSession(token_hash=token_hash(token), username="alice", expires_at=int(time.time()) + 3600)
        )
    client.cookies.clear()
    client.cookies.set(config.get_config().design_session_cookie, token)
    assert client.get("/api/design-operations/layout-save-001").status_code == 200
    response = client.put(
        f"/api/project/{owned['project_id']}/design-canvas",
        json=layout(owned),
        headers={"X-Design-Action-Id": "layout-save-001", "X-Design-Context": session_context(token_hash(token))},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "AUTH_CONTEXT_CHANGED"


def test_body_limit_rejected_without_layout(client, managed_access, owned):
    response = client.put(
        f"/api/project/{owned['project_id']}/design-canvas",
        content=b" " * (256 * 1024 + 1),
        headers=headers(managed_access, "oversize-layout-001"),
    )
    assert response.status_code == 413
    assert client.get(f"/api/project/{owned['project_id']}/design-canvas").json()["layout_revision"] == 0


def test_original_generated_inline_asset_resolves_without_materializing_asset(client, managed_access, owned):
    with transaction() as db:
        version = db.get(Record, owned["version_id"])
        image = {**version.payload["image"], "asset_id": version.payload["image"]["file"].removesuffix(".png")}
        version.payload = {**version.payload, "image": image}
        db.delete(db.get(Record, owned["asset_id"]))
    response = message(client, managed_access, owned)
    assert response.status_code == 201 and response.json()["selected_context"]["asset_id"] == image["asset_id"]
    with models.session() as db:
        assert db.get(Record, image["asset_id"]) is None


def test_session_expiry_at_layout_commit_rolls_back(client, managed_access, owned, monkeypatch):
    original = routes.workbench.save_canvas

    def expired(db, pid, data):
        result = original(db, pid, data)
        monkeypatch.setattr(access_context.time, "time", lambda: time.monotonic() + 10**12)
        return result

    monkeypatch.setattr(routes.workbench, "save_canvas", expired)
    assert canvas(client, managed_access, owned).status_code == 401
    with models.session() as db:
        assert db.get(Record, f"canvas_{owned['project_id']}") is None
        assert not list(db.scalars(select(ManagedAction).where(ManagedAction.action_id == "layout-save-001")))


def test_confirmation_failure_rolls_back_prompt_spec_relation_and_head(client, managed_access, owned, monkeypatch):
    candidate = brief(client, managed_access, owned).json()["brief"]
    before = snapshot(client, owned)
    original = routes.EngineAction.finish

    def fail(operation, response):
        if operation.request.url.path.endswith("design-prompts"):
            raise AgentError("INTERNAL_ERROR", "合成确认事务失败", 500)
        return original(operation, response)

    monkeypatch.setattr(routes.EngineAction, "finish", fail)
    assert confirm(client, managed_access, owned, candidate).status_code == 500
    assert snapshot(client, owned) == before


def test_layout_replay_loses_access_after_scope_change(client, managed_access, owned):
    assert canvas(client, managed_access, owned).status_code == 200
    with models.session() as db, db.begin():
        db.get(IndependentProject, owned["project_id"]).scope_id = "different-scope"
    assert canvas(client, managed_access, owned).status_code == 404
    assert client.get("/api/design-operations/layout-save-001").status_code == 404


def test_upstream_candidates_freeze_receipt_digest_and_hard_constraints(client, managed_access):
    from app.agent.managed_protocol import decode_delivery
    from app.agent.managed_store import ManagedReceipt
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
        row.package_id, row.version = decoded["package"]["package_id"], decoded["package"]["version"]
        meta = db.get(ManagedReceipt, row.id)
        meta.package_id, meta.package_version = row.package_id, row.version
        meta.proof = {k: decoded[k] for k in ("package", "requirement", "prompt")}
        meta.receipt = {**meta.receipt, "content_digest": decoded["delivery"]["content_digest"]}
    owned = {"project_id": pid}
    assert brief(client, managed_access, owned).json()["error"]["code"] == "SOURCE_CONSTRAINT_CONFLICT"
    candidate = brief(
        client,
        managed_access,
        owned,
        positive_prompt=decoded["prompt"]["positive_prompt"],
        avoid_items=decoded["prompt"]["avoid_items"],
    ).json()["brief"]
    assert candidate["source_binding_digest"] == decoded["delivery"]["content_digest"]
    confirmed = confirm(client, managed_access, owned, candidate)
    assert confirmed.status_code == 201, confirmed.text
    with models.session() as db:
        spec = db.get(Record, confirmed.json()["spec_id"])
        assert spec.payload["product_source"]["prompt"] == decoded["prompt"]
        assert spec.payload["product_source"]["requirement"] == decoded["requirement"]


def test_mock_model_proposal_is_candidate_and_does_not_advance_execution_head(
    client, managed_access, owned, monkeypatch, tmp_path
):
    # Offline transaction coverage only; this is explicitly not real model evidence.
    path = tmp_path / "synthetic-conversation-authorization.json"
    cfg = config.get_config()
    path.write_text(
        json.dumps(
            {
                "authorized": True,
                "grant_id": "synthetic-conversation-001",
                "project_id": owned["project_id"],
                "subject": "alice",
                "scope_id": cfg.design_scope_id,
                "target_instance_id": cfg.design_instance_id,
                "expires_at": int(time.time()) + 600,
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv("DESIGN_PAID_PROVIDERS_ENABLED", "true")
    monkeypatch.setenv("DESIGN_CONVERSATION_AUTHORIZATION_FILE", str(path))
    config.get_config.cache_clear()
    caps = Provider().capabilities()
    monkeypatch.setattr(Provider, "capabilities", lambda _: {**caps, "understand": True})
    sent = message(client, managed_access, owned, selected_context=None, authorized=True)
    assert sent.status_code == 201 and sent.json()["reply_state"] == "queued"
    before = snapshot(client, owned)
    task_id = sent.json()["task_id"]
    with transaction() as db:
        task = db.get(Record, task_id)
        task.status = "running"
        spec = db.get(Record, task.payload["spec_id"]).payload["spec"]
        task.payload = {
            **task.payload,
            "steps": [{"id": "synthetic-plan-only", "tool": "plan", "status": "done", "receipt": {"synthetic": True}}],
            "pending_action": {
                "action": "propose_spec",
                "summary": "合成候选逻辑测试",
                "answer": "",
                "questions": [],
                "proposed_spec": {**spec, "intent": "人工测试的合成模型提案"},
            },
        }
    # propose_spec is a local tool after an already-journaled synthetic plan; _post is still forbidden.
    assert runner.one_step(task_id, Provider()) is False
    state = snapshot(client, owned)
    assert state["head"] == before["head"] and state["current_prompt_id"] == before["current_prompt_id"]
    assert len(state["briefs"]) == 1 and state["briefs"][0]["origin"] == "model"
    assert state["briefs"][0]["model_task_id"] == task_id
    assert state["execution_brief_state"] == "needs_confirmation"
    confirmed = confirm(client, managed_access, owned, state["briefs"][0])
    assert confirmed.status_code == 201, confirmed.text
