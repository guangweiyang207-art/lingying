"""create market_installation table

Revision ID: c5a819b247de
Revises: b4e8d2c61a57
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5a819b247de"
down_revision: str | Sequence[str] | None = "b4e8d2c61a57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # MySQL：(source_key VARCHAR(2048), slug VARCHAR(64)) 组合唯一超过索引键 3072 字节上限，
    # 以 CONCAT 后取 MD5 的生成列承载唯一约束（语义等价全值唯一）。
    is_mysql = op.get_bind().dialect.name == "mysql"
    op.create_table(
        "market_installation",
        sa.Column("custom_endpoint_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(2048), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("installed_version", sa.Text(), nullable=False),
        sa.Column("installed_digest", sa.String(64), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["custom_endpoint_id"], ["custom_endpoint.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("custom_endpoint_id"),
        *([] if is_mysql else [sa.UniqueConstraint("source_key", "slug")]),
    )
    if is_mysql:
        op.execute(
            "ALTER TABLE market_installation ADD COLUMN source_slug_hash CHAR(32) CHARACTER SET ascii "
            "AS (MD5(CONCAT(source_key, '|', slug))) VIRTUAL"
        )
        op.create_index("uq_market_installation_source_slug", "market_installation", ["source_slug_hash"], unique=True)


def downgrade() -> None:
    op.drop_table("market_installation")
