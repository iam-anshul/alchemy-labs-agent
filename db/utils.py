"""CRUD helpers for all ORM models.

Every function takes an explicit SQLAlchemy ``Session`` as its first argument
and commits within that session.  The caller manages session lifecycle.
"""
from __future__ import annotations

import base64
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


def list_ready_docs_for_planner(db: Session, workspace_id: str) -> list[Doc]:
    """Return every fully-ingested doc the planner may route to.

    The planner does not get a document lookup tool; instead, it receives this
    ready-doc inventory in prompt context. Only `ready` docs are usable by the
    document_answering executor. Queued/building/failed docs are intentionally
    omitted so the planner does not route work to sources the doc reasoner
    cannot query yet.
    """
    return list(db.scalars(
        select(Doc)
        .where(Doc.workspace_id == workspace_id, Doc.status == "ready")
        .order_by(desc(Doc.created_at))
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


def list_workspace_runs(
    db: Session, workspace_id: str, user_id: UUID
) -> list[QueryRun]:
    """Return every run in a workspace owned by this user, newest first.

    Powers the workspace "Recent runs" list in the UI. Scoped by user_id (not
    just workspace_id) so a run list can never leak across users, mirroring the
    other workspace-scoped reads. Unlike list_recent_runs (planner history,
    capped at 5 and chronological), this is the full history newest-first for
    display."""
    return list(db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.user_id == user_id,
        )
        .order_by(desc(QueryRun.started_at))
    ))


def get_workspace_run(
    db: Session, workspace_id: str, query_id: UUID, user_id: UUID
) -> QueryRun | None:
    """Fetch a single run by id, scoped to its workspace and owning user.

    Backs the run-detail page (GET /workspace/{ws}/runs/{run_id}). Returns None
    if there is no such run for this user — the route turns that into a 404,
    which the frontend treats as 'not found / history unavailable'."""
    return db.scalar(
        select(QueryRun).where(
            QueryRun.query_id == query_id,
            QueryRun.workspace_id == workspace_id,
            QueryRun.user_id == user_id,
        )
    )

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
    success, or the Exception on failure (the caller surfaces it as a 500).

    Upsert, not insert-only: a run row may already exist because artifacts were
    persisted incrementally during the run (see append_run_artifacts). In that
    case we update the existing row rather than inserting a duplicate PK. We
    deliberately do NOT touch produced_artifacts here — those were written
    incrementally and the end-of-run model doesn't carry them."""
    try:
        existing = db.get(QueryRun, run.query_id)
        if existing is not None:
            existing.user_query = run.user_query
            existing.goal = run.goal
            existing.workspace = run.workspace
            existing.started_at = run.started_at
            existing.replans_used = run.replans_used
            existing.replan_budget = run.replan_budget
            existing.todo_md = final_todo
            existing.workspace_id = run.workspace_id
            existing.user_id = run.user_id
            existing.status = run.status
            existing.query_counter = run.query_counter
        else:
            db.add(QueryRun(
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
                query_counter=run.query_counter,
            ))
        db.commit()
    except Exception as e:
        return e
    return None


def append_run_artifacts(
    db: Session,
    *,
    query_id: UUID,
    workspace_id: str,
    user_id: UUID,
    artifacts: list[dict],
    row_defaults: dict[str, Any],
) -> None:
    """Append produced-file artifacts to a run row, creating the row if needed.

    Called as each task completes so produced files are durable mid-run (they
    survive a crash before the end-of-run register_query_run). `artifacts` is a
    list of {rel_path, content_b64, bytes, task_id}; entries are appended to the
    existing produced_artifacts array, de-duplicated by rel_path (a re-run of a
    task overwrites its earlier entry). `row_defaults` supplies the NOT NULL
    columns (user_query, goal, workspace, started_at, status, query_counter)
    needed when the row is first created here, before register_query_run runs."""
    run = db.get(QueryRun, query_id)
    if run is None:
        run = QueryRun(
            query_id=query_id,
            workspace_id=workspace_id,
            user_id=user_id,
            produced_artifacts=[],
            **row_defaults,
        )
        db.add(run)

    existing = list(run.produced_artifacts or [])
    by_path = {a["rel_path"]: a for a in existing}
    for art in artifacts:
        by_path[art["rel_path"]] = art
    # Reassign (not in-place mutate) so SQLAlchemy detects the JSONB change.
    run.produced_artifacts = list(by_path.values())
    db.commit()


