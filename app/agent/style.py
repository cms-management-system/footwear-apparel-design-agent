"""Versioned, designer-confirmed style directions for one requirements snapshot."""

from app.category import is_shoe

from .schemas import StylePlanEdit, StylePlanOutput
from .store import AgentError, change, create, fingerprint, head, now, require, serialize, transaction, uid


def _validate_distinct(directions):
    names = [d["name"].strip().casefold() for d in directions]
    axes = ["".join(d["explore"].split()).casefold() for d in directions]
    if len(names) != len(set(names)) or len(axes) != len(set(axes)):
        raise AgentError("DIRECTIONS_TOO_SIMILAR", "风格方向的名称和主要变化点不能重复，请调整后再确认", 422)


def _validate_category(directions, category):
    # Reject only unambiguous cross-category construction terms. Motif and mood text stays free-form.
    wrong = ("领口", "袖口", "袖长", "裙摆", "裤腿", "衣身") if category == "shoe" else (
        "鞋头", "鞋底", "鞋面", "鞋跟", "鞋带"
    )
    if any(term in item["structure"] for item in directions for term in wrong):
        raise AgentError("CATEGORY_MISMATCH", "风格方向的结构描述混入了其他品类，请重新规划", 422)


def save_generated(db, task, raw):
    output = StylePlanOutput.model_validate(raw)
    if len(output.directions) != task.payload["design_count"]:
        raise AgentError("DIRECTION_COUNT_MISMATCH", "模型返回的风格方向数量不符合本次选择")
    directions = [
        {"id": "d_" + uid()[:12], **item.model_dump(), "selected": True}
        for item in output.directions
    ]
    _validate_distinct(directions)
    spec = require(db, task.payload["spec_id"], "spec", task.project_id)
    category = "shoe" if is_shoe(spec.payload["spec"]["intent"]) else "apparel"
    _validate_category(directions, category)
    h = head(db, task.project_id)
    if h.payload.get("spec_id") != spec.id or spec.status != "confirmed":
        raise AgentError("STALE_SOURCE", "设计要求已变化，请按最新要求重新规划")
    locked = [
        {"id": item["id"], "text": item["text"], "kind": item["kind"]}
        for item in spec.payload["spec"]["constraints"]
        if item["kind"] in {"must_keep", "forbidden"}
    ]
    content = {
        "spec_id": spec.id,
        "source_fingerprint": spec.payload["fingerprint"],
        "category": category,
        "locked_requirements": locked,
        "directions": directions,
        "task_id": task.id,
        "parent_plan_id": h.payload.get("style_plan_id"),
    }
    old_id = h.payload.get("style_plan_id")
    if old_id:
        old = require(db, old_id, "style_plan", task.project_id)
        old.status = "superseded"
    row = create(db, task.project_id, "style_plan", {**content, "fingerprint": fingerprint(content)})
    change(h, style_plan_id=row.id)
    return row


def revise(plan_id: str, data: StylePlanEdit):
    with transaction() as db:
        old = require(db, plan_id, "style_plan")
        h = head(db, old.project_id)
        if data.expected_plan_id != plan_id or h.payload.get("style_plan_id") != plan_id:
            raise AgentError("STALE_SOURCE", "风格方向已有新版本，请刷新后再修改")
        if old.status != "draft":
            raise AgentError("PLAN_LOCKED", "已确认的风格方向不能直接覆盖，请先修改设计要求")
        if h.payload.get("spec_id") != old.payload["spec_id"]:
            raise AgentError("STALE_SOURCE", "设计要求已变化，请重新规划")
        from .service import ensure_idle

        ensure_idle(db, old.project_id)
        directions = [item.model_dump() for item in data.directions]
        if [item["id"] for item in directions] != [item["id"] for item in old.payload["directions"]]:
            raise AgentError("DIRECTION_CHANGED", "只能编辑本次规划的方向，不能替换方向编号", 422)
        if not any(item["selected"] for item in directions):
            raise AgentError("DIRECTION_REQUIRED", "至少保留一个设计方向", 422)
        _validate_distinct(directions)
        _validate_category(directions, old.payload["category"])
        content = {
            key: old.payload[key]
            for key in ("spec_id", "source_fingerprint", "category", "locked_requirements", "task_id")
        }
        content.update(directions=directions, parent_plan_id=old.id)
        row = create(db, old.project_id, "style_plan", {**content, "fingerprint": fingerprint(content)})
        old.status = "superseded"
        change(h, style_plan_id=row.id)
        return serialize(row)


def confirm(plan_id: str):
    with transaction() as db:
        row = require(db, plan_id, "style_plan")
        h = head(db, row.project_id)
        spec = require(db, row.payload["spec_id"], "spec", row.project_id)
        if h.payload.get("style_plan_id") != row.id or h.payload.get("spec_id") != spec.id:
            raise AgentError("STALE_SOURCE", "只能确认当前设计要求的最新风格方向")
        if spec.status != "confirmed" or spec.payload["fingerprint"] != row.payload["source_fingerprint"]:
            raise AgentError("STALE_SOURCE", "设计要求已变化，请重新规划")
        if not any(item["selected"] for item in row.payload["directions"]):
            raise AgentError("DIRECTION_REQUIRED", "至少保留一个设计方向", 422)
        if row.status == "confirmed":
            return serialize(row)
        if row.status != "draft":
            raise AgentError("PLAN_LOCKED", "历史风格方向不能确认")
        from .service import ensure_idle

        ensure_idle(db, row.project_id)
        row.status = "confirmed"
        change(row, confirmed_at=now())
        return serialize(row)
