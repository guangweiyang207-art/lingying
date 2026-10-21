"""mysql dialect equivalents

MySQL 5.7 无部分索引与表达式索引，链上各迁移对这两类索引跳过创建；本迁移在链尾
以「生成列 + 全列唯一索引」补齐语义等价，并把 b942e8c5d545 只对 PG 改型的历史
VARCHAR 时间戳列定型为 DATETIME(6)。非 MySQL 方言整体空操作——部分索引的语义由
sqlite_where / postgresql_where 承载，时间戳列在 PG 早已是 timestamptz、在 SQLite
保持 VARCHAR（类型转换在 Python 层完成）。

Revision ID: 9d3f2a7b6c51
Revises: b25cfcfb8bad
Create Date: 2026-10-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d3f2a7b6c51"
down_revision: str | Sequence[str] | None = "b25cfcfb8bad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# b942e8c5d545 的 TIMESTAMP_COLUMNS 在 MySQL 上的现状：b942 只对 PG 做 ALTER TYPE，
# SQLite 保持 VARCHAR，MySQL 因此同样是 VARCHAR（无长度 String 经 lib.db.mysql_compat
# 定型为 VARCHAR(255)）。本迁移把它们定型为 DATETIME(6)。
_TIMESTAMP_COLUMNS: dict[str, list[str]] = {
    "tasks": ["queued_at", "started_at", "finished_at", "updated_at"],
    "worker_lease": ["updated_at"],
    "api_calls": ["started_at", "finished_at", "created_at"],
    "agent_sessions": ["created_at", "updated_at"],
}
_NULLABLE: set[tuple[str, str]] = {
    ("tasks", "started_at"),
    ("tasks", "finished_at"),
    ("api_calls", "finished_at"),
    ("api_calls", "created_at"),
}

# 应用侧音色试听记录写出的 segment_id（voice_sample__<名称>__<hash>）超过建表时的
# VARCHAR(20) 声明——SQLite 不校验长度，数据实际更宽，这里定型为 VARCHAR(255)。
_COLUMN_WIDTH_FIXES: tuple[tuple[str, str, int, str], ...] = (("api_calls", "segment_id", 255, "NULL"),)

# 部分唯一索引的生成列等价：生成列在「参与唯一性判定」时取业务键、否则取 NULL，
# MySQL 唯一索引对 NULL 不判重，恰好还原 WHERE 条件的豁免语义。MD5 定长 CHAR(32)
# 是为了绕开 InnoDB 3072 字节索引键上限（六个 VARCHAR(255) 拼起来远超）。
_PARTIAL_UNIQUE_EQUIVALENTS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        "tasks",
        "dedupe_active_key",
        "idx_tasks_dedupe_active",
        "CHAR(32) CHARACTER SET ascii AS ("
        "CASE WHEN `status` IN ('queued', 'running') "
        "THEN MD5(CONCAT_WS('|', `project_name`, `user_id`, `task_type`, `resource_id`, "
        "COALESCE(`script_file`, ''), COALESCE(`resource_type`, ''))) "
        "ELSE NULL END) VIRTUAL",
        ("dedupe_active_key",),
    ),
    (
        "provider_credential",
        "active_marker",
        "uq_provider_credential_one_active",
        "TINYINT AS (CASE WHEN `is_active` = 1 THEN 1 ELSE NULL END) VIRTUAL",
        ("provider", "active_marker"),
    ),
    (
        "agent_anthropic_credentials",
        "active_marker",
        "uq_agent_credential_one_active_per_user",
        "TINYINT AS (CASE WHEN `is_active` = 1 THEN 1 ELSE NULL END) VIRTUAL",
        ("user_id", "active_marker"),
    ),
)


def _mysql_only(bind) -> bool:
    return bind.dialect.name == "mysql"


def _convert_varchar_timestamps(bind) -> None:
    """历史 VARCHAR 时间戳列 → DATETIME(6)。

    链上部分时间戳列已被更早的迁移定型为 DATETIME（如 ea2e1a477bbf 对 api_calls.created_at），
    MySQL 严格模式下对 DATETIME 列做 `WHERE col = ''` 比较会直接报 1292——DATETIME 列也
    不可能存空串。因此按实际列型分流：仅对仍为 VARCHAR 的列做空串清洗 + MODIFY，其余跳过。

    清洗规则：可空列清成 NULL；非空列（应用侧总是写入完整时间戳，空串只可能是历史脏数据）
    回填纪元起点。
    """
    inspector = sa.inspect(bind)
    for table, columns in _TIMESTAMP_COLUMNS.items():
        column_types = {column["name"]: column["type"] for column in inspector.get_columns(table)}
        for col in columns:
            if not isinstance(column_types[col], sa.String):
                continue
            nullable = (table, col) in _NULLABLE
            replacement = "NULL" if nullable else "'1970-01-01 00:00:00.000000'"
            bind.execute(sa.text(f"UPDATE `{table}` SET `{col}` = {replacement} WHERE `{col}` = ''"))
            bind.execute(
                sa.text(f"ALTER TABLE `{table}` MODIFY `{col}` DATETIME(6) {'NULL' if nullable else 'NOT NULL'}")
            )


def upgrade() -> None:
    bind = op.get_bind()
    if not _mysql_only(bind):
        return

    _convert_varchar_timestamps(bind)

    for table, column, width, nullability in _COLUMN_WIDTH_FIXES:
        bind.execute(sa.text(f"ALTER TABLE `{table}` MODIFY `{column}` VARCHAR({width}) {nullability}"))

    for table, column, index, generation, index_columns in _PARTIAL_UNIQUE_EQUIVALENTS:
        bind.execute(sa.text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {generation}"))
        op.create_index(index, table, list(index_columns), unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not _mysql_only(bind):
        return

    for table, column, index, _generation, _index_columns in reversed(_PARTIAL_UNIQUE_EQUIVALENTS):
        op.drop_index(index, table_name=table)
        bind.execute(sa.text(f"ALTER TABLE `{table}` DROP COLUMN `{column}`"))

    # VARCHAR 时间戳不回退：DATETIME(6) 是这里唯一有精度的形态，回退只会丢微秒。