def list_prior_runs_meta(
    db: Session, workspace_id: str, exclude_query_id: UUID, limit: int = 50
) -> list[dict]:
    """Cheap metadata for the planner's prior-run history tools.

    Returns up to `limit` prior runs in this workspace (newest first),
    EXCLUDING the in-flight run, as lightweight dicts:
    {query_id, user_query, status, started_at, query_counter, todo_md_chars}.

    Deliberately does NOT return todo_md content — only its length — so the
    planner can browse the workspace timeline without pulling every plan into
    context. It then calls get_run_todo_md(query_id) for the specific runs it
    decides are relevant. Runs that never rendered a todo (todo_md is NULL) are
    still listed with todo_md_chars=0 so the planner sees they exist but knows
    there is no plan to fetch."""
    rows = db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.query_id != exclude_query_id,
        )
        .order_by(desc(QueryRun.started_at))
        .limit(limit)
    )
    return [
        {
            "query_id": str(run.query_id),
            "user_query": run.user_query,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "query_counter": run.query_counter,
            "todo_md_chars": len(run.todo_md) if run.todo_md else 0,
        }
        for run in rows
    ]


def count_prior_runs(
    db: Session, workspace_id: str, exclude_query_id: UUID
) -> tuple[int, dict | None]:
    """Return (count_of_prior_runs, latest_prior_run_meta_or_None) for the
    one-line history hint pushed into planner context. The latest meta is the
    same lightweight dict shape as list_prior_runs_meta entries. Excludes the
    in-flight run. count counts ALL prior runs in the workspace (not capped),
    so the hint can say e.g. 'this workspace has 37 prior runs'."""
    count = db.scalar(
        select(func.count())
        .select_from(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.query_id != exclude_query_id,
        )
    ) or 0
    latest = db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.query_id != exclude_query_id,
        )
        .order_by(desc(QueryRun.started_at))
        .limit(1)
    ).first()
    latest_meta = None
    if latest is not None:
        latest_meta = {
            "query_id": str(latest.query_id),
            "user_query": latest.user_query,
            "status": latest.status,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "query_counter": latest.query_counter,
            "todo_md_chars": len(latest.todo_md) if latest.todo_md else 0,
        }
    return int(count), latest_meta


def get_run_todo_md(
    db: Session, workspace_id: str, query_id: UUID
) -> str | None:
    """Return one prior run's full final todo.md, or None if the run doesn't
    exist in this workspace or never rendered a todo. Workspace-scoped (the
    planner already operates within a single workspace; no user_id needed here
    because the caller's run is already user-authenticated and same-workspace).
    Backs the planner's get_run_todo tool."""
    run = db.scalar(
        select(QueryRun).where(
            QueryRun.query_id == query_id,
            QueryRun.workspace_id == workspace_id,
        )
    )
    if run is None:
        return None
    return run.todo_md


def get_run_artifacts_by_query_id(
    db: Session, workspace_id: str, query_id: UUID
) -> list[dict] | None:
    """Return the raw stored produced_artifacts ({rel_path, content_b64, bytes,
    task_id}) for a single prior run in this workspace, or None if the run
    doesn't exist. Unlike get_run_produced_artifacts this is NOT user-scoped and
    returns the RAW stored dicts (with content_b64) so the planner's
    fetch_prior_artifact tool can decode and copy them into the current subdir.
    Workspace scoping is sufficient — the planner only ever runs inside one
    workspace it is already authorized for."""
    run = db.scalar(
        select(QueryRun).where(
            QueryRun.query_id == query_id,
            QueryRun.workspace_id == workspace_id,
        )
    )
    if run is None:
        return None
    return list(run.produced_artifacts or [])


def list_prior_artifact_manifest(
    db: Session, workspace_id: str, exclude_query_id: UUID
) -> list[dict]:
    """Content-free manifest of every prior run's produced artifacts in this
    workspace, newest run first, EXCLUDING the in-flight run. Each entry:
    {query_id, run_started_at, task_id, rel_path, bytes}. No content_b64 — this
    is the cheap 'what files exist across prior runs' listing the planner browses
    before deciding what to fetch_prior_artifact. Mirrors the (run-scoped)
    metadata the artifact download routes expose, but stripped of bytes."""
    rows = db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.query_id != exclude_query_id,
        )
        .order_by(desc(QueryRun.started_at))
    )
    out: list[dict] = []
    for run in rows:
        for art in (run.produced_artifacts or []):
            out.append({
                "query_id": str(run.query_id),
                "run_started_at": run.started_at.isoformat() if run.started_at else None,
                "task_id": art.get("task_id"),
                "rel_path": art.get("rel_path"),
                "bytes": art.get("bytes"),
            })
    return out


