"""Device-protocol schedule (To-Do) endpoints.

The Manta's To-Do sync calls flat POST endpoints under `/api/file/schedule/*`
(mirrored in `supernote.models.schedule`). These are the endpoints the device
actually uses; the previous invented REST API (`/api/schedule/*`) has been
removed.

Implemented so far: the read path (`group/all`, `task/all`) and task create/
update via `POST /task` (the device upserts a task by its own string ID). The
remaining write surface (group CRUD, delete, sort) and incremental-sync
semantics (`nextSyncToken` filtering) are added as on-device traffic exercises
them.
"""

import logging
import time

from aiohttp import web

from supernote.models.base import BooleanEnum, create_error_response
from supernote.models.schedule import (
    AddScheduleTaskDTO,
    AddScheduleTaskVO,
    ScheduleTaskAllVO,
    ScheduleTaskGroupItem,
    ScheduleTaskGroupVO,
    ScheduleTaskInfo,
)
from supernote.server.db.models.schedule import ScheduleTaskDO
from supernote.server.services.schedule import ScheduleService

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _bool_enum(value: bool) -> BooleanEnum:
    return BooleanEnum.YES if value else BooleanEnum.NO


def _task_to_info(t: ScheduleTaskDO) -> ScheduleTaskInfo:
    return ScheduleTaskInfo(
        task_id=str(t.task_id),
        task_list_id=str(t.task_list_id) if t.task_list_id is not None else None,
        title=t.title,
        detail=t.detail,
        status=t.status,
        importance=t.importance,
        due_time=t.due_time,
        completed_time=t.completed_time,
        recurrence=t.recurrence,
        is_reminder_on=_bool_enum(t.is_reminder_on),
        is_deleted=_bool_enum(t.is_deleted),
        links=t.links,
        sort=t.sort,
        sort_completed=t.sort_completed,
        planer_sort=t.planer_sort,
        all_sort=t.all_sort,
        all_sort_completed=t.all_sort_completed,
        sort_time=t.sort_time,
        planer_sort_time=t.planer_sort_time,
        all_sort_time=t.all_sort_time,
        last_modified=t.last_modified,
    )


@routes.post("/api/file/schedule/group/all")
async def list_group_all(request: web.Request) -> web.Response:
    # Request: ScheduleTaskGroupDTO {maxResults, pageToken} (paging not yet used)
    # Response: ScheduleTaskGroupVO
    user = request["user"]
    schedule_service: ScheduleService = request.app["schedule_service"]
    user_id = await request.app["user_service"].get_user_id(user)

    groups = await schedule_service.list_groups(user_id)
    items = [
        ScheduleTaskGroupItem(
            task_list_id=str(g.task_list_id),
            user_id=g.user_id,
            title=g.title,
            is_deleted=_bool_enum(g.is_deleted),
            create_time=g.create_time,
            last_modified=g.last_modified,
        )
        for g in groups
    ]
    return web.json_response(
        ScheduleTaskGroupVO(success=True, schedule_task_group=items).to_dict()
    )


@routes.post("/api/file/schedule/task/all")
async def list_task_all(request: web.Request) -> web.Response:
    # Request: ScheduleTaskDTO {maxResults, nextPageTokens, nextSyncToken}
    #          (incremental sync not yet used; returns all non-deleted tasks)
    # Response: ScheduleTaskAllVO
    user = request["user"]
    schedule_service: ScheduleService = request.app["schedule_service"]
    user_id = await request.app["user_service"].get_user_id(user)

    tasks = await schedule_service.list_tasks(user_id)
    infos = [_task_to_info(t) for t in tasks]
    next_sync_token = max(
        (t.last_modified for t in tasks), default=int(time.time() * 1000)
    )
    return web.json_response(
        ScheduleTaskAllVO(
            success=True, next_sync_token=next_sync_token, schedule_task=infos
        ).to_dict()
    )


@routes.post("/api/file/schedule/task")
async def create_task(request: web.Request) -> web.Response:
    # Request: AddScheduleTaskDTO (device upserts by its own string taskId)
    # Response: AddScheduleTaskVO {taskId}
    user = request["user"]
    try:
        dto = AddScheduleTaskDTO.from_dict(await request.json())
    except Exception as e:
        return web.json_response(
            create_error_response(f"Invalid request: {e}").to_dict(), status=400
        )

    schedule_service: ScheduleService = request.app["schedule_service"]
    user_id = await request.app["user_service"].get_user_id(user)

    try:
        task = await schedule_service.upsert_task(user_id, dto)
    except ValueError as e:
        return web.json_response(create_error_response(str(e)).to_dict(), status=400)

    return web.json_response(
        AddScheduleTaskVO(success=True, task_id=str(task.task_id)).to_dict()
    )
