import logging
import time
import uuid

from sqlalchemy import select

from supernote.models.base import BooleanEnum
from supernote.models.schedule import AddScheduleTaskDTO, UpdateScheduleTaskDTO
from supernote.server.db.models.schedule import ScheduleTaskDO, ScheduleTaskGroupDO
from supernote.server.db.session import DatabaseSessionManager

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_DETAIL_LENGTH = 1 * 1024 * 1024  # 1MB


def _now_ms() -> int:
    return int(time.time() * 1000)


class ScheduleService:
    """To-Do (schedule) storage for the device protocol.

    Groups and tasks carry device-generated string IDs and are upserted by ID
    (single writer per ID — the personal device). Deletions are soft.
    """

    def __init__(self, session_manager: DatabaseSessionManager):
        """Initialize the schedule service."""
        self.session_manager = session_manager

    async def list_groups(self, user_id: int) -> list[ScheduleTaskGroupDO]:
        """List a user's non-deleted task groups."""
        async with self.session_manager.session() as session:
            stmt = (
                select(ScheduleTaskGroupDO)
                .where(
                    ScheduleTaskGroupDO.user_id == user_id,
                    ScheduleTaskGroupDO.is_deleted.is_(False),
                )
                .order_by(ScheduleTaskGroupDO.create_time.desc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_tasks(self, user_id: int) -> list[ScheduleTaskDO]:
        """List a user's non-deleted tasks."""
        async with self.session_manager.session() as session:
            stmt = (
                select(ScheduleTaskDO)
                .where(
                    ScheduleTaskDO.user_id == user_id,
                    ScheduleTaskDO.is_deleted.is_(False),
                )
                .order_by(ScheduleTaskDO.create_time.desc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert_task(
        self, user_id: int, dto: AddScheduleTaskDTO | UpdateScheduleTaskDTO
    ) -> ScheduleTaskDO:
        """Create or update a task by its device-provided ID.

        The device is the single writer per task ID, so a matching ID updates
        the existing row (last-write-wins) and a new ID inserts. Accepts both
        the create DTO (POST /task) and the batch-update DTO (PUT /task/list),
        which share the same task fields. `isDeleted=Y` soft-deletes.
        """
        if dto.title is not None and len(dto.title) > MAX_TITLE_LENGTH:
            raise ValueError("Title is too long")
        if dto.detail is not None and len(dto.detail) > MAX_DETAIL_LENGTH:
            raise ValueError("Detail is too long")

        task_id = dto.task_id or uuid.uuid4().hex
        now = _now_ms()
        async with self.session_manager.session() as session:
            task = await session.get(ScheduleTaskDO, task_id)
            if task is None or task.user_id != user_id:
                task = ScheduleTaskDO(
                    task_id=task_id, user_id=user_id, create_time=now, title=""
                )
                session.add(task)

            task.task_list_id = dto.task_list_id
            task.title = dto.title or task.title or ""
            task.detail = dto.detail
            task.status = dto.status or "needsAction"
            task.importance = dto.importance
            task.due_time = dto.due_time
            task.completed_time = dto.completed_time
            task.recurrence = dto.recurrence
            task.recurrence_id = dto.recurrence_id
            task.is_reminder_on = dto.is_reminder_on == BooleanEnum.YES
            task.is_deleted = dto.is_deleted == BooleanEnum.YES
            task.links = dto.links
            task.sort = dto.sort
            task.sort_completed = dto.sort_completed
            task.planer_sort = dto.planer_sort
            task.all_sort = dto.all_sort
            task.all_sort_completed = dto.all_sort_completed
            task.sort_time = dto.sort_time
            task.planer_sort_time = dto.planer_sort_time
            task.all_sort_time = dto.all_sort_time
            task.last_modified = dto.last_modified or now
            task.update_time = now

            await session.commit()
            await session.refresh(task)
            return task
