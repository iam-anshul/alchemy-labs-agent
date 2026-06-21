from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DocumentResponse(BaseModel):
    doc_id: str
    workspace_id: str
    uploaded_by_user_id: str
    title: str | None = None
    source_path: str | None = None
    n_pages: int | None = None
    n_tables: int | None = None
    doc_summary: str | None = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentAcceptedResponse(BaseModel):
    doc_id: str
    status: str
    stream_url: str


class QueryRequest(BaseModel):
    user_id: str
    query: str
    doc_ids: list[str] | None = None


class QueryAcceptedResponse(BaseModel):
    query_id: str
    stream_url: str


class QueryRunningResponse(BaseModel):
    status: str = "running"


class StoredQueryResponse(BaseModel):
    query_id: str
    workspace_id: str
    user_id: str
    query: str | None = Field(default=None, validation_alias="query_text")
    doc_ids_used: list[str] | None = None
    table_ids_used: list[str] | None = None
    answer: str | None = None
    citations: list | dict | None = Field(default=None, validation_alias="citations_json")
    latency_ms: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ReportRequest(BaseModel):
    user_id: str
    brief: str
    doc_ids: list[str] | None = None
    target_length: Literal["brief", "standard", "deep"] = "standard"


class ReportAcceptedResponse(BaseModel):
    report_id: str
    stream_url: str


class ReportRunningResponse(BaseModel):
    status: str = "running"


class StoredReportResponse(BaseModel):
    report_id: str
    workspace_id: str
    user_id: str
    brief: str
    target_length: str
    status: str
    outline: dict | list | None = Field(default=None, validation_alias="outline_json")
    draft_md: str | None = None
    output_path: str | None = None
    n_sections: int = 0
    n_words: int = 0
    n_hops: int = 0
    latency_ms: int = 0
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
