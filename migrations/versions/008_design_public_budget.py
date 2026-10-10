"""Add cumulative public dispatch ledger without changing existing quotas or rows."""
from alembic import op

from app.agent.managed_store import PublicExecutionBudget, PublicProviderCall

revision = "008_design_public_budget"
down_revision = "007_design_demo_access"


def upgrade():
    PublicExecutionBudget.__table__.create(bind=op.get_bind(), checkfirst=True)
    PublicProviderCall.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("累计调用与未知预留必须保留，不自动删表或重置额度")
