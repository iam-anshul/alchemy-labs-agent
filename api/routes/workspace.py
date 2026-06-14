from db import utils as db_utils
from fastapi import Depends, APIRouter, HTTPException, Response
from urllib.parse import quote
import mimetypes
from api.routes.chat import make_workspace
from db import SessionLocal
from supertokens_python.recipe.session.framework.fastapi import verify_session
from supertokens_python.recipe.session import SessionContainer
import shutil
from pathlib import Path
from typing import List
from uuid import UUID
from api.response_models import FileInfo, WorkspaceRunInfo, WorkspaceOutput

workspace_router = APIRouter()


def _to_run_info(run) -> WorkspaceRunInfo:
    """Serialize a QueryRun ORM row to the WorkspaceRunInfo the frontend wants
    (started_at as an ISO string, query_id as str)."""
    return WorkspaceRunInfo(
        query_id=str(run.query_id),
        workspace_id=run.workspace_id,
        user_query=run.user_query,
        status=run.status,
        started_at=run.started_at.isoformat(),
        query_counter=run.query_counter,
        todo_md=run.todo_md,
    )


def _to_output(workspace_id: str, art: dict) -> WorkspaceOutput:
    """Serialize a stored produced-artifact dict (augmented with run_id /
    run_started_at by the db helper) into the WorkspaceOutput the frontend
    wants. preview_url/download_url point at the file-serving route below;
    rel_path segments are URL-encoded so paths with spaces/subdirs work."""
    rel_path = art["rel_path"]
    run_id = art["run_id"]
    filename = rel_path.rsplit("/", 1)[-1]
    encoded_path = "/".join(quote(seg) for seg in rel_path.split("/"))
    base = f"/workspace/{quote(workspace_id)}/runs/{run_id}/outputs/{encoded_path}"
    return WorkspaceOutput(
        run_id=run_id,
        task_id=art.get("task_id"),
        filename=filename,
        relative_path=rel_path,
        bytes=art.get("bytes", 0),
        mime_type=mimetypes.guess_type(filename)[0],
        modified_at=art.get("run_started_at", ""),
        preview_url=f"{base}?disposition=inline",
        download_url=base,
    )

@workspace_router.post("/create_workspace")
async def register__workspace(workspace_name: str, session: SessionContainer = Depends(verify_session())) -> str:
    with SessionLocal() as db:
        db_utils.create_workspace(
            db,
            workspace_id=workspace_name,
            user_id=session.get_user_id()
        )
    make_workspace(f"{Path.cwd()}/file_system_root/{workspace_name}")
    return workspace_name

