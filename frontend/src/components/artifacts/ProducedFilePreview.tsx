import DOMPurify from "dompurify";
import { AlertCircle, Download, FileText, LoaderCircle } from "lucide-react";
import Papa from "papaparse";
import { useEffect, useRef, useState } from "react";

import type { Artifact } from "../../types/events";
import { formatBytes } from "../../utils/format";
import MarkdownPreview from "./MarkdownPreview";
import "./ProducedFilePreview.css";

type PreviewKind =
  | "markdown"
  | "text"
  | "json"
  | "csv"
  | "xlsx"
  | "image"
  | "pdf"
  | "docx"
  | "pptx"
  | "audio"
  | "video"
  | "download";

interface ProducedFilePreviewProps {
  artifact: Artifact;
}

export default function ProducedFilePreview({
  artifact,
}: ProducedFilePreviewProps) {
  const previewKind = getPreviewKind(artifact);

  if (previewKind === "markdown" && artifact.content !== null) {
    return (
      <MarkdownPreview
        content={artifact.content}
        filename={artifact.filename}
      />
    );
  }
  if (previewKind === "image" && artifact.url) {
    return <img className="produced-image" src={artifact.url} alt={artifact.filename ?? "Produced image"} />;
  }
  if (previewKind === "pdf" && artifact.url) {
    return <iframe className="produced-frame" src={artifact.url} title={artifact.filename ?? "PDF preview"} />;
  }
  if (previewKind === "audio" && artifact.url) {
    return <audio className="produced-media" src={artifact.url} controls />;
  }
  if (previewKind === "video" && artifact.url) {
    return <video className="produced-video" src={artifact.url} controls />;
  }
  if (previewKind === "docx") {
    return <DocxPreview artifact={artifact} />;
  }
  if (previewKind === "xlsx") {
    return <WorkbookPreview artifact={artifact} />;
  }
  if (previewKind === "pptx") {
    return <PresentationPreview artifact={artifact} />;
  }
  if (previewKind === "text" || previewKind === "json" || previewKind === "csv" || previewKind === "markdown") {
    return <TextFilePreview artifact={artifact} kind={previewKind} />;
  }
  return <DownloadFallback artifact={artifact} />;
}

export function getPreviewKind(artifact: Artifact): PreviewKind {
  const mimeType = artifact.mime_type?.toLowerCase() ?? "";
  const extension = (
    artifact.type
    ?? artifact.filename?.split(".").pop()
    ?? ""
  ).toLowerCase();

  if (artifact.kind === "markdown" || extension === "md" || mimeType === "text/markdown") return "markdown";
  if (mimeType === "application/json" || extension === "json") return "json";
  if (mimeType === "text/csv" || extension === "csv") return "csv";
  if (extension === "xlsx" || extension === "xlsm") return "xlsx";
  if (extension === "docx") return "docx";
  if (extension === "pptx") return "pptx";
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType === "application/pdf" || extension === "pdf") return "pdf";
  if (mimeType.startsWith("audio/")) return "audio";
  if (mimeType.startsWith("video/")) return "video";
  if (mimeType.startsWith("text/") || ["txt", "log", "xml", "yaml", "yml"].includes(extension)) return "text";
  return "download";
}

function TextFilePreview({
  artifact,
  kind,
}: {
  artifact: Artifact;
  kind: "markdown" | "text" | "json" | "csv";
}) {
  const remote = useRemoteText(artifact);
  if (remote.isLoading) return <PreviewLoading />;
  if (remote.error) return <DownloadFallback artifact={artifact} error={remote.error} />;
  const content = remote.content ?? "";

  if (kind === "markdown") {
    return <MarkdownPreview content={content} filename={artifact.filename} />;
  }
  if (kind === "json") {
    let formatted = content;
    try {
      formatted = JSON.stringify(JSON.parse(content), null, 2);
    } catch {
      // Invalid JSON is still useful as plain text.
    }
    return <pre className="produced-code">{formatted}</pre>;
  }
  if (kind === "csv") {
    const parsed = Papa.parse<string[]>(content, { skipEmptyLines: true });
    return <DataTable rows={parsed.data} />;
  }
  return <pre className="produced-code produced-code--text">{content}</pre>;
}

function DocxPreview({ artifact }: ProducedFilePreviewProps) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setHtml(null);
    setError(null);
    void (async () => {
      try {
        const arrayBuffer = await fetchArtifact(artifact, controller.signal);
        const mammoth = await import("mammoth");
        const result = await mammoth.convertToHtml({ arrayBuffer });
        setHtml(DOMPurify.sanitize(result.value));
      } catch (loadError) {
        if (!controller.signal.aborted) setError(errorMessage(loadError));
      }
    })();
    return () => controller.abort();
  }, [artifact]);

  if (error) return <DownloadFallback artifact={artifact} error={error} />;
  if (html === null) return <PreviewLoading />;
  return <article className="office-document" dangerouslySetInnerHTML={{ __html: html }} />;
}

