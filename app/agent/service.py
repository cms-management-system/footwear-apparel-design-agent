from sqlalchemy import select

from ..models import Project
from .providers import Provider
from .schemas import Constraint, SamplingSheetIn, SpecIn, TaskIn
from .store import (
    AgentError,
    Record,
    change,
    create,
    fingerprint,
    head,
    now,
    project,
    records,
    require,
    serialize,
    transaction,
    uid,
)

BUSY = {"queued", "running", "awaiting_input"}
REVISION_ASSUMPTION = "保留所有未明确撤销的要求；请确认本轮改动是否与旧要求冲突"


def ensure_idle(db, pid):
    if any(r.status in BUSY for r in records(db, pid, "task")):
        raise AgentError("TASK_ACTIVE", "请先完成或取消当前任务，再修改要求单")


def save_spec(db, pid, data: SpecIn, check_idle=True):
    if check_idle:
        ensure_idle(db, pid)
    h = head(db, pid)
    if data.expected_spec_id != h.payload.get("spec_id"):
        raise AgentError("STALE_SOURCE", "要求单已有新版本，请刷新后再保存")
    for ref in data.references:
        require(db, ref.asset_id, "asset", pid)
    if data.base_version_id:
        require(db, data.base_version_id, "version", pid)
    payload = data.model_dump(exclude={"expected_spec_id"})
    row = create(
        db, pid, "spec", {"spec": payload, "fingerprint": fingerprint(payload), "parent_spec_id": data.expected_spec_id}
    )
    old_version = h.payload.get("confirmed_version_id")
    # Editing one existing design is a draft branch. Keep the chosen image
    # available until the designer explicitly confirms a replacement.
    if not data.base_version_id:
        for version in records(db, pid, "version"):
            if version.status == "confirmed":
                version.status = "superseded"
    change(h, spec_id=row.id, confirmed_version_id=old_version if data.base_version_id else None, style_plan_id=None)
    return row


def confirm_spec(id):
    with transaction() as db:
        row = require(db, id, "spec")
        h = head(db, row.project_id)
        ensure_idle(db, row.project_id)
        if h.payload.get("spec_id") != id:
            raise AgentError("STALE_SOURCE", "只能确认当前要求单")
        if row.payload["spec"]["conflicts"]:
            raise AgentError("SPEC_CONFLICT", "请先解决要求单中的冲突")
        row.status = "confirmed"
        change(row, confirmed_at=now())
        return serialize(row)


def submit(pid, data: TaskIn, provider=None):
    provider = provider or Provider()
    with transaction() as db:
        return submit_in(db, pid, data, provider)


