"""Owned projects, immutable offline prompts and a one-call image execution path."""

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, StrictBool, model_validator
from sqlalchemy import select

from ..config import get_config
from ..models import DesignerUser, DesignHandoff, DesignProjectAccess, Project
from .access_context import project_binding, validate_transaction_access
from .managed_store import ImageReservation, IndependentProject, ManagedReceipt, canonical_bytes, sha256
from .schemas import SpecIn, Strict
from .service import ensure_idle, save_spec
from .store import AgentError, Record, change, create, head, now, require, uid


class ProjectIn(Strict):
    title: str = Field(min_length=1, max_length=120)
    design_object: str = Field(min_length=1, max_length=100)
    initial_prompt: str = Field(min_length=1, max_length=10000)
    output_kind: str = Field(pattern=r"^(effect_image|design_draft)$")

    @model_validator(mode="after")
    def nonblank(self):
        if not all(value.strip() for value in (self.title, self.design_object, self.initial_prompt)):
            raise ValueError("必填字段不可为空白")
        return self


class PromptIn(Strict):
    expected_prompt_id: str | None
    expected_spec_id: str | None = Field(default=None, min_length=1, max_length=100)
    candidate_id: str | None = Field(default=None, min_length=1, max_length=100)
    positive_prompt: str = Field(min_length=1, max_length=10000)
    avoid_items: list[str] = Field(max_length=100)
    output_kind: str = Field(pattern=r"^(effect_image|design_draft)$")
    base_version_id: str | None
    edit_region: str = Field(max_length=200)

    @model_validator(mode="after")
    def valid_edit(self):
        if not self.positive_prompt.strip() or any(not item.strip() or len(item) > 10000 for item in self.avoid_items):
            raise ValueError("提示词/避免项不可为空白")
        if self.base_version_id and not self.edit_region.strip():
            raise ValueError("修改需明确区域")
        return self


class ImageIn(Strict):
    prompt_id: str
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    authorized: StrictBool

    @model_validator(mode="after")
    def authorization(self):
        if self.authorized is not True:
            raise ValueError("需要明确确认")
        return self


def binding_context(db, pid, user):
    row, meta = project_binding(db, pid, user)
    independent = isinstance(meta, IndependentProject)
    owner = meta.owner_subject if independent else row.assignee
    allowed = []
    if user.role == "designer" and (independent or row.status == "assigned"):
        allowed = [
            "save_prompt",
            "generate_image",
            "select_version",
            "download",
            "save_canvas",
            "send_message",
            "save_brief",
        ]
    return {
        "project_id": pid,
        "source_mode": "independent" if independent else "upstream",
        "owner_subject": owner,
        "scope_id": meta.scope_id,
        "revision": meta.revision,
        "status": row.status,
        "allowed_actions": allowed,
    }


def create_project(db, data):
    user = validate_transaction_access(db)
    if user is None or user.role != "designer":
        raise AgentError("ROLE_FORBIDDEN", "请以设计员工身份创建本人项目", 403)
    project = Project(name=data.title, category=data.design_object)
    db.add(project)
    db.flush()
    cfg = get_config()
    local = IndependentProject(
        project_id=project.id,
        id=f"independent:{project.id}",
        owner_subject=user.username,
        scope_id=cfg.design_scope_id,
        target_instance_id=cfg.design_instance_id,
        revision=0,
        status="draft",
    )
    db.add(local)
    db.add(DesignProjectAccess(project_id=project.id, username=user.username))
    db.flush()
    prompt = save_prompt(
        db,
        project.id,
        PromptIn(
            expected_prompt_id=None,
            positive_prompt=data.initial_prompt,
            avoid_items=[],
            output_kind=data.output_kind,
            base_version_id=None,
            edit_region="",
        ),
    )
    return {**binding_context(db, project.id, user), "prompt": prompt}


