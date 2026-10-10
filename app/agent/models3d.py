"""One paid, asynchronous 3D preview per immutable design version.

The provider creates a ZIP containing a GLB. We persist the GLB because its
download link expires, and never pretend the 2D source is a rotating model.
"""

import base64
import io
import os
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_config
from . import assets
from .store import AgentError, Record, change, create, now, require, serialize, transaction, uid

PRICE_FEN = 180  # Ark Hyper3D Gen2: 30,000 output tokens at ¥0.06 / 1,000.
MODEL = "hyper3d-gen2-260112"
API_ROOT = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_MODEL = 80 * 1024 * 1024


class Generate3DIn(BaseModel):
    authorized: bool
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def _allocation():
    try:
        assigned = max(0, int(os.getenv("AGENT_MONTHLY_ALLOCATION_FEN", "0")))
        requested = max(0, int(os.getenv("AGENT_3D_MONTHLY_ALLOCATION_FEN", "0")))
        return min(requested, max(0, 20000 - assigned))
    except ValueError:
        return 0


def capabilities():
    configured = bool(
        get_config().design_paid_providers_enabled and os.getenv("AGENT_3D_ENABLED") == "true"
        and _key() and _allocation() >= PRICE_FEN
    )
    return {
        "enabled": configured,
        "model": MODEL if configured else None,
        "provider": "火山方舟影眸",
        "estimated_cost_fen": PRICE_FEN,
        "monthly_allocation_fen": _allocation(),
        "note": "每款单独生成可旋转 3D 概念模型；背面等未出现在图片中的细节由模型推测，需设计师检查。",
    }


def _key():
    return os.getenv("AGENT_3D_API_KEY", "") or get_config().image_api_key


def _provider_image(payload):
    if payload["width"] < 4096 and payload["height"] < 4096:
        return assets.data_url(payload)
    try:
        with Image.open(assets.file_path(payload)) as image:
            image.thumbnail((4095, 4095), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, format="PNG")
            if out.tell() > 29 * 1024 * 1024:
                out = io.BytesIO()
                image.convert("RGB").save(out, format="JPEG", quality=90)
                if out.tell() > 29 * 1024 * 1024:
                    raise AgentError("3D_IMAGE_TOO_LARGE", "图片超出 3D 服务输入上限，未提交付费请求", 422)
                return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except OSError as exc:
        raise AgentError("3D_IMAGE_INVALID", "所选方案的原图无法读取，未提交付费请求", 422) from exc
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def _request(method, url, *, json_body=None):
    if not get_config().design_paid_providers_enabled:
        raise AgentError("CAPABILITY_UNAVAILABLE", "本实例尚未授权 3D 服务调用", 503)
    try:
        with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False) as client:
            response = client.request(method, url, headers={"Authorization": f"Bearer {_key()}"}, json=json_body)
        if response.status_code >= 400:
            if response.status_code in {401, 403, 404}:
                raise AgentError("3D_MODEL_UNAVAILABLE", "3D 模型尚未开通或密钥没有访问权限，请核对方舟配置", 502)
            if response.status_code == 429:
                raise AgentError("3D_RATE_LIMITED", "3D 模型当前限流或额度不足；不会自动再次提交", 502)
            raise AgentError("3D_PROVIDER_REJECTED", "3D 服务拒绝了请求，请检查模型开通状态；不会自动再次提交", 502)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("non-object response")
        return data
    except AgentError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise AgentError("3D_PROVIDER_UNKNOWN", "3D 服务连接或返回异常；请求结果待核查，不会自动重试", 502) from exc


