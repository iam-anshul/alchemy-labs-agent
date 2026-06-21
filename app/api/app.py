from __future__ import annotations

# Import first so Logfire is configured and pydantic-ai is instrumented before
# any agent modules (imported transitively via the routes) are loaded.
import app.core.observability as observability  # noqa: F401

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ingest
from app.api.routes import documents, health, queries, reports
from app.core.config import get_settings

log = logging.getLogger(__name__)


def _ensure_dirs() -> None:
    settings = get_settings()
    for path in (
        settings.api_upload_dir,
        settings.workspace_output_dir,
        settings.report_output_dir,
    ):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Could not create directory %s: %s", path, e)
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        try:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Could not create database directory for %s: %s", db_path, e)


def _run_migrations() -> None:
    _ensure_dirs()
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("Starting doc-reasoner API (upload_dir=%s)", settings.api_upload_dir)
    _run_migrations()
    ingest.start_workers(settings.api_ingest_workers)
    yield
    log.info("Shutting down doc-reasoner API")


app = FastAPI(title="Doc Reasoner API", lifespan=lifespan)

# Trace incoming requests so each agent run is nested under the API call that
# triggered it. No-op if Logfire wasn't configured (no token).
observability.instrument_fastapi_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(documents.router)
app.include_router(queries.router)
app.include_router(reports.router)
