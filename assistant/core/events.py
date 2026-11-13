"""In-process event bus. Plugins and the item store publish; the proactive engine
(Phase 5) subscribes."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import utcnow

log = logging.getLogger(__name__)


@dataclass
class Event:
    type: str  # e.g. "item.created", "weather.rain_expected"
    source: str  # plugin name
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=utcnow)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, history: int = 200):
        self._handlers: list[Handler] = []
        self.recent: deque[Event] = deque(maxlen=history)

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def emit(self, event: Event) -> None:
        self.recent.append(event)
        for handler in list(self._handlers):
            try:
                await handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.type)
