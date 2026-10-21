"""MySQL 方言的编译期适配。

只通过 ``@compiles`` 挂钩影响 MySQL 的 DDL 渲染，SQLite / PostgreSQL 路径零改动。
import 本模块即完成注册；应用、alembic env 与脚本统一经 ``lib.db.engine`` 取数据库
地址，而 engine 模块顶部 import 本模块，三条路径天然都注册。

适配点（MySQL 5.7.8+ 为基准）：

- ``CREATE TABLE`` 追加 ``ENGINE=InnoDB`` 与 utf8mb4：phpstudy 等托管环境常把服务器
  ``default_storage_engine`` 设为 MyISAM，而 MyISAM 不支持外键与事务，且索引键上限
  只有 1000 字节——本仓的复合主键/外键在 MyISAM 上要么建不出来要么语义缺失。只对
  本应用建出的表强制 InnoDB，不改服务器全局默认，不影响同机其他库。
- 无长度 ``String`` → ``VARCHAR(255)``：通用型 ``String()`` 在 SQLite/PG 是无限长文本，
  MySQL 必须给长度才能建表建索引；255 兼顾 utf8mb4 下 4×255=1020 字节的索引前缀预算
  （InnoDB 单索引键上限 3072 字节，允许 2~3 个此类列组合成索引）。超长文本本就应使用
  ``Text``，本仓模型已是如此。
- ``DateTime`` → ``DATETIME(6)``：MySQL 的 DATETIME 默认秒级精度，会截掉微秒；
  ``timezone=True`` 对 MySQL 无意义（无时区列型），一并按 DATETIME(6) 渲染。
- ``Float`` → ``DOUBLE``：通用 ``Float`` 在 MySQL 渲染成单精度 FLOAT，仅约 7 位有效
  数字；``worker_lease.lease_until`` 存 ``time.time()`` 纪元秒（10 位整数 + 小数），
  FLOAT 会失真，DOUBLE 才保得住。
- ``now()`` → ``CURRENT_TIMESTAMP(6)``：MySQL 要求默认值的小数位宽与列一致，
  DATETIME(6) 列上裸的 ``DEFAULT now()`` 报错 1067，必须带 ``(6)``。
"""

from __future__ import annotations

from sqlalchemy import DateTime, Float, String
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.functions import now as sa_now

#: 无长度 String 在 MySQL 上的定型长度。
UNLENGTHED_STRING_LENGTH = 255


@compiles(sa_now, "mysql")
def _compile_mysql_now(element: sa_now, compiler, **kw) -> str:
    return "CURRENT_TIMESTAMP(6)"


@compiles(CreateTable, "mysql")
def _compile_mysql_create_table(element: CreateTable, compiler, **kw) -> str:
    ddl = compiler.visit_create_table(element, **kw)
    return ddl + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"


@compiles(String, "mysql")
def _compile_mysql_string(element: String, compiler, **kw) -> str:
    if element.length is None:
        return f"VARCHAR({UNLENGTHED_STRING_LENGTH})"
    return compiler.visit_VARCHAR(element, **kw)


@compiles(DateTime, "mysql")
def _compile_mysql_datetime(element: DateTime, compiler, **kw) -> str:
    return "DATETIME(6)"


@compiles(Float, "mysql")
def _compile_mysql_float(element: Float, compiler, **kw) -> str:
    return "DOUBLE"
