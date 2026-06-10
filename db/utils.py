"""CRUD helpers for all ORM models.

Every function takes an explicit SQLAlchemy ``Session`` as its first argument
and commits within that session.  The caller manages session lifecycle.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, exists, select, func
from sqlalchemy.orm import Session

from db.models import (
    Doc,
    ExtractedTable,
    Node,
    Page,
    Query,
    Report,
    Workspace,
    QueryRun
)
# Pydantic QueryRun (the in-flight run object) shares its name with the ORM
# QueryRun above, so it's aliased here to keep the two unambiguous in this file:
# `QueryRun` = ORM row we persist, `QueryRunModel` = the Pydantic input.
from formats_pydantic import QueryRun as QueryRunModel
from uuid import UUID
from formats_pydantic import QueryRun as queryRunFormat

# ── Docs ──────────────────────────────────────────────────────────────────

def create_doc(db: Session, **fields: Any) -> Doc:
    """Insert a new document row and return it."""
    doc = Doc(**fields)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def get_doc(db: Session, doc_id: str) -> Doc | None:
    """Fetch a single doc by primary key, or ``None``."""
    return db.get(Doc, doc_id)


def list_docs(db: Session, workspace_id: str) -> list[Doc]:
    """Return all docs in a workspace, newest first."""
    return list(db.scalars(
        select(Doc).where(Doc.workspace_id == workspace_id).order_by(desc(Doc.created_at))
    ))


# ── Nodes ─────────────────────────────────────────────────────────────────

def create_node(db: Session, **fields: Any) -> Node:
    """Insert a tree node and return it."""
    node = Node(**fields)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def get_root_nodes(db: Session, doc_id: str) -> list[Node]:
    """Return root-level nodes (parent_id IS NULL) for a doc."""
    return list(db.scalars(
        select(Node).where(Node.doc_id == doc_id, Node.parent_id.is_(None))
    ))


# ── Pages ─────────────────────────────────────────────────────────────────

def bulk_create_pages(db: Session, pages: list[dict[str, Any]]) -> int:
    """Bulk-insert page rows from a list of dicts. Returns count inserted."""
    db.add_all([Page(**p) for p in pages])
    db.commit()
    return len(pages)


def list_pages(db: Session, doc_id: str) -> list[Page]:
    """Return all pages for a doc, ordered by page number."""
    return list(db.scalars(
        select(Page).where(Page.doc_id == doc_id).order_by(Page.page_n)
    ))


# ── Tables ────────────────────────────────────────────────────────────────

def create_table(db: Session, **fields: Any) -> ExtractedTable:
    """Insert an extracted table row."""
    table = ExtractedTable(**fields)
    db.add(table)
    db.commit()
    db.refresh(table)
    return table


def get_table(db: Session, table_id: str) -> ExtractedTable | None:
    """Fetch a single extracted table by primary key."""
    return db.get(ExtractedTable, table_id)


def list_tables_for_doc(db: Session, doc_id: str) -> list[ExtractedTable]:
    """Return all tables for a doc, ordered by source page."""
    return list(db.scalars(
        select(ExtractedTable).where(ExtractedTable.doc_id == doc_id)
        .order_by(ExtractedTable.source_page)
    ))


# ── Queries ───────────────────────────────────────────────────────────────

def create_query(db: Session, **fields: Any) -> Query:
    """Insert a query audit row."""
    q = Query(**fields)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def get_query(db: Session, query_id: str) -> Query | None:
    """Fetch a stored query by primary key."""
    return db.get(Query, query_id)


def list_workspace_queries(db: Session, workspace_id: str, limit: int = 50) -> list[Query]:
    """Return recent queries across all users in a workspace."""
    return list(db.scalars(
        select(Query)
        .where(Query.workspace_id == workspace_id)
        .order_by(desc(Query.created_at))
        .limit(limit)
    ))


# ── Reports ───────────────────────────────────────────────────────────────

def create_report(db: Session, **fields: Any) -> Report:
    """Insert a report row."""
    report = Report(**fields)
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def get_report(db: Session, report_id: str) -> Report | None:
    """Fetch a stored report by primary key."""
    return db.get(Report, report_id)


def list_reports(db: Session, workspace_id: str, limit: int = 50) -> list[Report]:
    """Return recent reports in a workspace."""
    return list(db.scalars(
        select(Report)
        .where(Report.workspace_id == workspace_id)
        .order_by(desc(Report.created_at))
        .limit(limit)
    ))


def update_report(db: Session, report_id: str, **fields: Any) -> Report | None:
    """Update arbitrary fields on a report. Returns ``None`` if not found."""
    report = db.get(Report, report_id)
    if report is None:
        return None
    for key, value in fields.items():
        setattr(report, key, value)
    db.commit()
    db.refresh(report)
    return report


# ── Workspaces ────────────────────────────────────────────────────────────

def create_workspace(db: Session, **fields: Any) -> Workspace:
    """Insert a new workspace row and return it. Caller supplies workspace_id
    (since it's user-chosen) and user_id."""
    ws = Workspace(**fields)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def get_workspace(db: Session, workspace_id: str) -> Workspace | None:
    return db.get(Workspace, workspace_id)


def list_workspaces(db: Session, user_id: str) -> list[Workspace]:
    """Return all workspaces owned by a user, newest first."""
    return list(db.scalars(
        select(Workspace)
        .where(Workspace.user_id == user_id)
        .order_by(desc(Workspace.created_at))
    ))


def get_or_create_workspace(
    db: Session, workspace_id: str, user_id: str
) -> Workspace:
    """Idempotent lookup-or-insert. Returns the existing workspace if one with
    this id exists; otherwise creates and returns a new one. Used by main.py's
    interactive entrypoint so a user can either resume a chat or start one with
    a single prompt."""
    existing = db.get(Workspace, workspace_id)
    if existing is not None:
        return existing, True
    return create_workspace(db, workspace_id=workspace_id, user_id=user_id), False


# ── Query runs (planner chat history) ─────────────────────────────────

def create_query_run(db: Session, **fields: Any) -> QueryRun:
    """Insert a workspace run row at run-start (typically status='running').
    The caller fills in todo_md and flips status to 'completed' or 'failed'
    later via update_workspace_run."""
    run = QueryRun(**fields)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_workspace_run(db: Session, query_id: str) -> QueryRun | None:
    return db.get(QueryRun, query_id)


def update_workspace_run(
    db: Session, query_id: str, **fields: Any
) -> QueryRun | None:
    """Update mutable fields on a run (typically todo_md + status at end of
    run). Returns None if the row doesn't exist."""
    run = db.get(QueryRun, query_id)
    if run is None:
        return None
    for key, value in fields.items():
        setattr(run, key, value)
    db.commit()
    db.refresh(run)
    return run


def list_recent_runs(
    db: Session, workspace_id: str, limit: int = 5
) -> list[QueryRun]:
    """Return the most recent runs in a workspace, in CHRONOLOGICAL order
    (oldest first). The reverse-on-DESC trick gives us the newest N runs
    cheaply while still presenting them to the planner as a coherent
    conversation timeline."""
    rows = list(db.scalars(
        select(QueryRun)
        .where(QueryRun.workspace_id == workspace_id)
        .order_by(desc(QueryRun.started_at))
        .limit(limit)
    ))
    return list(reversed(rows))

#------------------------------------------my additions------------------------------------------------------
# functions needed for chat router APIs
# create workspace function which is already here
# get workspace function
# check if workspace exists function

# if workspace exist function
def does_workspace_exist(db: Session, workspace_id: str, user_id: str) -> bool:
    return bool(db.scalar(
        select(
            exists().where(
                Workspace.workspace_id == workspace_id,
                Workspace.user_id == user_id,
            )
        )
    ))

# fetch query counter value
def get_highest_query_counter(db: Session, workspace_id: str, user_id: UUID) -> int:
    return db.query(func.max(QueryRun.query_counter)).filter(
        QueryRun.user_id==user_id,
        QueryRun.workspace_id==workspace_id
        ).scalar()

# create query run
def register_query_run(
    db: Session, run: QueryRunModel, final_todo: str | None
) -> Exception | None:
    """Persist the finished run to the workspace_runs table. Returns None on
    success, or the Exception on failure (the caller surfaces it as a 500)."""
    try:
        thisQueryRun = QueryRun(
            user_query=run.user_query,
            goal=run.goal,
            workspace=run.workspace,
            started_at=run.started_at,
            replans_used=run.replans_used,
            replan_budget=run.replan_budget,
            todo_md=final_todo,
            workspace_id=run.workspace_id,
            query_id=run.query_id,
            user_id=run.user_id,
            status=run.status,
            query_counter=run.query_counter
        )
        db.add(thisQueryRun)
        db.commit()
        db.refresh(thisQueryRun)
    except Exception as e:
        return e
    return None


# delete a workspace and everything scoped to it
def delete_workspace(db: Session, workspace_id: str, user_id: str) -> bool:
    ws = db.get(Workspace, workspace_id)
    if ws is None or ws.user_id != user_id:
        return False
    db.delete(ws)   # DB cascade removes docs/nodes/pages/tables/queries/reports/runs
    db.commit()
    return True

# get document_id by document name
def get_docID_by_name(db: Session, workspace_id: str, doc_name:str):
    doc_ids = db.query(Doc.doc_id).filter(
        Doc.workspace_id==workspace_id,
        Doc.title==doc_name
    ).all()
    return [id[0] for id in doc_ids]

def get_reportID_by_name(db: Session, workspace_id: str, report_name:str):
    report_ids = db.query(Report.report_id).filter(
        Report.report_name==report_name,
        Report.workspace_id==workspace_id
    ).all()
    return [id[0] for id in report_ids]

def get_all_workspaces(db: Session, user_id: str) -> list[str]:
    return list(
        db.scalars(
            select(Workspace.workspace_id)
            .where(Workspace.user_id == user_id)
        )
    )

def get_workspace_ingested_files(
    db: Session,
    workspace_id: str,
    user_id: str
):
    rows = db.execute(
        select(
            Doc.doc_id,
            Doc.title,
            Doc.doc_summary
        ).where(
            Doc.workspace_id == workspace_id,
            Doc.uploaded_by_user_id == user_id
        )
    ).all()

    return {
        row.doc_id: {
            "doc_title": row.title,
            "doc_summary": row.doc_summary
        }
        for row in rows
    }
