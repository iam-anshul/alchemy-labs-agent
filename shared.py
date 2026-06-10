"""Shared utilities used across scripts and API routes."""
from __future__ import annotations

import json
import logging

from fastapi import Request
from sse_starlette.sse import EventSourceResponse

from api.events import EventBus


def setup_logging(verbose: bool = False) -> None:
    """Configure root logger and quiet noisy HTTP libraries."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("httpx", "openai", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def sse_stream(bus: EventBus, channel_id: str, request: Request) -> EventSourceResponse:
    """Create an SSE EventSourceResponse that streams events from a bus channel."""
    async def _generate():
        channel = bus.get_or_create(channel_id)
        async for event in channel.subscribe():
            if await request.is_disconnected():
                break
            payload = dict(event.payload)
            payload.setdefault("timestamp", event.ts)
            yield {"event": event.type, "data": json.dumps(payload, default=str)}

    return EventSourceResponse(_generate())
