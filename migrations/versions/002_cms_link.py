"""Store the linked CMS package and design responses on the project row."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "002_cms_link"
down_revision = "001_design_agent"

_COLUMNS = (
    sa.Column("cms_package_id", sa.String(80), nullable=True),
    sa.Column("cms_package_snapshot", sa.Text(), nullable=True),
    sa.Column("cms_responses", sa.Text(), nullable=True),
)


def upgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("project")}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("project", column)


def downgrade():
    raise RuntimeError("禁止自动删除项目上的 CMS 关联，请从迁移前备份恢复")
