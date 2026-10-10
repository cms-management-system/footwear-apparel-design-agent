"""2.0.3 operating policy; isolated mock providers and no real provider POST."""

import io
import json

import pytest
from PIL import Image
from sqlalchemy import select
from test_direct_create import BLANK, BODY, direct, execute, run_state, turn
from test_direct_create import DecisionProvider as SingleRunProvider
from test_direct_create import client as client
from test_direct_create import configured as configured
from test_direct_create import managed_access as managed_access
from test_dual_entry import assume, headers

from app import config, models
from app.agent import interactive_policy
from app.agent.managed_store import ExecutionContextPolicy, ExecutionReservation, InteractiveGrant
from app.agent.store import AgentError, Record, transaction
from app.design_auth import session_context, token_hash


class DecisionProvider(SingleRunProvider):
    """Check the current sent stage rather than assuming only one historical run exists."""

    def creation_decision(self, context, images):
        self.text_calls += 1
        self.context = context
        with models.session() as db:
            assert (
                len(
                    list(
                        db.scalars(
                            select(ExecutionReservation).where(
                                ExecutionReservation.stage == "text", ExecutionReservation.status == "sent"
                            )
                        )
                    )
                )
                == 1
            )
        self.last_receipt = {
            "model": "synthetic-response-gemini",
            "usage": {"total_tokens": 13},
            "actual_cost_fen": None,
        }
        return self.decision

    def render_direct(self, prompt, images):
        self.calls += 1
        self.prompt = prompt
        with models.session() as db:
            assert (
                len(
                    list(
                        db.scalars(
                            select(ExecutionReservation).where(
                                ExecutionReservation.stage == "image", ExecutionReservation.status == "sent"
                            )
                        )
                    )
                )
                == 1
            )
        target = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(target, "PNG")
        self.raw = target.getvalue()
        return self.raw


@pytest.fixture
def operating(configured, monkeypatch, tmp_path):
    cfg = config.get_config()
    p = tmp_path / "operating-policy.json"
    data = {
        "policy_id": "synthetic_interactive_v1",
        "mode": "interactive_local",
        "enabled": True,
        "scope_id": cfg.design_scope_id,
        "target_instance_id": cfg.design_instance_id,
        "allowed_roles": ["designer"],
        "per_intent": {"text": {"max_calls": 1, "max_cost_fen": 50}, "image": {"max_calls": 1, "max_cost_fen": 50}},
        "max_active_runs_per_subject": 1,
    }
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    monkeypatch.setenv("DESIGN_INTERACTIVE_POLICY_FILE", str(p))
    monkeypatch.setenv("DESIGN_CREATION_AUTHORIZATION_FILE", "")
    return p


def rows(kind):
    with models.session() as db:
        return list(db.scalars(select(kind)))


def complete(client, data):
    p = DecisionProvider()
    execute(run_state(client, data["run_id"])["text_task_id"], p)
    execute(run_state(client, data["run_id"])["image_task_id"], p)
    assert run_state(client, data["run_id"])["status"] == "succeeded"
    return p


def test_two_creations_multi_turns_no_refresh_or_replay_grants(client, managed_access, operating):
    first = direct(client, managed_access, action="operating-first-action").json()
    assert first["run_status"] == "queued"
    p = complete(client, first)
    second = direct(client, managed_access, action="operating-second-action").json()
    q = complete(client, second)
    for i in range(2):
        response = turn(
            client, managed_access, first["project_id"], action=f"operating-discussion-{i}", intent="discuss"
        )
        assert response.status_code == 201
        data = response.json()
        discussion = DecisionProvider(resolved="discuss")
        execute(run_state(client, data["run_id"])["text_task_id"], discussion)
        run = run_state(client, data["run_id"])
        assert run["status"] == "succeeded" and run["image_task_id"] is None
        assert discussion.text_calls == 1 and discussion.calls == 0
    assert len(rows(InteractiveGrant)) == 4
    for _ in range(2):
        assert direct(client, managed_access, action="operating-first-action").json() == first
        assert client.get("/api/design-agent/capabilities").json()["direct_creation"]["remaining"] == {
            "text_calls": 1,
            "image_calls": 1,
        }
        assert client.get(f"/api/project/{first['project_id']}/design-workspace").status_code == 200
    assert len(rows(InteractiveGrant)) == 4 and p.calls == q.calls == 1
    assert [g.payload["authorization"]["stages"]["image"]["max_calls"] for g in rows(InteractiveGrant)] == [1, 1, 0, 0]


