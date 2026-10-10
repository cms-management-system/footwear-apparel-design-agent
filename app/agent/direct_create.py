"""One user intent, one bounded model decision, optional one image; never retry a sent stage."""

import base64
import json
import time
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, ValidationError, model_validator
from sqlalchemy import select

from ..config import get_config
from ..models import DesignerUser, DesignProjectAccess, Project
from . import execution_quota as quota
from . import workbench
from .access_context import project_binding, validate_transaction_access
from .managed_store import (
    CreationGrantBinding,
    ExecutionReservation,
    IndependentProject,
    ManagedAudit,
    canonical_bytes,
    sha256,
)
from .schemas import SpecIn, Strict
from .service import save_spec
from .store import (
    AgentError,
    Record,
    WorkerLease,
    change,
    create,
    head,
    now,
    records,
    require,
    transaction,
    uid,
)


class DirectIn(Strict):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    entry_mode: Literal["idea", "blank"]
    text: str = Field(max_length=4000)
    output_kind: Literal["effect_image", "design_draft"]
    intent: Literal["generate_image", "discuss", "none"]
    authorized: StrictBool

    @model_validator(mode="after")
    def valid_entry(self):
        if self.entry_mode == "blank":
            if self.text != "" or self.intent != "none" or self.authorized:
                raise ValueError("空白入口不可自动执行")
        elif not self.text.strip() or self.intent == "none" or not self.authorized:
            raise ValueError("创作原文及明确授权缺失")
        return self


class RenameIn(Strict):
    expected_revision: int = Field(strict=True, ge=1)
    title: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def valid_title(self):
        if not self.title.strip():
            raise ValueError("名称不可为空白")
        return self


class ArchiveIn(Strict):
    expected_revision: int = Field(strict=True, ge=1)
    confirm: StrictBool

    @model_validator(mode="after")
    def confirmation(self):
        if self.confirm is not True:
            raise ValueError("需要明确确认")
        return self


class Decision(Strict):
    resolved_intent: Literal["discuss", "generate_image", "needs_input"]
    answer: str = Field(min_length=1, max_length=4000)
    positive_prompt: str = Field(default="", max_length=10000)
    avoid_items: list[str] = Field(default_factory=list, max_length=100)
    base_version_id: str | None = None
    edit_region: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def valid_decision(self):
        if not self.answer.strip() or any(not x.strip() or len(x) > 10000 for x in self.avoid_items):
            raise ValueError("回复/避免项缺失")
        if self.resolved_intent == "generate_image" and not self.positive_prompt.strip():
            raise ValueError("不能生成空执行稿")
        if self.base_version_id and not self.edit_region.strip():
            raise ValueError("修改需明确区域")
        return self


