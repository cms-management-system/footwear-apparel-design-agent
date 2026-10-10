"""Add managed receipts, action history and durable delivery outbox."""

from alembic import op

from app.agent.managed_store import ManagedBase

revision = "003_managed_handoff"
down_revision = "002_cms_link"


def upgrade():
    ManagedBase.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("禁止自动删除交接原件、审批历史与回传记录，请保留数据并恢复到单独数据库")
