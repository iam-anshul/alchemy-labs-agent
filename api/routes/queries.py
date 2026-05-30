from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agent import QueryAnswer, answer_query
from api.auth import get_current_user
from api.events import EventSink, bus
from api.schemas import (
    QueryAcceptedResponse,
    QueryRequest,
    QueryRunningResponse,
    StoredQueryResponse,
)
from db import SessionLocal, utils
from shared import sse_stream

router = APIRouter(prefix="/v1/workspaces/{ws_id}/queries", tags=["queries"])


# Internal, non-HTTP helpers shared by the route handlers and the doc
# sub-agent's tools. ask_local_query awaits answer_query directly (no
# fire-and-forget task, no SSE channel) so callers like the doc agent can
# get a QueryAnswer back synchronously. answer_query already persists the
# result to the queries table internally, so no extra DB write is needed
# here.
async def ask_local_query(
    workspace_id: str,
    user_id: str,
    query: str,
    doc_ids: list[str] | None = None,
) -> QueryAnswer:
    return await answer_query(workspace_id, user_id, query, doc_ids)


def list_local_queries(workspace_id: str, limit: int = 50) -> list[StoredQueryResponse]:
    """List recent queries in a workspace, newest first. Validates to Pydantic
    inside the session so callers can use the result after the session closes."""
    with SessionLocal() as db:
        rows = utils.list_workspace_queries(db, workspace_id, limit=limit)
        return [StoredQueryResponse.model_validate(r) for r in rows]


def get_local_query(workspace_id: str, query_id: str) -> StoredQueryResponse | None:
    """Fetch a single stored query by id, scoped to the workspace. Returns
    None if the query doesn't exist, belongs to a different workspace, or is
    still running (no answer yet)."""
    with SessionLocal() as db:
        row = utils.get_query(db, query_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        if row.answer is None:
            # still running — nothing useful to return to a non-streaming caller
            return None
        return StoredQueryResponse.model_validate(row)


async def _run_query(
    *,
    ws_id: str,
    query_id: str,
    user_id: str,
    query: str,
    doc_ids: list[str] | None,
) -> None:
    channel_id = f"query:{query_id}"
    sink = EventSink(bus=bus, channel_id=channel_id, query_id=query_id)
    try:
        await answer_query(ws_id, user_id, query, doc_ids, sink=sink)
    finally:
        bus.close(channel_id)


@router.post("", status_code=202, response_model=QueryAcceptedResponse)
async def create_query(
    ws_id: str,
    body: QueryRequest,
    current_user: str = Depends(get_current_user),
) -> QueryAcceptedResponse:
    """Start an async reasoning query. Returns query_id and SSE stream URL."""
    query_id = f"q_{uuid.uuid4().hex[:12]}"
    asyncio.create_task(
        _run_query(
            ws_id=ws_id,
            query_id=query_id,
            user_id=body.user_id,
            query=body.query,
            doc_ids=body.doc_ids,
        )
    )
    stream_url = f"/v1/workspaces/{ws_id}/queries/{query_id}/stream"
    return QueryAcceptedResponse(query_id=query_id, stream_url=stream_url)


@router.get("")
async def list_queries(
    ws_id: str,
    current_user: str = Depends(get_current_user),
) -> list[StoredQueryResponse]:
    """List recent queries in a workspace (limit 50, newest first)."""
    with SessionLocal() as db:
        rows = utils.list_workspace_queries(db, ws_id, limit=50)
    return [StoredQueryResponse.model_validate(r) for r in rows]


@router.get("/{query_id}")
async def get_query(
    ws_id: str,
    query_id: str,
    response: Response,
    current_user: str = Depends(get_current_user),
) -> QueryAnswer | QueryRunningResponse | StoredQueryResponse:
    """Poll query status. Returns 200 (complete), 202 (running), or 404."""
    channel_id = f"query:{query_id}"
    with SessionLocal() as db:
        row = utils.get_query(db, query_id)

    if row is not None and row.workspace_id == ws_id:
        if row.answer is not None:
            return StoredQueryResponse.model_validate(row)
        if bus.is_open(channel_id):
            response.status_code = 202
            return QueryRunningResponse()
        return StoredQueryResponse.model_validate(row)

    if bus.is_open(channel_id):
        response.status_code = 202
        return QueryRunningResponse()

    raise HTTPException(status_code=404, detail="Query not found")


@router.get("/{query_id}/stream")
async def stream_query(
    ws_id: str,
    query_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """SSE stream of query progress (router, excel, answer events)."""
    return sse_stream(bus, f"query:{query_id}", request)