def test_blank_unauthorized_body_purpose_and_context_never_mint(client, managed_access, operating):
    blank = direct(client, managed_access, BLANK, action="operating-blank-action")
    assert blank.status_code == 201
    assert not rows(InteractiveGrant) and not rows(ExecutionContextPolicy)
    assert (
        direct(client, managed_access, {**BODY, "purpose": "interactive"}, action="operating-fake-purpose").status_code
        == 422
    )
    wrong = headers(managed_access, "operating-fake-context")
    wrong["X-Design-Context"] = "fake-auth-context"
    assert client.post("/api/design-projects/direct", json=BODY, headers=wrong).status_code == 409
    response = turn(client, managed_access, blank.json()["project_id"], authorized=False, intent="discuss")
    assert run_state(client, response.json()["run_id"])["status"] == "blocked"
    assert not rows(InteractiveGrant)


def test_allocation_rolls_back_with_creation(client, managed_access, operating, monkeypatch):
    from app.agent.routes import EngineAction

    def fail(self, response):
        raise AgentError("SYNTHETIC", "transaction failed", 500)

    monkeypatch.setattr(EngineAction, "finish", fail)
    assert direct(client, managed_access, action="operating-rollback-action").status_code == 500
    assert not rows(InteractiveGrant) and not rows(ExecutionContextPolicy) and not rows(ExecutionReservation)


def test_subject_queue_and_unknown_block_new_actions(client, managed_access, operating):
    first = direct(client, managed_access, action="operating-queue-first").json()
    second = direct(client, managed_access, action="operating-queue-second").json()
    assert second["run_status"] == "blocked" and second["reason"] == "PROVIDER_BUSY"
    assert len(rows(InteractiveGrant)) == 1
    with transaction() as db:
        run = db.get(Record, first["run_id"])
        run.status = "unknown"
    third = direct(client, managed_access, action="operating-unknown-third").json()
    assert third["run_status"] == "blocked" and third["reason"] == "PROVIDER_BUSY"
    assert len(rows(InteractiveGrant)) == 1
    cap = client.get("/api/design-agent/capabilities").json()["direct_creation"]
    assert not cap["available"] and cap["reason"] == "PROVIDER_BUSY"


