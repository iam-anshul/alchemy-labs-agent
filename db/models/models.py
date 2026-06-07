from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Doc(Base):
    __tablename__ = "docs"
    __table_args__ = (Index("idx_docs_ws", "workspace_id"),)

    doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
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
    doc_id: Mapped[str] = mapped_column(
        String, ForeignKey("docs.doc_id", ondelete="CASCADE"), nullable=False
    )
    # DEFERRABLE INITIALLY DEFERRED: a doc's node tree is inserted in one
    # transaction, and a child's parent row may be inserted after the child.
    # Deferring this self-referential FK to commit time lets the whole tree be
    # written order-independently — the check still runs (a genuine dangling
    # parent_id fails at commit), just over the complete, consistent set.
    parent_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey(
            "nodes.node_id", ondelete="SET NULL",
            deferrable=True, initially="DEFERRED",
        ),
    )
    depth: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    start_page: Mapped[int | None] = mapped_column(Integer)
    end_page: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    table_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    child_ids: Mapped[list[str] | None] = mapped_column(JSONB)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (Index("idx_pages_ws", "workspace_id"),)

    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_id: Mapped[str] = mapped_column(
        String, ForeignKey("docs.doc_id", ondelete="CASCADE"), primary_key=True
    )
    page_n: Mapped[int] = mapped_column(Integer, primary_key=True)
    prose_text: Mapped[str | None] = mapped_column(Text)
    page_summary: Mapped[str | None] = mapped_column(Text)
    table_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    # Deferred for the same reason as nodes.parent_id: pages.node_id is
    # backfilled in the same transaction that inserts the nodes, so the FK
    # target may not exist yet at insert time. Checked at commit.
    node_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey(
            "nodes.node_id", ondelete="SET NULL",
            deferrable=True, initially="DEFERRED",
        ),
    )


class ExtractedTable(Base):
    __tablename__ = "tables"
    __table_args__ = (Index("idx_tables_ws", "workspace_id"),)

    table_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    doc_id: Mapped[str] = mapped_column(
        String, ForeignKey("docs.doc_id", ondelete="CASCADE"), nullable=False
    )
    source_page: Mapped[int | None] = mapped_column(Integer)
    title_guess: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    columns_json: Mapped[dict | list | None] = mapped_column(JSONB)
    row_count: Mapped[int | None] = mapped_column(Integer)
    xlsx_path: Mapped[str | None] = mapped_column(Text)
    xlsx_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    parquet_path: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("idx_reports_ws_user", "workspace_id", "user_id", "created_at"),)

    report_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    target_length: Mapped[str] = mapped_column(String, nullable=False, server_default="standard")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="queued")
    outline_json: Mapped[dict | list | None] = mapped_column(JSONB)
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
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    doc_ids_used: Mapped[list[str] | None] = mapped_column(JSONB)
    table_ids_used: Mapped[list[str] | None] = mapped_column(JSONB)
    answer: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[dict | list | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (Index("idx_workspaces_user_created", "user_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QueryRun(Base): # this model name is needed to me renamed to QueryRun in the upcoming versions
    __tablename__ = "workspace_runs"
    __table_args__ = (
        Index("idx_workspace_runs_ws_created", "workspace_id", "started_at"),
    )
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    # goal is the raw user message that kicked off this run. Kept
    # separate from todo_md so message-history reconstruction can use it as
    # the synthetic ModelRequest content.
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    workspace: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    replans_used: Mapped[int] = mapped_column(Integer, server_default="0")
    replan_budget: Mapped[int] = mapped_column(Integer, server_default="3")
    todo_md: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    query_id: Mapped[UUID] = mapped_column(UUID, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(UUID, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="running")
    query_counter: Mapped[int] = mapped_column(Integer, nullable=False)