def source_constraints(db, pid, user, data):
    row, meta = project_binding(db, pid, user, write=True)
    independent = isinstance(meta, IndependentProject)
    constraints = []
    if not independent:
        source = meta.proof
        expected_avoid = source["prompt"]["avoid_items"]
        if not set(expected_avoid).issubset(set(data.avoid_items)):
            raise AgentError("SOURCE_CONSTRAINT_CONFLICT", "不能删改上游已批准的避免项", 409)
        for text in source["requirement"]["constraints"]:
            if re.search(r"(?:取消|忽略|去掉|不必保留)\s*" + re.escape(text.strip()), data.positive_prompt):
                raise AgentError("SOURCE_CONSTRAINT_CONFLICT", "执行稿不能撤销上游硬条件", 409)
        constraints = [
            {
                "id": f"c_product_{i}",
                "kind": "must_keep",
                "text": text.strip(),
                "region": "整体",
                "verification": "visual",
            }
            for i, text in enumerate(source["requirement"]["constraints"])
        ] + [
            {
                "id": f"c_avoid_{i}",
                "kind": "forbidden",
                "text": text.strip(),
                "region": "整体",
                "verification": "visual",
            }
            for i, text in enumerate(expected_avoid)
        ]
    # All local avoid items are executable constraints, including independent projects.
    known = {c["text"] for c in constraints if c["kind"] == "forbidden"}
    constraints += [
        {"id": f"c_local_avoid_{i}", "kind": "forbidden", "text": text, "region": "整体", "verification": "visual"}
        for i, text in enumerate(data.avoid_items)
        if text not in known
    ]
    return constraints


def save_prompt(db, pid, data):
    from . import workbench

    user = validate_transaction_access(db)
    row, meta = project_binding(db, pid, user, write=True)
    ensure_idle(db, pid)
    h = head(db, pid)
    previous = h.payload.get("current_prompt_id")
    if data.expected_prompt_id != previous:
        raise AgentError("PROMPT_CONFLICT", "提示词已有新版本，请保留输入并重新核对", 409)
    if "expected_spec_id" in data.model_fields_set:
        workbench.check_head(db, pid, data.expected_prompt_id, data.expected_spec_id)
    if data.candidate_id and "expected_spec_id" not in data.model_fields_set:
        raise AgentError("INVALID_INPUT", "确认候选须提供要求单基准", 422)
    if data.candidate_id:
        workbench.candidate_for_confirmation(db, pid, data, user)
    if data.base_version_id:
        require(db, data.base_version_id, "version", pid)
    constraints = source_constraints(db, pid, user, data)
    spec = save_spec(
        db,
        pid,
        SpecIn(
            expected_spec_id=h.payload.get("spec_id"),
            intent=data.positive_prompt,
            constraints=constraints,
            deliverables="二维鞋服设计稿示意" if data.output_kind == "design_draft" else "鞋服效果示意图",
            base_version_id=data.base_version_id,
            edit_region=data.edit_region,
        ),
        actor=user.username,
    )
    spec.status = "confirmed"  # Explicitly saved execution brief; no model or automated review.
    projection = {
        "id": uid(),
        "project_id": pid,
        "positive_prompt": data.positive_prompt,
        "avoid_items": data.avoid_items,
        "output_kind": data.output_kind,
        "design_object": db.get(Project, pid).category,
        "base_version_id": data.base_version_id,
        "edit_region": data.edit_region,
        "source_mode": "independent" if isinstance(meta, IndependentProject) else "upstream",
        "source_brief_id": data.candidate_id,
        "confirmed_through_message_id": h.payload.get("latest_user_message_id"),
        "derived_from_prompt_id": previous,
        "created_by": user.username,
        "created_at": now(),
        "execution_intent_id": None,
        "confirmation_mode": "manual",
    }
    raw = canonical_bytes(projection)
    projection["digest"] = sha256(raw)
    change(
        spec,
        output_kind=data.output_kind,
        source_brief_id=data.candidate_id,
        execution_prompt_digest=projection["digest"],
    )
    saved = create(
        db,
        pid,
        "prompt",
        {
            "prompt": projection,
            "original_body_base64": base64.b64encode(raw).decode(),
            "spec_id": spec.id,
            "source_binding_digest": workbench.source_digest(db, pid, user),
            "source_identity_digest": workbench.source_identity_digest(db, pid, user),
        },
        "saved",
        id=projection["id"],
    )
    change(h, current_prompt_id=saved.id)
    if data.candidate_id:
        create(
            db,
            pid,
            "brief_confirmation",
            {"brief_id": data.candidate_id, "prompt_id": saved.id, "spec_id": spec.id, "created_by": user.username},
            "confirmed",
        )
    return projection


