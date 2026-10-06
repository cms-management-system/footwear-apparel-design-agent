"""Single leased worker; journals external calls before dispatch. Never replays unknown calls."""

import threading
import time

from pydantic import ValidationError
from sqlalchemy import select

from . import assets, style
from . import review as image_review
from .presentation import exploration_axes, presentation_for
from .providers import Provider
from .schemas import Action, Inspection
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


def sources(db, task):
    spec = require(db, task.payload["spec_id"], "spec", task.project_id).payload["spec"]
    images = [
        (
            f"asset_id={r['asset_id']}; role={r['role']}; region={r['region']}; {r['instruction']}",
            require(db, r["asset_id"], "asset", task.project_id).payload,
        )
        for r in spec["references"]
    ]
    if spec["base_version_id"]:
        base = require(db, spec["base_version_id"], "version", task.project_id)
        images.insert(0, (f"base_version_id={base.id}; 修改底图", base.payload["image"]))
    return spec, images


def revision_direction(direction, spec):
    """Keep the selected style while replacing palette guidance for a color edit."""
    if not direction or not spec.get("base_version_id"):
        return direction
    region = spec.get("edit_region", "")
    latest = next(
        (
            item["text"]
            for item in reversed(spec["constraints"])
            if item["kind"] == "may_change" and item["region"].strip() == region.strip()
        ),
        None,
    )
    if not latest:
        return direction
    updated = {**direction, "revision_override": f"本轮修改 {region}：{latest}。与此冲突的旧方向描述不再适用。"}
    if any(word in region for word in ("颜色", "配色", "色彩", "色号")):
        updated["color_story"] = latest
    return updated


def event(db, task, tool, provider):
    p = task.payload
    if task.status != "running":
        raise AgentError("STOPPED", "任务已停止")
    if head(db, task.project_id).payload.get("spec_id") != p["spec_id"]:
        raise AgentError("STALE_SOURCE", "任务引用的要求单已更新")
    # Enabling images must not invalidate an existing understanding conversation.
    ignored = {"monthly_allocation_fen"}
    if p["mode"] == "understand":
        ignored.update({"design", "image_service", "image_model", "image_call_max_fen", "edit_control"})
    current_config = {k: v for k, v in provider.capabilities().items() if k not in ignored}
    original_config = {k: v for k, v in p["provider"].items() if k not in ignored}
    if current_config != original_config:
        raise AgentError("PROVIDER_CHANGED", "服务或费用配置已变化，请重新授权")
    is_image = tool in {"generate_design", "edit_design"}
    field = "image_calls" if is_image else "reasoning_calls"
    cap = "max_image_calls" if is_image else "max_reasoning_calls"
    cost = provider.image_fen if is_image else provider.reasoning_fen
    # Understanding is an ongoing, user-driven conversation. Its old per-task
    # ceilings ended ordinary design discussions after a handful of replies.
    # Keep journaling each provider call, but apply these ceilings only to the
    # separately requested image-generation task.
    if p["mode"] in {"design", "style"} and p[field] >= p[cap]:
        raise AgentError("CALL_LIMIT_EXHAUSTED", "本轮调用次数已用完；已有图片和检查结果保留")
    if p["mode"] in {"design", "style"} and (
        p.get("max_cost_fen") is not None and p["reserved_cost_fen"] + cost > p["max_cost_fen"]
    ):
        raise AgentError("BUDGET_EXHAUSTED", "本轮设置的费用上限已用完；已有图片和检查结果保留")
    step = {
        "id": uid(),
        "tool": tool,
        "status": "pending",
        "reserved_cost_fen": cost,
        "started_at": now(),
        "prompt_version": "v1",
    }
    change(task, **{field: p[field] + 1}, reserved_cost_fen=p["reserved_cost_fen"] + cost, steps=[*p["steps"], step])
    return step["id"]


