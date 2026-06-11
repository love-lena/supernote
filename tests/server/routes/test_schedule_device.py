"""Device-protocol schedule (To-Do) endpoints at /api/file/schedule/*.

The Manta's To-Do sync calls these flat POST endpoints (captured from device
traffic + mirrored in models/schedule.py), NOT the invented /api/schedule/*
REST API. These tests assert the device's first read calls return a valid
envelope so it can proceed past them.
"""

import pytest
from aiohttp.test_utils import TestClient

from supernote.server.services.schedule import ScheduleService


@pytest.fixture
async def user_id(client: TestClient, auth_headers: dict[str, str]) -> int:
    """Resolve the test user's numeric id (user is created by auth_headers)."""
    return await client.app["user_service"].get_user_id("test@example.com")


async def test_group_all_returns_envelope_with_groups(
    client: TestClient, auth_headers: dict[str, str], user_id: int
) -> None:
    """POST /api/file/schedule/group/all -> ScheduleTaskGroupVO with the
    user's groups under `scheduleTaskGroup`."""
    service: ScheduleService = client.app["schedule_service"]
    group = await service.create_group(user_id, "Work")

    resp = await client.post(
        "/api/file/schedule/group/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    groups = body["scheduleTaskGroup"]
    assert [(g["taskListId"], g["title"]) for g in groups] == [
        (str(group.task_list_id), "Work")
    ]


async def test_task_all_returns_envelope_with_tasks(
    client: TestClient, auth_headers: dict[str, str], user_id: int
) -> None:
    """POST /api/file/schedule/task/all -> ScheduleTaskAllVO with the user's
    tasks under `scheduleTask` plus a nextSyncToken."""
    service: ScheduleService = client.app["schedule_service"]
    group = await service.create_group(user_id, "Work")
    task = await service.create_task(user_id, group.task_list_id, title="Ship it")

    resp = await client.post(
        "/api/file/schedule/task/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["nextSyncToken"] is not None
    tasks = body["scheduleTask"]
    assert [(t["taskId"], t["title"]) for t in tasks] == [
        (str(task.task_id), "Ship it")
    ]
