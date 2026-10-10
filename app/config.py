"""配置：只从环境变量/.env 读取；密钥不回显、不进日志。"""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def _positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value if value is not None else "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _image_reference_format(value: str | None) -> str:
    """list sends a JSON array (Ark). single sends one string (relays that reject arrays)."""
    fmt = (value or "").strip().lower()
    if fmt in {"list", "single"}:
        return fmt
    return "list"


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class Config:
    def __init__(self) -> None:
        _load_env()
        self.design_integration_mode = os.environ.get("DESIGN_INTEGRATION_MODE", "legacy").strip().lower()
        if self.design_integration_mode not in {"legacy", "managed"}:
            raise ValueError("DESIGN_INTEGRATION_MODE must be legacy or managed")
        self.managed = self.design_integration_mode == "managed"
        self.design_instance_id = os.environ.get("DESIGN_INSTANCE_ID", "").strip()
        self.design_scope_id = os.environ.get("DESIGN_SCOPE_ID", "").strip()
        if self.managed and not all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
            for value in (self.design_instance_id, self.design_scope_id)
        ):
            raise ValueError("Managed mode requires explicit DESIGN_INSTANCE_ID and DESIGN_SCOPE_ID")
        origins = os.environ.get("DESIGN_ALLOWED_ORIGINS", "http://localhost:3092,http://127.0.0.1:3092")
        self.design_allowed_origins = tuple(
            dict.fromkeys(value.strip() for value in origins.split(",") if value.strip())
        )
        if self.managed:
            for origin in self.design_allowed_origins:
                parsed = urlsplit(origin)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or "*" in origin
                        or parsed.username is not None or parsed.password is not None
                        or parsed.path or parsed.query or parsed.fragment):
                    raise ValueError("DESIGN_ALLOWED_ORIGINS must contain exact HTTP origins")
                _ = parsed.port  # Reject invalid port syntax before serving requests.
        default_cookie = (
            "design_session_" + hashlib.sha256(self.design_instance_id.encode()).hexdigest()[:12]
            if self.managed else "design_session"
        )
        self.design_session_cookie = os.environ.get("DESIGN_SESSION_COOKIE_NAME", default_cookie)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.design_session_cookie):
            raise ValueError("Invalid DESIGN_SESSION_COOKIE_NAME")
        self.design_session_cookie_secure = os.environ.get("DESIGN_SESSION_COOKIE_SECURE", "false").lower() == "true"
        self.design_paid_providers_enabled = os.environ.get(
            "DESIGN_PAID_PROVIDERS_ENABLED", "false" if self.managed else "true"
        ).lower() == "true"
        self.design_image_only_enabled = os.environ.get("DESIGN_IMAGE_ONLY_ENABLED", "false").lower() == "true"
        self.design_image_authorization_file = os.environ.get("DESIGN_IMAGE_AUTHORIZATION_FILE", "")
        self.design_conversation_authorization_file = os.environ.get("DESIGN_CONVERSATION_AUTHORIZATION_FILE", "")
        self.image_api_key = os.environ.get("IMAGE_API_KEY", "")
        self.image_base_url = os.environ.get("IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.image_model = os.environ.get("IMAGE_MODEL", "")
        self.image_size = os.environ.get("IMAGE_SIZE", "2K")
        self.image_reference_format = _image_reference_format(os.environ.get("IMAGE_REFERENCE_FORMAT"))
        self.db_url = os.environ.get("DATABASE_URL", "sqlite:///data/app.sqlite3")
        self.cms_base_url = os.environ.get("CMS_BASE_URL", "http://127.0.0.1:8001").strip().rstrip("/")
        self.cms_api_key = os.environ.get("CMS_API_KEY", "").strip()
        self.cms_timeout = _positive_float(os.environ.get("CMS_TIMEOUT", "10"), 10.0)
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8020").strip().rstrip("/")
        self.product_handoff_url = os.environ.get("PRODUCT_HANDOFF_URL", "http://127.0.0.1:3112").rstrip("/")
        self.product_handoff_key = os.environ.get("PRODUCT_HANDOFF_KEY", "")
        self.design_public_base_url = os.environ.get("DESIGN_PUBLIC_BASE_URL", "http://127.0.0.1:8020").rstrip("/")
        self.design_auth_required = self.managed or os.environ.get("DESIGN_AUTH_REQUIRED", "false").lower() == "true"
        self.design_manager_username = os.environ.get("DESIGN_MANAGER_USERNAME", "")
        self.design_manager_password_hash = os.environ.get("DESIGN_MANAGER_PASSWORD_HASH", "")
        self.design_demo_access_enabled = os.environ.get("DESIGN_DEMO_ACCESS_ENABLED", "false").lower() == "true"
        self.design_demo_designer_username = os.environ.get("DESIGN_DEMO_DESIGNER_USERNAME", "design-employee-a")
        self.design_demo_cookie = self.design_session_cookie + "_demo"
        self.design_demo_chain_cookie = self.design_session_cookie + "_demo_chain"
        self.design_demo_deployment_mode = os.environ.get("DESIGN_DEMO_DEPLOYMENT_MODE", "local")
        if self.design_demo_deployment_mode not in {"local", "public_demo"}:
            raise ValueError("DESIGN_DEMO_DEPLOYMENT_MODE must be local or public_demo")
        self.public_demo = self.design_demo_deployment_mode == "public_demo"
        self.design_public_origin = os.environ.get("DESIGN_PUBLIC_ORIGIN", "").strip()
        self.design_cookie_path = os.environ.get("DESIGN_COOKIE_PATH", "/")
        if not re.fullmatch(r"/[A-Za-z0-9/_-]*", self.design_cookie_path) or "//" in self.design_cookie_path:
            raise ValueError("Invalid DESIGN_COOKIE_PATH")
        if self.public_demo:
            parsed = urlsplit(self.design_public_origin)
            if (not self.managed or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or "*" in self.design_public_origin
                    or self.design_allowed_origins != (self.design_public_origin,)
                    or not self.design_session_cookie_secure or self.design_cookie_path != "/v2/design"
                    or self.design_instance_id != "design-public-20261010"
                    or self.design_scope_id != "public-three-agent-20261010"):
                raise ValueError(
                    "Public demo requires its exact HTTPS origin, Secure cookie, prefix and public identity"
                )
            _ = parsed.port
        self.design_public_text_limit = int(os.environ.get("DESIGN_PUBLIC_TEXT_CALL_LIMIT", "20"))
        self.design_public_image_limit = int(os.environ.get("DESIGN_PUBLIC_IMAGE_CALL_LIMIT", "4"))
        if not all(0 <= n <= 100000 for n in (self.design_public_text_limit, self.design_public_image_limit)):
            raise ValueError("Invalid public cumulative call limits")
        if self.design_demo_access_enabled:
            if not self.managed:
                raise ValueError("Demo access requires managed mode")
            for origin in self.design_allowed_origins if not self.public_demo else ():
                host = urlsplit(origin).hostname
                if host not in {"localhost", "127.0.0.1", "::1"} and not host.endswith(".localhost"):
                    raise ValueError("Demo access requires exact loopback origins")
        self.assets_dir = Path(os.environ.get("ASSETS_DIR", "data/assets"))
        if not self.assets_dir.is_absolute():
            self.assets_dir = ROOT / self.assets_dir
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.design_demo_secret_file = self.assets_dir.parent / ".design-demo-secret"

@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