def capabilities(db, pid, user, provider, creation_action=None):
    from . import interactive_policy as interactive

    p, ctx = interactive.preview(db, user)
    g = quota.grant()
    bound = head(db, pid).payload if pid else None
    permitted = (
        user is not None
        and user.role == "designer"
        and quota.matches(
            g,
            pid,
            user.username if user else None,
            creation_action or (g or {}).get("creation_action_id") if pid is None else creation_action,
            bound,
            db=db,
            allow_unbound_next=pid is None,
        )
    )
    if pid:
        project_binding(db, pid, user)
    if p:
        permitted = True
        if pid:
            try:
                project_binding(db, pid, user, write=True)
            except AgentError:
                permitted = False
        g = {"grant_id": "next_interactive_preview", "stages": p["per_intent"]}
        if pid and not isinstance(project_binding(db, pid, user)[1], IndependentProject):
            g = {**g, "stages": {**g["stages"], "image": {"max_calls": 0, "max_cost_fen": 0}}}
    binding_finished = False
    if g and quota.binding_mode(g) == "next_creation":
        binding = db.get(CreationGrantBinding, g["grant_id"])
        original_run = db.get(Record, binding.run_id) if binding else None
        binding_finished = bool(original_run and original_run.status not in {"queued", "running"})
    text_cost, image_cost = provider.reasoning_fen, provider.image_fen
    available = {}
    reasons = {}
    for stage, cost in [("text", text_cost), ("image", image_cost)]:
        count, total = quota.usage(db, g, stage) if permitted and not p else (0, 0)
        cap = g["stages"].get(stage, {}) if permitted else {}
        configured = provider.capabilities().get("understand" if stage == "text" else "image_only", False)
        reason = (
            "ready"
            if configured
            and permitted
            and cost > 0
            and count < cap.get("max_calls", 0)
            and total + cost <= cap.get("max_cost_fen", 0)
            else "CAPABILITY_UNAVAILABLE"
            if not configured
            else "CREATION_AUTHORIZATION_REQUIRED"
            if not permitted
            else "CALL_LIMIT_EXHAUSTED"
            if count >= cap.get("max_calls", 0)
            else "BUDGET_EXHAUSTED"
        )
        available[stage], reasons[stage] = reason == "ready", reason
        if available[stage] and binding_finished:
            available[stage], reasons[stage] = False, "GRANT_BINDING_CONFLICT"
        if available[stage] and quota.busy(db):
            available[stage], reasons[stage] = False, "PROVIDER_BUSY"
        if available[stage] and p and interactive.subject_busy(db, user.username):
            available[stage], reasons[stage] = False, "PROVIDER_BUSY"
        if (
            ctx
            and ctx.purpose == "validation"
            and interactive.validation_count(db, ctx, stage) >= ctx.payload["limits"][stage]
        ):
            available[stage], reasons[stage] = False, "CALL_LIMIT_EXHAUSTED"
    receipt_models = {"text": None, "image": None}
    if pid:
        for run in records(db, pid, "creation_run"):
            for step in run.payload.get("steps", []):
                if step.get("stage") in receipt_models and step.get("response_model"):
                    receipt_models[step["stage"]] = step["response_model"]
    return {
        "available": all(available.values()),
        "reason": next((reasons[x] for x in available if not available[x]), "ready"),
        "text": {
            "available": available["text"],
            "request_model": provider.text_model or provider.vision_model or None,
            "response_model": receipt_models["text"],
        },
        "image": {
            "available": available["image"],
            "request_model": provider.image_model or None,
            "response_model": receipt_models["image"],
            "single_image": True,
        },
        "remaining": {
            f"{x}_calls": min(
                quota.remaining(db, g, x) if permitted else 0,
                max(0, ctx.payload["limits"][x] - interactive.validation_count(db, ctx, x))
                if ctx and ctx.purpose == "validation"
                else 100,
            )
            for x in available
        },
    }


def create_project(db, data, action_id, auth_context):
    from .providers import Provider

    user = validate_transaction_access(db)
    if user is None or user.role != "designer":
        raise AgentError("ROLE_FORBIDDEN", "仅设计员工可创建本人自主项目", 403)
    p = Project(
        name=" ".join(data.text.split())[:32] if data.entry_mode == "idea" else "未命名设计", category="鞋服创意"
    )
    db.add(p)
    db.flush()
    cfg = get_config()
    db.add(
        IndependentProject(
            project_id=p.id,
            id=f"independent:{p.id}",
            owner_subject=user.username,
            scope_id=cfg.design_scope_id,
            target_instance_id=cfg.design_instance_id,
            revision=0,
            status="draft",
        )
    )
    db.add(DesignProjectAccess(project_id=p.id, username=user.username))
    db.flush()
    h = head(db, p.id)
    change(h, spec_id=None, current_prompt_id=None, creation_action_id=action_id, output_kind=data.output_kind)
    message = run = None
    if data.entry_mode == "idea":
        message, run = save_turn(
            db,
            p.id,
            user,
            text=data.text,
            requested=data.intent,
            output_kind=data.output_kind,
            authorized=data.authorized,
            selected=None,
            references=[],
            action_id=action_id,
            auth_context=auth_context,
            provider=Provider(),
            creating=True,
        )
    return {
        "project_id": p.id,
        "source_mode": "independent",
        "message_id": message.id if message else None,
        "intent_id": run.payload["intent_id"] if run else None,
        "run_id": run.id if run else None,
        "run_status": run.status if run else None,
        "url": f"/projects/{p.id}",
        **({"reason": run.payload["reason"]} if run and run.payload.get("reason") else {}),
    }


