"""Real, opt-in adapters. No runtime mock or placeholder fallback."""

import base64
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from ..config import get_config
from . import assets
from .schemas import Action, Inspection, Review, StylePlanOutput
from .store import AgentError
from .streaming import collect_stream

PROMPTS = Path(__file__).parent / "prompts"
log = logging.getLogger(__name__)


def _has_field(label, name):
    text = str(label).strip()
    return text.startswith(f"{name}=") or f"; {name}=" in text


def _role_phrase(label):
    return str(label).strip().split(";")[-1].strip()


def _is_current_design(label):
    if _has_field(label, "asset_id"):
        return False
    return _has_field(label, "current_version_id") or _role_phrase(label) == "本轮修正底图"


def _is_edit_base(label):
    if _has_field(label, "asset_id"):
        return False
    return _has_field(label, "base_version_id") or _role_phrase(label) == "修改底图"


def _is_user_reference(label):
    if _has_field(label, "asset_id"):
        return True
    return not any(
        _has_field(label, name) or _role_phrase(label) == phrase
        for name, phrase in (
            ("base_version_id", "修改底图"),
            ("current_version_id", "本轮修正底图"),
            ("parent_version_id", "修正前底图"),
            ("candidate_id", "待检查候选"),
        )
    )


def _primary_reference(images):
    """Image being edited or the current selected design, otherwise the first user reference."""
    for predicate in (_is_current_design, _is_edit_base, _is_user_reference):
        for item in images:
            if predicate(item[0]):
                return item
    return images[0]


