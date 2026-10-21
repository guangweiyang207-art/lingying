"""split default_image_backend setting into t2i and i2i

Revision ID: 5b87accc10dd
Revises: eedf0aa985e6
Create Date: 2026-05-02 21:51:02.087612

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b87accc10dd"
down_revision: str | Sequence[str] | None = "eedf0aa985e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# key 是 MySQL 保留字，统一用表达式构造让方言自动引用。
_SYSTEM_SETTING = sa.table(
    "system_setting",
    sa.column("key"),
    sa.column("value"),
    sa.column("updated_at"),
)


def upgrade() -> None:
    """把已有 default_image_backend 的值复制到 _t2i / _i2i 两条新 key（若新 key 已存在则跳过该条）。"""
    bind = op.get_bind()
    legacy_row = bind.execute(
        sa.select(_SYSTEM_SETTING.c.value).where(_SYSTEM_SETTING.c.key == "default_image_backend")
    ).fetchone()
    if legacy_row is None:
        return

    legacy_value = legacy_row[0]

    for new_key in ("default_image_backend_t2i", "default_image_backend_i2i"):
        existing = bind.execute(
            sa.select(sa.literal(1)).select_from(_SYSTEM_SETTING).where(_SYSTEM_SETTING.c.key == new_key)
        ).fetchone()
        if existing:
            continue
        bind.execute(_SYSTEM_SETTING.insert().values(key=new_key, value=legacy_value, updated_at=sa.func.now()))


def downgrade() -> None:
    """回滚仅删除新 key；旧 key 始终保留。"""
    op.execute("DELETE FROM system_setting WHERE key IN ('default_image_backend_t2i', 'default_image_backend_i2i')")
