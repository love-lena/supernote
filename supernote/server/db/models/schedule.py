import time
from typing import Optional

from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from supernote.server.db.base import Base


def _now_ms() -> int:
    return int(time.time() * 1000)


class ScheduleTaskGroupDO(Base):
    """Groups of tasks (e.g., 'Inbox', 'Work', 'Personal').

    IDs are device-generated strings (hex/UUID), matching the device protocol
    (`/api/file/schedule/*`). Deletions are soft (tombstones) so they sync.
    """

    __tablename__ = "t_schedule_task_group"

    task_list_id: Mapped[str] = mapped_column(String, primary_key=True)
    """Device-provided unique ID (hex string)."""

    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    """User ID."""

    title: Mapped[str] = mapped_column(String, nullable=False)
    """Title."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    """Tombstone flag for sync-visible deletion."""

    last_modified: Mapped[int] = mapped_column(BigInteger, default=_now_ms)
    """Last-modified time in epoch milliseconds."""

    create_time: Mapped[int] = mapped_column(BigInteger, default=_now_ms)
    """Creation time in epoch milliseconds."""


class ScheduleTaskDO(Base):
    """Individual Tasks (To-Do items).

    IDs are device-generated strings. `task_list_id` is nullable: note-linked
    todos arrive with no group. Deletions are soft (tombstones).
    """

    __tablename__ = "t_schedule_task"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    """Device-provided unique ID (hex string)."""

    task_list_id: Mapped[Optional[str]] = mapped_column(
        String, index=True, nullable=True
    )
    """Owning group ID, or NULL for ungrouped (note-linked) todos."""

    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    """User ID."""

    title: Mapped[str] = mapped_column(String, nullable=False)
    """A summary of the task."""

    detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """The task description."""

    status: Mapped[str] = mapped_column(String, default="needsAction")
    """Task status: 'needsAction' or 'completed'."""

    importance: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """Task importance level."""

    due_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    """Due time in epoch milliseconds."""

    completed_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    """Completed time in epoch milliseconds."""

    recurrence: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """The recurrence rule (RRULE)."""

    recurrence_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """Recurrence instance ID."""

    is_reminder_on: Mapped[bool] = mapped_column(Boolean, default=False)
    """Whether the task has a reminder."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    """Tombstone flag for sync-visible deletion."""

    links: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """Base64 JSON link to a document (appName/fileId/path/page/pageId)."""

    sort: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sort_completed: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    planer_sort: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    all_sort: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    all_sort_completed: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sort_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    planer_sort_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    all_sort_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    """Planner sort-order indices and their last-changed timestamps (ms)."""

    last_modified: Mapped[int] = mapped_column(BigInteger, default=_now_ms)
    """Last-modified time in epoch milliseconds (device `lastModified`)."""

    create_time: Mapped[int] = mapped_column(BigInteger, default=_now_ms)
    """Creation time in epoch milliseconds."""

    update_time: Mapped[int] = mapped_column(BigInteger, default=_now_ms)
    """Server-side update time in epoch milliseconds."""
