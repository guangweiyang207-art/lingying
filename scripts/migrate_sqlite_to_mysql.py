"""一次性把 SQLite 数据搬进 MySQL（切换存储后端用）。

前置条件：
- 目标 MySQL 库已由 ``alembic upgrade head`` 建好结构（本脚本只搬数据，不动 schema）。
- 源 SQLite 库不再被应用写入（后端已停止）。

用法（仓库根目录，目标取环境变量 DATABASE_URL，异步驱动自动换同步）：

    $env:DATABASE_URL = "mysql+aiomysql://user:pass@host:3306/arcreel?charset=utf8mb4"
    uv run python scripts/migrate_sqlite_to_mysql.py --source "C:\\path\\to\\arcreel.db"

做法：
- SQLite 侧用原生 ``SELECT *`` 取原始值（绕开任何方言 result processor），再按 MySQL
  反射列型在 Python 层显式转换：DATETIME 解析 ISO 文本、JSON 反序列化、BOOLEAN 转 bool。
- 空串时间戳兜底与链尾迁移 9d3f2a7b6c51 同规则：可空列 → NULL，非空列 → 纪元起点。
- ``SET FOREIGN_KEY_CHECKS=0`` 包裹单事务装载，装完恢复；显式 id 插入后把
  AUTO_INCREMENT 抬到 max+1。
- MySQL 生成列（唯一约束的 MD5 等价物）由数据库自动计算，INSERT 自动排除。
- 装载前检测超过 VARCHAR 声明长度的字符串数据（严格模式会整批拒绝），逐表校验行数。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import sqlalchemy as sa

# MySQL 方言编译钩子（DATETIME(6)/String(255)/DOUBLE 等）由 lib.db.engine 导入
# lib.db.mysql_compat 时注册，本条 import 即触发该链路。
from lib.db.engine import get_database_url

BATCH_SIZE = 500
#: 非空时间戳列的空串历史脏数据回填纪元起点（与 9d3f2a7b6c51 一致）。
_EPOCH = datetime(1970, 1, 1)
#: SQLite 内部表（AUTOINCREMENT 计数器等），reflect 不会收录，SELECT 时手动排除。
_SQLITE_INTERNAL_PREFIX = "sqlite_"


def _sync_url(url: str) -> str:
    """异步驱动（aiomysql/aiosqlite）换成同步驱动：一次性脚本无需事件循环。"""
    return url.replace("+aiomysql", "+pymysql").replace("+aiosqlite", "")


def _convert(value: object, mysql_col: sa.Column) -> object:
    """按目标列类型把 SQLite 原生值转成 MySQL 可接受的 Python 值。"""
    if value is None:
        return None
    col_type = mysql_col.type
    if isinstance(col_type, sa.DateTime):
        if value == "":
            return None if mysql_col.nullable else _EPOCH
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if isinstance(col_type, sa.JSON):
        return json.loads(value) if isinstance(value, str) else value
    if isinstance(col_type, sa.Boolean):
        return bool(value)
    return value


def _length_violations(rows: list[dict], mysql_table: sa.Table) -> list[str]:
    """超过 VARCHAR 声明长度的数据（MySQL 严格模式会整批拒绝），每列报一处。"""
    problems: list[str] = []
    for col in mysql_table.columns:
        length = getattr(col.type, "length", None)
        if length is None or not isinstance(col.type, sa.String) or col.computed is not None:
            continue
        for row in rows:
            value = row.get(col.name)
            if isinstance(value, str) and len(value) > length:
                problems.append(
                    f"{mysql_table.name}.{col.name}: 长度 {len(value)} > VARCHAR({length})，"
                    f"示例前 80 字符：{value[:80]!r}"
                )
                break
    return problems


def migrate(source_path: Path, target_url: str) -> int:
    sqlite_engine = sa.create_engine(f"sqlite:///{source_path}")
    mysql_engine = sa.create_engine(_sync_url(target_url))

    source_meta = sa.MetaData()
    source_meta.reflect(bind=sqlite_engine)
    target_meta = sa.MetaData()
    target_meta.reflect(bind=mysql_engine)

    source_names = sorted(name for name in source_meta.tables if not name.startswith(_SQLITE_INTERNAL_PREFIX))
    missing = sorted(set(source_names) - set(target_meta.tables) - {"alembic_version"})
    if missing:
        raise SystemExit(f"MySQL 侧缺表（先在目标库跑 alembic upgrade head）：{missing}")
    source_names = [name for name in source_names if name != "alembic_version"]

    all_problems: list[str] = []
    summary: list[tuple[str, int, int]] = []
    staged: list[tuple[str, sa.Table, list[dict], int]] = []

    # 第一遍：读源 + 长度校验。有超长数据时整体中止（严格模式下 MySQL 会整批拒绝，
    # 与其装一半回滚，不如先给出完整清单让人决策）。
    with sqlite_engine.connect() as source_conn:
        for name in source_names:
            mysql_table = target_meta.tables[name]
            loadable = {
                col.name: col
                for col in mysql_table.columns
                if col.computed is None and col.name in source_meta.tables[name].columns
            }
            raw = source_conn.exec_driver_sql(f'SELECT * FROM "{name}"')
            keys = list(raw.keys())
            raw_rows = raw.fetchall()
            rows = [
                {key: _convert(value, loadable[key]) for key, value in zip(keys, row, strict=True) if key in loadable}
                for row in raw_rows
            ]
            all_problems.extend(_length_violations(rows, mysql_table))
            staged.append((name, mysql_table, rows, len(raw_rows)))
    if all_problems:
        print("检测到超长数据（超过 VARCHAR 声明长度，未装载）：")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1

    # 第二遍：单事务装载。先清空目标表（建库迁移可能已写入种子数据，如
    # 8b1e8a1290ca 从旧 system_setting 迁出的凭证行），保证脚本可幂等重跑。
    with mysql_engine.begin() as target_conn:
        target_conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        try:
            for name, _mysql_table, _rows, _source_count in staged:
                target_conn.execute(sa.text(f"DELETE FROM `{name}`"))
            for name, mysql_table, rows, source_count in staged:
                inserted = 0
                for start in range(0, len(rows), BATCH_SIZE):
                    batch = rows[start : start + BATCH_SIZE]
                    if batch:
                        target_conn.execute(mysql_table.insert(), batch)
                        inserted += len(batch)

                auto_col = mysql_table.autoincrement_column
                if auto_col is not None:
                    max_id = target_conn.execute(sa.select(sa.func.max(auto_col))).scalar()
                    if max_id is not None:
                        target_conn.execute(sa.text(f"ALTER TABLE `{name}` AUTO_INCREMENT = {int(max_id) + 1}"))
                summary.append((name, source_count, inserted))
        finally:
            target_conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")

    # 逐表行数校验（含 alembic_version 之外的全部源表）。
    mismatches: list[str] = []
    with mysql_engine.connect() as verify_conn:
        for name, source_count, _inserted in summary:
            target_count = verify_conn.execute(sa.text(f"SELECT COUNT(*) FROM `{name}`")).scalar_one()
            if target_count != source_count:
                mismatches.append(f"{name}: 源 {source_count} 行，MySQL {target_count} 行")

    name_width = max(len(name) for name, _, _ in summary) if summary else 0
    print("表名".ljust(name_width), "  源行数  装载行数")
    for name, source_count, inserted in summary:
        print(name.ljust(name_width), f"{source_count:>8}{inserted:>10}")

    if all_problems:
        print("\n检测到超长数据（未装载，严格模式下 MySQL 会拒绝）：")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1
    if mismatches:
        print("\n行数校验失败：")
        for mismatch in mismatches:
            print(f"  - {mismatch}")
        return 1
    print(f"\n完成：{len(summary)} 张表全部装载并校验一致。")
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    default_source = Path.home() / "Desktop" / "navigete" / "arcreel.db"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source, help="源 SQLite 文件路径")
    parser.add_argument(
        "--target-url",
        default=None,
        help="目标 MySQL URL；缺省读环境变量 DATABASE_URL",
    )
    args = parser.parse_args()

    target_url = args.target_url or get_database_url()
    if target_url.startswith("sqlite"):
        raise SystemExit("目标不是 MySQL：请设置 DATABASE_URL 为 mysql+aiomysql://... 后重跑")

    source: Path = args.source
    if not source.exists():
        raise SystemExit(f"源库不存在：{source}")

    print(f"源库：{source}")
    print(f"目标：{_sync_url(target_url).split('@')[-1]}")
    return migrate(source, target_url)


if __name__ == "__main__":
    raise SystemExit(main())
