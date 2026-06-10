from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.auth import get_current_user
from api.events import EventSink, bus
from api.schemas import (
    ReportAcceptedResponse,
    ReportRequest,
    ReportRunningResponse,
    StoredReportResponse,
)
from db import SessionLocal, utils
from report import ReportResult, draft_report
from shared import sse_stream

router = APIRouter(prefix="/v1/workspaces/{ws_id}/reports", tags=["reports"])


# Internal, non-HTTP helpers shared by the route handlers and the doc
# sub-agent's tools. draft_local_report awaits draft_report directly (no
# fire-and-forget task, no SSE channel) so callers like the doc agent get
# the ReportResult back synchronously. draft_report persists to the reports
# table internally, so no extra DB write is needed here.
async def draft_local_report(
    workspace_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None = None,
    target_length: Literal["brief", "standard", "deep"] = "standard",
) -> ReportResult:
    report_id = f"rep_{uuid.uuid4().hex[:12]}"
    return await draft_report(
        workspace_id=workspace_id,
        user_id=user_id,
        brief=brief,
        doc_ids=doc_ids,
        target_length=target_length,
        report_id=report_id,
    )


def list_local_reports(workspace_id: str, limit: int = 50) -> list[StoredReportResponse]:
    """List recent reports in a workspace, newest first. Validates to Pydantic
    inside the session so callers can use the result after the session closes."""
    with SessionLocal() as db:
        rows = utils.list_reports(db, workspace_id, limit=limit)
        return [StoredReportResponse.model_validate(r) for r in rows]


def get_local_report(workspace_id: str, report_id: str) -> StoredReportResponse | None:
    """Fetch a single stored report by id, scoped to the workspace. Returns
    None if the report doesn't exist, belongs to a different workspace, or is
    still running / not yet complete (no draft_md)."""
    with SessionLocal() as db:
        row = utils.get_report(db, report_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        if row.status != "complete" or row.draft_md is None:
            return None
        return StoredReportResponse.model_validate(row)


async def _run_report(
    *,
    ws_id: str,
    report_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None,
    target_length: Literal["brief", "standard", "deep"],
) -> None:
    channel_id = f"report:{report_id}"
    sink = EventSink(
        bus=bus,
        channel_id=channel_id,
        query_id=report_id,
        workspace_id=ws_id,
        run_id=report_id,
        agent_type="document_answering",
    )
    try:
        await draft_report(
            workspace_id=ws_id,
            user_id=user_id,
            brief=brief,
            doc_ids=doc_ids,
            target_length=target_length,
            report_id=report_id,
            sink=sink,
        )
    finally:
        bus.close(channel_id)


@router.post("", status_code=202, response_model=ReportAcceptedResponse)
async def create_report(
    ws_id: str,
    body: ReportRequest,
    current_user: str = Depends(get_current_user),
) -> ReportAcceptedResponse:
    """Start an async report draft. Returns report_id and SSE stream URL."""
    report_id = f"rep_{uuid.uuid4().hex[:12]}"
    asyncio.create_task(
        _run_report(
            ws_id=ws_id,
            report_id=report_id,
            user_id=body.user_id,
            brief=body.brief,
            doc_ids=body.doc_ids,
            target_length=body.target_length,
        )
    )
    stream_url = f"/v1/workspaces/{ws_id}/reports/{report_id}/stream"
    return ReportAcceptedResponse(report_id=report_id, stream_url=stream_url)


@router.get("")
async def list_reports(
    ws_id: str,
    current_user: str = Depends(get_current_user),
) -> list[StoredReportResponse]:
    """List recent reports in a workspace (limit 50, newest first)."""
    with SessionLocal() as db:
        rows = utils.list_reports(db, ws_id, limit=50)
    return [StoredReportResponse.model_validate(r) for r in rows]


@router.get("/{report_id}")
async def get_report(
    ws_id: str,
    report_id: str,
    response: Response,
    current_user: str = Depends(get_current_user),
) -> ReportResult | ReportRunningResponse | StoredReportResponse:
    """Poll report status. Returns 200 (complete), 202 (running), or 404."""
    channel_id = f"report:{report_id}"
    with SessionLocal() as db:
        row = utils.get_report(db, report_id)

    if row is not None and row.workspace_id == ws_id:
        if row.status == "complete" and row.draft_md is not None:
            return StoredReportResponse.model_validate(row)
        if bus.is_open(channel_id):
            response.status_code = 202
            return ReportRunningResponse()
        return StoredReportResponse.model_validate(row)

    if bus.is_open(channel_id):
        response.status_code = 202
        return ReportRunningResponse()

    raise HTTPException(status_code=404, detail="Report not found")


@router.get("/{report_id}/stream")
async def stream_report(
    ws_id: str,
    report_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """SSE stream of report drafting progress."""
    return sse_stream(bus, f"report:{report_id}", request)