def submit(version_id: str, request: Generate3DIn):
    if not get_config().design_paid_providers_enabled:
        raise AgentError("CAPABILITY_UNAVAILABLE", "本实例尚未授权收费模型调用", 503)
    if not request.authorized:
        raise AgentError("3D_AUTH_REQUIRED", "请先确认这款图片会发送至火山方舟并可能产生费用", 422)
    if not capabilities()["enabled"]:
        raise AgentError("3D_NOT_CONFIGURED", "3D 生成功能尚未配置或未分配预算", 503)
    with transaction() as db:
        version = require(db, version_id, "version")
        if version.status == "candidate":
            raise AgentError("3D_VERSION_NOT_READY", "请等待这款图片生成完成后再制作 3D", 409)
        existing = db.scalar(select(Record).where(Record.kind == "model3d", Record.dedupe == f"model3d:{version_id}"))
        if existing:
            return serialize(existing)
        month = now()[:7]
        used = sum(
            int(row.payload.get("estimated_cost_fen", 0))
            for row in db.scalars(select(Record).where(Record.kind == "model3d"))
            if row.created_at.startswith(month) and row.status != "failed_before_submit"
        )
        if used + PRICE_FEN > _allocation():
            raise AgentError("3D_BUDGET_EXHAUSTED", "3D 月度额度不足，未提交付费请求", 409)
        return serialize(create(
            db, version.project_id, "model3d",
            {
                "version_id": version_id,
                "source_sha256": version.payload["image"]["sha256"],
                "estimated_cost_fen": PRICE_FEN,
                "provider": "ark-hyper3d-gen2",
                "provider_task_id": None,
                "model_file": None,
                "request_key": request.idempotency_key,
                "next_poll_at": 0,
                "retry_count": 0,
            },
            status="queued",
            dedupe=f"model3d:{version_id}",
        ))


def for_project(db, pid):
    return [serialize(row) for row in db.scalars(
        select(Record).where(Record.project_id == pid, Record.kind == "model3d").order_by(Record.created_at)
    )]


def _download_model(file_url: str) -> str:
    if not get_config().design_paid_providers_enabled:
        raise AgentError("CAPABILITY_UNAVAILABLE", "本实例尚未授权外部 3D 文件服务调用", 503)
    parts = urlparse(file_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in {None, 443} or not any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ("volces.com", "volcengine.com", "volccdn.com")
    ):
        raise AgentError("3D_FILE_HOST_INVALID", "3D 文件下载地址异常，未访问该地址", 502)
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False) as client:
            with client.stream("GET", file_url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise AgentError("3D_FILE_TOO_LARGE", "3D 文件超出保存上限，未保存", 502)
                    chunks.append(chunk)
        with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
            matches = [
                info for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".glb")
            ]
            if len(matches) != 1 or matches[0].file_size > MAX_MODEL:
                raise AgentError("3D_FILE_INVALID", "3D 压缩包没有唯一有效的 GLB 文件", 502)
            with archive.open(matches[0]) as source:
                chunks, size = [], 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_MODEL:
                        raise AgentError("3D_FILE_TOO_LARGE", "3D 模型文件超出保存上限", 502)
                    chunks.append(chunk)
            model = b"".join(chunks)
        if (
            len(model) < 12 or model[:4] != b"glTF"
            or int.from_bytes(model[4:8], "little") != 2
            or int.from_bytes(model[8:12], "little") != len(model)
        ):
            raise AgentError("3D_FILE_INVALID", "3D 文件格式无效", 502)
        root = get_config().assets_dir / "design-agent" / "models3d"
        root.mkdir(parents=True, exist_ok=True)
        name = f"{uid()}.glb"
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
            temporary.write(model)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, root / name)
        return name
    except AgentError:
        raise
    except (httpx.HTTPError, zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise AgentError("3D_FILE_UNAVAILABLE", "模型已生成，但文件暂时无法保存；可稍后继续获取", 502) from exc


def model_path(row):
    name = row.payload.get("model_file")
    if (
        row.kind != "model3d" or row.status != "succeeded"
        or not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}\.glb", name)
    ):
        raise AgentError("3D_NOT_READY", "这款 3D 文件尚未生成", 404)
    path = get_config().assets_dir / "design-agent" / "models3d" / name
    if not path.is_file():
        raise AgentError("3D_FILE_MISSING", "3D 文件缺失，请联系维护人员恢复", 404)
    return path


