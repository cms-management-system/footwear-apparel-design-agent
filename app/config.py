"""配置：只从环境变量/.env 读取；密钥不回显、不进日志。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        self.image_api_key = os.environ.get("IMAGE_API_KEY", "")
        self.image_base_url = os.environ.get("IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.image_model = os.environ.get("IMAGE_MODEL", "")
        self.image_size = os.environ.get("IMAGE_SIZE", "2K")
        self.db_url = os.environ.get("DATABASE_URL", "sqlite:///data/app.sqlite3")
        self.assets_dir = Path(os.environ.get("ASSETS_DIR", "data/assets"))
        if not self.assets_dir.is_absolute():
            self.assets_dir = ROOT / self.assets_dir
        self.assets_dir.mkdir(parents=True, exist_ok=True)

@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
