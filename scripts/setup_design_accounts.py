"""Create local manager/designer accounts and write initial credentials outside Git."""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.design_auth import password_hash  # noqa: E402
from app.models import DesignerUser, init_db, session  # noqa: E402


def main() -> None:
    init_db()
    credential_path = Path(__file__).resolve().parents[2] / ".local-agent-keys" / "design-accounts.txt"
    credential_path.parent.mkdir(mode=0o700, exist_ok=True)
    entries: list[str] = []
    with session() as db:
        for username, role, display_name in [
            ("design_manager", "manager", "设计负责人"),
            ("designer", "designer", "设计人员"),
        ]:
            if db.get(DesignerUser, username):
                continue
            password = secrets.token_urlsafe(18)
            db.add(
                DesignerUser(
                    username=username,
                    role=role,
                    display_name=display_name,
                    password_hash=password_hash(password),
                    active=True,
                )
            )
            entries.append(f"{display_name}\n账号：{username}\n密码：{password}")
        db.commit()
    if entries:
        credential_path.write_text("款式工场本机账号（勿分享）\n\n" + "\n\n".join(entries) + "\n", encoding="utf-8")
        credential_path.chmod(0o600)
    print(f"账号已配置；新账号信息保存在 {credential_path}。已存在账号不会重置密码。")


if __name__ == "__main__":
    main()