def submit_in(db, pid, data, provider, review_version=None):
    request_data = data.model_dump(exclude={"idempotency_key"})
    if review_version:
        request_data["review_version_id"] = review_version.id
    if not data.style_plan_id:
        request_data.pop("style_plan_id")
        request_data.pop("style_direction_ids")
    if data.design_count == 1:
        request_data.pop("design_count")  # Keep fingerprints compatible with existing single-design requests.
    request_hash = fingerprint(request_data)
    project(db, pid)
    existing = db.scalar(select(Record).where(Record.dedupe == f"task:{pid}:{data.idempotency_key}"))
    if existing:
        if existing.payload["request_hash"] != request_hash:
            raise AgentError("IDEMPOTENCY_CONFLICT", "同一个请求编号不能用于不同任务")
        return serialize(existing)
    if data.mode == "design" and any(
        task.status == "interrupted"
        and task.payload.get("spec_id") == data.spec_id
        and any(step.get("status") == "unknown" for step in task.payload.get("steps", []))
        for task in records(db, pid, "task")
    ):
        raise AgentError("UNKNOWN_CALL", "上次出图调用结果未知；请先结束本轮，再决定是否手动重新生成")
    ensure_idle(db, pid)
    s = require(db, data.spec_id, "spec", pid)
    if head(db, pid).payload.get("spec_id") != s.id:
        raise AgentError("STALE_SOURCE", "要求单已更新，请刷新")
    if data.mode in {"design", "style"} and s.status != "confirmed":
        raise AgentError("CONFIRM_REQUIRED", "请先确认设计要求单")
    provider.require("understand" if data.mode == "style" or review_version else data.mode)
    info = provider.capabilities()
    if data.mode == "design" and data.style_plan_id:
        plan = require(db, data.style_plan_id, "style_plan", pid)
        if (
            plan.status != "confirmed"
            or head(db, pid).payload.get("style_plan_id") != plan.id
            or plan.payload["spec_id"] != s.id
            or plan.payload["source_fingerprint"] != s.payload["fingerprint"]
            or s.payload["spec"]["base_version_id"]
        ):
            raise AgentError("STYLE_PLAN_STALE", "风格方向与当前设计要求不一致，请重新确认")
        selected = [item["id"] for item in plan.payload["directions"] if item["selected"]]
        if data.style_direction_ids != selected:
            raise AgentError("DIRECTION_CHANGED", "出图方向必须与已确认的选择一致")
    if data.mode == "style":
        if data.max_cost_fen is not None and provider.reasoning_fen > data.max_cost_fen:
            raise AgentError("BUDGET_EXHAUSTED", "本轮设置的费用上限不足以规划风格方向")
    if data.mode == "design" and data.design_count > 1:
        needed_reasoning = data.design_count + bool(s.payload["spec"]["references"])
        minimum_cost = data.design_count * provider.image_fen + needed_reasoning * provider.reasoning_fen
        if needed_reasoning > data.max_reasoning_calls:
            raise AgentError("CALL_LIMIT_EXHAUSTED", "本轮检查次数不足以覆盖所选款数")
        if data.max_cost_fen is not None and minimum_cost > data.max_cost_fen:
            raise AgentError("BUDGET_EXHAUSTED", "本轮设置的费用上限不足以生成并检查所选款数")
    row = create(
        db,
        pid,
        "task",
        {
            **data.model_dump(exclude={"authorized", "idempotency_key"}),
            "request_hash": request_hash,
            "authorized_at": now(),
            "source_fingerprint": s.payload["fingerprint"],
            "provider": info,
            "reasoning_calls": 0,
            "image_calls": 0,
            "reserved_cost_fen": 0,
            "steps": [],
            "observations": [],
            "feedback": [],
            "questions": [],
            "current_version_id": review_version.id if review_version else None,
            **({"review_only": True, "max_image_calls": 0} if review_version else {}),
            "pending_action": None,
            "outcome": None,
        },
        status="queued",
        dedupe=f"task:{pid}:{data.idempotency_key}",
    )
    return serialize(row)


def recheck_version(id, data, provider=None):
    """Queue a bounded visual review of a saved image; never submit another render."""
    provider = provider or Provider()
    with transaction() as db:
        version = require(db, id, "version")
        key = "recheck-" + fingerprint({"version_id": id, "key": data.idempotency_key})
        existing = db.scalar(select(Record).where(Record.dedupe == f"task:{version.project_id}:{key}"))
        if existing:
            return serialize(existing)
        spec = require(db, version.payload["spec_id"], "spec", version.project_id)
        if head(db, version.project_id).payload.get("spec_id") != spec.id:
            raise AgentError("STALE_SOURCE", "设计要求已变化，请返回对应要求后再检查这张图")
        if version.status in {"confirmed", "superseded"}:
            raise AgentError("VERSION_LOCKED", "已确认或历史方案不能覆盖检查结果")
        if not version.payload.get("image"):
            raise AgentError("IMAGE_REQUIRED", "尚无已保存图片可检查")
        result = submit_in(
            db, version.project_id,
            TaskIn(spec_id=spec.id, mode="design", authorized=data.authorized, idempotency_key=key),
            provider, review_version=version,
        )
        task = require(db, result["id"], "task", version.project_id)
        original = require(db, version.payload["task_id"], "task", version.project_id)
        change(task, observations=original.payload.get("observations", []))
        change(version, review_task_id=task.id)
        return serialize(task)


