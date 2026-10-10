"""Public instance cumulative dispatch ceiling. Reservations never expire or refund."""

from sqlalchemy import func, select

from ..config import get_config
from .managed_store import PublicExecutionBudget, PublicProviderCall
from .store import AgentError

MESSAGE = "公开演示累计额度已用完，暂不可生成；已有项目、历史与人工审批仍可使用"


def initialize(db):
    cfg = get_config()
    if not cfg.public_demo:
        return
    row = db.get(PublicExecutionBudget, 1)
    if not row:
        row = PublicExecutionBudget(id=1, instance_id=cfg.design_instance_id, scope_id=cfg.design_scope_id,
                                    text_limit=cfg.design_public_text_limit, image_limit=cfg.design_public_image_limit)
        db.add(row)
    else:
        _binding(row)
        # Only process startup reads the operator's server-side limits. Counts persist.
        row.text_limit, row.image_limit = cfg.design_public_text_limit, cfg.design_public_image_limit
    db.flush()


def _binding(row):
    cfg = get_config()
    if row.instance_id != cfg.design_instance_id or row.scope_id != cfg.design_scope_id:
        raise AgentError("PUBLIC_BUDGET_CONFIG_CHANGED", "公网累计账本归属不符，请由运行负责人核实", 503)


def active(db):
    return get_config().public_demo or db.get(PublicExecutionBudget, 1) is not None


def remaining(db, stage):
    row = db.get(PublicExecutionBudget, 1, populate_existing=True)
    if row is None:
        if get_config().public_demo:
            raise AgentError("PUBLIC_BUDGET_NOT_INITIALIZED", "公网累计账本尚未初始化，未派发供应商请求", 503)
        return None
    _binding(row)
    limit = row.text_limit if stage == "text" else row.image_limit
    used = db.scalar(select(func.count()).select_from(PublicProviderCall).where(PublicProviderCall.stage == stage))
    return max(0, limit - used)


def check_available(db, stage):
    count = remaining(db, stage)
    if count is not None and count <= 0:
        raise AgentError("PUBLIC_BUDGET_EXHAUSTED", MESSAGE, 409)


def reserve_dispatch(db, stage, attempt_id, run_id):
    if not active(db):
        return
    if stage not in {"text", "image"}:
        raise AgentError("PUBLIC_BUDGET_INVALID_STAGE", "公网未授权此供应商阶段", 409)
    if db.get(PublicProviderCall, attempt_id):
        raise AgentError("CALL_LIMIT_EXHAUSTED", "原请求已计入公网累计账本，不能再次派发", 409)
    check_available(db, stage)
    db.add(PublicProviderCall(attempt_id=attempt_id, stage=stage, run_id=run_id))
    db.flush()


def projection(db):
    if not active(db):
        return None
    return {"remaining": {stage + "_calls": remaining(db, stage) for stage in ["text", "image"]},
            "reset": "manual_server_only", "concurrency": 1, "unknown_refunded": False}
