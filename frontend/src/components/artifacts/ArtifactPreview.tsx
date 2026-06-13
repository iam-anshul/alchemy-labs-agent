import { Download, FileText, Globe, Image, Search, Table2 } from "lucide-react";

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
  const search = event ? parseSearch(event) : null;

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
        {!artifact && search && <SearchPreview search={search} />}
        {!artifact && !search && <StatusPreview event={event} />}
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

interface SearchSource {
  url: string;
  title: string | null;
}

interface ParsedSearch {
  query: string | null;
  depth: string | null;
  url: string | null;
  sources: SearchSource[];
}

/** Lift the web-search/fetch_url fields the backend puts on `event.data` into a
 * typed shape, or null when this isn't a web-search event. Mirrors the
 * `web_search` / `fetch_url` tool events emitted by web_agent.py. */
function parseSearch(event: RunEvent): ParsedSearch | null {
  if (event.agent_type !== "web_search" || event.stage !== "web_search") {
    return null;
  }
  const data = event.data ?? {};
  const query = typeof data.query === "string" ? data.query : null;
  const url = typeof data.url === "string" ? data.url : null;
  const depth = typeof data.depth === "string" ? data.depth : null;
  const rawSources = Array.isArray(data.sources) ? data.sources : [];
  const sources: SearchSource[] = rawSources.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) return [];
    const record = entry as Record<string, unknown>;
    if (typeof record.url !== "string") return [];
    return [{ url: record.url, title: typeof record.title === "string" ? record.title : null }];
  });
  // Only treat it as a search preview if there's something to show beyond the
  // message line StatusPreview already renders.
  if (!query && !url && sources.length === 0) return null;
  return { query, depth, url, sources };
}

function SearchPreview({ search }: { search: ParsedSearch }) {
  return (
    <div className="search-preview">
      <div className="search-preview__query">
        <Search size={16} />
        <div>
          {search.query && <h3>{search.query}</h3>}
          {search.url && !search.query && <h3>{search.url}</h3>}
          {search.depth && <span className="search-preview__depth">{search.depth} search</span>}
        </div>
      </div>
      {search.sources.length > 0 && (
        <ul className="search-preview__sources">
          {search.sources.map((source, index) => (
            <li key={`${source.url}-${index}`}>
              <Globe size={12} />
              <a href={source.url} target="_blank" rel="noreferrer">
                {source.title ?? source.url}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
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