def list_projects(db, user, offset, limit):
    cfg = get_config()
    items = []
    for local in db.scalars(
        select(IndependentProject).where(
            IndependentProject.scope_id == cfg.design_scope_id,
            IndependentProject.target_instance_id == cfg.design_instance_id,
        )
    ):
        if local.status == "archived":
            continue
        if user.role == "manager" or (user.role == "designer" and local.owner_subject == user.username):
            p = db.get(Project, local.project_id)
            items.append(
                {
                    "project_id": p.id,
                    "title": p.name,
                    "source_mode": "independent",
                    "owner_subject": local.owner_subject,
                    "revision": local.revision,
                    "status": local.status,
                    "updated_at": head(db, p.id).updated_at,
                    "id": p.id,
                    "name": p.name,
                }
            )
    for row, meta in db.execute(
        select(DesignHandoff, ManagedReceipt)
        .join(ManagedReceipt, ManagedReceipt.handoff_id == DesignHandoff.id)
        .where(
            ManagedReceipt.scope_id == cfg.design_scope_id,
            ManagedReceipt.target_instance_id == cfg.design_instance_id,
            DesignHandoff.project_id.is_not(None),
        )
    ):
        access = db.get(DesignProjectAccess, row.project_id)
        if user.role == "manager" or (
            user.role == "designer" and row.assignee == user.username and access and access.username == user.username
        ):
            p = db.get(Project, row.project_id)
            items.append(
                {
                    "project_id": p.id,
                    "title": p.name,
                    "source_mode": "upstream",
                    "owner_subject": row.assignee,
                    "revision": meta.revision,
                    "status": row.status,
                    "updated_at": head(db, p.id).updated_at,
                    "id": p.id,
                    "name": p.name,
                }
            )
    from .workbench import existing_image

    for item in items:
        local = db.get(IndependentProject, item["project_id"])
        item["archived"] = False
        item["allowed_project_actions"] = (
            ["rename", "archive"] if local and user.role == "designer" and local.owner_subject == user.username else []
        )
        rows = [
            r for r in db.scalars(select(Record).where(Record.project_id == item["project_id"])) if r.kind != "canvas"
        ]
        item["updated_at"] = max([item["updated_at"], *[r.updated_at for r in rows]])
        item["cover_version_id"] = None
        item["cover_image_url"] = None
        for version in sorted(
            (r for r in rows if r.kind == "version"), key=lambda r: (r.created_at, r.id), reverse=True
        ):
            try:
                existing_image(version.payload.get("image") or {})
            except AgentError:
                continue
            item["cover_version_id"] = version.id
            item["cover_image_url"] = f"/api/design-versions/{version.id}/image"
            break
    items.sort(key=lambda item: (item["updated_at"], item["project_id"]), reverse=True)
    return {"items": items[offset : offset + limit], "total": len(items), "offset": offset, "limit": limit}


