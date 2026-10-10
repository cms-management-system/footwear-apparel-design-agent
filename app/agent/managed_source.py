"""Carry the complete approved product brief into every derived design spec."""

from __future__ import annotations

import copy

from sqlalchemy import select

from ..config import get_config
from ..models import DesignHandoff
from .managed_store import IndependentProject, ManagedReceipt
from .store import AgentError, Record, now


def spec_context(db, pid, payload, parent_id, actor=None):
    if not get_config().managed:
        return {}
    if db.get(IndependentProject, pid):
        return {}
    row = db.scalar(select(DesignHandoff).where(DesignHandoff.project_id == pid))
    meta = db.get(ManagedReceipt, row.id) if row else None
    cfg = get_config()
    if not meta or meta.scope_id != cfg.design_scope_id or meta.target_instance_id != cfg.design_instance_id:
        raise AgentError("SOURCE_MAPPING_REQUIRED", "要求单缺少批准来源绑定", 409)
    proof = meta.proof
    package, requirement, prompt = proof["package"], proof["requirement"], proof["prompt"]
    # These are source hard requirements, never silently downgraded or removed by a local edit.
    expected = [(f"c_product_{i}", "must_keep", text.strip()) for i, text in enumerate(requirement["constraints"])]
    expected.extend((f"c_avoid_{i}", "forbidden", text.strip()) for i, text in enumerate(prompt["avoid_items"]))
    local = {item["id"]: item for item in payload["constraints"]}
    for key, kind, text in expected:
        if (key not in local or local[key]["kind"] != kind or local[key]["text"] != text
                or local[key]["region"] != "整体" or local[key]["verification"] != "visual"):
            raise AgentError("SOURCE_CONSTRAINT_CHANGED", "产品已批准的硬约束或避免项不能被本地要求单覆盖", 409)
    if actor is None:
        from .access_context import validate_transaction_access

        user = validate_transaction_access(db)
        actor = user.username if user else "system_agent"
    parent = db.get(Record, parent_id) if parent_id else None
    previous = parent.payload["spec"] if parent else None
    changed = {
        key: {"before": previous.get(key) if previous else None, "after": value}
        for key, value in payload.items()
        if previous is None or previous.get(key) != value
    }
    return {
        "product_source": copy.deepcopy(
            {
                "handoff_id": row.id,
                "package_id": row.package_id,
                "package_version": row.version,
                "package_content_digest": meta.receipt["content_digest"],
                "version_group_id": package["version_group_id"],
                "requirement_digest": package["requirement_digest"],
                "prompt_digest": package["prompt_digest"],
                "requirement_base64": package["requirement_base64"],
                "prompt_base64": package["prompt_base64"],
                "requirement": requirement,
                "prompt": prompt,
                "approval": package["approval"],
            }
        ),
        "execution_changes": {"parent_spec_id": parent_id, "actor": actor, "created_at": now(), "fields": changed},
    }