def finish_event(task, step_id, result, receipt=None):
    steps = [
        {**s, "status": "done", "finished_at": now(), "result": result, "receipt": receipt or {}}
        if s["id"] == step_id
        else s
        for s in task.payload["steps"]
    ]
    change(task, steps=steps)


def fail_task(id, error):
    with transaction() as db:
        task = require(db, id, "task")
        steps = [
            {
                **s,
                "status": "unknown" if error.code == "CALL_OUTCOME_UNKNOWN" else "failed",
                "error_code": error.code,
                "validation_errors": error.details,
                "receipt": error.receipt,
                "finished_at": now(),
            }
            if s["status"] == "pending"
            else s
            for s in task.payload["steps"]
        ]
        status = {"CALL_OUTCOME_UNKNOWN": "interrupted", "BUDGET_EXHAUSTED": "budget_exhausted"}.get(
            error.code, "failed"
        )
        if task.status != "cancelled":
            task.status = status
        change(task, steps=steps, error={"code": error.code, "message": error.message}, outcome=error.message)


def one_step(id, provider, owner=None):
    with transaction() as db:
        task = require(db, id, "task")
        if owner:
            lease = db.get(WorkerLease, 1)
            if not lease or lease.owner != owner or lease.expires < int(time.time()):
                raise AgentError("LEASE_LOST", "执行权已变更，已停止后续调用")
        if task.status != "running":
            return False
        spec, images = sources(db, task)
        payload = task.payload
        action = payload.get("pending_action")
        current = require(db, payload["current_version_id"], "version") if payload["current_version_id"] else None
        if action:
            action = Action.model_validate(action)
            tool = action.action
        else:
            tool = "plan"
        # Multiple alternatives share one confirmed spec but are independent designs, not revisions.
        count = payload.get("design_count", 1)
        direction_ids = payload.get("direction_version_ids", [])
        if payload["mode"] == "design" and count > 1:
            if current and current.payload.get("review"):
                if len(direction_ids) >= count:
                    tool = "finish"
                    warning = (
                        "部分图片的自动核验暂未完成，请查看各款说明。"
                        if payload.get("review_warnings")
                        else "请比较后选择喜欢的方向。"
                    )
                    action = Action(action="finish", summary=f"已生成 {count} 款设计。{warning}")
                else:
                    current = None
                    change(task, current_version_id=None)
                    tool = "edit_design" if spec["base_version_id"] else "generate_design"
            elif spec["references"] and not payload["observations"]:
                tool = "inspect_assets"
            elif current:
                tool = "compare_design"
            else:
                tool = "edit_design" if spec["base_version_id"] else "generate_design"
        # A confirmed local revision is exactly one edit + one check, not another alternatives batch.
        if payload["mode"] == "design" and count == 1 and spec["base_version_id"]:
            if current and current.payload.get("review"):
                tool = "finish"
                action = Action(action="finish", summary="这款的修改版已生成，请在原方案下查看图片与检查结果。")
            elif spec["references"] and not payload["observations"]:
                tool = "inspect_assets"
            else:
                tool = "compare_design" if current else "edit_design"
        if current and payload.get("review_retry_version") == current.id:
            tool = "compare_design"
        # inspect_assets is only for original references. Review an existing candidate
        # with compare_design even when the planner uses the broader word "inspect".
        if (
            payload["mode"] == "design"
            and tool == "inspect_assets"
            and current
            and (not spec["references"] or payload["observations"])
        ):
            tool = "compare_design"
        if payload.get("review_only"):
            if payload.get("review_completed"):
                tool = "finish"
                action = Action(action="finish", summary="已检查这张已有图片，请查看逐项结果后确认。")
            elif spec["references"] and not payload["observations"]:
                tool = "inspect_assets"
            else:
                tool = "compare_design"
        direction = None
        if payload.get("style_plan_id"):
            plan = require(db, payload["style_plan_id"], "style_plan", task.project_id)
            direction_id = payload["style_direction_ids"][len(direction_ids)] if len(direction_ids) < count else None
            selected_direction = next((d for d in plan.payload["directions"] if d["id"] == direction_id), None)
            if direction_id and not selected_direction:
                raise AgentError("DIRECTION_CHANGED", "选定的风格方向不存在")
            if selected_direction:
                direction = {
                    **selected_direction,
                    "number": len(direction_ids) + 1,
                    "total": count,
                    "previous_directions": [
                        (require(db, vid, "version", task.project_id).payload.get("review") or {}).get("summary", "")
                        for vid in direction_ids
                    ],
                }
        elif spec["base_version_id"]:
            base_version = require(db, spec["base_version_id"], "version", task.project_id)
            if base_version.payload.get("style_direction"):
                direction = base_version.payload["style_direction"]
        elif count > 1:
            direction = {
                "number": len(direction_ids) + 1,
                "total": count,
                "explore": exploration_axes(spec)[min(len(direction_ids), 5)],
                "previous_directions": [
                    (require(db, vid, "version", task.project_id).payload.get("review") or {}).get("summary", "")
                    for vid in direction_ids
                ],
            }
        direction = revision_direction(direction, spec)
        presentation = payload.get("presentation")
        if tool in {"generate_design", "edit_design"} and not presentation:
            presentation = presentation_for(spec)
            change(task, presentation=presentation)
        if tool in {"ask", "finish", "propose_spec", "reply"}:
            if tool == "reply":
                if payload["mode"] != "understand":
                    raise AgentError("TOOL_NOT_ALLOWED", "设计执行轮必须完成实际图片检查")
                task.status = "awaiting_review"
                change(task, outcome=action.answer)
            elif tool == "ask":
                task.status = "awaiting_input"
                change(task, questions=action.questions)
            elif tool == "propose_spec":
                if payload["mode"] != "understand":
                    raise AgentError("TOOL_NOT_ALLOWED", "出图任务不能自行改写已确认要求")
                if spec["references"] and not payload["observations"]:
                    raise AgentError("INSPECTION_REQUIRED", "必须先读取参考图片")
                proposed = action.proposed_spec
                previous = {c["id"]: c for c in spec["constraints"]}
                proposed_constraints = {c.id: c.model_dump() for c in proposed.constraints}
                if any(proposed_constraints.get(k) != v for k, v in previous.items()):
                    raise AgentError("CONSTRAINT_LOSS", "智能体尝试改写已有约束，已阻止")
                if [r.model_dump() for r in proposed.references] != spec["references"]:
                    raise AgentError("REFERENCE_CHANGED", "智能体不能自行改变参考图用途")
                proposed.expected_spec_id = payload["spec_id"]
                proposed.base_version_id = spec["base_version_id"]
                proposed.edit_region = spec["edit_region"]
                proposal = save_spec(db, task.project_id, proposed, check_idle=False)
                task.status = "awaiting_review"
                change(task, proposed_spec_id=proposal.id, outcome="要求单已整理，请设计师核对并确认")
            else:
                if payload["mode"] != "design" or current is None or not current.payload.get("review"):
                    raise AgentError("REVIEW_REQUIRED", "尚未生成并实际检查候选图，不能完成")
                task.status = "awaiting_review"
                change(task, outcome="候选与逐项检查已保存，请设计师评审")
            text = (
                action.answer if tool == "reply" else "\n".join(action.questions) if tool == "ask" else action.summary
            )
            create(
                db,
                task.project_id,
                "message",
                {
                    "role": "assistant",
                    "text": text,
                    "task_id": id,
                    "spec_id": task.payload.get("proposed_spec_id") if tool == "propose_spec" else None,
                },
                "sent",
            )
            change(
                task,
                live_text="",
                live_summary="",
                pending_action=None,
                steps=[
                    *task.payload["steps"],
                    {"id": uid(), "tool": tool, "status": "done", "summary": action.summary, "finished_at": now()},
                ],
            )
            return False
        if tool in {"generate_design", "edit_design", "compare_design"}:
            s = require(db, payload["spec_id"], "spec")
            if payload["mode"] != "design" or s.status != "confirmed":
                raise AgentError("TOOL_NOT_ALLOWED", "未确认要求单的任务不能出图")
            if spec["references"] and not payload["observations"]:
                raise AgentError("INSPECTION_REQUIRED", "必须先读取原始参考图")
            if payload.get("inspection_conflicts"):
                raise AgentError("SPEC_CONFLICT", "素材理解发现冲突，请修改要求单后确认")
        if tool == "generate_design" and (current or spec["base_version_id"]):
            raise AgentError("EDIT_REQUIRED", "已有底图必须通过编辑工具修改")
        if tool == "edit_design" and not (current or spec["base_version_id"]):
            raise AgentError("BASE_REQUIRED", "局部编辑缺少底图")
        if tool == "edit_design" and current and not current.payload.get("review"):
            raise AgentError("REVIEW_REQUIRED", "自动修正前必须检查实际图片")
        if tool == "compare_design" and current is None:
            raise AgentError("IMAGE_REQUIRED", "没有可检查的候选图")
        context = {
            "mode": payload["mode"],
            "schema_feedback": payload.get("schema_feedback", []),
            "conversation": [
                {"role": m.payload["role"], "text": m.payload["text"]}
                for m in records(db, task.project_id, "message")[-16:]
                if m.payload["role"] != "system"
            ],
            "spec": spec,
            "observations": payload["observations"],
            "inspection_conflicts": payload.get("inspection_conflicts", []),
            "feedback": payload["feedback"],
            "current_version_id": payload["current_version_id"],
            "existing_designs": [
                {
                    "id": v.id, "spec_id": v.payload["spec_id"], "status": v.status,
                    "has_image": bool(v.payload.get("image")),
                    "review_summary": (v.payload.get("review") or {}).get("summary"),
                    "task_error": require(
                        db, v.payload.get("review_task_id", v.payload["task_id"]), "task"
                    ).payload.get("error"),
                }
                for v in records(db, task.project_id, "version")[-6:]
            ],
            "review": current.payload.get("review") if current else None,
            "remaining_image_calls": payload["max_image_calls"] - payload["image_calls"],
            "remaining_reasoning_calls": (
                None if payload["mode"] == "understand"
                else payload["max_reasoning_calls"] - payload["reasoning_calls"]
            ),
        }
        if tool == "edit_design" and current:
            images.insert(0, (f"current_version_id={current.id}; 本轮修正底图", current.payload["image"]))
        if tool == "compare_design":
            prior_id = current.payload.get("parent_version_id")
            if prior_id and prior_id != spec["base_version_id"]:
                prior = require(db, prior_id, "version", task.project_id)
                images.append((f"parent_version_id={prior_id}; 修正前底图", prior.payload["image"]))
            images.append((f"candidate_id={current.id}; 待检查候选", current.payload["image"]))
        step_id = event(db, task, tool, provider)
        pid, spec_id, source_hash = task.project_id, payload["spec_id"], payload["source_fingerprint"]
        parent_id = current.id if current else spec["base_version_id"]
    last_update = [0.0]

    def preview(text):
        if time.monotonic() - last_update[0] < 0.15:
            return
        with transaction() as db:
            running = require(db, id, "task")
            if running.status == "running":
                change(running, live_text=text.get("answer", "")[:4000], live_summary=text.get("summary", "")[:1500])
        last_update[0] = time.monotonic()

    provider.on_preview = preview
    # No DB transaction or lock is held across an external request.
    if tool == "plan":
        try:
            result = Action.model_validate(provider.plan(context)).model_dump()
        except AgentError as exc:
            # Only a completely received, invalid structured plan gets one budgeted correction.
            # Unknown network outcomes and image calls are never replayed here.
            if exc.code != "MODEL_SCHEMA_INVALID" or not exc.details:
                raise
            with transaction() as db:
                task = require(db, id, "task")
                if task.status != "running" or task.payload.get("schema_repair_count", 0) >= 1:
                    raise
                if head(db, task.project_id).payload.get("spec_id") != task.payload["spec_id"]:
                    raise
                steps = [
                    {
                        **step,
                        "status": "failed",
                        "finished_at": now(),
                        "error_code": exc.code,
                        "validation_errors": exc.details,
                        "receipt": exc.receipt,
                        "summary": "回复格式需要重新整理",
                    }
                    if step["id"] == step_id
                    else step
                    for step in task.payload["steps"]
                ]
                change(
                    task,
                    steps=steps,
                    schema_repair_count=1,
                    schema_feedback=exc.details,
                    live_text="",
                    live_summary="正在重新整理这次回复…",
                )
            return True
    elif tool == "inspect_assets":
        result = Inspection.model_validate(provider.inspect(spec, images)).model_dump()
        expected = {r["asset_id"] for r in spec["references"]}
        actual = [r["asset_id"] for r in result["observations"]]
        if set(actual) != expected or len(actual) != len(expected):
            raise AgentError("INSPECTION_INVALID", "视觉结果未逐张对应原始素材")
    elif tool in {"generate_design", "edit_design"}:
        result = assets.save_image(
            provider.render(
                {**spec, "presentation": presentation, **({"design_direction": direction} if direction else {})},
                images,
                context.get("review"),
            )
        )
    else:
        review_spec = dict(spec)
        if presentation:
            review_spec["presentation"] = presentation
        if count > 1:
            review_spec["candidate_context"] = {
                "number": current.payload["design_index"],
                "total": count,
                "scope": "本次只检查当前一款。总款数由任务逐款生成，不要求单张图包含全部款数。",
            }
        if current.payload.get("style_direction"):
            review_spec["style_direction"] = current.payload["style_direction"]
        review_spec, review_images, image_mapping = image_review.prepare(review_spec, images)
        if payload.get("review_retry_version") == current.id:
            review_spec["validation_feedback"] = (
                "上次核验格式或图片引用无效。请重新看图，reference_ids只可选available_image_ids中的短编号，不加前缀。"
            )
        try:
            result = image_review.resolve(provider.compare(review_spec, review_images), image_mapping, spec)
        except (AgentError, ValidationError) as exc:
            if isinstance(exc, AgentError) and exc.code not in {"REVIEW_INVALID", "MODEL_SCHEMA_INVALID"}:
                raise
            with transaction() as db:
                active = require(db, id, "task")
                p = active.payload
                remaining = max(0, count - len(direction_ids)) if count > 1 else 0
                needed = remaining * (provider.image_fen + provider.reasoning_fen) + provider.reasoning_fen
                retry = (
                    active.status == "running"
                    and p.get("review_retry_version") != current.id
                    and head(db, pid).payload.get("spec_id") == spec_id
                    and p["reasoning_calls"] + remaining + 1 <= p["max_reasoning_calls"]
                    and (p.get("max_cost_fen") is None or p["reserved_cost_fen"] + needed <= p["max_cost_fen"])
                )
                details = exc.details if isinstance(exc, AgentError) else [{"path": "review", "type": "schema_invalid"}]
                steps = [
                    {
                        **step,
                        "status": "failed",
                        "finished_at": now(),
                        "error_code": "REVIEW_INVALID",
                        "validation_errors": details,
                        "receipt": getattr(provider, "last_receipt", {}),
                        "summary": "正在重新核对检查结果" if retry else "自动核验暂未完成，图片已保留",
                    }
                    if step["id"] == step_id
                    else step
                    for step in p["steps"]
                ]
                change(
                    active,
                    steps=steps,
                    live_text="",
                    live_summary="正在重新核对图片…" if retry else "",
                    review_retry_version=current.id if retry else None,
                )
            if retry:
                return True
            result = image_review.incomplete(spec)
            review_incomplete = True
        else:
            review_incomplete = False
        physical = {c["id"] for c in spec["constraints"] if c["verification"] == "physical"}
        for check in result["checks"]:
            if check["constraint_id"] in physical:
                check.update(status="unknown", evidence="物理性能或尺寸不能通过效果图确认，需实物或检测依据")
    with transaction() as db:
        task = require(db, id, "task")
        if not (tool == "compare_design" and review_incomplete):
            finish_event(task, step_id, result, getattr(provider, "last_receipt", {}))
        change(task, live_text="", live_summary="", schema_feedback=[])
        change(task, pending_action=result if tool == "plan" else None)
        if tool == "inspect_assets":
            change(task, observations=result["observations"], inspection_conflicts=result["conflicts"])
        elif tool in {"generate_design", "edit_design"}:
            inherited_direction = {}
            if direction and not payload.get("style_plan_id") and parent_id:
                parent = require(db, parent_id, "version", pid)
                if parent.payload.get("style_plan_id"):
                    inherited_direction = {
                        "style_plan_id": parent.payload["style_plan_id"],
                        "style_direction_id": parent.payload["style_direction_id"],
                        "style_direction": direction,
                    }
            version = create(
                db,
                pid,
                "version",
                {
                    "spec_id": spec_id,
                    "source_fingerprint": source_hash,
                    "task_id": id,
                    "parent_version_id": parent_id,
                    **(
                        {"design_index": direction["number"], "design_count": direction["total"]}
                        if direction and "number" in direction
                        else {}
                    ),
                    **(
                        {
                            "style_plan_id": payload["style_plan_id"],
                            "style_direction_id": direction["id"],
                            "style_direction": direction,
                        }
                        if direction and payload.get("style_plan_id")
                        else {}
                    ),
                    **inherited_direction,
                    "image": result,
                    "review": None,
                    "provider": task.payload["provider"],
                    "prompt_version": "v1",
                },
                status="candidate",
            )
            change(task, current_version_id=version.id)
            if count > 1:
                change(task, direction_version_ids=[*direction_ids, version.id])
        elif tool == "compare_design":
            change(task, review_retry_version=None, review_completed=True)
            if review_incomplete:
                change(task, review_warnings=[*task.payload.get("review_warnings", []), current.id])
                if count == 1 and task.status == "running":
                    task.status = "awaiting_review"
                    change(task, outcome=result["summary"])
            version = require(db, current.id, "version", pid)
            change(version, review=result, reviews=[*version.payload.get("reviews", []), result])
            version.status = (
                "ready_for_review"
                if all(c["status"] == "pass" for c in result["checks"])
                and result["goal"]["status"] == "pass"
                and (not spec["base_version_id"] or result["preservation"]["status"] == "pass")
                else "needs_revision"
            )
        return task.status == "running"