@pytest.mark.parametrize("change", ["disable", "digest", "new_id", "logout", "deactivate"])
def test_worker_rechecks_policy_and_session_before_text(client, managed_access, operating, change):
    first = direct(client, managed_access, action="operating-revocation-action").json()
    if change in ["disable", "digest", "new_id"]:
        p = json.loads(operating.read_text())
        if change == "disable":
            p["enabled"] = False
        elif change == "digest":
            p["per_intent"]["text"]["max_cost_fen"] = 100
        else:
            p["policy_id"] = "synthetic_interactive_v2"
        operating.write_text(json.dumps(p))
    else:
        with models.session() as db:
            if change == "logout":
                db.delete(db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"])))
            else:
                db.get(models.DesignerUser, "alice").active = False
            db.commit()
    p = DecisionProvider()
    execute(_task(first["run_id"]), p)
    with models.session() as db:
        assert db.get(Record, first["run_id"]).status == "blocked"
    assert p.text_calls == p.calls == 0


def _task(run_id):
    with models.session() as db:
        return db.get(Record, run_id).payload["text_task_id"]


@pytest.mark.parametrize("text", ["这款圆头有什么特点？", "不要生成图片，只讨论设计", "How does this design work?"])
def test_auto_model_proposal_cannot_upgrade_original_discussion(client, managed_access, operating, text):
    blank = direct(client, managed_access, BLANK, action="operating-auto-blank").json()
    response = turn(client, managed_access, blank["project_id"], text=text, intent="auto")
    p = DecisionProvider(resolved="generate_image")
    execute(_task(response.json()["run_id"]), p)
    run = run_state(client, response.json()["run_id"])
    assert run["status"] == "needs_input" and run["image_task_id"] is None
    assert p.text_calls == 1 and p.calls == 0
    assert rows(InteractiveGrant)[0].payload["authorization"]["stages"]["image"]["max_calls"] == 0


def test_auto_explicit_new_image_and_unspent_allowance_not_reused(client, managed_access, operating):
    blank = direct(client, managed_access, BLANK, action="operating-auto-image-blank").json()
    response = turn(client, managed_access, blank["project_id"], text="请生成一张奶白运动鞋设计稿", intent="auto")
    p = DecisionProvider(resolved="discuss")
    execute(_task(response.json()["run_id"]), p)
    assert run_state(client, response.json()["run_id"])["status"] == "succeeded" and p.calls == 0
    response = turn(
        client,
        managed_access,
        blank["project_id"],
        action="operating-auto-second-image",
        text="请生成一张奶白运动鞋设计稿",
        intent="auto",
    )
    complete(client, response.json())
    assert len(rows(InteractiveGrant)) == 2 and len([x for x in rows(ExecutionReservation) if x.stage == "image"]) == 1


def test_validation_central_two_text_one_image_no_operating_fallback(
    client, managed_access, operating, configured, monkeypatch
):
    monkeypatch.setenv("DESIGN_CREATION_AUTHORIZATION_FILE", str(configured))
    context = session_context(token_hash(managed_access["tokens"]["alice"]))
    with transaction() as db:
        interactive_policy.register_validation(
            db,
            context,
            "alice",
            ledger_id="synthetic-central-ledger",
            grant_ids=["synthetic-direct-grant-001", "synthetic-discussion-grant-002"],
        )
    first = direct(client, managed_access).json()
    complete(client, first)
    g = json.loads(configured.read_text())
    g.pop("creation_action_id")
    g.update(
        grant_id="synthetic-discussion-grant-002",
        binding_mode="fixed_project",
        project_id=first["project_id"],
        stages={"text": {"max_calls": 1, "max_cost_fen": 50}, "image": {"max_calls": 0, "max_cost_fen": 0}},
    )
    configured.write_text(json.dumps(g))
    response = turn(client, managed_access, first["project_id"], action="validation-second-text", intent="discuss")
    p = DecisionProvider(resolved="discuss")
    execute(_task(response.json()["run_id"]), p)
    assert p.text_calls == 1 and p.calls == 0
    # A freshly edited finite file cannot add a third central call or fallback to operating policy.
    g.update(
        grant_id="synthetic-extra-finite-003",
        stages={"text": {"max_calls": 1, "max_cost_fen": 50}, "image": {"max_calls": 1, "max_cost_fen": 50}},
    )
    configured.write_text(json.dumps(g))
    response = turn(client, managed_access, first["project_id"], action="validation-denied-third", intent="discuss")
    run = run_state(client, response.json()["run_id"])
    assert run["status"] == "blocked" and run["reason"] == "CALL_LIMIT_EXHAUSTED"
    cap = client.get(f"/api/design-agent/capabilities?project_id={first['project_id']}").json()["direct_creation"]
    assert (
        not cap["text"]["available"]
        and not cap["image"]["available"]
        and cap["remaining"] == {"text_calls": 0, "image_calls": 0}
    )
    assert not rows(InteractiveGrant) and len(rows(ExecutionReservation)) == 3
    with transaction() as db:
        with pytest.raises(AgentError):
            interactive_policy.guard_legacy(db, context=context)
        with pytest.raises(AgentError):
            interactive_policy.register_validation(db, context, "alice", ledger_id="changed", grant_ids=[])


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": 1},
        {"max_active_runs_per_subject": True},
        {"allowed_roles": ["manager"]},
        {"scope_id": "wrong"},
        {"unknown": 1},
        {"per_intent": {"text": {"max_calls": 2, "max_cost_fen": 50}, "image": {"max_calls": 1, "max_cost_fen": 50}}},
    ],
)
def test_policy_strict_and_no_unlimited_fallback(operating, patch):
    p = json.loads(operating.read_text())
    p.update(patch)
    operating.write_text(json.dumps(p))
    assert interactive_policy.policy() is None


def test_policy_digest_change_requires_new_id_even_for_new_actions(client, managed_access, operating):
    first = direct(client, managed_access, action="operating-digest-original").json()
    complete(client, first)
    p = json.loads(operating.read_text())
    p["per_intent"]["text"]["max_cost_fen"] = 100
    operating.write_text(json.dumps(p))
    assert direct(client, managed_access, action="operating-digest-altered").json()["run_status"] == "blocked"
    p["policy_id"] = "synthetic_interactive_v2"
    operating.write_text(json.dumps(p))
    assert direct(client, managed_access, action="operating-digest-new-id").json()["run_status"] == "queued"


