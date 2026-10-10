"""Bind the root-authorized single image to one assigned P0 project; never call a provider."""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-directory", required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--subject", required=True)
    args = parser.parse_args()
    private = Path(args.runtime_directory).resolve()
    for name in list(os.environ):
        if name.startswith(("MODEL_", "IMAGE_", "AGENT_", "DESIGN_", "CMS_", "PRODUCT_")):
            os.environ.pop(name, None)
    env = json.loads((private / "runtime-env.json").read_text())
    os.environ.update(env)
    sys.path.insert(0, env["PYTHONPATH"])
    from app import config

    config._load_env = lambda: None
    from sqlalchemy import select

    from app.agent.managed_store import ImageReservation, ManagedReceipt
    from app.agent.store import AgentError, Record
    from app.models import DesignerUser, DesignHandoff, session

    with session() as db:
        row = db.scalar(select(DesignHandoff).where(DesignHandoff.project_id == args.project_id))
        meta = db.get(ManagedReceipt, row.id) if row else None
        user = db.get(DesignerUser, args.subject)
        cfg = config.get_config()
        if (
            not row
            or not meta
            or row.status != "assigned"
            or row.assignee != args.subject
            or not user
            or not user.active
            or user.role != "designer"
            or row.package_id != args.package_id
            or meta.priority != "P0"
            or meta.proof["package"]["approval"]["final_priority"] != "P0"
            or meta.scope_id != cfg.design_scope_id
            or meta.target_instance_id != cfg.design_instance_id
        ):
            raise AgentError("IMAGE_AUTHORIZATION_REQUIRED", "仅允许本轮已批准并分派的准确P0项目", 409)
        if db.scalar(select(ImageReservation).limit(1)):
            raise AgentError("IMAGE_QUOTA_EXHAUSTED", "本轮单张已预留，禁止另发授权", 409)
        if db.scalar(select(Record).where(Record.kind == "task", Record.status.in_(["running", "queued"]))):
            raise AgentError("TASK_ACTIVE", "等待原任务安全点", 409)
        grant = {
            "grant_id": "full-chain-live-20261010-one-design",
            "project_id": args.project_id,
            "source_mode": "upstream",
            "subject": args.subject,
            "scope_id": cfg.design_scope_id,
            "expires_at": int(time.time()) + 3600,
            "max_calls": 1,
        }
    path = Path(env["DESIGN_IMAGE_AUTHORIZATION_FILE"])
    if path.exists():
        old = json.loads(path.read_text())
        if {k: v for k, v in old.items() if k != "expires_at"} != {k: v for k, v in grant.items() if k != "expires_at"}:
            raise AgentError("IMAGE_AUTHORIZATION_REQUIRED", "已有另一项目授权，未覆盖", 409)
        print(
            json.dumps(
                {
                    "status": "existing_authorization",
                    "project_id": args.project_id,
                    "grant_id": old["grant_id"],
                    "expires_at": old["expires_at"],
                }
            )
        )
    else:
        with path.open("x") as stream:
            os.chmod(path, 0o600)
            json.dump(grant, stream)
        print(
            json.dumps(
                {
                    "status": "authorized_one_image_not_generated",
                    "project_id": args.project_id,
                    "grant_id": grant["grant_id"],
                    "expires_at": grant["expires_at"],
                }
            )
        )


if __name__ == "__main__":
    main()
