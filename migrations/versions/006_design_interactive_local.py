"""Add private session purpose and per-action grants without touching existing rows."""

from alembic import op

from app.agent.managed_store import ManagedBase

revision = "006_design_interactive_local"
down_revision = "005_design_direct_create"


def upgrade():
    ManagedBase.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("保留会话分类、授权及执行历史，不自动删除")
