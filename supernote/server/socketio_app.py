"""Socket.IO push channel for device auto-sync.

Supernote devices open a Socket.IO connection (Engine.IO protocol v3, websocket
transport) and listen for a ``finishFolderMessage`` event that tells them content
changed and they should run a sync pass. Without this channel the device only
syncs on a manual tap (and otherwise hammers the server with a ~5s reconnect
loop that 401s, because the auth token rides in the query string, not the
``x-access-token`` header the HTTP middleware checks).

Implementing it gives two things:
  1. Near-real-time auto-sync — emit ``finishFolderMessage`` on change and the
     device pulls within seconds.
  2. A programmatic trigger — after an API ``cloud upload`` the same change event
     fires, so a pushed file lands on the device with no tap.

Compatibility note: the device ships an old okhttp Socket.IO v2 client that only
speaks Engine.IO protocol v3 (``EIO=3``). python-engineio 4.x hard-rejects EIO3,
so this is built against python-socketio 4.x / python-engineio 3.x — the
canonical server for that client (see constraints.txt). API differences vs 5.x:
``enter_room`` is synchronous, the connect handler takes no ``auth`` arg, and
there is no ``@sio.on('*')`` catch-all (we enable the engineio/socketio loggers
instead so received packets are visible for trial-and-watch).

The exact ``finishFolderMessage`` payload the official cloud sends is not
documented, so this starts minimal. The payload is built in one place
(:func:`_finish_folder_payload`) to make iteration cheap.
"""

import asyncio
import logging
import time
from typing import Any
from urllib.parse import parse_qs

import socketio
from aiohttp import web

from .events import FileChangedEvent

logger = logging.getLogger(__name__)

# The event name the device subscribes to for "content changed, go sync".
FINISH_FOLDER_EVENT = "finishFolderMessage"


class _Py314AsyncManager(socketio.AsyncManager):
    """Python 3.12+ compatibility shim for python-socketio 4.x.

    The stock ``AsyncManager.emit`` builds a list of bare coroutines and passes
    them to ``asyncio.wait()``. Python 3.12 removed support for that ("Passing
    coroutines is forbidden, use tasks explicitly"), so every room/sid emit
    raises on Python 3.14. This re-implements emit with ``asyncio.gather``, which
    still accepts coroutines. Remove if we ever move off the 4.x line.
    """

    async def emit(
        self,
        event: str,
        data: Any,
        namespace: str,
        room: str | None = None,
        skip_sid: Any = None,
        callback: Any = None,
        **kwargs: Any,
    ) -> None:
        if namespace not in self.rooms or room not in self.rooms[namespace]:
            return
        if not isinstance(skip_sid, list):
            skip_sid = [skip_sid]
        coros = []
        for sid in self.get_participants(namespace, room):
            if sid not in skip_sid:
                ack_id = (
                    self._generate_ack_id(sid, namespace, callback)
                    if callback is not None
                    else None
                )
                coros.append(
                    self.server._emit_internal(sid, event, data, namespace, ack_id)
                )
        if coros:
            await asyncio.gather(*coros)


def _room_for_user(user_id: int) -> str:
    return f"user:{user_id}"


def _finish_folder_payload(event: FileChangedEvent) -> dict[str, Any]:
    """Build the body emitted with ``finishFolderMessage``.

    Kept deliberately small until we observe what the device actually requires.
    The hypothesis is the device re-syncs on receiving the event regardless of
    body; if it needs specific fields, the catch-all logging will reveal it and
    we extend this one function.
    """
    payload: dict[str, Any] = {"type": event.change}
    if event.file_path:
        payload["path"] = event.file_path
    return payload