def save_turn(
    db,
    pid,
    user,
    *,
    text,
    requested,
    output_kind,
    authorized,
    selected,
    references,
    action_id,
    auth_context,
    provider,
    max_cost_fen=None,
    creating=False,
):
    row, meta = project_binding(db, pid, user, write=True)
    h = head(db, pid)
    selected = workbench.selected_context(db, pid, selected)
    if len({r["asset_id"] for r in references}) != len(references):
        raise AgentError("DUPLICATE_REFERENCE", "同一素材不能重复指定", 422)
    for r in references:
        workbench.existing_image(require(db, r["asset_id"], "asset", pid).payload)
    if requested == "generate_image" and authorized is not True:
        raise AgentError("AUTHORIZATION_REQUIRED", "需要明确授权本次生成请求", 409)
    message = create(
        db,
        pid,
        "message",
        {
            "role": "user",
            "text": text,
            "references": references,
            "created_by": user.username,
            "selected_context": selected,
            "prompt_id": h.payload.get("current_prompt_id"),
            "spec_id": h.payload.get("spec_id"),
            "task_id": None,
            "reply_state": "queued",
            "source_binding_digest": workbench.source_digest(db, pid, user),
        },
        "sent",
        dedupe=f"message:{pid}:{action_id}",
    )
    change(h, latest_user_message_id=message.id)
    intent = {
        "id": uid(),
        "project_id": pid,
        "message_id": message.id,
        "requested_intent": requested,
        "output_kind": output_kind or h.payload.get("output_kind", "effect_image"),
        "selected_context": selected,
        "source_mode": "independent" if isinstance(meta, IndependentProject) else "upstream",
        "source_binding_digest": workbench.source_digest(db, pid, user),
        "actor": user.username,
        "authorized": authorized,
        "auth_context_id": auth_context,
        "created_at": now(),
    }
    intent["digest"] = sha256(canonical_bytes(intent))
    intent_row = create(
        db, pid, "creation_intent", {"intent": intent, "resolved_intent": "pending"}, "saved", id=intent["id"]
    )
    run = create(
        db,
        pid,
        "creation_run",
        {
            "intent_id": intent_row.id,
            "message_id": message.id,
            "actor": user.username,
            "source_mode": intent["source_mode"],
            "auth_context_id": auth_context,
            "user_action_id": action_id,
            "source_identity_digest": workbench.source_identity_digest(db, pid, user),
            "creation_action_id": h.payload.get("creation_action_id"),
            "requested_intent": requested,
            "output_kind": intent["output_kind"],
            "selected_context": selected,
            "references": references,
            "stage": "text",
            "reason": None,
            "error": None,
            "text_task_id": None,
            "image_task_id": None,
            "prompt_id": None,
            "spec_id": None,
            "version_id": None,
            "base_version_id": None,
            "steps": [],
            "canvas_placement": "pending",
            "base_prompt_id": h.payload.get("current_prompt_id"),
            "base_spec_id": h.payload.get("spec_id"),
            "expected_business_revision": meta.revision + 1,
            "max_text_calls": 1,
            "max_image_calls": 1,
            "max_cost_fen": max_cost_fen,
        },
        "queued",
    )
    change(run, binding_run_id=run.id)
    try:
        if authorized is not True:
            raise AgentError("AUTHORIZATION_REQUIRED", "消息已保存，尚未授权发送模型", 409)
        if not provider.capabilities().get("understand"):
            raise AgentError("CAPABILITY_UNAVAILABLE", "消息已保存，当前未启用对话模型", 409)
        if (selected or references) and not (
            getattr(provider, "vision_model", None)
            and getattr(provider, "vision_url", None)
            and getattr(provider, "vision_key", None)
        ):
            raise AgentError("CAPABILITY_UNAVAILABLE", "带图理解模型尚未配置，未派发", 409)
        from . import interactive_policy as interactive

        g = interactive.allocate(db, run, user, action_id, provider, text) or quota.grant()
        if (
            g
            and g["subject"] == user.username
            and quota.binding_mode(g) == "next_creation"
            and creating
            and requested == "generate_image"
        ):
            quota.claim_next(db, g, run, action_id, provider)
        if not quota.matches(g, pid, user.username, bound=run.payload, db=db):
            raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "消息已保存，本次执行尚无有效服务端授权", 409)
        if max_cost_fen is not None and provider.reasoning_fen > max_cost_fen:
            raise AgentError("BUDGET_EXHAUSTED", "文本费用预留超出本次请求上限", 409)
        if (
            requested == "generate_image"
            and run.payload.get("interactive_grant_id")
            and max_cost_fen is not None
            and provider.reasoning_fen + provider.image_fen > max_cost_fen
        ):
            raise AgentError("BUDGET_EXHAUSTED", "本次费用上限不足以覆盖整理和图片预留", 409)
        # Explicit image intent must have a complete affordable grant before any paid text dispatch.
        if (
            requested == "generate_image"
            and isinstance(meta, IndependentProject)
            and not run.payload.get("interactive_grant_id")
        ):
            cap = capabilities(db, pid, user, provider)
            if not cap["image"]["available"]:
                raise AgentError(cap["reason"], "生成消息已保存，本次图片授权或能力不可用", 409)
            if max_cost_fen is not None and provider.reasoning_fen + provider.image_fen > max_cost_fen:
                raise AgentError("BUDGET_EXHAUSTED", "本次费用上限不足以覆盖整理和图片预留", 409)
        quota.reserve(db, g, run, "text", provider.reasoning_fen)
        task = create(
            db,
            pid,
            "task",
            {
                "mode": "direct_creation",
                "creation_run_id": run.id,
                "actor": user.username,
                "steps": [],
                "reasoning_calls": 0,
                "image_calls": 0,
                "max_reasoning_calls": 1,
                "max_image_calls": 1,
                "max_cost_fen": max_cost_fen,
            },
            "queued",
            dedupe=f"creation-text:{run.id}",
        )
        change(run, text_task_id=task.id)
        change(message, task_id=task.id, intent_id=intent_row.id, run_id=run.id)
        change(h, task_id=task.id)
    except AgentError as e:
        run.status = "blocked"
        change(run, reason=e.code, error={"code": e.code, "message": e.message})
        change(message, reply_state="blocked", reason=e.code, intent_id=intent_row.id, run_id=run.id)
        create(
            db,
            pid,
            "message",
            {"role": "system", "text": e.message, "reply_to": message.id, "reason": e.code, "run_id": run.id},
            "saved",
        )
    return message, run


