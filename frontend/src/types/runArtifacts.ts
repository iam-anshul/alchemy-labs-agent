import type { WorkspaceOutput } from "./api";
import type { Artifact, RunEvent } from "./events";

export interface RunArtifactEntry {
  id: string;
  artifact: Artifact;
  eventId: string | null;
  artifactIndex: number;
}

export function collectRunArtifacts(
  events: RunEvent[],
  savedOutputs: WorkspaceOutput[],
): RunArtifactEntry[] {
  const entriesByPath = new Map<string, RunArtifactEntry>();

  for (const event of events) {
    event.artifacts.forEach((artifact, artifactIndex) => {
      if (!isProducedArtifact(artifact)) return;
      const id = artifact.path ?? `${event.id}:${artifactIndex}`;
      entriesByPath.set(id, {
        id,
        artifact,
        eventId: event.id,
        artifactIndex,
      });
    });
  }

  for (const output of savedOutputs) {
    entriesByPath.set(output.relative_path, {
      id: output.relative_path,
      artifact: workspaceOutputToArtifact(output),
      eventId: null,
      artifactIndex: 0,
    });
  }

  return [...entriesByPath.values()];
}

export function workspaceOutputToArtifact(output: WorkspaceOutput): Artifact {
  const extension = output.filename.split(".").pop()?.toLowerCase() ?? null;
  return {
    kind: extension === "md" ? "markdown" : "file",
    path: output.relative_path,
    filename: output.filename,
    type: extension,
    mime_type: output.mime_type,
    bytes: output.bytes,
    content: null,
    content_base64: null,
    url: output.preview_url,
    metadata: {
      download_url: output.download_url,
      task_id: output.task_id,
      modified_at: output.modified_at,
    },
  };
}

function isProducedArtifact(artifact: Artifact): boolean {
  return Boolean(
    artifact.path?.startsWith("outputs/")
    || artifact.metadata.download_url
    || (
      artifact.kind === "file"
      && artifact.path
    ),
  );
}