def image_grant():
    cfg = get_config()
    try:
        path = Path(cfg.design_image_authorization_file)
        if not cfg.design_image_only_enabled or not path.is_file() or path.stat().st_mode & 0o077:
            return None
        data = json.loads(path.read_text())
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", data["grant_id"])
            or type(data["project_id"]) is not int
            or type(data["expires_at"]) is not int
            or data["expires_at"] <= time.time()
            or type(data.get("max_calls")) is not int
            or data["max_calls"] != 1
            or data["scope_id"] != cfg.design_scope_id
            or data["source_mode"] not in {"upstream", "independent"}
        ):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def image_capability(db, pid=None, user=None):
    from .providers import Provider

    provider = Provider()
    configured = provider.capabilities().get("image_only", False)
    from . import execution_quota, interactive_policy

    p, ctx = interactive_policy.preview(db, user)
    from .public_budget import remaining as public_remaining
    public_left = public_remaining(db, "image")
    if ctx and ctx.purpose == "validation":
        return {
            "available": False,
            "reason": "CREATION_AUTHORIZATION_REQUIRED",
            "remaining": 0,
            "single_image": True,
            "concurrency": 1,
            "paid_text": False,
            "vision_check": False,
        }
    if p:
        manual_upstream = False
        if pid:
            try:
                manual_upstream = not isinstance(project_binding(db, pid, user, write=True)[1], IndependentProject)
            except AgentError:
                pass
        ready = (
            manual_upstream
            and (public_left is None or public_left > 0)
            and configured
            and 0 < provider.image_fen <= p["per_intent"]["image"]["max_cost_fen"]
            and not interactive_policy.subject_busy(db, user.username)
            and not execution_quota.busy(db)
        )
        return {
            "available": ready,
            "reason": "PUBLIC_BUDGET_EXHAUSTED" if public_left == 0 else
                      "ready" if ready else "CREATION_AUTHORIZATION_REQUIRED",
            "remaining": 1 if ready else 0,
            "single_image": True,
            "concurrency": 1,
            "paid_text": False,
            "vision_check": False,
        }
    grant = image_grant()
    used = bool(grant and db.get(ImageReservation, grant["grant_id"]))
    reason = (
        "ready"
        if configured and grant and not used
        else (
            "IMAGE_QUOTA_EXHAUSTED"
            if used
            else "IMAGE_AUTHORIZATION_REQUIRED"
            if not grant
            else "CAPABILITY_UNAVAILABLE"
        )
    )
    if pid is not None and grant:
        context = binding_context(db, pid, user)
        if (
            grant["project_id"] != pid
            or grant["source_mode"] != context["source_mode"]
            or grant["subject"] != context["owner_subject"]
            or user.username != grant["subject"]
            or user.role != "designer"
        ):
            reason = "IMAGE_AUTHORIZATION_REQUIRED"
    return {
        "available": reason == "ready",
        "reason": reason,
        "remaining": 1 if grant and not used and reason == "ready" else 0,
        "single_image": True,
        "concurrency": 1,
        "paid_text": False,
        "vision_check": False,
    }


def queue_image(db, pid, data, action_id):
    from .workbench import execution_state, source_digest, source_identity_digest

    user = validate_transaction_access(db)
    from .interactive_policy import legacy_submission, valid_policy

    _, meta = project_binding(db, pid, user, write=True)
    manual_upstream = not isinstance(meta, IndependentProject)
    trusted_execution = legacy_submission(db, manual_image=manual_upstream)
    p = valid_policy(db)
    if manual_upstream and p:
        from .providers import Provider

        cost = Provider().image_fen
        if cost <= 0 or cost > p["per_intent"]["image"]["max_cost_fen"]:
            raise AgentError("BUDGET_EXHAUSTED", "原上游单图费用不能超过当前本机受控上限", 409)
    if data.idempotency_key != action_id:
        raise AgentError("INVALID_INPUT", "生成请求编号须与动作编号一致", 422)
    h = head(db, pid)
    if h.payload.get("current_prompt_id") != data.prompt_id:
        raise AgentError("PROMPT_CONFLICT", "只能生成当前已保存提示词", 409)
    ensure_idle(db, pid)
    prompt_row = require(db, data.prompt_id, "prompt", pid)
    prompt = prompt_row.payload["prompt"]
    spec = require(db, prompt_row.payload["spec_id"], "spec", pid)
    binding_digest = source_digest(db, pid, user)
    identity_digest = source_identity_digest(db, pid, user)
    if execution_state(db, pid, user) != "ready":
        raise AgentError("EXECUTION_BRIEF_CONFLICT", "执行稿与要求单或来源不一致；请明确确认新执行稿", 409)
    capability = image_capability(db, pid, user)
    if not capability["available"]:
        raise AgentError(capability["reason"], "本项目没有可用的单张图片授权；输入已保存", 409)
    source_prompt = spec.payload.get("product_source", {}).get("prompt")
    render_prompt = (
        "仅生成一张二维鞋服设计示意图。不是实物试穿、纸样或生产证明。\n"
        + json.dumps(prompt, ensure_ascii=False)
        + "\n完整上游已批准提示词："
        + json.dumps(source_prompt, ensure_ascii=False)
        + "\n硬条件："
        + json.dumps(spec.payload["spec"]["constraints"], ensure_ascii=False)
    )
    if len(render_prompt) > 10000:
        raise AgentError("INVALID_INPUT", "完整执行提示词超过10000字符；未截断或调用供应商", 422)
    operating_run = operating_reservation = None
    if manual_upstream and p:
        from .interactive_policy import manual_upstream_run
        from .providers import Provider

        operating_run, operating_reservation = manual_upstream_run(
            db, pid, user, action_id, Provider(), h, prompt, spec, meta, identity_digest
        )
    task = create(
        db,
        pid,
        "task",
        {
            "mode": "image_only",
            **trusted_execution,
            "prompt_id": data.prompt_id,
            "prompt_digest": prompt["digest"],
            "source_binding_digest": binding_digest,
            "source_identity_digest": identity_digest,
            "spec_id": spec.id,
            "base_version_id": prompt["base_version_id"],
            "actor": user.username,
            "render_prompt": render_prompt,
            "render_prompt_digest": sha256(render_prompt.encode()),
            "steps": [],
            "image_calls": 0,
            "reasoning_calls": 0,
            "actual_cost_fen": None,
            **(
                {"creation_run_id": operating_run.id, "execution_reservation_id": operating_reservation.id}
                if operating_run
                else {}
            ),
        },
        "queued",
        dedupe=f"image-only:{pid}:{action_id}",
    )
    if operating_run:
        change(operating_run, image_task_id=task.id)
    else:
        grant = image_grant()
        db.add(
            ImageReservation(
                grant_id=grant["grant_id"], task_id=task.id, project_id=pid, subject=user.username, authorization=grant
            )
        )
    local = db.get(IndependentProject, pid)
    if local:
        local.status = "working"
    change(h, task_id=task.id)
    return {"task_id": task.id, "project_id": pid, "prompt_id": data.prompt_id, "status": "queued"}


