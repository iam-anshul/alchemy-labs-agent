from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from app.api.events import bus
from app.api.ingest import enqueue_ingest
from app.api.schemas import DocumentAcceptedResponse, DocumentResponse
from app.core.config import get_settings
from app.db import SessionLocal, utils
from app.core.shared import sse_stream

from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

document_router = APIRouter(prefix="/v1/workspaces/{ws_id}/documents", tags=["documents"])

# Internal, non-HTTP helpers shared by the route handlers below and the doc
# sub-agent's tools. Each opens its own short-lived DB session and validates
# ORM rows into DocumentResponse *while the session is still open* so the
# returned object is safe to use after the session closes (no lazy-load on
# detached instance).
def ingest_local_file(local_path: Path, workspace_id: str, user_id: str) -> str:
    settings = get_settings()
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    upload_dir = Path(settings.api_upload_dir) / workspace_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{doc_id}{local_path.suffix}"
    raw = local_path.read_bytes()
    saved_path.write_bytes(raw)
    content_hash = hashlib.sha256(raw).hexdigest()

    with SessionLocal() as db:
        utils.create_doc(
            db, doc_id=doc_id, workspace_id=workspace_id,
            uploaded_by_user_id=user_id, title=local_path.name,
            source_path=str(saved_path), content_hash=content_hash,
            status="queued",
        )
    enqueue_ingest(doc_id)
    return doc_id


def list_local_documents(workspace_id: str) -> list[DocumentResponse]:
    """List all documents in a workspace. Returns Pydantic responses so the
    caller doesn't depend on the ORM session staying open."""
    with SessionLocal() as db:
        docs = utils.list_docs(db, workspace_id)
        return [DocumentResponse.model_validate(d) for d in docs]


def get_local_document(workspace_id: str, doc_id: str) -> DocumentResponse | None:
    """Fetch a single document by id, scoped to the given workspace. Returns
    None if the doc doesn't exist or belongs to a different workspace."""
    with SessionLocal() as db:
        doc = utils.get_doc(db, doc_id)
        if doc is None or doc.workspace_id != workspace_id:
            return None
        return DocumentResponse.model_validate(doc)

@document_router.post("", status_code=202, response_model=DocumentAcceptedResponse)
async def upload_document(
    ws_id: str,
    file: UploadFile,
    session: SessionContainer = Depends(verify_session()),
) -> DocumentAcceptedResponse:
    """Upload a document for async ingestion (parse + tree build)."""
    current_user = session.get_user_id()

    settings = get_settings()
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    ext = Path(file.filename or "upload").suffix
    upload_dir = Path(settings.api_upload_dir) / ws_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{doc_id}{ext}"

    content = await file.read()
    saved_path.write_bytes(content)
    content_hash = hashlib.sha256(content).hexdigest()

    with SessionLocal() as db:
        utils.create_doc(
            db,
            doc_id=doc_id,
            workspace_id=ws_id,
            uploaded_by_user_id=current_user,
            title=file.filename,
            source_path=str(saved_path),
            content_hash=content_hash,
            status="queued",
        )

    enqueue_ingest(doc_id)
    stream_url = f"/v1/workspaces/{ws_id}/documents/{doc_id}/stream"
    return DocumentAcceptedResponse(doc_id=doc_id, status="queued", stream_url=stream_url)

@document_router.get("", response_model=list[DocumentResponse])
async def list_documents(
    ws_id: str,
    session: SessionContainer = Depends(verify_session()),
) -> list[DocumentResponse]:
    """List all documents in a workspace."""
    with SessionLocal() as db:
        docs = utils.list_docs(db, ws_id)
    return [DocumentResponse.model_validate(d) for d in docs]


@document_router.get("/{doc_id}", response_model=DocumentResponse)
async def get_document(
    ws_id: str,
    doc_id: str,
    session: SessionContainer = Depends(verify_session()),
) -> DocumentResponse:
    """Get a single document by id."""
    doc = get_local_document(ws_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@document_router.get("/{doc_id}/stream")
async def stream_document_ingest(
    ws_id: str,
    doc_id: str,
    request: Request,
    session: SessionContainer = Depends(verify_session()),
):
    """SSE stream of ingest progress for a document."""
    with SessionLocal() as db:
        doc = utils.get_doc(db, doc_id)
    if doc is None or doc.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return sse_stream(bus, f"ingest:{doc_id}", request)
