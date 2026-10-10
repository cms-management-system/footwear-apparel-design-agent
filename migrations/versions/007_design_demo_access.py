"""Add reversible local demo identity metadata; preserve all business rows."""
from alembic import op

from app.models import DesignDemoChain, DesignDemoSession

revision = "007_design_demo_access"
down_revision = "006_design_interactive_local"


def upgrade():
    DesignDemoChain.__table__.create(bind=op.get_bind(), checkfirst=True)
    DesignDemoSession.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("保留演示用途链与原会话历史，关闭开关撤权，不自动删表")
