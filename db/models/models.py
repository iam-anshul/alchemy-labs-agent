from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
import uuid


class Doc(Base):
    __tablename__ = "docs"
    __table_args__ = (Index("idx_docs_ws", "workspace_id"),)

    doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_by_user_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)
    n_pages: Mapped[int | None] = mapped_column(Integer)
    n_tables: Mapped[int | None] = mapped_column(Integer)
    doc_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (Index("idx_nodes_ws_doc", "workspace_id", "doc_id"),)

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("docs.doc_id"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String, ForeignKey("nodes.node_id"))
    depth: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    start_page: Mapped[int | None] = mapped_column(Integer)
    end_page: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    table_ids: Mapped[list[str] | None] = mapped_column(JSON)
    child_ids: Mapped[list[str] | None] = mapped_column(JSON)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (Index("idx_pages_ws", "workspace_id"),)

    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_id: Mapped[str] = mapped_column(
        String, ForeignKey("docs.doc_id"), primary_key=True
    )
    page_n: Mapped[int] = mapped_column(Integer, primary_key=True)
    prose_text: Mapped[str | None] = mapped_column(Text)
    page_summary: Mapped[str | None] = mapped_column(Text)
    table_ids: Mapped[list[str] | None] = mapped_column(JSON)
    node_id: Mapped[str | None] = mapped_column(String, ForeignKey("nodes.node_id"))


class ExtractedTable(Base):
    __tablename__ = "tables"
    __table_args__ = (Index("idx_tables_ws", "workspace_id"),)

    table_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_id: Mapped[str] = mapped_column(String, ForeignKey("docs.doc_id"), nullable=False)
    source_page: Mapped[int | None] = mapped_column(Integer)
    title_guess: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    columns_json: Mapped[dict | list | None] = mapped_column(JSON)
    row_count: Mapped[int | None] = mapped_column(Integer)
    xlsx_path: Mapped[str | None] = mapped_column(Text)
    xlsx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    parquet_path: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("idx_reports_ws_user", "workspace_id", "user_id", "created_at"),)

    report_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    target_length: Mapped[str] = mapped_column(String, nullable=False, server_default="standard")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="queued")
    outline_json: Mapped[dict | list | None] = mapped_column(JSON)
    draft_md: Mapped[str | None] = mapped_column(Text)
    output_path: Mapped[str | None] = mapped_column(Text)
    n_sections: Mapped[int] = mapped_column(Integer, server_default="0")
    n_words: Mapped[int] = mapped_column(Integer, server_default="0")
    n_hops: Mapped[int] = mapped_column(Integer, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Query(Base):
    __tablename__ = "queries"
    __table_args__ = (
        Index("idx_queries_ws_user", "workspace_id", "user_id", "created_at"),
    )

    query_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    doc_ids_used: Mapped[list[str] | None] = mapped_column(JSON)
    table_ids_used: Mapped[list[str] | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[dict | list | None] = mapped_column(JSON)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (Index("idx_workspaces_user_created", "user_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceRun(Base):
    __tablename__ = "workspace_runs"
    __table_args__ = (
        Index("idx_workspace_runs_ws_created", "workspace_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    # user_goal is the raw user message that kicked off this run. Kept
    # separate from todo_md so message-history reconstruction can use it as
    # the synthetic ModelRequest content.
    user_goal: Mapped[str] = mapped_column(Text, nullable=False)
    todo_md: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="running")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
