"""Add isolated agent storage; existing garment tables are untouched."""

import sqlalchemy as sa
from alembic import op

revision = "001_design_agent"
down_revision = None


def upgrade():
    op.create_table(
        "design_agent_record",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("dedupe", sa.String(160), nullable=False, unique=True),
    )
    op.create_index("ix_design_agent_record_project_id", "design_agent_record", ["project_id"])
    op.create_index("ix_design_agent_record_kind", "design_agent_record", ["kind"])
    op.create_table(
        "design_agent_lease",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("expires", sa.Integer(), nullable=False),
    )


def downgrade():
    raise RuntimeError("禁止自动删除设计数据，请从迁移前备份恢复到单独数据库")