def get_latest_prior_run_artifacts(
    db: Session, workspace_id: str, exclude_query_id: UUID
) -> list[dict]:
    """Return the produced_artifacts of the most recent prior run in this
    workspace, excluding the current run. Empty list if there is no prior run
    or it produced nothing. Used to seed a continuation run's workspace."""
    row = db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.query_id != exclude_query_id,
        )
        .order_by(desc(QueryRun.started_at))
        .limit(1)
    ).first()
    if row is None or not row.produced_artifacts:
        return []
    return list(row.produced_artifacts)


def list_workspace_produced_artifacts(
    db: Session, workspace_id: str, user_id: UUID
) -> list[dict]:
    """Flatten every run's produced_artifacts in this workspace into one list,
    newest run first. Each entry is the stored
    {rel_path, content_b64, bytes, task_id} dict, augmented with the owning
    run's query_id (as 'run_id') and its started_at so the route can build URLs
    and a modified_at without a second lookup. Scoped by user_id."""
    rows = db.scalars(
        select(QueryRun)
        .where(
            QueryRun.workspace_id == workspace_id,
            QueryRun.user_id == user_id,
        )
        .order_by(desc(QueryRun.started_at))
    )
    out: list[dict] = []
    for run in rows:
        for art in (run.produced_artifacts or []):
            out.append({
                **art,
                "run_id": str(run.query_id),
                "run_started_at": run.started_at.isoformat(),
            })
    return out


def get_run_produced_artifacts(
    db: Session, workspace_id: str, query_id: UUID, user_id: UUID
) -> list[dict] | None:
    """Produced artifacts for a single run (same augmented shape as
    list_workspace_produced_artifacts). Returns None if the run doesn't exist
    for this user (so the route can 404), or [] if it produced nothing."""
    run = db.scalar(
        select(QueryRun).where(
            QueryRun.query_id == query_id,
            QueryRun.workspace_id == workspace_id,
            QueryRun.user_id == user_id,
        )
    )
    if run is None:
        return None
    return [
        {
            **art,
            "run_id": str(run.query_id),
            "run_started_at": run.started_at.isoformat(),
        }
        for art in (run.produced_artifacts or [])
    ]


def get_run_artifact_bytes(
    db: Session, workspace_id: str, query_id: UUID, user_id: UUID, rel_path: str
) -> tuple[bytes, dict] | None:
    """Return (raw_bytes, artifact_dict) for one produced file of a run, decoded
    from its stored base64. None if the run or the file isn't found for this
    user. Backs the file download/preview route."""
    run = db.scalar(
        select(QueryRun).where(
            QueryRun.query_id == query_id,
            QueryRun.workspace_id == workspace_id,
            QueryRun.user_id == user_id,
        )
    )
    if run is None:
        return None
    for art in (run.produced_artifacts or []):
        if art.get("rel_path") == rel_path:
            content_b64 = art.get("content_b64")
            if content_b64 is None:
                return None
            return base64.b64decode(content_b64), art
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

def get_ready_docID_by_hash(db: Session, workspace_id: str, content_hash: str) -> str | None:
    """Return the doc_id of a fully-ingested ('ready') doc in this workspace
    whose original file bytes match content_hash, or None. Workspace-scoped
    (NOT subdir/path-scoped) so a doc ingested in an earlier chat run of the
    same workspace is reused instead of re-ingested. Matching on the sha256 of
    the bytes (not the filename) means same-name-different-content PDFs are
    treated as distinct, and same-content-different-name PDFs are deduped.
    Picks the most recently created match if several exist."""
    row = db.query(Doc.doc_id).filter(
        Doc.workspace_id == workspace_id,
        Doc.content_hash == content_hash,
        Doc.status == "ready",
    ).order_by(Doc.created_at.desc()).first()
    return row[0] if row else None

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