def projection(run):
    names = {
        "intent_id",
        "message_id",
        "stage",
        "reason",
        "error",
        "text_task_id",
        "image_task_id",
        "prompt_id",
        "spec_id",
        "version_id",
        "base_version_id",
        "steps",
        "canvas_placement",
    }
    return {
        "id": run.id,
        "project_id": run.project_id,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        **{k: v for k, v in run.payload.items() if k in names},
    }


def worker_actor(db, run):
    user = db.get(DesignerUser, run.payload["actor"], populate_existing=True)
    if not user or not user.active or user.role != "designer":
        raise AgentError("ROLE_FORBIDDEN", "原执行人员权限已失效", 403)
    project_binding(db, run.project_id, user, write=True)
    return user


def check_basis(db, run, user):
    h = head(db, run.project_id)
    _, meta = project_binding(db, run.project_id, user, write=True)
    if (
        h.payload.get("latest_user_message_id") != run.payload["message_id"]
        or h.payload.get("current_prompt_id") != run.payload["base_prompt_id"]
        or h.payload.get("spec_id") != run.payload["base_spec_id"]
        or meta.revision != run.payload["expected_business_revision"]
        or workbench.source_identity_digest(db, run.project_id, user) != run.payload["source_identity_digest"]
    ):
        raise AgentError("EXECUTION_SUPERSEDED", "执行依据已有新消息或修订，原执行停止自动生成", 409)


def revision(db, run, kind):
    user = worker_actor(db, run)
    row, meta = project_binding(db, run.project_id, user, write=True)
    meta.revision += 1
    db.add(
        ManagedAudit(
            handoff_id=row.id,
            revision=meta.revision,
            kind=kind,
            actor=user.username,
            payload={"run_id": run.id, "intent_id": run.payload["intent_id"]},
        )
    )
    return meta.revision


def reservation(db, run, stage):
    return db.scalar(
        select(ExecutionReservation).where(
            ExecutionReservation.run_id == run.id,
            ExecutionReservation.stage == stage,
        )
    )


