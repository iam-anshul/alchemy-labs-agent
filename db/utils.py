"""CRUD helpers for all ORM models.

Every function takes an explicit SQLAlchemy ``Session`` as its first argument
and commits within that session.  The caller manages session lifecycle.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from db.models import Doc, ExtractedTable, Node, Page, Query, Report


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
