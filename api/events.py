from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

_SENTINEL = object()


@dataclass
class Event:
    type: str
    payload: dict
    ts: float


class EventChannel:
    def __init__(self) -> None:
        self._log: list[Event] = []
        self._subscribers: set[asyncio.Queue[Event | object]] = set()
        self.closed: bool = False
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, payload: dict) -> None:
        event = Event(type=event_type, payload=payload, ts=time.time())
        async with self._lock:
            self._log.append(event)
            for q in list(self._subscribers):
                await q.put(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | object] = asyncio.Queue()
        async with self._lock:
            for event in self._log:
                await queue.put(event)
            self._subscribers.add(queue)
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item  # type: ignore[misc]
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def mark_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                pass


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[str, EventChannel] = {}

    def get_or_create(self, channel_id: str) -> EventChannel:
        if channel_id not in self._channels:
            self._channels[channel_id] = EventChannel()
        return self._channels[channel_id]

    def close(self, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is not None:
            channel.mark_closed()

    def is_open(self, channel_id: str) -> bool:
        channel = self._channels.get(channel_id)
        if channel is None:
            return False
        return not channel.closed


@dataclass
class EventSink:
    bus: EventBus | None = field(default=None, repr=False)
    channel_id: str = ""
    query_id: str | None = None

    async def publish(self, event_type: str, payload: dict) -> None:
        if self.bus is None or not self.channel_id:
            return
        channel = self.bus.get_or_create(self.channel_id)
        await channel.publish(event_type, payload)


bus = EventBus()