def run_text(id, provider, owner=None):
    try:
        with transaction() as db:
            task = require(db, id, "task")
            run = require(db, task.payload["creation_run_id"], "creation_run")
            if task.status != "running" or run.status not in {"queued", "running"}:
                return
            if owner:
                lease = db.get(WorkerLease, 1)
                if not lease or lease.owner != owner or lease.expires < time.time():
                    raise AgentError("LEASE_LOST", "执行权已经变化", 409)
            user = worker_actor(db, run)
            check_basis(db, run, user)
            provider.require("understand")
            if task.payload["reasoning_calls"] >= task.payload["max_reasoning_calls"]:
                raise AgentError("CALL_LIMIT_EXHAUSTED", "文本任务次数已用完，未再次派发", 409)
            reserve = reservation(db, run, "text")
            if not reserve or task.payload.get("steps"):
                raise AgentError("UNKNOWN_CALL", "阶段已派发或预留缺失，禁止再次调用", 409)
            message = require(db, run.payload["message_id"], "message", run.project_id)
            images = []
            if run.payload["selected_context"]:
                selected = workbench.selected_context(db, run.project_id, run.payload["selected_context"])
                images.append(
                    ("明确选中的图片上下文", workbench.asset_payload(db, run.project_id, selected["asset_id"]))
                )
            for ref in run.payload["references"]:
                image = require(db, ref["asset_id"], "asset", run.project_id).payload
                if not any(x[1].get("file") == image.get("file") for x in images):
                    images.append((f"asset_id={ref['asset_id']}; {ref['instruction']}", image))
            context = {
                "text": message.payload["text"],
                "requested_intent": run.payload["requested_intent"],
                "output_kind": run.payload["output_kind"],
                "selected_context": run.payload["selected_context"],
                "source": workbench.source_binding(db, run.project_id, user),
                "current_prompt": require(db, run.payload["base_prompt_id"], "prompt").payload["prompt"]
                if run.payload["base_prompt_id"]
                else None,
            }
            quota.dispatch(db, reserve, run, provider.reasoning_fen)
            step = {
                "stage": "text",
                "status": "sent",
                "attempt_id": reserve.payload["attempt_id"],
                "request_id": None,
                "request_model": provider.vision_model if images else provider.text_model or provider.vision_model,
                "response_model": None,
                "started_at": now(),
                "actual_cost_fen": None,
                "usage": None,
            }
            task_step = {"id": step["attempt_id"], "status": "pending", "tool": "creation_decision"}
            task.status = run.status = "running"
            change(task, steps=[task_step], reasoning_calls=1)
            change(run, steps=[step])
        # No response repair or hidden second model call. Failure remains a visible original attempt.
        decision = Decision.model_validate(provider.creation_decision(context, images))
        receipt = getattr(provider, "last_receipt", {})
        with transaction() as db:
            task = require(db, id, "task")
            run = require(db, task.payload["creation_run_id"], "creation_run")
            quota.complete(db, reservation(db, run, "text"), "succeeded", receipt)
            change(task, steps=[{**task_step, "status": "done", "receipt": receipt}], outcome=decision.answer)
            change(
                run,
                steps=[
                    {
                        **step,
                        "status": "succeeded",
                        "finished_at": now(),
                        "request_id": receipt.get("request_id") or None,
                        "response_model": receipt.get("model"),
                        "usage": receipt.get("usage"),
                        "actual_cost_fen": receipt.get("actual_cost_fen"),
                    }
                ],
            )
            user = worker_actor(db, run)
            check_basis(db, run, user)
            intent = require(db, run.payload["intent_id"], "creation_intent")
            resolved = decision.resolved_intent
            auto_denied = (
                run.payload.get("interactive_grant_id")
                and run.payload["requested_intent"] == "auto"
                and not run.payload.get("image_intent_allowed")
                and resolved == "generate_image"
            )
            if auto_denied:
                resolved = "needs_input"
            if run.payload["requested_intent"] == "discuss" and resolved == "generate_image":
                resolved = "discuss"  # A model suggestion cannot broaden a user's discussion request.
            change(intent, resolved_intent=resolved)
            create(
                db,
                run.project_id,
                "intent_resolution",
                {"intent_id": intent.id, "resolved_intent": resolved, "model_task_id": id},
                "saved",
            )
            reply_text = decision.answer
            if auto_denied:
                reply_text = "如需生成图片，请在下一条消息中明确说明要生成的内容。"
            if resolved == "generate_image":
                # Text completion cannot certify an image that has not been dispatched yet.
                reply_text = (
                    "已整理本次创作要求，准备为你生成图片，图片尚未完成。请以创作进度和画布结果为准。"
                    if run.payload["source_mode"] == "independent"
                    else "已整理本次创作要求，图片尚未完成；仍需人工确认和批准后才能生成。"
                )
            create(
                db,
                run.project_id,
                "message",
                {
                    "role": "assistant",
                    "text": reply_text,
                    "reply_to": run.payload["message_id"],
                    "task_id": id,
                    "run_id": run.id,
                },
                "sent",
            )
            change(require(db, run.payload["message_id"], "message"), reply_state="replied")
            task.status = "succeeded"
            if resolved != "generate_image" or run.payload["source_mode"] != "independent":
                run.status = "needs_input" if resolved == "needs_input" else "succeeded"
                change(run, stage="done", canvas_placement="unavailable")
                if resolved == "generate_image":
                    change(run, reason="UPSTREAM_MANUAL_CONFIRMATION_REQUIRED")
                revision(db, run, "creation_text_completed")
                return
            change(run, stage="freeze")
            freeze_and_queue(db, run, decision, user, provider)
    except Exception as error:
        finish_error(id, error, getattr(provider, "last_receipt", {}))