def run_image_task(id, provider, owner=None):
    """Commit dispatch intention before the only external call; never hold SQLite over network."""
    from . import assets
    from .store import WorkerLease, transaction

    with transaction() as db:
        task = require(db, id, "task")
        if task.status != "running" or task.payload.get("steps"):
            raise AgentError("UNKNOWN_CALL", "原调用已经派发或任务已停止，禁止重新收费", 409)
        if owner:
            lease = db.get(WorkerLease, 1)
            if not lease or lease.owner != owner or lease.expires < time.time():
                raise AgentError("LEASE_LOST", "任务执行权已经变化", 409)
        reservation = db.scalar(select(ImageReservation).where(ImageReservation.task_id == id))
        direct = bool(task.payload.get("creation_run_id"))
        if direct:
            from .direct_create import image_dispatch

            reservation = image_dispatch(db, task, provider)
            subject = reservation.payload["actor"]
        else:
            from .interactive_policy import guard_legacy, valid_policy

            actor = db.get(DesignerUser, task.payload.get("actor"), populate_existing=True)
            _, meta = project_binding(db, task.project_id, actor, write=True)
            manual_upstream = not isinstance(meta, IndependentProject)
            guard_legacy(
                db,
                actor=task.payload.get("actor"),
                context=task.payload.get("auth_context_id"),
                worker=True,
                manual_image=manual_upstream,
            )
            p = valid_policy(db)
            if task.payload.get("legacy_policy_sha256") != (sha256(canonical_bytes(p)) if p else None):
                raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "原单图策略已变化，未再次派发", 409)
            grant = image_grant()
            if not reservation or grant != reservation.authorization:
                raise AgentError("IMAGE_AUTHORIZATION_REQUIRED", "图片授权已变化或过期，未出网", 409)
            subject = reservation.subject
        authorization_ref = reservation.grant_id
        actor = db.get(DesignerUser, subject, populate_existing=True)
        if not actor or not actor.active or actor.role != "designer":
            raise AgentError("IMAGE_AUTHORIZATION_REQUIRED", "原设计人员当前不再有执行权限，未出网", 409)
        project_binding(db, task.project_id, actor, write=True)
        provider.require("image_only")
        prompt = task.payload["render_prompt"]
        base = task.payload["base_version_id"]
        images = []
        if base:
            base_image = require(db, base, "version", task.project_id).payload["image"]
            images = [(f"base_version_id={base}; 修改底图", base_image)]
        step = {
            "id": reservation.payload["attempt_id"] if direct else uid(),
            "tool": "generate_image_only",
            "status": "pending",
            "started_at": now(),
            "prompt_digest": task.payload["render_prompt_digest"],
            "provider": provider.image_url,
            "configured_model": provider.image_model,
            "reserved_cost_fen": provider.image_fen,
        }
        if not direct:
            from .execution_quota import acquire_slot

            acquire_slot(db, step["id"])
        else:
            run = require(db, task.payload["creation_run_id"], "creation_run")
            change(
                run,
                steps=[
                    *run.payload["steps"],
                    {
                        "stage": "image",
                        "status": "sent",
                        "attempt_id": step["id"],
                        "request_id": None,
                        "request_model": provider.image_model,
                        "response_model": None,
                        "started_at": step["started_at"],
                        "actual_cost_fen": None,
                        "usage": None,
                    },
                ],
            )
        change(task, steps=[step], image_calls=1)
    try:
        raw = provider.render_direct(prompt, images)
        saved = assets.save_original_image(raw)
        receipt = getattr(provider, "last_receipt", {})
        with transaction() as db:
            task = require(db, id, "task")
            spec = require(db, task.payload["spec_id"], "spec", task.project_id)
            review = {
                "goal": {"status": "unknown", "evidence": "未调用付费识图；等待人工查看"},
                "preservation": {"status": "unknown", "evidence": "未检查"},
                "checks": [
                    {"constraint_id": c["id"], "status": "unknown", "evidence": "未检查"}
                    for c in spec.payload["spec"]["constraints"]
                ],
            }
            completed_at = now()
            provenance = {
                "schema_version": "pa-image-generation/1",
                "provider": urlsplit(provider.image_url).hostname,
                "model": provider.image_model,
                "response_model": receipt.get("model"),
                "generation_task_id": id,
                "generation_attempt_id": step["id"],
                "provider_request_id": receipt.get("request_id") or None,
                "execution_prompt_id": task.payload["prompt_id"],
                "execution_prompt_digest": task.payload["render_prompt_digest"],
                "execution_prompt_base64": base64.b64encode(prompt.encode()).decode(),
                "source_product_prompt_digest": spec.payload.get("product_source", {}).get("prompt_digest"),
                "authorization_ref": authorization_ref,
                "started_at": step["started_at"],
                "completed_at": completed_at,
                "actual_cost_fen": receipt.get("actual_cost_fen"),
            }
            version = create(
                db,
                task.project_id,
                "version",
                {
                    "task_id": id,
                    "spec_id": spec.id,
                    "source_fingerprint": spec.payload["fingerprint"],
                    "prompt_id": task.payload["prompt_id"],
                    "prompt_digest": task.payload["prompt_digest"],
                    "parent_version_id": task.payload["base_version_id"],
                    "created_by": task.payload["actor"],
                    "image": saved,
                    "generation_mode": "model_generated",
                    "execution_mode": "image_only",
                    "provider": {
                        "service": provider.image_url,
                        "configured_model": provider.image_model,
                        "actual_model": receipt.get("model"),
                        "request_id": receipt.get("request_id"),
                    },
                    "receipt": receipt,
                    "actual_cost_fen": receipt.get("actual_cost_fen"),
                    "generation_provenance": provenance,
                    "review": review,
                    "check_status": "not_checked",
                },
                "candidate",
            )
            h = head(db, task.project_id)
            current = h.payload.get("current_prompt_id") == task.payload["prompt_id"]
            if direct:
                run = require(db, task.payload["creation_run_id"], "creation_run")
                current = current and h.payload.get("latest_user_message_id") == run.payload["message_id"]
            if current and task.status == "running":
                change(h, version_id=version.id)
            if task.status == "running":
                task.status = "succeeded"
            change(
                task,
                version_ids=[version.id],
                steps=[
                    {
                        **step,
                        "status": "done",
                        "finished_at": now(),
                        "receipt": receipt,
                        "asset_sha256": saved["sha256"],
                    }
                ],
                outcome="实际图已保存；等待人工查看",
            )
            local = db.get(IndependentProject, task.project_id)
            if local:
                local.status = "ready"
            if direct:
                from .direct_create import image_completed

                image_completed(db, task, version, receipt)
            else:
                from .managed_store import ProviderSlot

                slot = db.get(ProviderSlot, 1)
                if slot and slot.attempt_id == step["id"]:
                    slot.status = "idle"
    except Exception as error:
        # Once a request was dispatched, even invalid/download/local-storage failures may have incurred a charge.
        raise AgentError(
            "CALL_OUTCOME_UNKNOWN",
            "图片调用已派发但结果未完整保存；只读核查原任务，禁止再发",
            502,
            receipt=getattr(provider, "last_receipt", {}),
        ) from error
