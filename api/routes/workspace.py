from db import utils as db_utils
from fastapi import Depends, APIRouter, HTTPException
from api.routes.chat import make_workspace
from db import SessionLocal
from supertokens_python.recipe.session.framework.fastapi import verify_session
from supertokens_python.recipe.session import SessionContainer
import shutil
from pathlib import Path
from api.response_models import FileInfo

workspace_router = APIRouter()

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