def task_control(id, action, text=""):
    with transaction() as db:
        row = require(db, id, "task")
        if action == "cancel":
            if row.status in BUSY or row.status == "interrupted":
                row.status = "cancelled"
                change(row, outcome="已取消后续步骤；进行中的调用可能仍计费，返回结果会保留")
        elif action == "input":
            if row.status != "awaiting_input":
                raise AgentError("INVALID_STATE", "当前任务没有等待补充")
            row.status = "queued"
            change(row, feedback=[*row.payload["feedback"], {"text": text, "at": now()}], questions=[])
        elif action == "resume":
            if row.status != "interrupted":
                raise AgentError("INVALID_STATE", "只有中断的任务可以恢复")
            uncertain = [s for s in row.payload["steps"] if s["status"] == "unknown"]
            if any(s["status"] == "pending" for s in row.payload["steps"]) or (
                uncertain and (
                    row.payload["mode"] != "design"
                    or any(s["tool"] not in {"generate_design", "edit_design"} for s in uncertain)
                )
            ):
                raise AgentError("UNKNOWN_CALL", "有结果未确定的步骤，禁止自动重放；请先核查记录")
            current_head = head(db, row.project_id)
            if current_head.payload.get("spec_id") != row.payload["spec_id"]:
                original = require(db, row.payload["spec_id"], "spec", row.project_id)
                latest = require(db, current_head.payload["spec_id"], "spec", row.project_id)
                if latest.payload["fingerprint"] != original.payload["fingerprint"] or original.status != "confirmed":
                    raise AgentError("STALE_SOURCE", "设计要求已变化，请按最新要求另开一轮")
                # A follow-up question can create an identical draft. Restore the confirmed
                # source and its plan so the unfinished batch remains attached to its brief.
                change(current_head, spec_id=original.id, style_plan_id=row.payload.get("style_plan_id"))
            ensure_idle(db, row.project_id)
            if uncertain:
                # The explicit resume may dispatch the missing image again. Keep the
                # unknown attempt in the journal and allow one replacement call.
                change(row, max_image_calls=row.payload["max_image_calls"] + len(uncertain), error=None)
            row.status = "queued"
        return serialize(row)


def revise(id, data):
    with transaction() as db:
        row = require(db, id, "version")
        original = require(db, row.payload["spec_id"], "spec", row.project_id)
        h = head(db, row.project_id)
        # Prevent rolling back silently over a newer requirements document.
        if h.payload.get("spec_id") != data.expected_spec_id:
            raise AgentError("STALE_SOURCE", "要求单已更新，请刷新")
        current = require(db, data.expected_spec_id, "spec", row.project_id)
        source = current if current.payload["spec"].get("base_version_id") == id else original
        spec = SpecIn.model_validate(
            {
                **source.payload["spec"],
                "expected_spec_id": current.id,
                "base_version_id": id,
                "edit_region": data.edit_region,
            }
        )
        # A new edit of the same region replaces the previous edit instruction.
        # Keeping both "change to white" and "change to red" makes the image
        # request contradictory even though the newest user instruction is clear.
        spec.constraints = [
            constraint
            for constraint in spec.constraints
            if not (constraint.kind == "may_change" and constraint.region.strip() == data.edit_region.strip())
        ]
        if len(spec.constraints) >= 30:
            raise AgentError("CONSTRAINT_LIMIT", "本轮要求已达 30 条，请先整理要求再修订")
        spec.constraints.append(
            Constraint(id="c_" + uid()[:12], kind="may_change", text=data.text, region=data.edit_region)
        )
        spec.assumptions = [
            *[assumption for assumption in spec.assumptions if assumption != REVISION_ASSUMPTION][:9],
            REVISION_ASSUMPTION,
        ]
        result = save_spec(db, row.project_id, spec)
        change(result, revision_feedback={"text": data.text, "base_spec_id": original.id})
        return serialize(result)


