"""Linux public initializer/runner. Inputs are private JSON; values are never printed."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path("/srv/contest/data/three-agent-public-20261010/design")
SECRETS_ROOT = Path("/srv/contest/secrets/three-agent-public-20261010")
ORIGIN = "https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com"
PROVIDER_FIELDS = frozenset({
    "MODEL_BASE_URL", "MODEL_ID", "MODEL_API_KEY", "IMAGE_BASE_URL", "IMAGE_MODEL", "IMAGE_API_KEY",
    "IMAGE_SIZE", "IMAGE_REFERENCE_FORMAT", "AGENT_VISION_BASE_URL", "AGENT_VISION_MODEL", "AGENT_VISION_API_KEY",
    "AGENT_VISION_THINKING", "AGENT_IMAGE_DOWNLOAD_HOSTS", "AGENT_ENABLED", "DESIGN_PAID_PROVIDERS_ENABLED",
    "DESIGN_IMAGE_ONLY_ENABLED", "AGENT_REASONING_CALL_MAX_FEN", "AGENT_IMAGE_CALL_MAX_FEN",
    "DESIGN_PUBLIC_TEXT_CALL_LIMIT", "DESIGN_PUBLIC_IMAGE_CALL_LIMIT",
})
OPERATOR_FIELDS = frozenset({"DESIGN_DEMO_ACCESS_ENABLED"})
PAIRING_FIELDS = ("session_secret", "product_workbench_reader", "styling_sender", "styling_product_verifier",
                  "design_package_reader", "design_event_writer", "design_asset_reader")


def private_json(path):
    path = Path(path)
    info = path.lstat()
    if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Input must be a private absolute regular JSON file (0600)")
    if info.st_size > 65536:
        raise ValueError("Private input too large")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Private JSON object required")
    return data


def defaults():
    return {
        "DESIGN_INTEGRATION_MODE": "managed", "DESIGN_INSTANCE_ID": "design-public-20261010",
        "DESIGN_SCOPE_ID": "public-three-agent-20261010", "DESIGN_DEMO_ACCESS_ENABLED": "true",
        "DESIGN_DEMO_DEPLOYMENT_MODE": "public_demo", "DESIGN_PUBLIC_ORIGIN": ORIGIN,
        "DESIGN_ALLOWED_ORIGINS": ORIGIN, "DESIGN_SESSION_COOKIE_SECURE": "true", "DESIGN_COOKIE_PATH": "/v2/design",
        "DESIGN_DEMO_DESIGNER_USERNAME": "design-employee-a", "DESIGN_MANAGER_USERNAME": "design-manager",
        "DATABASE_URL": f"sqlite:///{DATA_ROOT / 'design.sqlite3'}", "ASSETS_DIR": str(DATA_ROOT / "assets"),
        "DESIGN_PAIRING_FILE": str(SECRETS_ROOT / "design-pairing.json"),
        "DESIGN_INTERACTIVE_POLICY_FILE": str(SECRETS_ROOT / "design-interactive-policy.json"),
        "PRODUCT_HANDOFF_URL": "http://127.0.0.1:8291", "DESIGN_PUBLIC_BASE_URL": ORIGIN + "/v2/design",
        "PUBLIC_BASE_URL": ORIGIN + "/v2/design", "AGENT_ENABLED": "false",
        "DESIGN_PAID_PROVIDERS_ENABLED": "false", "DESIGN_IMAGE_ONLY_ENABLED": "false",
        "AGENT_REFERENCE_IMAGES_ENABLED": "false", "AGENT_3D_ENABLED": "false",
        "AGENT_REASONING_CALL_MAX_FEN": "50", "AGENT_IMAGE_CALL_MAX_FEN": "50",
        "DESIGN_PUBLIC_TEXT_CALL_LIMIT": "20", "DESIGN_PUBLIC_IMAGE_CALL_LIMIT": "4",
        "AGENT_WORKER_ENABLED": "true",
    }


def load_runtime(path):
    supplied = private_json(path)
    immutable = defaults()
    allowed = set(immutable) | PROVIDER_FIELDS | OPERATOR_FIELDS
    if set(supplied) - allowed or any(not isinstance(v, str) for v in supplied.values()):
        raise ValueError("Unknown or non-string runtime field")
    for key, value in supplied.items():
        if key in OPERATOR_FIELDS and value not in {"true", "false"}:
            raise ValueError("Invalid operator access switch")
        if key not in PROVIDER_FIELDS | OPERATOR_FIELDS and value != immutable[key]:
            raise ValueError("Frozen public deployment field mismatch")
    # Discard ambient project/operator env so a stale local grant cannot leak into public runtime.
    for key in list(os.environ):
        if key.startswith(("DESIGN_", "AGENT_", "MODEL_", "IMAGE_", "CMS_", "PRODUCT_HANDOFF_")):
            del os.environ[key]
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("ASSETS_DIR", None)
    os.environ.update(immutable | supplied)
    os.umask(0o077)
    for folder in (DATA_ROOT, DATA_ROOT / "assets", SECRETS_ROOT):
        if folder.is_symlink():
            raise ValueError("Persistent directory must not be a symlink")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    sys.path.insert(0, str(SOURCE_ROOT))
    from app import config
    config._load_env = lambda: None  # Never read a source checkout .env.
    config.get_config.cache_clear()
    return config.get_config()


def pairing_document(keys):
    scope, design, product = "public-three-agent-20261010", "design-public-20261010", "product-public-20261010"
    result = {"design_instance_id": design, "product_instance_id": product, "authorized_scope": scope,
              "product_base_url": "http://127.0.0.1:8291"}
    for name, purpose, subject in (
        ("package_reader", "design_package_reader", "design-public-package-reader"),
        ("event_writer", "design_event_writer", "design-public-event-writer"),
        ("asset_reader", "design_asset_reader", "product-public-asset-reader"),
    ):
        result[name] = {"subject": subject, "token": keys[purpose], "purpose": purpose,
                        "source_instance_id": product if name == "asset_reader" else design,
                        "target_instance_id": design if name == "asset_reader" else product, "authorized_scope": scope}
    return result


def policy_document():
    # Existing per-intent policy plus separate durable public instance ceiling.
    return {"policy_id": "public_design_interactive_v1", "mode": "interactive_local", "enabled": True,
            "scope_id": "public-three-agent-20261010", "target_instance_id": "design-public-20261010",
            "allowed_roles": ["designer"], "max_active_runs_per_subject": 1,
            "per_intent": {stage: {"max_calls": 1, "max_cost_fen": 50} for stage in ("text", "image")}}


def preserve_private(path, data):
    if path.exists() or path.is_symlink():
        if private_json(path) != data:
            raise ValueError("Existing private configuration differs; refusing rotation or overwrite")
        return
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        out.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")



def verify_database(*, required=False):
    database = DATA_ROOT / "design.sqlite3"
    if database.is_symlink():
        raise ValueError("Public database must not be a symlink")
    if not database.exists():
        if required:
            raise ValueError("Initialize the public database before starting the service")
        return
    # Read before migrations: never import a private/foreign DB into the public instance.
    import sqlite3
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        row = db.execute("SELECT instance_id, scope_id FROM design_public_execution_budget WHERE id=1").fetchone()
    if row != ("design-public-20261010", "public-three-agent-20261010"):
        raise ValueError("Existing database does not belong to this public instance")
    secret = DATA_ROOT / ".design-demo-secret"
    info = secret.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 48:
        raise ValueError("Preserve the existing private demo recovery secret")

def initialize(pairing_input):
    keys = private_json(pairing_input)
    if (set(keys) != set(PAIRING_FIELDS)
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", v) for v in keys.values())
            or len(set(keys.values())) != len(keys)):
        raise ValueError("Seven distinct paired service keys required")
    from app import models
    from app.config import get_config
    if not get_config().design_demo_access_enabled:
        raise ValueError("Initialize demo roles before restoring ordinary login")
    from app.agent import public_budget
    from app.agent.managed_store import PublicExecutionBudget
    from app.agent.migrate import migrate
    from app.design_demo_access import initialize as initialize_demo
    database = DATA_ROOT / "design.sqlite3"
    verify_database()
    preserve_private(SECRETS_ROOT / "design-pairing.json", pairing_document(keys))
    preserve_private(SECRETS_ROOT / "design-interactive-policy.json", policy_document())
    models.init_db()
    migrate()
    initialize_demo()
    with models.session() as db:
        public_budget.initialize(db)
        db.commit()
        row = db.get(PublicExecutionBudget, 1)
        print(json.dumps({"ready": True, "instance_id": row.instance_id, "scope_id": row.scope_id,
                          "roles": ["designer", "manager"], "provider_requests": 0,
                          "budget": public_budget.projection(db)}, ensure_ascii=False))
    database.chmod(0o600)


def run():
    verify_database(required=True)
    from app.agent.managed_bridge import Settings
    settings = Settings()
    for name in ("package_reader", "event_writer", "asset_reader"):
        settings.credential(name)
    import uvicorn
    # Retain actual loopback peer for public proxy guard; one embedded worker, one process.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8192, workers=1, proxy_headers=False, access_log=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-json", required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--pairing-input", default=str(SECRETS_ROOT / "pairing-input.json"))
    args = parser.parse_args()
    try:
        load_runtime(args.env_json)
        if args.initialize:
            initialize(args.pairing_input)
        else:
            run()
    except Exception:
        # Configuration values and provider secrets must never enter a traceback or stdout.
        print("Public design startup failed: verify private inputs, frozen paths and database ownership",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