def freeze_and_queue(db, run, decision, user, provider):
    from .dual_entry import source_constraints

    # Base version comes only from the explicit selected version, never a generic asset or invented model ID.
    selected = run.payload["selected_context"]
    if decision.base_version_id and (not selected or selected["version_id"] != decision.base_version_id):
        raise AgentError("CONTEXT_MISMATCH", "修改基准与所选版本不一致，需要重新说明", 409)
    data = workbench.BriefIn(
        expected_prompt_id=run.payload["base_prompt_id"],
        expected_spec_id=run.payload["base_spec_id"],
        message_id=run.payload["message_id"],
        selected_context=selected,
        positive_prompt=decision.positive_prompt,
        avoid_items=decision.avoid_items,
        output_kind=run.payload["output_kind"],
        base_version_id=decision.base_version_id,
        edit_region=decision.edit_region,
    )
    if (
        run.payload["max_cost_fen"] is not None
        and provider.reasoning_fen + provider.image_fen > run.payload["max_cost_fen"]
    ):
        raise AgentError("BUDGET_EXHAUSTED", "本次请求上限不足以覆盖图片预留", 409)
    provider.require("image_only")
    from .interactive_policy import current_grant

    g = current_grant(db, run)
    if g != reservation(db, run, "text").payload["authorization"]:
        raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "原整理授权已变化，未冻结或派发图片", 409)
    reserve = quota.reserve(db, g, run, "image", provider.image_fen)
    brief = workbench.save_brief(
        db,
        run.project_id,
        data,
        origin="model",
        actor=user,
        provenance={"task_id": run.payload["text_task_id"], "steps": run.payload["steps"]},
    )
    constraints = source_constraints(db, run.project_id, user, data)
    spec = save_spec(
        db,
        run.project_id,
        SpecIn(
            expected_spec_id=data.expected_spec_id,
            intent=data.positive_prompt,
            constraints=constraints,
            base_version_id=data.base_version_id,
            edit_region=data.edit_region,
            deliverables="二维鞋服设计稿示意" if data.output_kind == "design_draft" else "鞋服效果示意图",
        ),
        check_idle=False,
        actor=user.username,
    )
    spec.status = "confirmed"
    prompt = {
        "id": uid(),
        "project_id": run.project_id,
        "positive_prompt": data.positive_prompt,
        "avoid_items": data.avoid_items,
        "output_kind": data.output_kind,
        "design_object": db.get(Project, run.project_id).category,
        "base_version_id": data.base_version_id,
        "edit_region": data.edit_region,
        "source_mode": "independent",
        "source_brief_id": brief["id"],
        "confirmed_through_message_id": run.payload["message_id"],
        "derived_from_prompt_id": data.expected_prompt_id,
        "execution_intent_id": run.payload["intent_id"],
        "confirmation_mode": "user_intent",
        "created_by": user.username,
        "created_at": now(),
    }
    raw = canonical_bytes(prompt)
    prompt["digest"] = sha256(raw)
    create(
        db,
        run.project_id,
        "prompt",
        {
            "prompt": prompt,
            "original_body_base64": base64.b64encode(raw).decode(),
            "spec_id": spec.id,
            "source_binding_digest": None,
            "source_identity_digest": run.payload["source_identity_digest"],
        },
        "saved",
        id=prompt["id"],
    )
    change(spec, output_kind=data.output_kind, source_brief_id=brief["id"], execution_prompt_digest=prompt["digest"])
    create(
        db,
        run.project_id,
        "brief_confirmation",
        {
            "brief_id": brief["id"],
            "prompt_id": prompt["id"],
            "spec_id": spec.id,
            "created_by": user.username,
            "confirmation_mode": "user_intent",
            "execution_intent_id": run.payload["intent_id"],
        },
        "confirmed",
    )
    render = "仅生成一张二维鞋服设计示意图，不是试穿或生产证明。\n" + json.dumps(prompt, ensure_ascii=False)
    if len(render) > 10000:
        raise AgentError("INVALID_INPUT", "完整执行提示词超过10000字符，未截断或派发图片", 422)
    image_task = create(
        db,
        run.project_id,
        "task",
        {
            "mode": "image_only",
            "creation_run_id": run.id,
            "execution_reservation_id": reserve.id,
            "prompt_id": prompt["id"],
            "prompt_digest": prompt["digest"],
            "source_binding_digest": None,
            "source_identity_digest": run.payload["source_identity_digest"],
            "spec_id": spec.id,
            "base_version_id": data.base_version_id,
            "actor": user.username,
            "render_prompt": render,
            "render_prompt_digest": sha256(render.encode()),
            "steps": [],
            "image_calls": 0,
            "reasoning_calls": 0,
            "actual_cost_fen": None,
        },
        "queued",
        dedupe=f"creation-image:{run.id}",
    )
    change(head(db, run.project_id), current_prompt_id=prompt["id"], task_id=image_task.id)
    change(
        run,
        prompt_id=prompt["id"],
        spec_id=spec.id,
        image_task_id=image_task.id,
        base_version_id=data.base_version_id,
        stage="image",
    )
    revision(db, run, "creation_auto_freeze")