def tick():
    """Process at most one state transition; create is never replayed after uncertainty."""
    if not get_config().design_paid_providers_enabled:
        return  # Preserve pending history without polling or downloading external resources.
    timestamp = datetime.now(UTC).timestamp()
    with transaction() as db:
        candidates = db.scalars(
            select(Record)
            .where(Record.kind == "model3d", Record.status.in_(["queued", "running", "saving"]))
            .order_by(Record.created_at)
        )
        row = next(
            (candidate for candidate in candidates if candidate.payload.get("next_poll_at", 0) <= timestamp),
            None,
        )
        if not row:
            return
        id, state = row.id, row.status
        if state == "queued":
            if not capabilities()["enabled"]:
                row.status = "interrupted"
                change(row, error={"code": "3D_DISABLED", "message": "3D 服务配置已关闭，未提交付费请求"})
                return
            row.status = "submitting"
            change(row, submitted_at=now())
        else:
            change(row, next_poll_at=timestamp + 8)
    try:
        if state == "queued":
            with transaction() as db:
                source = require(db, id, "model3d")
                version = require(db, source.payload["version_id"], "version", source.project_id)
                if version.payload["image"]["sha256"] != source.payload["source_sha256"]:
                    raise AgentError("3D_SOURCE_CHANGED", "设计版本图片已变化，3D 任务已停止")
                image = _provider_image(version.payload["image"])
            body = {
                "model": MODEL,
                "content": [
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "text", "text": (
                        "Reconstruct only the single complete product shown in the reference image. "
                        "Preserve its visible silhouette, construction, color and material. "
                        "Make a clean standalone object. --fileformat glb --material PBR"
                    )},
                ],
            }
            result = _request("POST", API_ROOT, json_body=body)
            provider_id = result.get("id")
            if not isinstance(provider_id, str) or not re.fullmatch(r"cgt-[A-Za-z0-9_-]{8,100}", provider_id):
                raise AgentError("3D_PROVIDER_UNKNOWN", "3D 服务未返回任务编号；结果待核查，不会自动重试", 502)
            with transaction() as db:
                row = require(db, id, "model3d")
                row.status = "running"
                change(row, provider_task_id=provider_id, next_poll_at=timestamp + 8)
            return
        with transaction() as db:
            provider_id = require(db, id, "model3d").payload["provider_task_id"]
        if not provider_id:
            raise AgentError("3D_PROVIDER_UNKNOWN", "3D 任务编号缺失，未重新提交", 502)
        result = _request("GET", f"{API_ROOT}/{provider_id}")
        remote_status = result.get("status")
        if remote_status in {"queued", "running"}:
            with transaction() as db:
                row = require(db, id, "model3d")
                row.status = "running"
                change(row, retry_count=0, next_poll_at=datetime.now(UTC).timestamp() + 8)
            return
        if remote_status != "succeeded":
            raise AgentError("3D_GENERATION_FAILED", "3D 服务未能生成这款模型，请查看服务端任务状态", 502)
        content = result.get("content")
        file_url = content.get("file_url") if isinstance(content, dict) else None
        if not isinstance(file_url, str):
            raise AgentError("3D_FILE_UNAVAILABLE", "3D 任务成功但没有下载地址；可稍后继续获取", 502)
        filename = _download_model(file_url)
        with transaction() as db:
            row = require(db, id, "model3d")
            row.status = "succeeded"
            raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            usage = {
                key: raw_usage[key]
                for key in ("completion_tokens", "total_tokens")
                if isinstance(raw_usage.get(key), int)
            }
            change(row, model_file=filename, usage=usage, completed_at=now())
    except AgentError as exc:
        with transaction() as db:
            row = require(db, id, "model3d")
            retryable = {
                "3D_FILE_UNAVAILABLE", "3D_PROVIDER_UNKNOWN", "3D_RATE_LIMITED", "3D_PROVIDER_REJECTED"
            }
            if state != "queued" and exc.code in retryable and row.payload.get("retry_count", 0) < 3:
                row.status = "saving" if exc.code == "3D_FILE_UNAVAILABLE" else "running"
                change(
                    row, error={"code": exc.code, "message": exc.message},
                    retry_count=row.payload.get("retry_count", 0) + 1,
                    next_poll_at=datetime.now(UTC).timestamp() + 30,
                )
            else:
                if state == "queued" and exc.code in {
                    "3D_IMAGE_TOO_LARGE", "3D_IMAGE_INVALID", "3D_SOURCE_CHANGED", "ASSET_MISSING"
                }:
                    row.status = "failed_before_submit"
                else:
                    row.status = "interrupted" if state == "queued" and exc.code == "3D_PROVIDER_UNKNOWN" else "failed"
                change(row, error={"code": exc.code, "message": exc.message})


def recover(db):
    for row in db.scalars(select(Record).where(Record.kind == "model3d", Record.status == "submitting")):
        row.status = "interrupted"
        change(row, error={"code": "3D_PROVIDER_UNKNOWN", "message": "服务重启时提交结果未知；不会重复付费提交"})
