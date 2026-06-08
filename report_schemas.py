"""Pydantic schemas for the report drafting pipeline."""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent_schemas import Citation


class PageRef(BaseModel):
    """A resolved page range with its leaf-node summary."""
    doc_id: str
    start_page: int
    end_page: int
    leaf_summary: str
    reason: str = ""


class TableRef(BaseModel):
    """A resolved table reference with column metadata."""
    doc_id: str
    table_id: str
    source_page: int | None = None
    columns: list[str] = Field(default_factory=list)
    description: str | None = None
    reason: str = ""


class ReportSection(BaseModel):
    """One section in the report outline."""
    section_id: str
    title: str
    purpose: str
    assigned_page_refs: list[str] = Field(default_factory=list)
    assigned_table_ids: list[str] = Field(default_factory=list)
    must_cover: list[str] = Field(default_factory=list)


class ReportOutline(BaseModel):
    """The structured outline for a report."""
    title: str
    abstract: str
    sections: list[ReportSection]
    reasoning: str = ""


class ReportGap(BaseModel):
    """A gap found by the critic agent."""
    topic: str
    follow_up_query: str
    target_section: str


class CritiqueResult(BaseModel):
    """Structured output of the Critic agent."""
    gaps: list[ReportGap] = Field(default_factory=list)
    notes: str = ""


class SectionDraft(BaseModel):
    """A drafted section with its markdown body and citations."""
    section_id: str
    title: str
    markdown: str
    citations: list[Citation] = Field(default_factory=list)
    n_words: int


class ReportResult(BaseModel):
    """Final result of draft_report()."""
    report_id: str
    brief: str
    outline: ReportOutline
    sections: list[SectionDraft]
    draft_md: str
    output_path: str | None
    n_sections: int
    n_words: int
    n_hops: int
    latency_ms: int
    report_name: str


class ExecutiveSummary(BaseModel):
    """Output of the summary agent."""
    summary: str