def create_socketio_server(app: web.Application) -> socketio.AsyncServer:
    """Create the Socket.IO server, wire auth + rooms, and subscribe to changes.

    Returns the server; the caller is responsible for ``sio.attach(app, ...)``.
    """
    sio = socketio.AsyncServer(
        async_mode="aiohttp",
        client_manager=_Py314AsyncManager(),
        cors_allowed_origins="*",
        # Verbose during bring-up so the handshake / ping-pong and any packets
        # the device sends are visible (4.x has no catch-all handler). Quiet
        # these once the channel is proven.
        logger=logger,
        engineio_logger=logging.getLogger("supernote.server.engineio"),
    )

    user_service = app["user_service"]
    event_bus = app["event_bus"]

    # Track live device sockets per user. user_id -> set of sids.
    connected: dict[int, set[str]] = {}
    # sid -> equipment_no, so we can tell which physical device a socket is.
    sid_equip: dict[str, str] = {}
    # sid -> the aiohttp Request behind the socket, so request_resync can abort
    # its TCP transport (hard reset) to force the device to reconnect.
    sid_request: dict[str, Any] = {}
    # user_id <-> email maps (sync_locks is keyed by email; resync by user_id).
    uid_email: dict[int, str] = {}
    email_uid: dict[str, int] = {}
    # user_ids whose resync was deferred because the device was mid-sync; fired
    # when the device's sync ends (see request_resync / on_sync_end).
    pending_resync: set[int] = set()

    @sio.event
    async def connect(sid: str, environ: dict, auth: Any = None) -> bool:
        """Authenticate the device from its query string and join its user room.

        The device connects with ``?token=<JWT>&type=<equipment_no>&sign=...``.
        We verify the JWT (same secret/sessions as the HTTP API), ignore ``sign``
        for now, and room the socket by user id so change events fan out per user.
        """
        query = parse_qs(environ.get("QUERY_STRING", ""))
        token = _first(query.get("token"))
        equipment_no = _first(query.get("type"))

        if not token:
            logger.warning("socket.io connect %s rejected: no token", sid)
            return False

        session = await user_service.verify_token(token)
        if not session:
            logger.warning("socket.io connect %s rejected: invalid token", sid)
            return False

        try:
            user_id = await user_service.get_user_id(session.email)
        except ValueError:
            logger.warning(
                "socket.io connect %s rejected: unknown user %s", sid, session.email
            )
            return False

        room = _room_for_user(user_id)
        # NOTE: enter_room is synchronous in python-socketio 4.x (async in 5.x).
        sio.enter_room(sid, room)
        connected.setdefault(user_id, set()).add(sid)
        sid_equip[sid] = equipment_no or ""
        sid_request[sid] = environ.get("aiohttp.request")
        uid_email[user_id] = session.email
        email_uid[session.email] = user_id
        # A fresh connection runs a full sync on its own, so any deferred resync
        # for this user is already satisfied.
        pending_resync.discard(user_id)
        await sio.save_session(
            sid,
            {
                "user_id": user_id,
                "email": session.email,
                "equipment_no": equipment_no,
            },
        )
        logger.info(
            "socket.io connect sid=%s user=%s(id=%s) equipment=%s room=%s",
            sid,
            session.email,
            user_id,
            equipment_no,
            room,
        )
        return True

    @sio.event
    async def disconnect(sid: str) -> None:
        try:
            data = await sio.get_session(sid)
        except KeyError:
            data = {}
        uid = data.get("user_id")
        if uid is not None and uid in connected:
            connected[uid].discard(sid)
            if not connected[uid]:
                del connected[uid]
        sid_equip.pop(sid, None)
        sid_request.pop(sid, None)
        logger.info("socket.io disconnect sid=%s user=%s", sid, data.get("email"))

    @sio.on("ratta_ping")
    async def ratta_ping(sid: str, *args: Any) -> None:
        # Ratta's application-level heartbeat (on top of engine.io ping/pong).
        # The device emits it ~every 25s and drops the socket if unanswered, so
        # always reply to keep the connection stable. (Resyncs are forced by an
        # abrupt transport close in request_resync, NOT by withholding this —
        # the device tolerates a missed pong inconsistently.)
        logger.debug("socket.io ratta_ping from %s -> ratta_pong", sid)
        await sio.emit("ratta_pong", room=sid)

    async def emit_folder_change(event: FileChangedEvent) -> None:
        room = _room_for_user(event.user_id)
        payload = _finish_folder_payload(event)
        logger.info(
            "socket.io emit %s -> room=%s payload=%r",
            FINISH_FOLDER_EVENT,
            room,
            payload,
        )
        await sio.emit(FINISH_FOLDER_EVENT, payload, room=room)

    def _is_syncing(email: str | None) -> bool:
        """True if the user's device is mid-sync (sync lock held, not expired)."""
        if not email:
            return False
        lock = app["sync_locks"].get(email)
        return bool(lock) and lock[1] > time.time()

    async def _reset_transports(user_id: int) -> int:
        """Abort the user's device socket transports (hard TCP reset → reconnect).

        The reset is read by the okhttp client as a transport ERROR, so it
        reconnects (and runs a full sync). Clean closes don't trigger reconnect;
        only a transport error does. NEVER call this while the device is
        mid-sync — that interrupts an in-flight sync and produces _CONFLICT_
        files. request_resync enforces that.
        """
        sids = list(connected.get(user_id, ()))
        reset = 0
        for sid in sids:
            request = sid_request.get(sid)
            transport = getattr(request, "transport", None) if request else None
            if transport is None or transport.is_closing():
                continue
            try:
                transport.abort()
                reset += 1
            except Exception:
                logger.exception("resync: transport.abort %s failed", sid)
        logger.info("resync user=%s -> reset %d transport(s)", user_id, reset)
        return reset

    async def _deferred_reset(user_id: int) -> None:
        # Small debounce so we land in the idle gap, not racing the next sync.
        await asyncio.sleep(0.8)
        if _is_syncing(uid_email.get(user_id)):
            # A new sync already started; catch it when that one ends.
            pending_resync.add(user_id)
            logger.info("deferred_reset user=%s still mid-sync — re-queued", user_id)
            return
        await _reset_transports(user_id)

    async def request_resync(user_id: int) -> dict[str, Any]:
        """Force the user's device(s) to resync — but never mid-sync.

        If the device is idle, reset its transport now (it reconnects and syncs,
        ~5s). If it's mid-sync, defer: mark pending and fire on the next
        ``synchronous/end`` (see on_sync_end) so we never interrupt an in-flight
        sync — interrupting one is what creates _CONFLICT_ files.
        """
        email = uid_email.get(user_id)
        if _is_syncing(email):
            pending_resync.add(user_id)
            logger.info("request_resync user=%s deferred — device mid-sync", user_id)
            return {"user_id": user_id, "deferred": True}
        reset = await _reset_transports(user_id)
        return {"user_id": user_id, "online": bool(connected.get(user_id)), "reset": reset}

    async def on_sync_end(user_email: str) -> None:
        """Called by the synchronous/end route — flush a deferred resync, if any."""
        user_id = email_uid.get(user_email)
        if user_id is not None and user_id in pending_resync:
            pending_resync.discard(user_id)
            logger.info("sync ended user=%s — firing deferred resync", user_id)
            asyncio.create_task(_deferred_reset(user_id))

    async def _on_file_changed(event: Any) -> None:
        if isinstance(event, FileChangedEvent):
            # Don't tell a device to resync a change it made itself — that's
            # what caused reconnect churn while the device was uploading its own
            # annotations. Only resync for changes from elsewhere (e.g. an API
            # push, which carries no/!= equipment_no).
            orig = event.originator_equipment
            if orig:
                here = {sid_equip.get(s) for s in connected.get(event.user_id, set())}
                if orig in here:
                    logger.info(
                        "file change from connected device %s — skip resync", orig
                    )
                    return
            await request_resync(event.user_id)

    event_bus.subscribe(FileChangedEvent, _on_file_changed)

    # Expose hooks for other callers (e.g. the debug route, sync-end handler).
    app["sio_emit_folder_change"] = emit_folder_change
    app["sio_request_resync"] = request_resync
    app["sio_notify_sync_end"] = on_sync_end

    return sio


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None
