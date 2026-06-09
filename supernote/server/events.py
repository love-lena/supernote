import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Type

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Base class for all events."""

    pass


@dataclass
class NoteUpdatedEvent(Event):
    """Triggered when a .note file is created or updated."""

    file_id: int
    user_id: int
    file_path: str


@dataclass
class NoteDeletedEvent(Event):
    """Triggered when a .note file is deleted."""

    file_id: int
    user_id: int


@dataclass
class FileChangedEvent(Event):
    """Triggered when ANY user file changes (create/update/delete).

    Distinct from NoteUpdatedEvent, which is .note-only and drives server-side
    processing (OCR, summaries). This event exists to notify connected devices
    that their content changed so they should run a sync pass — it fires for
    every file type, including PDFs pushed to INBOX via the API.
    """

    user_id: int
    file_path: str | None = None
    change: str = "update"  # "update" | "delete"
    # equipment_no of the device/client that made the change, if known. Used to
    # avoid telling a device to resync a change it originated.
    originator_equipment: str | None = None


EventHandler = Callable[[Event], Awaitable[Any]]


class LocalEventBus:
    """A simple in-process event bus using asyncio."""

    def __init__(self) -> None:
        self._subscribers: Dict[Type[Event], List[EventHandler]] = {}

    def subscribe(self, event_type: Type[Event], handler: EventHandler) -> None:
        """Subscribe a handler to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug(f"Subscribed {handler} to {event_type.__name__}")

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        event_type = type(event)
        if event_type in self._subscribers:
            for handler in self._subscribers[event_type]:
                # We fire and forget using create_task to ensure background execution
                # and not block the caller (e.g., the API response).
                asyncio.create_task(self._safe_execute(handler, event))

    async def _safe_execute(self, handler: EventHandler, event: Event) -> None:
        """Execute handler and log exceptions."""
        try:
            await handler(event)
        except Exception:
            logger.exception(f"Error handling event {event} with handler {handler}")
