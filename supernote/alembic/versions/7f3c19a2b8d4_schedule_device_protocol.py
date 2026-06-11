"""Schedule tables: device-protocol shape (string IDs, tombstones, sort/links)

Revision ID: 7f3c19a2b8d4
Revises: 0543a383957b
Create Date: 2026-06-11

The original schedule tables used numeric PKs, but the Manta generates its own
string (hex) IDs for to-do groups/tasks and pushes rich fields (soft-delete
tombstones, planner sort order, base64 note links). The tables are empty
(To-Do sync never worked), so we drop and recreate them with the device shape
rather than do fragile SQLite column-type ALTERs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7f3c19a2b8d4"
down_revision: Union[str, Sequence[str], None] = "0543a383957b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f("ix_t_schedule_task_task_list_id"), table_name="t_schedule_task")
    op.drop_index(op.f("ix_t_schedule_task_user_id"), table_name="t_schedule_task")
    op.drop_table("t_schedule_task")
    op.drop_index(
        op.f("ix_t_schedule_task_group_user_id"), table_name="t_schedule_task_group"
    )
    op.drop_table("t_schedule_task_group")

    op.create_table(
        "t_schedule_task_group",
        sa.Column("task_list_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("last_modified", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("task_list_id"),
    )
    op.create_index(
        op.f("ix_t_schedule_task_group_user_id"),
        "t_schedule_task_group",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "t_schedule_task",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("task_list_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("importance", sa.String(), nullable=True),
        sa.Column("due_time", sa.BigInteger(), nullable=True),
        sa.Column("completed_time", sa.BigInteger(), nullable=True),
        sa.Column("recurrence", sa.String(), nullable=True),
        sa.Column("recurrence_id", sa.String(), nullable=True),
        sa.Column("is_reminder_on", sa.Boolean(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("links", sa.String(), nullable=True),
        sa.Column("sort", sa.BigInteger(), nullable=True),
        sa.Column("sort_completed", sa.BigInteger(), nullable=True),
        sa.Column("planer_sort", sa.BigInteger(), nullable=True),
        sa.Column("all_sort", sa.BigInteger(), nullable=True),
        sa.Column("all_sort_completed", sa.BigInteger(), nullable=True),
        sa.Column("sort_time", sa.BigInteger(), nullable=True),
        sa.Column("planer_sort_time", sa.BigInteger(), nullable=True),
        sa.Column("all_sort_time", sa.BigInteger(), nullable=True),
        sa.Column("last_modified", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.BigInteger(), nullable=False),
        sa.Column("update_time", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        op.f("ix_t_schedule_task_user_id"),
        "t_schedule_task",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_t_schedule_task_task_list_id"),
        "t_schedule_task",
        ["task_list_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema (restore the original numeric-PK tables)."""
    op.drop_index(op.f("ix_t_schedule_task_task_list_id"), table_name="t_schedule_task")
    op.drop_index(op.f("ix_t_schedule_task_user_id"), table_name="t_schedule_task")
    op.drop_table("t_schedule_task")
    op.drop_index(
        op.f("ix_t_schedule_task_group_user_id"), table_name="t_schedule_task_group"
    )
    op.drop_table("t_schedule_task_group")

    op.create_table(
        "t_schedule_task_group",
        sa.Column("task_list_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("create_time", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("task_list_id"),
    )
    op.create_index(
        op.f("ix_t_schedule_task_group_user_id"),
        "t_schedule_task_group",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "t_schedule_task",
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("task_list_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("importance", sa.String(), nullable=True),
        sa.Column("due_time", sa.BigInteger(), nullable=True),
        sa.Column("completed_time", sa.BigInteger(), nullable=True),
        sa.Column("recurrence", sa.String(), nullable=True),
        sa.Column("is_reminder_on", sa.Boolean(), nullable=False),
        sa.Column("create_time", sa.BigInteger(), nullable=False),
        sa.Column("update_time", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        op.f("ix_t_schedule_task_task_list_id"),
        "t_schedule_task",
        ["task_list_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_t_schedule_task_user_id"),
        "t_schedule_task",
        ["user_id"],
        unique=False,
    )
