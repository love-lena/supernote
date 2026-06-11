"""Device-protocol schedule (To-Do) endpoints at /api/file/schedule/*.

The Manta's To-Do sync calls these flat POST endpoints (captured from device
traffic + mirrored in models/schedule.py). It generates its own string IDs and
upserts tasks via POST /task.
"""

import pytest
from aiohttp.test_utils import TestClient

from supernote.models.schedule import AddScheduleTaskDTO
from supernote.server.db.models.schedule import ScheduleTaskGroupDO
from supernote.server.services.schedule import ScheduleService

# A real POST /api/file/schedule/task body captured from the device.
DEVICE_TASK_BODY = {
    "taskId": "33db3d7f80d31330c170f49d56c88182",
    "title": "HEY CLAUDE!!!",
    "status": "needsAction",
    "isDeleted": "N",
    "isReminderOn": "N",
    "completedTime": 1781205746360,
    "dueTime": 0,
    "lastModified": 1781205746360,
    "links": "eyJhcHBOYW1lIjoibm90ZSIsImZpbGVJZCI6IkYxIn0=",
    "sort": 0,
    "sortCompleted": 0,
    "planerSort": 0,
    "sortTime": 1781205746360,
    "planerSortTime": 1781205746360,
}


@pytest.fixture
async def user_id(client: TestClient, auth_headers: dict[str, str]) -> int:
    """Resolve the test user's numeric id (user is created by auth_headers)."""
    return await client.app["user_service"].get_user_id("test@example.com")


async def test_group_all_returns_envelope_with_groups(
    client: TestClient, auth_headers: dict[str, str], user_id: int
) -> None:
    """POST group/all -> ScheduleTaskGroupVO listing the user's groups."""
    sm = client.app["session_manager"]
    async with sm.session() as session:
        session.add(
            ScheduleTaskGroupDO(task_list_id="grp-1", user_id=user_id, title="Work")
        )
        await session.commit()

    resp = await client.post(
        "/api/file/schedule/group/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert [(g["taskListId"], g["title"]) for g in body["scheduleTaskGroup"]] == [
        ("grp-1", "Work")
    ]


async def test_task_all_returns_envelope_with_tasks(
    client: TestClient, auth_headers: dict[str, str], user_id: int
) -> None:
    """POST task/all -> ScheduleTaskAllVO with tasks + a nextSyncToken."""
    service: ScheduleService = client.app["schedule_service"]
    await service.upsert_task(
        user_id, AddScheduleTaskDTO(task_id="t-1", title="Ship it")
    )

    resp = await client.post(
        "/api/file/schedule/task/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["nextSyncToken"] is not None
    assert [(t["taskId"], t["title"]) for t in body["scheduleTask"]] == [
        ("t-1", "Ship it")
    ]


async def test_device_create_task_upserts_and_round_trips(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """POST /task accepts the device's string taskId + rich fields, persists
    them, and returns them unchanged via task/all (the sync round-trip)."""
    resp = await client.post(
        "/api/file/schedule/task", json=DEVICE_TASK_BODY, headers=auth_headers
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["taskId"] == DEVICE_TASK_BODY["taskId"]

    resp2 = await client.post(
        "/api/file/schedule/task/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    tasks = (await resp2.json())["scheduleTask"]
    match = [t for t in tasks if t["taskId"] == DEVICE_TASK_BODY["taskId"]]
    assert len(match) == 1
    t = match[0]
    assert t["title"] == "HEY CLAUDE!!!"
    assert t["links"] == DEVICE_TASK_BODY["links"]
    assert t["status"] == "needsAction"


async def test_device_create_task_is_idempotent_on_id(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Re-posting the same taskId updates in place (no duplicate row)."""
    await client.post(
        "/api/file/schedule/task", json=DEVICE_TASK_BODY, headers=auth_headers
    )
    edited = {**DEVICE_TASK_BODY, "title": "edited"}
    await client.post("/api/file/schedule/task", json=edited, headers=auth_headers)

    resp = await client.post(
        "/api/file/schedule/task/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    tasks = (await resp.json())["scheduleTask"]
    match = [t for t in tasks if t["taskId"] == DEVICE_TASK_BODY["taskId"]]
    assert len(match) == 1
    assert match[0]["title"] == "edited"


async def test_task_list_batch_completes_and_deletes(
    client: TestClient, auth_headers: dict[str, str], user_id: int
) -> None:
    """PUT /task/list applies a batch: status changes stay visible, isDeleted=Y
    tombstones drop out of task/all. (The device routes edit/complete/delete
    through this one endpoint.)"""
    service: ScheduleService = client.app["schedule_service"]
    await service.upsert_task(user_id, AddScheduleTaskDTO(task_id="a", title="A"))
    await service.upsert_task(user_id, AddScheduleTaskDTO(task_id="b", title="B"))

    body = {
        "updateScheduleTaskList": [
            {
                "taskId": "a",
                "title": "A",
                "lastModified": 222,
                "status": "completed",
                "isDeleted": "N",
            },
            {"taskId": "b", "title": "B", "lastModified": 222, "isDeleted": "Y"},
        ]
    }
    resp = await client.put(
        "/api/file/schedule/task/list", json=body, headers=auth_headers
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["success"] is True

    r2 = await client.post(
        "/api/file/schedule/task/all",
        json={"maxResults": "200"},
        headers=auth_headers,
    )
    by_id = {t["taskId"]: t for t in (await r2.json())["scheduleTask"]}
    assert by_id["a"]["status"] == "completed"  # completed task stays visible
    assert "b" not in by_id  # deleted task tombstoned out
