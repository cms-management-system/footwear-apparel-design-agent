"""Owner bindings and durable single-image authorization reservations."""

from alembic import op

from app.agent.managed_store import ManagedBase

revision = "004_design_dual_entry"
down_revision = "003_managed_handoff"


def upgrade():
    ManagedBase.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("保留自主项目与图片调用授权记录，不自动删除")