@workspace_router.delete("/delete_workspace")
async def delete_workspace(
    workspace_name: str, session: SessionContainer = Depends(verify_session())
) -> str:
    """Delete a workspace and everything scoped to it — its QueryRuns and other
    workspace-scoped rows in the DB, plus its directory tree on disk.

    DB first, filesystem best-effort: the DB delete is the source of truth and
    commits in one transaction. If the rmtree afterwards fails (e.g. a
    permission error), it's logged and the request still succeeds — a leftover
    directory with no DB rows pointing at it is harmless.
    """
    user_id = session.get_user_id()
    with SessionLocal() as db:
        deleted = db_utils.delete_workspace(
            db, workspace_id=workspace_name, user_id=user_id
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Best-effort filesystem cleanup. ignore_errors swallows a missing dir (the
    # workspace may have had no runs yet) and partial-permission failures; the
    # DB is already authoritative at this point.
    workspace_path = Path(f"{Path.cwd()}/file_system_root/{workspace_name}")
    shutil.rmtree(workspace_path, ignore_errors=True)

    return workspace_name

@workspace_router.get("/list_workspace")
async def list_workspace(session: SessionContainer = Depends(verify_session())) -> list[str]:
    with SessionLocal() as db:
        return db_utils.get_all_workspaces(db, session.get_user_id())

@workspace_router.post("/list_files", response_model=dict[str, FileInfo])
async def list(
    workspace_id: str,
    session: SessionContainer = Depends(verify_session())
):
    with SessionLocal() as db:
        return db_utils.get_workspace_ingested_files(db, workspace_id, session.get_user_id())


@workspace_router.get("/{workspace_id}/runs", response_model=List[WorkspaceRunInfo])
async def list_workspace_runs(
    workspace_id: str,
    session: SessionContainer = Depends(verify_session())
) -> List[WorkspaceRunInfo]:
    """Run history for a workspace, newest first. Backs the "Recent runs" list
    on the workspace page (GET /workspace/{workspace_id}/runs)."""
    user_id = session.get_user_id()
    with SessionLocal() as db:
        runs = db_utils.list_workspace_runs(db, workspace_id=workspace_id, user_id=user_id)
        return [_to_run_info(run) for run in runs]


@workspace_router.get("/{workspace_id}/runs/{run_id}", response_model=WorkspaceRunInfo)
async def get_workspace_run(
    workspace_id: str,
    run_id: str,
    session: SessionContainer = Depends(verify_session())
) -> WorkspaceRunInfo:
    """Saved detail for a single run. Backs the run-detail page when it loads a
    past (already-finished) run that has no live SSE stream
    (GET /workspace/{workspace_id}/runs/{run_id})."""
    try:
        query_id = UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run not found")
    user_id = session.get_user_id()
    with SessionLocal() as db:
        run = db_utils.get_workspace_run(
            db, workspace_id=workspace_id, query_id=query_id, user_id=user_id
        )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _to_run_info(run)


@workspace_router.get("/{workspace_id}/outputs", response_model=List[WorkspaceOutput])
async def list_workspace_outputs(
    workspace_id: str,
    session: SessionContainer = Depends(verify_session())
) -> List[WorkspaceOutput]:
    """Every produced file across this workspace's runs, newest run first.
    Backs the "Produced" files tab (GET /workspace/{workspace_id}/outputs)."""
    user_id = session.get_user_id()
    with SessionLocal() as db:
        arts = db_utils.list_workspace_produced_artifacts(
            db, workspace_id=workspace_id, user_id=user_id
        )
    return [_to_output(workspace_id, art) for art in arts]


@workspace_router.get(
    "/{workspace_id}/runs/{run_id}/outputs", response_model=List[WorkspaceOutput]
)
async def list_run_outputs(
    workspace_id: str,
    run_id: str,
    session: SessionContainer = Depends(verify_session())
) -> List[WorkspaceOutput]:
    """Produced files for a single run. Backs the run-detail page's output
    shelf (GET /workspace/{workspace_id}/runs/{run_id}/outputs)."""
    try:
        query_id = UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run not found")
    user_id = session.get_user_id()
    with SessionLocal() as db:
        arts = db_utils.get_run_produced_artifacts(
            db, workspace_id=workspace_id, query_id=query_id, user_id=user_id
        )
    if arts is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [_to_output(workspace_id, art) for art in arts]


@workspace_router.get("/{workspace_id}/runs/{run_id}/outputs/{rel_path:path}")
async def download_run_output(
    workspace_id: str,
    run_id: str,
    rel_path: str,
    disposition: str = "attachment",
    session: SessionContainer = Depends(verify_session())
):
    """Serve the bytes of one produced file, decoded from its stored base64.
    disposition=inline lets the browser preview it; the default attachment
    triggers a download (GET /workspace/{ws}/runs/{run_id}/outputs/{rel_path})."""
    try:
        query_id = UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run not found")
    user_id = session.get_user_id()
    with SessionLocal() as db:
        found = db_utils.get_run_artifact_bytes(
            db,
            workspace_id=workspace_id,
            query_id=query_id,
            user_id=user_id,
            rel_path=rel_path,
        )
    if found is None:
        raise HTTPException(status_code=404, detail="file not found")
    raw, art = found
    filename = rel_path.rsplit("/", 1)[-1]
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # inline vs attachment: only honor the two known values, default to download.
    mode = "inline" if disposition == "inline" else "attachment"
    return Response(
        content=raw,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{mode}; filename="{filename}"',
        },
    )