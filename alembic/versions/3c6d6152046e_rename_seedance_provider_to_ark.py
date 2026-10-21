"""rename seedance provider to ark

Revision ID: 3c6d6152046e
Revises: ea2e1a477bbf
Create Date: 2026-03-26 12:57:36.230376

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c6d6152046e"
down_revision: str | Sequence[str] | None = "ea2e1a477bbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE provider_config SET provider = 'ark' WHERE provider = 'seedance'")
    # key 是 MySQL 保留字，用表达式构造让方言自动引用。
    system_setting = sa.table("system_setting", sa.column("key"), sa.column("value"))
    op.execute(
        system_setting.update()
        .where(system_setting.c.key.in_(["default_video_backend", "default_image_backend"]))
        .values(value=sa.func.replace(system_setting.c.value, "seedance/", "ark/"))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE provider_config SET provider = 'seedance' WHERE provider = 'ark'")
    op.execute(
        "UPDATE system_setting SET value = REPLACE(value, 'ark/', 'seedance/') "
        "WHERE key IN ('default_video_backend', 'default_image_backend')"
    )
