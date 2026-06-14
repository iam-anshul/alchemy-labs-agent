import base64
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from api.response_models import WorkspaceOutput


def output_url(
    workspace_id: str,
    run_id: UUID,
    relative_path: str,
    disposition: str,
) -> str:
    public_path = relative_path.removeprefix("outputs/")
    return (
        f"/workspace/{quote(workspace_id, safe='')}/runs/{run_id}/outputs/"
        f"{quote(public_path, safe='/')}?disposition={disposition}"
    )


def database_outputs(run) -> list[WorkspaceOutput]:
    outputs: list[WorkspaceOutput] = []
    for artifact in run.produced_artifacts or []:
        relative_path = artifact.get("rel_path")
        content_base64 = artifact.get("content_b64")
        if not relative_path or content_base64 is None:
            continue
        filename = artifact.get("filename") or Path(relative_path).name
        modified_value = artifact.get("modified_at")
        modified_at = (
            datetime.fromtimestamp(modified_value, tz=timezone.utc)
            if isinstance(modified_value, (int, float))
            else run.started_at
        )
        outputs.append(WorkspaceOutput(
            run_id=run.query_id,
            task_id=artifact.get("task_id"),
            filename=filename,
            relative_path=relative_path,
            bytes=artifact.get("bytes") or 0,
            mime_type=artifact.get("mime_type") or mimetypes.guess_type(filename)[0],
            modified_at=modified_at,
            preview_url=output_url(
                run.workspace_id, run.query_id, relative_path, "inline"
            ),
            download_url=output_url(
                run.workspace_id, run.query_id, relative_path, "attachment"
            ),
        ))
    return outputs


def filesystem_outputs(run) -> list[WorkspaceOutput]:
    output_dir = Path(run.workspace) / "outputs"
    if not output_dir.is_dir():
        return []
    outputs: list[WorkspaceOutput] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = f"outputs/{path.relative_to(output_dir).as_posix()}"
        stat = path.stat()
        outputs.append(WorkspaceOutput(
            run_id=run.query_id,
            task_id=None,
            filename=path.name,
            relative_path=relative_path,
            bytes=stat.st_size,
            mime_type=mimetypes.guess_type(str(path))[0],
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            preview_url=output_url(
                run.workspace_id, run.query_id, relative_path, "inline"
            ),
            download_url=output_url(
                run.workspace_id, run.query_id, relative_path, "attachment"
            ),
        ))
    return outputs


def run_outputs(run) -> list[WorkspaceOutput]:
    outputs = (
        database_outputs(run)
        if run.produced_artifacts is not None
        else filesystem_outputs(run)
    )
    outputs.sort(key=lambda output: output.modified_at, reverse=True)
    return outputs


def find_persisted_artifact(run, relative_path: str) -> dict | None:
    candidate_paths = {
        relative_path,
        f"outputs/{relative_path.removeprefix('outputs/')}",
    }
    for artifact in run.produced_artifacts or []:
        if artifact.get("rel_path") in candidate_paths:
            return artifact
    return None


def decode_artifact(artifact: dict) -> bytes:
    return base64.b64decode(artifact["content_b64"])
