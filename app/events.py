from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .domain import EventType, WaymarkEvent, utc_now
from .store import SQLiteStore


class EventHub:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self._subscribers: dict[str, set[asyncio.Queue[WaymarkEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: EventType,
        delivery_id: str,
        data: dict,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> WaymarkEvent:
        event = WaymarkEvent(
            id=f"evt_{uuid.uuid4().hex}",
            type=event_type,
            delivery_id=delivery_id,
            call_id=call_id,
            trace_id=trace_id or f"trace_{uuid.uuid4().hex}",
            timestamp=utc_now(),
            data=data,
        )
        self.store.save_event(event)
        async with self._lock:
            queues = tuple(self._subscribers.get(delivery_id, ()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
        return event

    @asynccontextmanager
    async def subscribe(self, delivery_id: str) -> AsyncIterator[asyncio.Queue[WaymarkEvent]]:
        queue: asyncio.Queue[WaymarkEvent] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers[delivery_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[delivery_id].discard(queue)
                if not self._subscribers[delivery_id]:
                    self._subscribers.pop(delivery_id, None)

