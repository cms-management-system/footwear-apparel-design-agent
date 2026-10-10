"""Project canvas and immutable execution candidates; no provider calls or migrations."""

import base64
import copy
import json
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..config import get_config
from . import assets
from .access_context import project_binding, validate_transaction_access
from .managed_store import IndependentProject, canonical_bytes, sha256
from .schemas import SelectedContext, Strict
from .store import AgentError, Record, create, head, now, records, require, uid

Position = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-100000, le=100000)]
Size = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=24, le=8192)]


class CanvasNode(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    kind: Literal["version", "asset"]
    ref_id: str = Field(min_length=1, max_length=100)
    x: Position
    y: Position
    width: Size
    height: Size


class Viewport(Strict):
    x: Position
    y: Position
    zoom: float = Field(strict=True, allow_inf_nan=False, ge=0.1, le=4)


class Layout(Strict):
    nodes: list[CanvasNode] = Field(max_length=200)
    viewport: Viewport

    @model_validator(mode="after")
    def unique_nodes(self):
        if len({n.id for n in self.nodes}) != len(self.nodes) or len({(n.kind, n.ref_id) for n in self.nodes}) != len(
            self.nodes
        ):
            raise ValueError("画布节点不能重复")
        return self


class CanvasIn(Strict):
    schema_version: Literal["design-canvas/1"]
    expected_layout_revision: int = Field(strict=True, ge=0, le=2147483647)
    layout: Layout


class BriefIn(Strict):
    expected_prompt_id: str | None = Field(min_length=1, max_length=100)
    expected_spec_id: str | None = Field(min_length=1, max_length=100)
    message_id: str | None = Field(min_length=1, max_length=100)
    selected_context: SelectedContext | None
    positive_prompt: str = Field(min_length=1, max_length=10000)
    avoid_items: list[str] = Field(max_length=100)
    output_kind: Literal["effect_image", "design_draft"]
    base_version_id: str | None = Field(min_length=1, max_length=100)
    edit_region: str = Field(max_length=200)

    @model_validator(mode="after")
    def valid_text(self):
        if not self.positive_prompt.strip() or any(not a.strip() or len(a) > 10000 for a in self.avoid_items):
            raise ValueError("执行稿/避免项不可为空白")
        if self.base_version_id and not self.edit_region.strip():
            raise ValueError("修改需明确区域")
        return self


def image_identity(image):
    return image.get("asset_id") or image.get("file", "").removesuffix(".png")


def existing_image(image):
    try:
        path = assets.file_path(image)
        if not image.get("width") or not image.get("height"):
            raise ValueError()
        return path
    except (AgentError, KeyError, TypeError, ValueError):
        raise AgentError("NOT_FOUND", "图片不存在或当前项目不可访问", 404) from None


def asset_payload(db, pid, asset_id):
    row = db.get(Record, asset_id)
    if row and row.kind == "asset" and row.project_id == pid:
        existing_image(row.payload)
        return row.payload
    # Generated originals have inline image metadata, not an asset Record.
    for version in records(db, pid, "version"):
        image = version.payload.get("image") or {}
        if image_identity(image) == asset_id:
            existing_image(image)
            return image
    raise AgentError("NOT_FOUND", "图片不存在或当前项目不可访问", 404)


def selected_context(db, pid, selected):
    if selected is None:
        return None
    data = (
        selected.model_dump()
        if isinstance(selected, SelectedContext)
        else SelectedContext.model_validate(selected).model_dump()
    )
    version = require(db, data["version_id"], "version", pid) if data["version_id"] else None
    if data["asset_id"]:
        asset_payload(db, pid, data["asset_id"])
    if version:
        image = version.payload.get("image") or {}
        existing_image(image)
        original_id = image_identity(image)
        # Uploaded assets can also serve as a version's original image.
        for row in records(db, pid, "asset"):
            if row.payload.get("file") == image.get("file"):
                original_id = row.id
                break
        if data["asset_id"] and data["asset_id"] != original_id:
            raise AgentError("CONTEXT_MISMATCH", "所选素材不是该版本的原图", 422)
        data["asset_id"] = original_id
    return data


def source_binding(db, pid, user):
    row, meta = project_binding(db, pid, user)
    if isinstance(meta, IndependentProject):
        return {
            "source_mode": "independent",
            "project_id": pid,
            "owner_subject": meta.owner_subject,
            "scope_id": meta.scope_id,
            "target_instance_id": meta.target_instance_id,
        }
    return copy.deepcopy(
        {
            "source_mode": "upstream",
            "handoff_id": row.id,
            "source_instance_id": meta.source_instance_id,
            "target_instance_id": meta.target_instance_id,
            "scope_id": meta.scope_id,
            "package_id": meta.package_id,
            "package_version": meta.package_version,
            "content_digest": meta.receipt["content_digest"],
            "envelope_sha256": meta.envelope_sha256,
            "package": meta.proof.get("package"),
            "requirement": meta.proof.get("requirement"),
            "prompt": meta.proof.get("prompt"),
            "receipt": meta.receipt,
        }
    )


def source_identity_digest(db, pid, user):
    return sha256(canonical_bytes(source_binding(db, pid, user)))


def source_digest(db, pid, user):
    row, meta = project_binding(db, pid, user)
    return None if isinstance(meta, IndependentProject) else meta.receipt["content_digest"]


def conversation_grant(pid, subject):
    """A request flag and provider config cannot create local human authorization."""
    cfg = get_config()
    try:
        path = Path(cfg.design_conversation_authorization_file)
        if not cfg.design_paid_providers_enabled or not path.is_file() or path.stat().st_mode & 0o077:
            return None
        grant = json.loads(path.read_text())
        if (
            grant.get("authorized") is not True
            or type(grant.get("project_id")) is not int
            or grant["project_id"] != pid
            or grant.get("subject") != subject
            or grant.get("scope_id") != cfg.design_scope_id
            or grant.get("target_instance_id") != cfg.design_instance_id
            or type(grant.get("expires_at")) is not int
            or grant["expires_at"] <= time.time()
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", grant.get("grant_id", ""))
        ):
            return None
        return grant
    except (OSError, ValueError, TypeError):
        return None


def execution_state(db, pid, user=None):
    h = head(db, pid)
    if h.payload.get("creation_action_id") and not h.payload.get("spec_id") and not h.payload.get("current_prompt_id"):
        return "needs_confirmation"  # New empty draft, not a corrupted historic prompt/spec pair.
    prompt = db.get(Record, h.payload.get("current_prompt_id")) if h.payload.get("current_prompt_id") else None
    spec = db.get(Record, h.payload.get("spec_id")) if h.payload.get("spec_id") else None
    if h.payload.get("latest_user_message_id") and (
        not prompt
        or prompt.payload.get("prompt", {}).get("confirmed_through_message_id") != h.payload["latest_user_message_id"]
    ):
        return "needs_confirmation"
    if not prompt or prompt.kind != "prompt" or not spec or spec.kind != "spec":
        return "inconsistent"
    if prompt.payload["prompt"].get("confirmed_through_message_id") != h.payload.get("latest_user_message_id"):
        return "needs_confirmation"
    if prompt.payload.get("spec_id") != spec.id or spec.status != "confirmed":
        return "inconsistent"
    projection = prompt.payload["prompt"]
    content = spec.payload["spec"]
    if (
        content["intent"] != projection["positive_prompt"]
        or content["base_version_id"] != projection["base_version_id"]
        or content["edit_region"] != projection["edit_region"]
        or spec.payload.get("output_kind", projection["output_kind"]) != projection["output_kind"]
    ):
        return "inconsistent"
    if spec.payload.get("execution_prompt_digest", projection["digest"]) != projection["digest"]:
        return "inconsistent"
    try:
        original = base64.b64decode(prompt.payload["original_body_base64"], validate=True)
        if sha256(original) != projection["digest"] or json.loads(original) != {
            k: v for k, v in projection.items() if k != "digest"
        }:
            return "inconsistent"
    except (ValueError, KeyError, TypeError):
        return "inconsistent"
    user = user or validate_transaction_access(db, require_editable=False)
    if user:
        row, meta = project_binding(db, pid, user)
        if prompt.payload.get("source_binding_digest", source_digest(db, pid, user)) != source_digest(
            db, pid, user
        ) or prompt.payload.get(
            "source_identity_digest", source_identity_digest(db, pid, user)
        ) != source_identity_digest(db, pid, user):
            return "inconsistent"
        if not isinstance(meta, IndependentProject):
            source = spec.payload.get("product_source", {})
            if (
                source.get("handoff_id") != row.id
                or source.get("package_id") != meta.package_id
                or source.get("package_version") != meta.package_version
                or source.get("package_content_digest") != meta.receipt["content_digest"]
                or source.get("requirement") != meta.proof.get("requirement")
                or source.get("prompt") != meta.proof.get("prompt")
                or source.get("approval") != meta.proof.get("package", {}).get("approval")
            ):
                return "inconsistent"
    return "ready"


def check_head(db, pid, prompt_id, spec_id):
    h = head(db, pid)
    if prompt_id != h.payload.get("current_prompt_id"):
        raise AgentError("PROMPT_CONFLICT", "执行稿已有新版本，请保留输入并重新核对", 409)
    if spec_id != h.payload.get("spec_id"):
        raise AgentError("STALE_SOURCE", "设计要求已有新版本，请保留输入并重新核对", 409)
    return h


def canvas_document(db, pid):
    row = db.get(Record, f"canvas_{pid}")
    if row:
        return copy.deepcopy(row.payload)
    return {
        "schema_version": "design-canvas/1",
        "project_id": pid,
        "layout_revision": 0,
        "layout": {"nodes": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        "updated_at": None,
        "updated_by": None,
    }


def save_canvas(db, pid, data):
    user = validate_transaction_access(db)
    project_binding(db, pid, user, write=True)
    current = canvas_document(db, pid)
    if data.expected_layout_revision != current["layout_revision"]:
        raise AgentError("LAYOUT_CONFLICT", "画布已有新布局，请保留本地布局并重新核对", 409)
    for node in data.layout.nodes:
        if node.kind == "version":
            version = require(db, node.ref_id, "version", pid)
            existing_image(version.payload.get("image") or {})
        else:
            # Canvas asset nodes must refer to actual uploaded asset objects.
            row = require(db, node.ref_id, "asset", pid)
            existing_image(row.payload)
    result = {
        "schema_version": data.schema_version,
        "project_id": pid,
        "layout_revision": current["layout_revision"] + 1,
        "layout": data.layout.model_dump(),
        "updated_at": now(),
        "updated_by": user.username,
    }
    row = db.get(Record, f"canvas_{pid}")
    if row:
        row.payload = result
        row.updated_at = result["updated_at"]
    else:
        create(db, pid, "canvas", result, "saved", id=f"canvas_{pid}")
    return result


def save_brief(db, pid, data, *, origin="manual", actor=None, provenance=None):
    from .dual_entry import source_constraints

    user = actor or validate_transaction_access(db)
    project_binding(db, pid, user, write=True)
    check_head(db, pid, data.expected_prompt_id, data.expected_spec_id)
    context = selected_context(db, pid, data.selected_context)
    if data.base_version_id:
        require(db, data.base_version_id, "version", pid)
        if not context or context["version_id"] != data.base_version_id:
            raise AgentError("CONTEXT_MISMATCH", "修改基准必须与所选版本一致", 422)
    if data.message_id:
        message = require(db, data.message_id, "message", pid)
        if message.payload.get("role") != "user" or message.payload.get("created_by") != user.username:
            raise AgentError("NOT_FOUND", "消息不存在或不是本人的消息", 404)
    source_constraints(db, pid, user, data)
    projection = {
        "id": uid(),
        "project_id": pid,
        "origin": origin,
        "message_id": data.message_id,
        "model_task_id": provenance["task_id"] if origin == "model" and provenance else None,
        "based_on_latest_message_id": head(db, pid).payload.get("latest_user_message_id"),
        "selected_context": context,
        "based_on_prompt_id": data.expected_prompt_id,
        "based_on_spec_id": data.expected_spec_id,
        "source_binding_digest": source_digest(db, pid, user),
        "positive_prompt": data.positive_prompt,
        "avoid_items": data.avoid_items,
        "output_kind": data.output_kind,
        "base_version_id": data.base_version_id,
        "edit_region": data.edit_region,
        "created_by": user.username,
        "created_at": now(),
    }
    if origin == "model":
        if not provenance or not provenance.get("task_id"):
            raise AgentError("PROVENANCE_REQUIRED", "模型候选必须保留原任务与调用记录", 409)
    raw = canonical_bytes(projection)
    projection["digest"] = sha256(raw)
    row = create(
        db,
        pid,
        "brief",
        {"brief": projection, **({"model_provenance": copy.deepcopy(provenance)} if origin == "model" else {})},
        "candidate",
        id=projection["id"],
    )
    return brief_projection(db, row)


def brief_projection(db, row):
    confirmed = any(r.payload.get("brief_id") == row.id for r in records(db, row.project_id, "brief_confirmation"))
    return {**copy.deepcopy(row.payload["brief"]), "status": "confirmed" if confirmed else "candidate"}


def briefs(db, pid):
    return [brief_projection(db, r) for r in records(db, pid, "brief")]


def candidate_for_confirmation(db, pid, data, user):
    candidate = require(db, data.candidate_id, "brief", pid)
    brief = candidate.payload["brief"]
    if (
        brief["based_on_prompt_id"] != data.expected_prompt_id
        or brief["based_on_spec_id"] != data.expected_spec_id
        or brief["source_binding_digest"] != source_digest(db, pid, user)
    ):
        raise AgentError("BRIEF_CONFLICT", "候选依据已变化，请保存新候选后重新确认", 409)
    if brief["based_on_latest_message_id"] != head(db, pid).payload.get("latest_user_message_id") or any(
        r.payload.get("brief_id") == candidate.id for r in records(db, pid, "brief_confirmation")
    ):
        raise AgentError("BRIEF_CONFLICT", "候选已过时或已经确认，请保存新候选", 409)
    for key in ("positive_prompt", "avoid_items", "output_kind", "base_version_id", "edit_region"):
        if brief[key] != getattr(data, key):
            raise AgentError("BRIEF_CONFLICT", "候选内容已冻结；修改后请另存新候选", 409)
    selected_context(db, pid, brief["selected_context"])
    return brief


def model_candidate(db, task, proposed):
    from ..models import DesignerUser

    user = db.get(DesignerUser, task.payload.get("actor"))
    if not user or not user.active:
        raise AgentError("LOGIN_REQUIRED", "原任务主体已失效", 401)
    if task.payload.get("based_on_latest_message_id") != head(db, task.project_id).payload.get(
        "latest_user_message_id"
    ):
        raise AgentError("BRIEF_CONFLICT", "模型候选之后已有新消息，请重新整理", 409)
    prompt_id = task.payload.get("prompt_id")
    prompt = require(db, prompt_id, "prompt", task.project_id).payload["prompt"] if prompt_id else {}
    data = BriefIn(
        expected_prompt_id=prompt_id,
        expected_spec_id=task.payload["spec_id"],
        message_id=task.payload.get("message_id"),
        selected_context=task.payload.get("selected_context"),
        positive_prompt=proposed.intent,
        avoid_items=prompt.get("avoid_items", []),
        output_kind=prompt.get("output_kind", "effect_image"),
        base_version_id=proposed.base_version_id,
        edit_region=proposed.edit_region,
    )
    if task.payload.get("source_binding_digest") != source_digest(db, task.project_id, user):
        raise AgentError("BRIEF_CONFLICT", "模型候选来源已变化", 409)
    return save_brief(
        db,
        task.project_id,
        data,
        origin="model",
        actor=user,
        provenance={
            "task_id": task.id,
            "steps": task.payload["steps"],
            "provider": task.payload["provider"],
            "proposed_spec": proposed.model_dump(),
        },
    )
