import { Download, FileText, Globe, Image, Search, Table2 } from "lucide-react";

import type { Artifact, RunEvent } from "../../types/events";
import { formatBytes } from "../../utils/format";
import { getActivityPresentation } from "../runs/activityPresentation";
import "./ArtifactPreview.css";
import MarkdownPreview from "./MarkdownPreview";

interface ArtifactPreviewProps {
  event: RunEvent | null;
}

export default function ArtifactPreview({ event }: ArtifactPreviewProps) {
  const artifact = event?.artifacts[0] ?? null;
  const activity = event ? getActivityPresentation(event) : null;
  const pageActivity = event ? parsePageActivity(event) : null;

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
        {!artifact && pageActivity && <PageActivityPreview page={pageActivity} />}
        {!artifact && !pageActivity && <StatusPreview event={event} />}
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
      <MarkdownPreview
        content={artifact.content ?? "This document is ready."}
        filename={artifact.filename}
      />
    );
  }

  if (artifact.kind === "extracted_content") {
    const content = artifact.content ?? JSON.stringify(artifact.metadata, null, 2);
    return (
      <article className="data-preview">
        <Table2 size={22} />
        <h3>{artifact.filename ?? "Extracted findings"}</h3>
        <pre>{content}</pre>
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
  text: string | null;
}

interface PageActivity {
  kind: "search" | "page";
  query: string | null;
  depth: string | null;
  url: string | null;
  title: string | null;
  content: string | null;
  sources: SearchSource[];
}

export function parsePageActivity(event: RunEvent): PageActivity | null {
  if (event.agent_type !== "web_search" && event.agent_type !== "browser") {
    return null;
  }
  const data = event.data ?? {};
  const query = typeof data.query === "string" ? data.query : null;
  const url = typeof data.url === "string" ? data.url : null;
  const title = typeof data.title === "string" ? data.title : null;
  const depth = typeof data.depth === "string" ? data.depth : null;
  const content = firstString(
    data.content,
    data.text,
    data.page_content,
    data.answer,
  );
  const rawSources = Array.isArray(data.sources) ? data.sources : [];
  const sources: SearchSource[] = rawSources.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) return [];
    const record = entry as Record<string, unknown>;
    if (typeof record.url !== "string") return [];
    return [{
      url: record.url,
      title: typeof record.title === "string" ? record.title : null,
      text: firstString(record.text, record.snippet),
    }];
  });
  if (!query && !url && !title && !content && sources.length === 0) return null;
  return {
    kind: query || sources.length > 0 ? "search" : "page",
    query,
    depth,
    url,
    title,
    content,
    sources,
  };
}

function firstString(...values: unknown[]): string | null {
  const value = values.find((candidate) =>
    typeof candidate === "string" && candidate.trim().length > 0
  );
  return typeof value === "string" ? value : null;
}

function PageActivityPreview({ page }: { page: PageActivity }) {
  return (
    <div className="search-preview">
      <div className="search-preview__query">
        {page.kind === "search" ? <Search size={16} /> : <Globe size={16} />}
        <div>
          <h3>{page.query ?? page.title ?? page.url ?? "Web page"}</h3>
          {page.url && (
            <a
              className="search-preview__url"
              href={page.url}
              target="_blank"
              rel="noreferrer"
            >
              {page.url}
            </a>
          )}
          {page.depth && (
            <span className="search-preview__depth">{page.depth} search</span>
          )}
        </div>
      </div>
      {page.content && (
        <div className="search-preview__content">
          <MarkdownPreview content={page.content} />
        </div>
      )}
      {page.sources.length > 0 && (
        <ul className="search-preview__sources">
          {page.sources.map((source, index) => (
            <li key={`${source.url}-${index}`}>
              <Globe size={12} />
              <div>
                <a href={source.url} target="_blank" rel="noreferrer">
                  {source.title ?? source.url}
                </a>
                {source.text && <p>{source.text}</p>}
              </div>
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
