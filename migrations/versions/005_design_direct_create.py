"""Add execution reservations and persistent provider concurrency; preserve history."""

from alembic import op

from app.agent.managed_store import ManagedBase

revision = "005_design_direct_create"
down_revision = "004_design_dual_entry"


def upgrade():
    ManagedBase.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("保留执行授权、未知调用和历史记录，不自动删除")
