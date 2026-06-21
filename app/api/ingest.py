from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.api.events import EventSink, bus
from app.core.config import get_settings
from app.db import SessionLocal, utils
from app.ingestion.parsing import parse_document_async
from app.ingestion.tree import build_tree

log = logging.getLogger(__name__)

_ingest_queue: asyncio.Queue[str] = asyncio.Queue()
_worker_tasks: list[asyncio.Task[None]] = []


def _persist_parsed(
    db: Any,
    *,
    doc_id: str,
    workspace_id: str,
    parsed: Any,
) -> None:
    tables_by_page: dict[int, list[str]] = {}
    for tbl in parsed.tables:
        tables_by_page.setdefault(tbl.page, []).append(tbl.table_id)
        cols = [str(c) for c in tbl.rows[0]] if tbl.rows else []
        utils.create_table(
            db,
            table_id=tbl.table_id,
            workspace_id=workspace_id,
            doc_id=doc_id,
            source_page=tbl.page,
            row_count=tbl.n_rows,
            columns_json=cols,
            xlsx_bytes=tbl.xlsx_bytes,
        )

    pages_payload: list[dict[str, Any]] = []
    for i, text in enumerate(parsed.pages_text, start=1):
        pages_payload.append({
            "workspace_id": workspace_id,
            "doc_id": doc_id,
            "page_n": i,
            "prose_text": text,
            "table_ids": tables_by_page.get(i, []),
        })
    utils.bulk_create_pages(db, pages_payload)

    doc = utils.get_doc(db, doc_id)
    if doc is not None:
        doc.n_pages = parsed.n_pages
        doc.n_tables = len(parsed.tables)
        doc.status = "building_tree"
        db.commit()


async def _process_ingest(doc_id: str) -> None:
    channel_id = f"ingest:{doc_id}"
    sink = EventSink(bus=bus, channel_id=channel_id)
    stage = "lookup"

    try:
        with SessionLocal() as db:
            doc = utils.get_doc(db, doc_id)
            if doc is None:
                log.warning("ingest: doc %s not found", doc_id)
                return
            if doc.status != "queued":
                log.info("ingest: doc %s status=%s, skipping", doc_id, doc.status)
                return
            source_path = doc.source_path
            workspace_id = doc.workspace_id

        if not source_path:
            raise ValueError(f"doc {doc_id} has no source_path")

        stage = "parse"
        parsed = await parse_document_async(source_path, sink=sink)

        stage = "persist"
        with SessionLocal() as db:
            _persist_parsed(db, doc_id=doc_id, workspace_id=workspace_id, parsed=parsed)

        stage = "build_tree"
        with SessionLocal() as db:
            root_id = await build_tree(doc_id, workspace_id, db, sink=sink)

        with SessionLocal() as db:
            doc = utils.get_doc(db, doc_id)
            if doc is not None:
                doc.status = "ready"
                db.commit()

        await sink.publish("complete", {"doc_id": doc_id, "root_id": root_id, "status": "ready"})
    except Exception as e:
        log.exception("ingest failed for doc %s at stage %s", doc_id, stage)
        with SessionLocal() as db:
            doc = utils.get_doc(db, doc_id)
            if doc is not None:
                doc.status = "failed"
                db.commit()
        await sink.publish("error", {
            "stage": stage,
            "error_class": type(e).__name__,
            "message": str(e),
        })
        await sink.publish("complete", {"doc_id": doc_id, "status": "failed"})
    finally:
        bus.close(channel_id)


async def ingest_worker() -> None:
    while True:
        doc_id = await _ingest_queue.get()
        try:
            await _process_ingest(doc_id)
        finally:
            _ingest_queue.task_done()


def enqueue_ingest(doc_id: str) -> None:
    _ingest_queue.put_nowait(doc_id)


def start_workers(n: int | None = None) -> None:
    count = n if n is not None else get_settings().api_ingest_workers
    for _ in range(count):
        _worker_tasks.append(asyncio.create_task(ingest_worker()))
    log.info("Started %d ingest worker(s)", count)


async def shutdown_workers() -> None:
    """Drain the queue, then cancel the worker tasks. Call from whichever
    entrypoint started them (api/app.py lifespan, main.py finally block, etc.)
    before that entrypoint exits — otherwise the worker tasks dangle and any
    docs still in `queued` state never finish processing.

    Order matters: join() first so already-queued items get processed; then
    cancel() to break the workers out of their `await queue.get()` loop;
    then gather() so the cancellations actually settle before we return."""
    if not _worker_tasks:
        return
    log.info("Waiting for ingest queue to drain (%d pending)...", _ingest_queue.qsize())
    await _ingest_queue.join()
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks.clear()
    log.info("Ingest workers shut down")
