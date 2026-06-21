"""Pydantic output schemas for the multi-stage reasoning agent.

These models define the structured outputs of the Router, Excel, and Answer
agents, plus the cumulative QueryAnswer returned to callers.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PageTarget(BaseModel):
    """A page range the router wants the answer agent to read."""
    doc_id: str
    start_page: int
    end_page: int
    reason: str = Field(description="One short sentence: why this range matches the query")


class TableTarget(BaseModel):
    """A table the router wants the Excel agent to analyse."""
    doc_id: str
    table_id: str
    reason: str = Field(description="One short sentence: why this table matches the query")


class RouterResult(BaseModel):
    """Structured output of the Router agent."""
    page_targets: list[PageTarget] = Field(
        default_factory=list,
        description="Page ranges to read. Be liberal — better to over-include than miss."
    )
    table_targets: list[TableTarget] = Field(
        default_factory=list,
        description="Tables to load as DataFrames. Pick when the answer requires aggregation, filtering, ranking, or precise numeric lookups."
    )
    reasoning: str = Field(description="One paragraph: how you navigated the tree and why these picks.")


class Citation(BaseModel):
    """Source reference for an answer: document + page range."""
    doc_id: str
    doc_title: str
    pages: str = Field(description="Page range like '12-15' or single page '7'")


class TableFinding(BaseModel):
    """A fact extracted/computed by the Excel agent from one table."""
    table_id: str
    doc_id: str
    finding: str = Field(description="What the Excel agent extracted/computed from this table for the query")


class ExcelResult(BaseModel):
    """Structured output of the Excel agent."""
    findings: list[TableFinding] = Field(default_factory=list)
    notes: str = Field(default="", description="Caveats, missing data, multi-table cross-refs")


class AnswerResult(BaseModel):
    """Structured output of the Answer agent."""
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: str = Field(
        description="One of: 'high' (fully grounded), 'medium' (partial), 'low' (insufficient context)"
    )
    needs_more: bool = Field(
        default=False,
        description="True only if confidence is medium/low AND specific extra info from the docs would close the gap.",
    )
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description=(
            "If needs_more is True, 1–3 SPECIFIC questions that would each close a gap. "
            "Each must be a precise search target (e.g. 'What was HDFC's FY25 CSR spend?'), "
            "NOT vague phrases like 'more context'."
        ),
    )
    save_to_file: bool = Field(
        default=True,
        description="False if the user's query explicitly asks not to save the answer as a file.",
    )
    suggested_filename: str | None = Field(
        default=None,
        description=(
            "If save_to_file is True, a kebab-case filename ending in .md "
            "(e.g. 'hdfc-csr-summary-fy25.md'). Otherwise None."
        ),
    )


class HopTrace(BaseModel):
    """Diagnostic trace of a single hop in the multi-hop loop."""
    hop: int
    question: str
    page_targets: list[PageTarget]
    table_targets: list[TableTarget]
    table_findings: list[TableFinding]
    confidence: str
    needs_more: bool
    follow_up_questions: list[str]


class QueryAnswer(BaseModel):
    """Final result of answer_query(), aggregating all hops."""
    query_id: str
    query: str
    page_targets: list[PageTarget]
    table_targets: list[TableTarget]
    table_findings: list[TableFinding]
    answer: str
    confidence: str
    citations: list[Citation]
    latency_ms: int
    n_hops: int = 1
    hops: list[HopTrace] = Field(default_factory=list)
    output_path: str | None = None
