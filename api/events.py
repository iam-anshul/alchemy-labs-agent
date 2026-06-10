from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

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
    workspace_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_type: str | None = None
    attempt: int | None = None

    async def publish(self, event_type: str, payload: dict) -> None:
        if self.bus is None or not self.channel_id:
            return
        channel = self.bus.get_or_create(self.channel_id)
        await channel.publish(event_type, payload)

    async def publish_ui(
        self,
        event_type: str,
        *,
        agent_type: str | None = None,
        stage: str,
        status: str,
        message: str,
        task_id: str | None = None,
        attempt: int | None = None,
        data: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "query_id": self.query_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id or self.query_id,
            "task_id": self.task_id if task_id is None else task_id,
            "agent_type": agent_type or self.agent_type or "system",
            "stage": stage,
            "status": status,
            "message": message,
            "attempt": self.attempt if attempt is None else attempt,
            "timestamp": time.time(),
            "data": data or {},
            "artifacts": artifacts or [],
        }
        await self.publish(event_type, payload)

    def child(
        self,
        *,
        task_id: str | None = None,
        agent_type: str | None = None,
        attempt: int | None = None,
    ) -> "EventSink":
        return EventSink(
            bus=self.bus,
            channel_id=self.channel_id,
            query_id=self.query_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            task_id=self.task_id if task_id is None else task_id,
            agent_type=self.agent_type if agent_type is None else agent_type,
            attempt=self.attempt if attempt is None else attempt,
        )


def file_artifact(
    *,
    path: str | None = None,
    filename: str | None = None,
    kind: str = "file",
    type: str | None = None,
    mime_type: str | None = None,
    bytes: int | None = None,
    content: str | None = None,
    content_base64: str | None = None,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if filename is None and path:
        filename = path.rsplit("/", 1)[-1]
    return {
        "kind": kind,
        "path": path,
        "filename": filename,
        "type": type,
        "mime_type": mime_type,
        "bytes": bytes,
        "content": content,
        "content_base64": content_base64,
        "url": url,
        "metadata": metadata or {},
    }


bus = EventBus()