def confirm_version(id):
    with transaction() as db:
        row = require(db, id, "version")
        ensure_idle(db, row.project_id)
        h = head(db, row.project_id)
        spec = require(db, row.payload["spec_id"], "spec", row.project_id)
        current_spec_id = h.payload.get("spec_id")
        # Sibling concepts from the same generation remain selectable while a
        # local edit of another concept is in progress.
        family_spec_id = current_spec_id
        if current_spec_id and current_spec_id != spec.id:
            current_spec = require(db, current_spec_id, "spec", row.project_id)
            base_id = current_spec.payload["spec"].get("base_version_id")
            seen = set()
            while base_id and base_id not in seen:
                seen.add(base_id)
                base = require(db, base_id, "version", row.project_id)
                family_spec_id = base.payload["spec_id"]
                base_id = base.payload.get("parent_version_id")
        if (
            spec.id not in {current_spec_id, family_spec_id}
            or spec.payload["fingerprint"] != row.payload["source_fingerprint"]
        ):
            raise AgentError("STALE_SOURCE", "该版本的要求单已变更，请基于当前要求重新检查")
        review = row.payload.get("review")
        if row.status == "confirmed":
            return serialize(row)
        if not review:
            raise AgentError("REVIEW_REQUIRED", "该图片尚未完成实际视觉检查")
        if review["goal"]["status"] != "pass":
            raise AgentError("GOAL_UNRESOLVED", "设计意图尚未得到完整满足，请继续修改或补充依据")
        if spec.payload["spec"]["base_version_id"] and review["preservation"]["status"] != "pass":
            raise AgentError("PRESERVATION_UNRESOLVED", "非目标区域存在偏差或无法确认，请继续修改")
        hard_ids = {
            c["id"]
            for c in spec.payload["spec"]["constraints"]
            if c["kind"] in {"must_keep", "forbidden", "may_change"}
        }
        if any(c["constraint_id"] in hard_ids and c["status"] != "pass" for c in review["checks"]):
            raise AgentError("UNRESOLVED_CONSTRAINTS", "仍有硬性要求不符合或无法确认，请修改要求或继续修订")
        # A new concept adds to the selection; a revised image replaces only
        # its own parent, leaving other confirmed concepts untouched.
        parent_id = row.payload.get("parent_version_id")
        if parent_id:
            parent = require(db, parent_id, "version", row.project_id)
            if parent.status == "confirmed":
                parent.status = "superseded"
        row.status = "confirmed"
        change(row, confirmed_at=now())
        task = require(db, row.payload.get("review_task_id", row.payload["task_id"]), "task", row.project_id)
        if task.status == "awaiting_review":
            task.status = "completed"
            change(task, outcome="设计师已确认该候选版本")
        change(h, confirmed_version_id=id)
        create(
            db,
            row.project_id,
            "confirmation",
            {"version_id": id, "spec_id": spec.id, "source_fingerprint": spec.payload["fingerprint"]},
            "confirmed",
        )
        return serialize(row)


def save_sampling_sheet(id: str, data: SamplingSheetIn):
    """Save a designer-authored sampling draft against one confirmed image version."""
    with transaction() as db:
        version = require(db, id, "version")
        if version.status != "confirmed":
            raise AgentError("CONFIRM_REQUIRED", "请先确认当前设计方案，再整理这款的打样资料")
        existing = [r for r in records(db, version.project_id, "sampling_sheet") if r.payload["version_id"] == id]
        latest = existing[-1] if existing else None
        if data.expected_sheet_id != (latest.id if latest else None):
            raise AgentError("STALE_SOURCE", "打样资料已有新版本，请刷新后再保存")
        fields = data.model_dump(exclude={"expected_sheet_id"})
        sheet = create(
            db,
            version.project_id,
            "sampling_sheet",
            {
                "version_id": id,
                "spec_id": version.payload["spec_id"],
                "previous_sheet_id": latest.id if latest else None,
                "fields": fields,
                "basis": latest.payload.get("basis") if latest else None,
            },
            "draft",
        )
        return serialize(sheet)


