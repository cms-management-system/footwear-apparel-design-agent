import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from ..config import ROOT
from ..models import engine


def migrate():
    eng = engine()
    if "design_agent_schema_version" not in inspect(eng).get_table_names():
        path = eng.url.database
        if eng.dialect.name == "sqlite" and path and Path(path).is_file():
            backup = Path(path).with_suffix(".before-design-agent.sqlite3")
            if not backup.exists():
                with sqlite3.connect(path) as src, sqlite3.connect(backup) as dst:
                    src.backup(dst)
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
