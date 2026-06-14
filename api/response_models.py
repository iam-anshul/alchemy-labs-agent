from pydantic import BaseModel

class FileInfo(BaseModel):
    doc_title: str | None
    doc_summary: str | None


class WorkspaceRunInfo(BaseModel):
    """One row of a workspace's run history, shaped to match the frontend
    WorkspaceRun type (see frontend/src/types/api.ts). started_at is serialized
    as an ISO-8601 string."""
    query_id: str
    workspace_id: str
    user_query: str
    status: str
    started_at: str
    query_counter: int
    todo_md: str | None


class WorkspaceOutput(BaseModel):
    """One produced file from a run, shaped to match the frontend
    WorkspaceOutput type (see frontend/src/types/api.ts). preview_url and
    download_url point at the file-serving route; the frontend uses them
    directly as link hrefs."""
    run_id: str
    task_id: str | None
    filename: str
    relative_path: str
    bytes: int
    mime_type: str | None
    modified_at: str
    preview_url: str
    download_url: str