def auto_sampling_sheet(id: str):
    """Carry forward recorded design decisions without guessing physical specifications."""
    with transaction() as db:
        version = require(db, id, "version")
        if version.status != "confirmed":
            raise AgentError("CONFIRM_REQUIRED", "请先确认当前设计方案，再整理这款的打样资料")
        existing = [r for r in records(db, version.project_id, "sampling_sheet") if r.payload["version_id"] == id]
        if existing:
            return serialize(existing[-1])
        spec = require(db, version.payload["spec_id"], "spec", version.project_id).payload["spec"]
        review = version.payload.get("review") or {}
        basis = {
            "intent": spec["intent"],
            "requirements": [c["text"] for c in spec["constraints"] if c["kind"] == "must_keep"],
            "visual_review": review.get("summary", ""),
        }
        fields = SamplingSheetIn().model_dump(exclude={"expected_sheet_id"})
        for constraint in spec["constraints"]:
            if not any(word in constraint["text"] for word in ("图案", "印花", "刺绣", "装饰")):
                continue
            check = next(
                (
                    c for c in review.get("checks", [])
                    if c["constraint_id"] == constraint["id"] and c["status"] == "pass"
                ),
                None,
            )
            if check and any(word in check["evidence"] for word in ("胸前", "胸口", "背部", "袖口", "袖子")):
                fields["graphic_placement"] = "效果图参考：" + check["evidence"][:1100]
                break
        sheet = create(
            db,
            version.project_id,
            "sampling_sheet",
            {
                "version_id": id,
                "spec_id": version.payload["spec_id"],
                "previous_sheet_id": None,
                "fields": fields,
                "basis": basis,
            },
            "draft",
        )
        return serialize(sheet)


def workspace(pid):
    from .cms import project_cms
    from .models3d import for_project

    with transaction() as db:
        h = head(db, pid)
        project = db.get(Project, pid)
        return {
            "project": {"id": pid, "name": project.name, "cms": project_cms(project)},
            "head": h.payload,
            "assets": [serialize(r) for r in records(db, pid, "asset")],
            "specs": [serialize(r) for r in records(db, pid, "spec")],
            "style_plans": [serialize(r) for r in records(db, pid, "style_plan")],
            "tasks": [serialize(r) for r in records(db, pid, "task")],
            "versions": [serialize(r) for r in records(db, pid, "version")],
            "models3d": for_project(db, pid),
            "sampling_sheets": [serialize(r) for r in records(db, pid, "sampling_sheet")],
            "technical_flats": [serialize(r) for r in records(db, pid, "technical_flat")],
            "messages": [serialize(r) for r in records(db, pid, "message")],
        }


def correct_check(id, data):
    """Explicit designer correction with evidence; cannot certify physical properties."""
    with transaction() as db:
        row = require(db, id, "version")
        ensure_idle(db, row.project_id)
        if row.status in {"confirmed", "superseded"}:
            raise AgentError("VERSION_LOCKED", "已确认的版本不可修改检查记录，请创建新一轮")
        spec = require(db, row.payload["spec_id"], "spec", row.project_id)
        if head(db, row.project_id).payload.get("spec_id") != spec.id:
            raise AgentError("STALE_SOURCE", "要求单已更新，不能修改旧版本的检查")
        constraint = next((c for c in spec.payload["spec"]["constraints"] if c["id"] == data.constraint_id), None)
        if not constraint or constraint["verification"] != "visual":
            raise AgentError("PHYSICAL_PROOF_REQUIRED", "只能纠正有图片依据的视觉判断，不能勾选通过物理性能要求")
        review = row.payload.get("review")
        if not review:
            raise AgentError("REVIEW_REQUIRED", "还没有视觉检查结果可供纠正")
        corrected = {
            **review,
            "checks": [
                {**c, "status": data.status, "evidence": data.evidence, "source": "designer_correction"}
                if c["constraint_id"] == data.constraint_id
                else c
                for c in review["checks"]
            ],
        }
        change(
            row,
            review=corrected,
            human_corrections=[*row.payload.get("human_corrections", []), {**data.model_dump(), "at": now()}],
        )
        return serialize(row)