def finish_error(task_id, error, receipt=None):
    code = (
        error.code
        if isinstance(error, AgentError)
        else "MODEL_SCHEMA_INVALID"
        if isinstance(error, (ValidationError, ValueError))
        else "INTERNAL_ERROR"
    )
    with transaction() as db:
        task = require(db, task_id, "task")
        run = require(db, task.payload["creation_run_id"], "creation_run")
        stage = "image" if task.payload["mode"] == "image_only" else "text"
        reserve = reservation(db, run, stage)
        sent = reserve and reserve.status in {"sent", "unknown"}
        unknown = sent and code in {"CALL_OUTCOME_UNKNOWN", "UNKNOWN_CALL", "INTERNAL_ERROR"}
        if reserve and reserve.status == "sent":
            quota.complete(db, reserve, "unknown" if unknown else "failed_after_sent", receipt)
        quota.release_unsent(db, run)
        run.status = (
            "unknown"
            if unknown
            else "superseded"
            if code == "EXECUTION_SUPERSEDED"
            else "needs_input"
            if code == "CONTEXT_MISMATCH"
            else "blocked"
            if code
            in {
                "CAPABILITY_UNAVAILABLE",
                "CREATION_AUTHORIZATION_REQUIRED",
                "BUDGET_EXHAUSTED",
                "CALL_LIMIT_EXHAUSTED",
                "PROVIDER_BUSY",
                "ROLE_FORBIDDEN",
                "NOT_FOUND",
            }
            else "failed"
        )
        task.status = "interrupted" if unknown else run.status
        message = "原调用结果未知，请核对原任务；不会重复收费" if unknown else "本次执行已停止，输入与已有结果已保留"
        change(run, reason=code, error={"code": code, "message": message})
        change(
            task,
            steps=[
                {**s, "status": "unknown" if unknown else "failed"} if s.get("status") == "pending" else s
                for s in task.payload.get("steps", [])
            ],
            error={"code": code, "message": message},
        )
        change(
            run,
            steps=[
                {
                    **s,
                    "status": "unknown" if unknown else "failed_after_sent" if sent else "failed_not_sent",
                    "finished_at": now(),
                }
                if s.get("status") == "sent"
                else s
                for s in run.payload.get("steps", [])
            ],
        )


def image_dispatch(db, task, provider):
    run = require(db, task.payload["creation_run_id"], "creation_run")
    user = worker_actor(db, run)
    if task.payload["image_calls"] >= run.payload["max_image_calls"]:
        raise AgentError("CALL_LIMIT_EXHAUSTED", "图片任务次数已用完，未再次派发", 409)
    h = head(db, run.project_id)
    if (
        h.payload.get("latest_user_message_id") != run.payload["message_id"]
        or h.payload.get("current_prompt_id") != task.payload["prompt_id"]
        or h.payload.get("spec_id") != task.payload["spec_id"]
        or workbench.source_identity_digest(db, run.project_id, user) != run.payload["source_identity_digest"]
    ):
        raise AgentError("EXECUTION_SUPERSEDED", "图片尚未派发，执行依据已变化", 409)
    reserve = reservation(db, run, "image")
    quota.dispatch(db, reserve, run, provider.image_fen)
    return reserve