function WorkbookPreview({ artifact }: ProducedFilePreviewProps) {
  const [sheets, setSheets] = useState<Array<{ sheet: string; data: unknown[][] }>>([]);
  const [activeSheet, setActiveSheet] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSheets([]);
    setActiveSheet(0);
    setError(null);
    void (async () => {
      try {
        const arrayBuffer = await fetchArtifact(artifact, controller.signal);
        const { default: readXlsxFile } = await import("read-excel-file/browser");
        const workbook = await readXlsxFile(new Blob([arrayBuffer]));
        setSheets(workbook);
      } catch (loadError) {
        if (!controller.signal.aborted) setError(errorMessage(loadError));
      }
    })();
    return () => controller.abort();
  }, [artifact]);

  if (error) return <DownloadFallback artifact={artifact} error={error} />;
  if (sheets.length === 0) return <PreviewLoading />;
  return (
    <section className="workbook-preview">
      <div className="workbook-preview__tabs">
        {sheets.map((sheet, index) => (
          <button
            type="button"
            data-active={activeSheet === index}
            key={sheet.sheet}
            onClick={() => setActiveSheet(index)}
          >
            {sheet.sheet}
          </button>
        ))}
      </div>
      <DataTable rows={sheets[activeSheet]?.data ?? []} />
    </section>
  );
}

function PresentationPreview({ artifact }: ProducedFilePreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    const container = containerRef.current;
    if (!container) return;
    container.replaceChildren();
    setError(null);
    setIsLoading(true);
    void (async () => {
      try {
        const arrayBuffer = await fetchArtifact(artifact, controller.signal);
        const { init } = await import("pptx-preview");
        if (controller.signal.aborted) return;
        const previewer = init(container, { width: 960, height: 540 });
        await previewer.preview(arrayBuffer);
        if (!controller.signal.aborted) setIsLoading(false);
      } catch (loadError) {
        if (!controller.signal.aborted) {
          setError(errorMessage(loadError));
          setIsLoading(false);
        }
      }
    })();
    return () => {
      controller.abort();
      container.replaceChildren();
    };
  }, [artifact]);

  if (error) return <DownloadFallback artifact={artifact} error={error} />;
  return (
    <div className="presentation-preview-shell">
      {isLoading && <PreviewLoading />}
      <div className="presentation-preview" ref={containerRef} />
    </div>
  );
}

function DataTable({ rows }: { rows: unknown[][] }) {
  return (
    <div className="data-table-wrap">
      <table className="produced-table">
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((value, columnIndex) => {
                const Cell = rowIndex === 0 ? "th" : "td";
                return <Cell key={columnIndex}>{formatCell(value)}</Cell>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DownloadFallback({
  artifact,
  error,
}: ProducedFilePreviewProps & { error?: string }) {
  const downloadUrl = getDownloadUrl(artifact);
  return (
    <div className="file-preview">
      <span className="file-preview__badge">{(artifact.type ?? "file").toUpperCase()}</span>
      <h3>{artifact.filename ?? "Produced file"}</h3>
      {error ? <p className="preview-error"><AlertCircle size={14} /> {error}</p> : <p>{artifact.path}</p>}
      {artifact.bytes !== null && <span>{formatBytes(artifact.bytes)}</span>}
      {downloadUrl && (
        <a className="button button--outline" href={downloadUrl}>
          <Download size={14} /> Download
        </a>
      )}
    </div>
  );
}

function PreviewLoading() {
  return (
    <div className="preview-loading">
      <LoaderCircle className="spin" size={20} />
      <span>Preparing preview...</span>
    </div>
  );
}

function useRemoteText(artifact: Artifact) {
  const [content, setContent] = useState<string | null>(artifact.content);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(artifact.content === null);

  useEffect(() => {
    if (artifact.content !== null) {
      setContent(artifact.content);
      setIsLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setContent(null);
    setIsLoading(true);
    setError(null);
    void fetchArtifactResponse(artifact, controller.signal)
      .then((response) => response.text())
      .then(setContent)
      .catch((loadError) => {
        if (!controller.signal.aborted) setError(errorMessage(loadError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false);
      });
    return () => controller.abort();
  }, [artifact]);

  return { content, error, isLoading };
}

async function fetchArtifact(artifact: Artifact, signal: AbortSignal) {
  const response = await fetchArtifactResponse(artifact, signal);
  return response.arrayBuffer();
}

async function fetchArtifactResponse(artifact: Artifact, signal: AbortSignal) {
  if (!artifact.url) throw new Error("No preview URL is available");
  const response = await fetch(artifact.url, {
    credentials: "include",
    signal,
  });
  if (!response.ok) throw new Error(`Preview failed with status ${response.status}`);
  return response;
}

export function getDownloadUrl(artifact: Artifact): string | null {
  const metadataUrl = artifact.metadata.download_url;
  if (typeof metadataUrl === "string") return metadataUrl;
  return artifact.url;
}

function formatCell(value: unknown) {
  if (value instanceof Date) return value.toLocaleString();
  if (value === null || value === undefined) return "";
  return String(value);
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Could not render this file";
}
