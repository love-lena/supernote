"""Device-protocol schedule (To-Do) endpoints.

The Manta's To-Do sync calls flat POST endpoints under `/api/file/schedule/*`
(mirrored in `supernote.models.schedule`), NOT the invented REST API in
`routes/schedule.py` (`/api/schedule/*`), which the device never calls.

Round 1 implements the read path the device hits first (`group/all`,
`task/all`) so it gets past those calls. The write path (group/task
create/update/delete, sort) and incremental-sync semantics (tombstones,
nextSyncToken) follow once on-device traffic confirms the push sequence.
"""

import logging
import time

from aiohttp import web

from supernote.models.base import BooleanEnum
from supernote.models.schedule import (
    ScheduleTaskAllVO,
    ScheduleTaskGroupItem,
    ScheduleTaskGroupVO,
    ScheduleTaskInfo,
)
from supernote.server.services.schedule import ScheduleService

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


@routes.post("/api/file/schedule/group/all")
async def list_group_all(request: web.Request) -> web.Response:
    # Endpoint: POST /api/file/schedule/group/all
    # Request: ScheduleTaskGroupDTO {maxResults, pageToken} (ignored in round 1)
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
            create_time=g.create_time,
            last_modified=g.create_time,
        )
        for g in groups
    ]
    return web.json_response(
        ScheduleTaskGroupVO(success=True, schedule_task_group=items).to_dict()
    )


@routes.post("/api/file/schedule/task/all")
async def list_task_all(request: web.Request) -> web.Response:
    # Endpoint: POST /api/file/schedule/task/all
    # Request: ScheduleTaskDTO {maxResults, nextPageTokens, nextSyncToken} (ignored in round 1)
    # Response: ScheduleTaskAllVO
    user = request["user"]
    schedule_service: ScheduleService = request.app["schedule_service"]
    user_id = await request.app["user_service"].get_user_id(user)

    tasks = await schedule_service.list_tasks(user_id)
    infos = [
        ScheduleTaskInfo(
            task_id=str(t.task_id),
            task_list_id=str(t.task_list_id),
            title=t.title,
            detail=t.detail,
            status=t.status,
            importance=t.importance,
            due_time=t.due_time,
            completed_time=t.completed_time,
            recurrence=t.recurrence,
            is_reminder_on=(BooleanEnum.YES if t.is_reminder_on else BooleanEnum.NO),
            last_modified=t.update_time,
        )
        for t in tasks
    ]
    next_sync_token = max(
        (t.update_time for t in tasks), default=int(time.time() * 1000)
    )
    return web.json_response(
        ScheduleTaskAllVO(
            success=True, next_sync_token=next_sync_token, schedule_task=infos
        ).to_dict()
    )
