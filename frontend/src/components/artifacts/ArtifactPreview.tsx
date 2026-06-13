import { Download, FileText, Image, Table2 } from "lucide-react";

import type { Artifact, RunEvent } from "../../types/events";
import { formatBytes } from "../../utils/format";
import { getActivityPresentation } from "../runs/activityPresentation";
import "./ArtifactPreview.css";

interface ArtifactPreviewProps {
  event: RunEvent | null;
}

export default function ArtifactPreview({ event }: ArtifactPreviewProps) {
  const artifact = event?.artifacts[0] ?? null;
  const activity = event ? getActivityPresentation(event) : null;

  return (
    <section className="artifact-panel">
      <header className="artifact-panel__header">
        <span className={`activity-label activity-label--${activity?.tone ?? "slate"}`}>
          {activity?.label ?? "Run"}
        </span>
        <strong>{artifact?.filename ?? "Live focus"}</strong>
        <span>{artifact?.kind.replaceAll("_", " ") ?? "status"}</span>
      </header>
      <div className="artifact-panel__body">
        {!artifact && <StatusPreview event={event} />}
        {artifact && <ArtifactContent artifact={artifact} />}
      </div>
    </section>
  );
}

function ArtifactContent({ artifact }: { artifact: Artifact }) {
  if (artifact.kind === "screenshot") {
    if (!artifact.content_base64 && !artifact.url) {
      return <FallbackPreview icon={<Image />} artifact={artifact} />;
    }
    const source = artifact.content_base64
      ? `data:${artifact.mime_type ?? "image/png"};base64,${artifact.content_base64}`
      : artifact.url ?? "";
    return (
      <div className="screenshot-preview">
        <div className="screenshot-preview__chrome"><i /><i /><i /><span>Live browser capture</span></div>
        <img src={source} alt={artifact.filename ?? "Browser capture"} />
      </div>
    );
  }

  if (artifact.kind === "markdown" || artifact.kind === "final_answer") {
    return (
      <article className="document-preview">
        <pre>{artifact.content ?? "This document is ready."}</pre>
      </article>
    );
  }

  if (artifact.kind === "extracted_content") {
    return (
      <article className="data-preview">
        <Table2 size={22} />
        <h3>{artifact.filename ?? "Extracted findings"}</h3>
        <pre>{JSON.stringify(artifact.metadata, null, 2)}</pre>
      </article>
    );
  }

  if (artifact.kind === "file") {
    const downloadUrl = getArtifactDownloadUrl(artifact);
    return (
      <div className="file-preview">
        <span className="file-preview__badge">
          {(artifact.type ?? "file").toUpperCase()}
        </span>
        <h3>{artifact.filename ?? "Produced file"}</h3>
        <p>{artifact.path}</p>
        {artifact.bytes !== null && <span>{formatBytes(artifact.bytes)}</span>}
        {downloadUrl && (
          <a className="button button--outline" href={downloadUrl}>
            <Download size={14} /> Download
          </a>
        )}
      </div>
    );
  }

  return <FallbackPreview icon={<FileText />} artifact={artifact} />;
}

export function getArtifactDownloadUrl(artifact: Artifact): string | null {
  if (!artifact.url) return null;
  if (artifact.url.startsWith("/")) return artifact.url;

  try {
    const parsed = new URL(artifact.url);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? artifact.url
      : null;
  } catch {
    return null;
  }
}

function StatusPreview({ event }: { event: RunEvent | null }) {
  return (
    <div className="status-preview">
      <span className="status-preview__pulse" />
      <h2>{event?.message ?? "Waiting for the run to start"}</h2>
      <p>{event?.status === "failed" ? "This step failed." : "Work is in progress."}</p>
    </div>
  );
}

function FallbackPreview({
  icon,
  artifact,
}: {
  icon: React.ReactNode;
  artifact: Artifact;
}) {
  return (
    <div className="status-preview">
      <span className="status-preview__icon">{icon}</span>
      <h2>{artifact.filename ?? "Artifact ready"}</h2>
      <p>This artifact type does not have an inline preview yet.</p>
    </div>
  );
}