def test_upstream_text_only_and_old_entry_cannot_bypass_policy(client, managed_access, operating):
    pid = managed_access["projects"]["alice"]["pid"]
    response = turn(client, managed_access, pid, action="operating-upstream-text", intent="generate_image")
    p = DecisionProvider(resolved="generate_image")
    execute(_task(response.json()["run_id"]), p)
    run = run_state(client, response.json()["run_id"])
    assert run["reason"] == "UPSTREAM_MANUAL_CONFIRMATION_REQUIRED" and run["image_task_id"] is None
    assert rows(InteractiveGrant)[0].payload["authorization"]["stages"]["image"]["max_calls"] == 0
    assert p.text_calls == 1 and p.calls == 0
    with transaction() as db:
        with pytest.raises(AgentError):
            interactive_policy.guard_legacy(db)
    assume(client, managed_access, "lead")
    assert (
        client.post(
            "/api/design-projects/direct",
            json=BODY,
            headers=headers(managed_access, "operating-manager-denied", username="lead"),
        ).status_code
        == 403
    )


@pytest.mark.parametrize("change", ["disable", "new_id", "logout"])
def test_unfinished_image_rechecks_original_policy_and_context(client, managed_access, operating, change):
    first = direct(client, managed_access, action="operating-image-recheck").json()
    p = DecisionProvider()
    execute(_task(first["run_id"]), p)
    image_task = run_state(client, first["run_id"])["image_task_id"]
    assert image_task
    if change == "logout":
        with models.session() as db:
            db.delete(db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"])))
            db.commit()
    else:
        data = json.loads(operating.read_text())
        if change == "disable":
            data["enabled"] = False
        else:
            data["policy_id"] = "synthetic_next_policy_id"
        operating.write_text(json.dumps(data))
    execute(image_task, p)
    assert p.text_calls == 1 and p.calls == 0
    with models.session() as db:
        assert db.get(Record, first["run_id"]).status == "blocked"
    assert len(rows(InteractiveGrant)) == 1


def test_interactive_old_image_route_and_foreign_object_denied(client, managed_access, operating):
    first = direct(client, managed_access, action="operating-protection-first").json()
    complete(client, first)
    run = run_state(client, first["run_id"])
    response = client.post(
        f"/api/project/{first['project_id']}/design-text-to-image",
        json={"prompt_id": run["prompt_id"], "idempotency_key": "operating-legacy-attempt", "authorized": True},
        headers=headers(
            managed_access,
            "operating-legacy-attempt",
            client.get(f"/api/project/{first['project_id']}/design-workspace").json()["revision"],
        ),
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "CREATION_AUTHORIZATION_REQUIRED"
    assert len(rows(InteractiveGrant)) == 1
    assume(client, managed_access, "bob")
    assert client.get(f"/api/project/{first['project_id']}/design-workspace").status_code == 404
    assert client.get(f"/api/design-creation-runs/{first['run_id']}").status_code == 404
    assume(client, managed_access, "alice")
    revision = client.get(f"/api/project/{first['project_id']}/design-workspace").json()["revision"]
    response = client.post(
        f"/api/design-projects/{first['project_id']}/archive",
        json={"expected_revision": revision, "confirm": True},
        headers=headers(managed_access, "operating-archive-finished", revision),
    )
    assert response.status_code == 200
    assert client.get(f"/api/design-agent/capabilities?project_id={first['project_id']}").status_code == 404
    assert len(rows(InteractiveGrant)) == 1


def test_policy_private_and_authorization_expires_with_session(client, managed_access, operating):
    operating.chmod(0o644)
    assert interactive_policy.policy() is None
    operating.chmod(0o600)
    first = direct(client, managed_access, action="operating-session-expiry").json()
    with models.session() as db:
        session = db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"]))
        g = db.get(InteractiveGrant, db.get(Record, first["run_id"]).payload["interactive_grant_id"]).payload[
            "authorization"
        ]
        assert g["expires_at"] == session.expires_at
    assert client.get(f"/api/design-creation-runs/{first['run_id']}").json().get("auth_context_id") is None


def test_validation_purpose_survives_policy_removed_and_legacy_worker_context_absent(
    client, managed_access, operating, monkeypatch
):
    context = session_context(token_hash(managed_access["tokens"]["alice"]))
    with transaction() as db:
        interactive_policy.register_validation(db, context, "alice", ledger_id="synthetic-no-fallback", grant_ids=[])
    monkeypatch.delenv("DESIGN_INTERACTIVE_POLICY_FILE")
    with transaction() as db:
        with pytest.raises(AgentError):
            interactive_policy.guard_legacy(db, actor="alice")
    assert not rows(InteractiveGrant)


def test_disabled_operating_policy_preserves_finite_mode(client, managed_access, operating, configured, monkeypatch):
    p = json.loads(operating.read_text())
    p["enabled"] = False
    operating.write_text(json.dumps(p))
    monkeypatch.setenv("DESIGN_CREATION_AUTHORIZATION_FILE", str(configured))
    first = direct(client, managed_access).json()
    assert first["run_status"] == "queued" and not rows(InteractiveGrant)
    with transaction() as db:
        interactive_policy.guard_legacy(db)


@pytest.mark.parametrize("text", ["请解释一下设计一款鞋的流程", "如何设计一款通勤鞋？", "帮我解释生成一张设计稿的含义"])
def test_auto_explicit_explanation_not_an_image_request(client, managed_access, operating, text):
    blank = direct(client, managed_access, BLANK, action="operating-explanation-blank").json()
    response = turn(client, managed_access, blank["project_id"], text=text, intent="auto")
    p = DecisionProvider(resolved="generate_image")
    execute(_task(response.json()["run_id"]), p)
    run = run_state(client, response.json()["run_id"])
    assert run["status"] == "needs_input" and run["image_task_id"] is None
    assert rows(InteractiveGrant)[0].payload["authorization"]["stages"]["image"]["max_calls"] == 0
    assert p.text_calls == 1 and p.calls == 0


def confirmed_upstream(client, fixture):
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
    pid = fixture["projects"]["alice"]["pid"]
    with models.session() as db, db.begin():
        handoff = db.get(models.DesignHandoff, "synthetic-alice")
        handoff.package_id = decoded["package"]["package_id"]
        handoff.version = decoded["package"]["version"]
        receipt = db.get(ManagedReceipt, handoff.id)
        receipt.package_id = handoff.package_id
        receipt.package_version = handoff.version
        receipt.proof = {key: decoded[key] for key in ("package", "requirement", "prompt")}
        receipt.receipt = {
            **receipt.receipt,
            "content_digest": decoded["delivery"]["content_digest"],
            "design_receive_id": "DR-SYNTHETIC-A-CONTINUOUS",
        }
    source = decoded["prompt"]
    body = {
        "expected_prompt_id": None,
        "positive_prompt": source["positive_prompt"],
        "avoid_items": source["avoid_items"],
        "output_kind": "effect_image",
        "base_version_id": None,
        "edit_region": "",
    }
    saved = client.post(
        f"/api/project/{pid}/design-prompts", json=body, headers=headers(fixture, "operating-A-manual-confirm", 1)
    )
    assert saved.status_code == 201, saved.text
    return {"project_id": pid, "prompt": saved.json()["prompt"]}


def test_upstream_manual_image_uses_persistent_action_budget_without_static_grant(client, managed_access, operating):
    from test_dual_entry import queue

    created = confirmed_upstream(client, managed_access)
    response = queue(client, managed_access, created, action="operating-A-single-manual-image", revision=2)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    p = DecisionProvider()
    execute(task_id, p)
    task = client.get(f"/api/design-tasks/{task_id}").json()
    assert task["status"] == "succeeded" and p.calls == 1 and p.text_calls == 0
    grant = rows(InteractiveGrant)[0].payload["authorization"]
    assert grant["stages"]["text"]["max_calls"] == 0 and grant["stages"]["image"]["max_calls"] == 1
    assert len(rows(ExecutionReservation)) == 1 and rows(ExecutionReservation)[0].stage == "image"
    assert (
        queue(client, managed_access, created, action="operating-A-single-manual-image", revision=2).content
        == response.content
    )
    assert len(rows(InteractiveGrant)) == 1
    with models.session() as db:
        assert db.get(models.DesignHandoff, "synthetic-alice").status == "assigned"
    assert client.get(f"/api/project/{created['project_id']}/design-workspace").json()["source_binding"] is not None


@pytest.mark.parametrize("case", ["validation_no_policy", "queued_logout", "missing_worker_context"])
def test_legacy_trusted_context_never_defaults_to_paid(client, managed_access, operating, monkeypatch, case):
    from app.agent import runner
    from app.agent.store import create

    if case == "validation_no_policy":
        created = confirmed_upstream(client, managed_access)
        context = session_context(token_hash(managed_access["tokens"]["alice"]))
        with transaction() as db:
            interactive_policy.register_validation(
                db, context, "alice", ledger_id="legacy-central-validation", grant_ids=[]
            )
        monkeypatch.delenv("DESIGN_INTERACTIVE_POLICY_FILE")
        workspace = client.get(f"/api/project/{created['project_id']}/design-workspace").json()
        response = client.post(
            f"/api/project/{created['project_id']}/design-tasks",
            json={
                "spec_id": workspace["specs"][0]["id"],
                "mode": "understand",
                "authorized": True,
                "idempotency_key": "legacy-validation-attempt",
            },
            headers=headers(managed_access, "legacy-validation-attempt", 2),
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "CREATION_AUTHORIZATION_REQUIRED"
    else:
        monkeypatch.delenv("DESIGN_INTERACTIVE_POLICY_FILE")
        context = session_context(token_hash(managed_access["tokens"]["alice"])) if case == "queued_logout" else None
        with transaction() as db:
            task = create(
                db,
                managed_access["projects"]["alice"]["pid"],
                "task",
                {
                    "spec_id": None,
                    "mode": "understand",
                    "provider": {
                        "text": True,
                        "image": True,
                        "models": {"text": "synthetic", "image": "synthetic"},
                        "reasoning_call_max_fen": 50,
                        "image_call_max_fen": 50,
                    },
                    "actor": "alice",
                    "auth_context_id": context,
                    "reasoning_calls": 0,
                    "image_calls": 0,
                    "max_reasoning_calls": 1,
                    "max_image_calls": 1,
                    "max_cost_fen": 50,
                    "reserved_cost_fen": 0,
                    "steps": [],
                },
                "running",
            )
            if case == "queued_logout":
                db.delete(db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"])))
                db.flush()

            class BudgetProvider:
                reasoning_fen = image_fen = 50

                def capabilities(self):
                    return task.payload["provider"]

            with pytest.raises(AgentError) as error:
                runner.event(db, task, "plan", BudgetProvider())
            assert error.value.code == "AUTH_CONTEXT_CHANGED"
            assert task.payload["steps"] == []
    assert not rows(InteractiveGrant) and not rows(ExecutionReservation)


@pytest.mark.parametrize("case", ["validation", "logout"])
def test_upstream_manual_image_cannot_bypass_validation_or_session_revocation(client, managed_access, operating, case):
    from test_dual_entry import queue

    created = confirmed_upstream(client, managed_access)
    if case == "validation":
        context = session_context(token_hash(managed_access["tokens"]["alice"]))
        with transaction() as db:
            interactive_policy.register_validation(
                db, context, "alice", ledger_id="A-root-validation", grant_ids=[], limits={"text": 0, "image": 0}
            )
        response = queue(client, managed_access, created, action="A-validation-image-denied", revision=2)
        assert response.status_code == 409
        assert not rows(InteractiveGrant) and not rows(ExecutionReservation)
    else:
        response = queue(client, managed_access, created, action="A-queued-image-revoke", revision=2)
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        with models.session() as db:
            db.delete(db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"])))
            db.commit()
        p = DecisionProvider()
        execute(task_id, p)
        assert p.calls == 0
        assert rows(ExecutionReservation)[0].status == "failed_not_sent"


def test_legacy_submission_freezes_context_but_does_not_publish_it(client, managed_access, operating, monkeypatch):
    monkeypatch.delenv("DESIGN_INTERACTIVE_POLICY_FILE")
    created = confirmed_upstream(client, managed_access)
    workspace = client.get(f"/api/project/{created['project_id']}/design-workspace").json()
    response = client.post(
        f"/api/project/{created['project_id']}/design-tasks",
        json={
            "spec_id": workspace["specs"][0]["id"],
            "mode": "understand",
            "authorized": True,
            "idempotency_key": "legacy-trusted-submit",
        },
        headers=headers(managed_access, "legacy-trusted-submit", 2),
    )
    assert response.status_code == 202, response.text
    assert "auth_context_id" not in response.json() and "legacy_policy_sha256" not in response.json()
    with models.session() as db:
        task = db.get(Record, response.json()["id"])
        assert task.payload["actor"] == "alice" and task.payload["auth_context_id"] == session_context(
            token_hash(managed_access["tokens"]["alice"])
        )