def run_style_task(id, provider, owner=None):
    with transaction() as db:
        task = require(db, id, "task")
        if owner:
            lease = db.get(WorkerLease, 1)
            if not lease or lease.owner != owner or lease.expires < int(time.time()):
                raise AgentError("LEASE_LOST", "执行权已变更，已停止后续调用")
        if task.status != "running":
            return
        spec = require(db, task.payload["spec_id"], "spec", task.project_id)
        if spec.status != "confirmed" or head(db, task.project_id).payload.get("spec_id") != spec.id:
            raise AgentError("STALE_SOURCE", "设计要求已变化，请重新规划")
        context = {
            "count": task.payload["design_count"],
            "category": "shoe" if style.is_shoe(spec.payload["spec"]["intent"]) else "apparel",
            "spec": spec.payload["spec"],
        }
        step_id = event(db, task, "plan_directions", provider)
    # External model calls happen outside the SQLite transaction.
    result = provider.plan_directions(context)
    with transaction() as db:
        task = require(db, id, "task")
        if task.status != "running":
            return
        plan = style.save_generated(db, task, result)
        finish_event(
            task,
            step_id,
            {"plan_id": plan.id, "direction_count": len(plan.payload["directions"])},
            getattr(provider, "last_receipt", {}),
        )
        task.status = "awaiting_review"
        change(task, style_plan_id=plan.id, outcome="风格方向已整理，请设计师检查和确认")