def place_canvas(db, run, version):
    try:
        worker_actor(db, run)
    except AgentError:
        return "unavailable"
    current = workbench.canvas_document(db, run.project_id)
    nodes = current["layout"]["nodes"]
    if any(n["kind"] == "version" and n["ref_id"] == version.id for n in nodes):
        return "placed"
    image = version.payload["image"]
    x = max(n["x"] + n["width"] for n in nodes) + 32 if nodes else 80
    node = {
        "id": f"generated_{version.id}",
        "kind": "version",
        "ref_id": version.id,
        "x": x,
        "y": 80,
        "width": 480,
        "height": 480 * image["height"] / image["width"],
    }
    try:
        workbench.CanvasNode.model_validate(node)
        if len(nodes) >= 200:
            return "limit_reached"
    except ValidationError:
        return "limit_reached"
    result = {
        **current,
        "layout_revision": current["layout_revision"] + 1,
        "layout": {**current["layout"], "nodes": [*nodes, node]},
        "updated_at": now(),
        "updated_by": run.payload["actor"],
    }
    canvas = db.get(Record, f"canvas_{run.project_id}")
    if canvas:
        canvas.payload, canvas.updated_at = result, result["updated_at"]
    else:
        create(db, run.project_id, "canvas", result, "saved", id=f"canvas_{run.project_id}")
    return "placed"


def image_completed(db, task, version, receipt):
    run = require(db, task.payload["creation_run_id"], "creation_run")
    reserve = reservation(db, run, "image")
    quota.complete(db, reserve, "succeeded", receipt)
    step = task.payload["steps"][0]
    run.status = "succeeded"
    change(
        run,
        stage="done",
        version_id=version.id,
        canvas_placement=place_canvas(db, run, version),
        steps=[
            *[s for s in run.payload["steps"] if s.get("stage") != "image"],
            {
                "stage": "image",
                "status": "succeeded",
                "attempt_id": step["id"],
                "request_id": receipt.get("request_id") or None,
                "request_model": step["configured_model"],
                "response_model": receipt.get("model"),
                "started_at": step["started_at"],
                "finished_at": now(),
                "usage": receipt.get("usage"),
                "actual_cost_fen": receipt.get("actual_cost_fen"),
            },
        ],
    )
    try:
        revision(db, run, "creation_image_completed")
    except AgentError:
        pass  # Already-sent result is retained even if personnel access was revoked.


def recover(db):
    for r in db.scalars(select(ExecutionReservation).where(ExecutionReservation.status == "sent")):
        quota.complete(db, r, "unknown")
        run = db.get(Record, r.run_id)
        if run:
            run.status = "unknown"
            change(
                run,
                reason="CALL_OUTCOME_UNKNOWN",
                error={"code": "CALL_OUTCOME_UNKNOWN", "message": "服务重启，原阶段结果未知，不重新派发"},
                steps=[
                    {**s, "status": "unknown"} if s.get("status") == "sent" else s for s in run.payload.get("steps", [])
                ],
            )
    for task in db.scalars(select(Record).where(Record.kind == "task", Record.status == "running")):
        if task.payload.get("creation_run_id") and not task.payload.get("steps"):
            task.status = "queued"  # Proved unsent; keep original task/reservation/action.


def rename(db, pid, data):
    user = validate_transaction_access(db)
    row, meta = project_binding(db, pid, user, write=True)
    if not isinstance(meta, IndependentProject):
        raise AgentError("ROLE_FORBIDDEN", "上游任务名称由任务管理维护", 403)
    if meta.revision != data.expected_revision:
        raise AgentError("STATE_CONFLICT", "项目已有新修订，请重新核对", 409)
    db.get(Project, pid).name = data.title
    create(db, pid, "project_rename", {"title": data.title, "created_by": user.username}, "saved")
    return {"project_id": pid, "title": data.title, "archived": False}


def archive(db, pid, data, action_id):
    user = validate_transaction_access(db)
    _, meta = project_binding(db, pid, user, write=True)
    if not isinstance(meta, IndependentProject):
        raise AgentError("ROLE_FORBIDDEN", "不能从创作菜单删除上游任务", 403)
    if meta.revision != data.expected_revision:
        raise AgentError("STATE_CONFLICT", "项目已有新修订，请重新核对", 409)
    for r in db.scalars(select(Record).where(Record.project_id == pid)):
        if (r.kind == "creation_run" and r.status in {"queued", "running", "unknown"}) or (
            r.kind == "task"
            and (
                r.status in {"queued", "running"}
                or any(s.get("status") in {"pending", "unknown"} for s in r.payload.get("steps", []))
            )
        ):
            raise AgentError("PROJECT_BUSY", "项目存在进行中或结果未知的调用，请先核对原任务", 409)
    # EngineAction sets archived after its final permission recheck, in the same commit.
    create(
        db,
        pid,
        "project_archive",
        {"archived_at": now(), "archived_by": user.username, "action_id": action_id},
        "saved",
    )
    return {"project_id": pid, "archived": True}
