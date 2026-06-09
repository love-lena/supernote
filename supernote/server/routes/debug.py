"""Debug-only routes for reverse-engineering the device push protocol.

Registered ONLY when ``SUPERNOTE_DEBUG_EMIT=1`` (see app.py). Lets us emit an
arbitrary Socket.IO event + payload to a room and watch how the device reacts,
so we can reverse the exact ``finishFolderMessage`` payload the device acts on
without rebuilding the image per attempt. Never enable in production.
"""

import json
import logging

from aiohttp import web

from .decorators import public_route

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


@routes.post("/api/debug/emit")
@public_route
async def debug_emit(request: web.Request) -> web.Response:
    """Emit a Socket.IO event to a room.

    Body: {"event": str, "payload": any, "room": "user:1", "raw": bool}
    ``raw=true`` sends the payload as a JSON *string* instead of an object, to
    test whether the device's handler expects a stringified body.
    """
    sio = request.app["sio"]
    body = await request.json()
    event = body.get("event", "finishFolderMessage")
    payload = body.get("payload", {})
    room = body.get("room", "user:1")
    if body.get("raw") and isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    logger.info("debug_emit event=%r room=%r payload=%r", event, room, payload)
    await sio.emit(event, payload, room=room)
    return web.json_response({"emitted": event, "room": room, "payload": payload})


@routes.post("/api/debug/resync")
@public_route
async def debug_resync(request: web.Request) -> web.Response:
    """Queue a resync for a user's device(s).

    Body: {"user_id": 1}. Marks the user pending; the next ratta_ping goes
    unanswered, so the device times out, reconnects, and runs a full sync.
    """
    body = await request.json()
    user_id = int(body.get("user_id", 1))
    request_resync = request.app["sio_request_resync"]
    result = await request_resync(user_id)
    return web.json_response(result)