class Provider:
    def __init__(self):
        cfg = get_config()
        self.vision_url = os.getenv("AGENT_VISION_BASE_URL", "")
        self.vision_model = os.getenv("AGENT_VISION_MODEL", "")
        self.vision_key = os.getenv("AGENT_VISION_API_KEY", "")
        self.image_url, self.image_key, self.image_model = cfg.image_base_url, cfg.image_api_key, cfg.image_model
        self.image_size = cfg.image_size
        self.image_reference_format = cfg.image_reference_format
        self.vision_thinking = os.getenv("AGENT_VISION_THINKING", "")
        self.enabled = os.getenv("AGENT_ENABLED", "false") == "true"
        self.image_verified = os.getenv("AGENT_REFERENCE_IMAGES_ENABLED", "false") == "true"
        self.reasoning_fen = self._number("AGENT_REASONING_CALL_MAX_FEN")
        self.image_fen = self._number("AGENT_IMAGE_CALL_MAX_FEN")
        # Retained in capabilities for older clients; it no longer blocks image generation.
        self.monthly_fen = min(self._number("AGENT_MONTHLY_ALLOCATION_FEN"), 20000)

    @staticmethod
    def _number(key):
        try:
            return max(0, int(os.getenv(key, "0")))
        except ValueError:
            return 0

    def capabilities(self):
        vision = bool(
            self.enabled
            and self.vision_model
            and self.vision_key
            and self.vision_url
            and self.reasoning_fen
        )
        render = bool(
            vision
            and self.image_verified
            and self.image_key
            and self.image_model
            and self.image_fen
        )
        return {
            "understand": vision,
            "design": render,
            "vision_service": urlparse(self.vision_url).hostname or "尚未配置视觉服务",
            "image_service": urlparse(self.image_url).hostname or "尚未配置图片服务",
            "vision_model": self.vision_model,
            "vision_thinking": self.vision_thinking,
            "image_model": self.image_model,
            "reasoning_call_max_fen": self.reasoning_fen,
            "image_call_max_fen": self.image_fen,
            "monthly_allocation_fen": self.monthly_fen,
            "quality_status": "待真实素材验收",
            "edit_control": "参考图引导编辑，需逐项检查非目标区域",
            "note": "识图模型、参考图接口和费用上界配置齐全后方可运行；效果图不证明面料物理性能。",
        }

    def require(self, mode):
        if not self.capabilities()[mode]:
            raise AgentError("CAPABILITY_UNAVAILABLE", "当前模型能力或费用配置尚未就绪；要求单和素材已保存", 503)

    @staticmethod
    def _post(url, key, payload, on_preview=None):
        try:
            # Construction is within the protected block. No hidden HTTP/SDK retries.
            with httpx.Client(timeout=httpx.Timeout(150, connect=15), follow_redirects=False) as client:
                with client.stream("POST", url, headers={"Authorization": f"Bearer {key}"}, json=payload) as res:
                    if res.status_code >= 400:
                        # Interpret only an allowlisted vendor code, never surface its body.
                        error_body = bytearray()
                        for part in res.iter_bytes():
                            error_body.extend(part[: 32769 - len(error_body)])
                            if len(error_body) > 32768:
                                break
                        try:
                            vendor_code = json.loads(error_body).get("error", {}).get("code")
                        except (ValueError, AttributeError, TypeError):
                            vendor_code = None
                        if vendor_code == "ModelNotOpen":
                            raise AgentError(
                                "PROVIDER_MODEL_NOT_OPEN",
                                "方舟账号尚未开通所选视觉模型，请在开通管理中启用后再运行",
                                502,
                            )
                        if res.status_code in {401, 403}:
                            raise AgentError(
                                "PROVIDER_ACCESS_DENIED",
                                "模型服务鉴权或访问权限不足，请核对模型开通状态和密钥权限",
                                502,
                            )
                        if res.status_code == 404:
                            raise AgentError(
                                "PROVIDER_MODEL_UNAVAILABLE", "模型或接入点不可用，请核对模型名称及开通状态", 502
                            )
                        if res.status_code == 429:
                            raise AgentError(
                                "PROVIDER_RATE_LIMITED", "模型服务限流或额度不足；本轮不会自动重复调用", 502
                            )
                        raise AgentError(
                            "PROVIDER_REJECTED", "模型服务拒绝了请求，请检查后台配置；不会自动重复调用", 502
                        )
                    if on_preview is not None:
                        return collect_stream(res.iter_lines(), on_preview)
                    chunks, size = [], 0
                    for part in res.iter_bytes():
                        size += len(part)
                        if size > 20 * 1024 * 1024:
                            raise AgentError("PROVIDER_OUTPUT_INVALID", "模型返回内容过大，请核查本次调用", 502)
                        chunks.append(part)
                    return json.loads(b"".join(chunks))
        except AgentError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise AgentError("CALL_OUTCOME_UNKNOWN", "服务连接中断，本次调用是否计费未知；不会自动重试", 502) from exc
        except Exception as exc:
            raise AgentError("PROVIDER_OUTPUT_INVALID", "模型返回格式无效；已保留本次调用记录", 502) from exc

    @staticmethod
    def receipt(result):
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        allowed = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"}
        return {
            "request_id": str(result.get("id", ""))[:128],
            "usage": {k: v for k, v in usage.items() if k in allowed and isinstance(v, int)},
        }

    def structured(self, prompt, context, schema, images=()):
        self.require("understand")
        content = [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}]
        for label, payload in images:
            content += [
                {"type": "text", "text": label},
                {"type": "image_url", "image_url": {"url": assets.data_url(payload)}},
            ]
        system = (PROMPTS / f"{prompt}.txt").read_text() + "\n只输出符合以下 Schema 的 JSON：\n"
        system += json.dumps(schema.model_json_schema(), ensure_ascii=False)
        self.last_receipt = {}
        preview = getattr(self, "on_preview", None)
        result = self._post(
            self.vision_url.rstrip("/") + "/chat/completions",
            self.vision_key,
            {
                "model": self.vision_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "max_tokens": 4096,
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                **({"stream": True, "stream_options": {"include_usage": True}} if preview else {}),
                **({"thinking": {"type": self.vision_thinking}} if self.vision_thinking else {}),
            },
            **({"on_preview": preview} if preview else {}),
        )
        self.last_receipt = self.receipt(result)
        try:
            return schema.model_validate_json(result["choices"][0]["message"]["content"]).model_dump()
        except ValidationError as exc:
            # Store structure only: error inputs/messages/context can contain designer material.
            definitions = schema.model_json_schema()
            fields = set(definitions.get("properties", {}))
            for definition in definitions.get("$defs", {}).values():
                fields.update(definition.get("properties", {}))
            details = [
                {
                    "path": ".".join(
                        str(part) if isinstance(part, int) or part in fields else "unknown" for part in error["loc"]
                    ),
                    "type": error["type"],
                }
                for error in exc.errors(include_input=False, include_context=False, include_url=False)[:12]
            ]
            raise AgentError(
                "MODEL_SCHEMA_INVALID",
                "这次回复没有整理成功，你的消息已保存。可以继续补充想法，我会接着处理。",
                502,
                details=details,
                receipt=self.last_receipt,
            ) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError(
                "PROVIDER_OUTPUT_INVALID",
                "模型服务返回异常，你的消息已保存，可以稍后继续。",
                502,
                receipt=self.last_receipt,
            ) from exc

    def plan(self, context):
        return self.structured("agent-v1", context, Action)

    def plan_directions(self, context):
        return self.structured("style-v1", context, StylePlanOutput)

    def inspect(self, spec, images):
        return self.structured("inspect-v1", spec, Inspection, images)

    def compare(self, spec, images):
        return self.structured("review-v1", spec, Review, images)

    def render(self, spec, images, feedback):
        self.require("design")
        prompt = (PROMPTS / "render-v1.txt").read_text()
        prompt += "\n设计要求：" + json.dumps(spec, ensure_ascii=False)
        prompt += "\n图像顺序及用途：" + json.dumps([label for label, _ in images], ensure_ascii=False)
        prompt += "\n本轮修正依据：" + json.dumps(feedback, ensure_ascii=False)
        payload = {
            "model": self.image_model,
            "prompt": prompt,
            "size": self.image_size,
            "response_format": "b64_json",
            "sequential_image_generation": "disabled",
            "stream": False,
            "watermark": True,
        }
        if images:
            payload["image"] = self._reference_images(images)
        result = self._post(self.image_url.rstrip("/") + "/images/generations", self.image_key, payload)
        self.last_receipt = self.receipt(result)
        try:
            data = result["data"]
            if len(data) != 1:
                raise ValueError()
            return base64.b64decode(data[0]["b64_json"], validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AgentError("IMAGE_OUTPUT_INVALID", "服务未返回一张有效图片；不会用占位图替代", 502) from exc

    def _reference_images(self, images):
        if self.image_reference_format != "single":
            return [assets.data_url(payload) for _, payload in images]
        _, payload = _primary_reference(images)
        dropped = len(images) - 1
        if dropped:
            log.info("已忽略其余 %d 张参考图，仅发送主参考图", dropped)
        return assets.data_url(payload)