def run_task(id, provider=None, owner=None):
    provider = provider or Provider()
    try:
        with transaction() as db:
            mode = require(db, id, "task").payload["mode"]
        if mode == "style":
            run_style_task(id, provider, owner)
            return
        # Local tool actions + at most 8 reasoning and 3 image calls.
        for _ in range(24):
            if not one_step(id, provider, owner):
                return
        raise AgentError("STEP_LIMIT", "达到步骤上限，已停止本轮")
    except AgentError as exc:
        fail_task(id, exc)
    except (ValidationError, ValueError):
        fail_task(id, AgentError("MODEL_SCHEMA_INVALID", "模型或图片结果不符合协议，任务已停止"))
    except Exception as exc:
        # Keep only exception kind and code locations, never exception text or local values.
        frames = []
        trace = exc.__traceback__
        while trace:
            code = trace.tb_frame.f_code
            if "/app/" in code.co_filename:
                frames.append({"function": code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        fail_task(
            id,
            AgentError(
                "INTERNAL_ERROR",
                "执行异常，已保留已有结果，请检查后台状态",
                details=[{"type": type(exc).__name__, "frames": frames[-8:]}],
                receipt=getattr(provider, "last_receipt", {}),
            ),
        )


def recover(db):
    from . import models3d

    models3d.recover(db)
    for task in db.scalars(select(Record).where(Record.kind == "task", Record.status == "running")):
        pending = any(s["status"] == "pending" for s in task.payload["steps"])
        task.status = "interrupted"
        change(
            task,
            steps=[{**s, "status": "unknown"} if s["status"] == "pending" else s for s in task.payload["steps"]],
            outcome="服务重启；存在结果未知的调用，未重试" if pending else "服务重启，可从已保存步骤恢复",
        )


class Worker:
    def __init__(self):
        self.owner = uid()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="design-agent")
        self.thread.start()

    def heartbeat(self):
        with transaction() as db:
            lease = db.get(WorkerLease, 1)
            clock = int(time.time())
            if lease is None:
                lease = WorkerLease(id=1, owner=self.owner, expires=clock + 15)
                db.add(lease)
                recover(db)
            elif lease.owner == self.owner or lease.expires < clock:
                if lease.owner != self.owner:
                    recover(db)
                lease.owner, lease.expires = self.owner, clock + 15
            return lease.owner == self.owner

    def keep_alive(self, done):
        while not done.wait(3):
            try:
                if not self.heartbeat():
                    self.stop_event.set()
                    return
            except Exception:
                self.stop_event.set()
                return

    def loop(self):
        while not self.stop_event.is_set():
            try:
                if self.heartbeat():
                    with transaction() as db:
                        task = db.scalar(
                            select(Record)
                            .where(Record.kind == "task", Record.status == "queued")
                            .order_by(Record.created_at)
                            .limit(1)
                        )
                        id = task.id if task else None
                        if task:
                            task.status = "running"
                    if id:
                        done = threading.Event()
                        keeper = threading.Thread(target=self.keep_alive, args=(done,), daemon=True)
                        keeper.start()
                        try:
                            run_task(id, owner=self.owner)
                        finally:
                            done.set()
                            keeper.join(timeout=4)
                    else:
                        from . import models3d

                        done = threading.Event()
                        keeper = threading.Thread(target=self.keep_alive, args=(done,), daemon=True)
                        keeper.start()
                        try:
                            models3d.tick()
                        finally:
                            done.set()
                            keeper.join(timeout=4)
            except Exception:
                # No raw exception data (provider content/credentials) is logged.
                pass
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Keep the lease if an external call is still running; otherwise release immediately.
        if not self.thread or not self.thread.is_alive():
            with transaction() as db:
                lease = db.get(WorkerLease, 1)
                if lease and lease.owner == self.owner:
                    lease.expires = 0


worker = Worker()
