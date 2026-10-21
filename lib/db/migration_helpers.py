"""Alembic 迁移共用的 schema 辅助。

放在 lib 下而非 alembic/ 下：``alembic`` 这个导入名已被安装的 alembic 包占用，迁移脚本无法
从版本目录旁边导入同级模块。迁移执行时项目根目录已在 sys.path 上（alembic.ini 的
``prepend_sys_path = .``）。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op


def _index_ddl(bind: Connection, table_name: str) -> dict[str, str]:
    """读 sqlite_master 里该表的索引原始 DDL。

    ``sql IS NOT NULL`` 过滤掉 UNIQUE 约束隐式生成的 ``sqlite_autoindex_*``——它们随表定义走，
    没有独立 DDL 可回放。
    """
    rows = bind.execute(
        sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table AND sql IS NOT NULL"),
        {"table": table_name},
    ).fetchall()
    return {row[0]: row[1] for row in rows}


@contextmanager
def preserve_sqlite_indexes(table_name: str) -> Generator[None]:
    """在 SQLite 重建表期间保住反射不出的索引。

    SQLite 上 ``batch_alter_table`` 的增删列走重建表路径，而 SQLAlchemy 反射不出表达式型索引
    （反射时仅给出 ``SAWarning: Skipped unsupported reflection of expression-based index``），
    重建后这类索引会静默消失——``tasks`` 上的 ``idx_tasks_dedupe_active`` 就是这样丢的，丢了
    等于去重闸失效，同一资源可并发起两个活动任务。进入时抓 ``sqlite_master`` 里的原始 DDL、
    退出时把缺失的原样回放，避免在迁移里复制一份会随后续迁移漂移的索引定义。
    PostgreSQL 走原生 ALTER 不重建表，此处为空操作。

    回放以「进入前存在、退出时缺失」判定，分不清静默丢失与有意删除，所以迁移自身要删的索引须
    放在本上下文之外（``DROP INDEX`` 在两种方言上都是原生操作，不触发重建）。
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return

    captured = _index_ddl(bind, table_name)
    yield
    surviving = _index_ddl(bind, table_name)
    for name, ddl in captured.items():
        if name not in surviving:
            op.execute(ddl)


def drop_index_if_exists(bind: Connection, table_name: str, index_name: str) -> None:
    """跨方言安全地删除索引：存在才删，不存在静默跳过。

    SQLite / PostgreSQL 支持 ``DROP INDEX IF EXISTS``；MySQL 5.7 两者皆无——既没有
    IF EXISTS 语法，``ALTER TABLE ... DROP INDEX`` 在索引缺失时直接报错，因此先查
    information_schema 判存再删。三个方言都不触发 SQLite 的表重建路径。
    """
    dialect = bind.dialect.name
    if dialect in ("sqlite", "postgresql"):
        bind.execute(sa.text(f'DROP INDEX IF EXISTS "{index_name}"'))
        return
    if dialect != "mysql":
        raise ValueError(f"不支持的方言：{dialect}")
    existing = sa.inspect(bind).get_indexes(table_name)
    if any(index["name"] == index_name for index in existing):
        bind.execute(sa.text(f"ALTER TABLE `{table_name}` DROP INDEX `{index_name}`"))
