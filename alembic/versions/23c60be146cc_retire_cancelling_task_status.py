"""retire the cancelling task status

Revision ID: 23c60be146cc
Revises: c5a819b247de
Create Date: 2026-09-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from lib.db.migration_helpers import drop_index_if_exists

revision: str = "23c60be146cc"
down_revision: str | Sequence[str] | None = "c5a819b247de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_dedup_index() -> None:
    drop_index_if_exists(op.get_bind(), "tasks", "idx_tasks_dedupe_active")


def _create_dedup_index(active_statuses: str) -> None:
    # MySQL 无表达式/部分索引：跳过，链尾 9d3f2a7b6c51 补等价语义。
    if op.get_bind().dialect.name == "mysql":
        return
    where = sa.text(f"status IN ({active_statuses})")
    op.create_index(
        "idx_tasks_dedupe_active",
        "tasks",
        [
            "project_name",
            "user_id",
            "task_type",
            "resource_id",
            sa.text("COALESCE(script_file, '')"),
            sa.text("COALESCE(resource_type, '')"),
        ],
        unique=True,
        postgresql_where=where,
        sqlite_where=where,
    )


def _cascade_cancel_doomed_mysql(bind) -> None:
    """MySQL 5.7 无 CTE，且临时表在同一条语句内不可二次引用：把任务行读进内存算依赖闭包。

    语义与上方的递归 CTE 相同：从 cancelling 行出发，沿 dependency_task_id 把排队中的
    下游（含孙辈）逐层收进 doomed，直到不再增长，再统一落 cancelled。
    单机任务表行数量级很小，内存展开无压力。
    """
    rows = bind.execute(sa.text("SELECT task_id, dependency_task_id, status FROM tasks")).fetchall()
    queued_children: dict[str, list[str]] = {}
    for task_id, dependency_task_id, status in rows:
        if status == "queued" and dependency_task_id:
            queued_children.setdefault(dependency_task_id, []).append(task_id)

    doomed = {row[0] for row in rows if row[2] == "cancelling"}
    frontier = list(doomed)
    while frontier:
        frontier = [child for parent in frontier for child in queued_children.get(parent, ()) if child not in doomed]
        doomed.update(frontier)

    if doomed:
        bind.execute(
            sa.text(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    cancelled_by = 'cascade',
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'queued' AND task_id IN :doomed_ids
                """
            ).bindparams(sa.bindparam("doomed_ids", expanding=True)),
            {"doomed_ids": sorted(doomed)},
        )


def upgrade() -> None:
    # cancelling 父行的排队下游（含孙辈）按级联取消落终态：下游只在上游 succeeded 后被认领，
    # 不级联会永远停在 queued 并占住去重槽位。须在父行离开 cancelling 之前执行。
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        _cascade_cancel_doomed_mysql(bind)
    else:
        op.execute(
            sa.text(
                """
                WITH RECURSIVE doomed(task_id) AS (
                    SELECT task_id FROM tasks WHERE status = 'cancelling'
                    UNION
                    SELECT tasks.task_id
                    FROM tasks JOIN doomed ON tasks.dependency_task_id = doomed.task_id
                    WHERE tasks.status = 'queued'
                )
                UPDATE tasks
                SET status = 'cancelled',
                    cancelled_by = 'cascade',
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'queued' AND task_id IN (SELECT task_id FROM doomed)
                """
            )
        )
    # 存量 cancelling 行已无人接手，收敛为 cancelled；cancelled_by 在进入 cancelling 时已写入。
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET status = 'cancelled',
                cancelled_by = COALESCE(cancelled_by, 'user'),
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'cancelling'
            """
        )
    )
    _drop_dedup_index()
    _create_dedup_index("'queued', 'running'")


def downgrade() -> None:
    _drop_dedup_index()
    _create_dedup_index("'queued', 'running', 'cancelling'")
