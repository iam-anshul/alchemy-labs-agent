import { Download, FileText, Upload } from "lucide-react";
import { useRef, useState } from "react";

import type { WorkspaceDocument, WorkspaceOutput } from "../../types/api";
import { formatBytes, formatRelativeTime } from "../../utils/format";
import "./FileList.css";

interface FileListProps {
  documents: WorkspaceDocument[];
  outputs: WorkspaceOutput[];
  areOutputsAvailable: boolean;
  isUploading: boolean;
  onUpload: (files: File[]) => Promise<void>;
}

export default function FileList({
  documents,
  outputs,
  areOutputsAvailable,
  isUploading,
  onUpload,
}: FileListProps) {
  const [tab, setTab] = useState<"documents" | "outputs">("documents");
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function upload(fileList: FileList | null) {
    const files = Array.from(fileList ?? []);
    if (files.length > 0) await onUpload(files);
  }

  return (
    <section className="file-panel">
      <header className="file-panel__header">
        <div className="segmented-control" aria-label="File type">
          <button
            type="button"
            data-active={tab === "documents"}
            onClick={() => setTab("documents")}
          >
            Uploaded <span>{documents.length}</span>
          </button>
          <button
            type="button"
            data-active={tab === "outputs"}
            disabled={!areOutputsAvailable}
            onClick={() => setTab("outputs")}
          >
            Produced <span>{outputs.length}</span>
          </button>
        </div>
        {tab === "documents" && (
          <>
            <button
              className="button button--outline"
              type="button"
              disabled={isUploading}
              onClick={() => inputRef.current?.click()}
            >
              <Upload size={14} />
              {isUploading ? "Uploading..." : "Upload files"}
            </button>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => void upload(event.target.files)}
            />
          </>
        )}
      </header>

      <div
        className="file-panel__body"
        data-dragging={isDragging}
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          setTab("documents");
          void upload(event.dataTransfer.files);
        }}
      >
        {tab === "documents" && documents.map((document) => (
          <article className="file-row" key={document.doc_id}>
            <span className="file-row__icon"><FileText size={15} /></span>
            <div className="file-row__main">
              <strong>{document.title ?? "Untitled document"}</strong>
              <span>{document.doc_summary ?? statusLabel(document.status)}</span>
            </div>
            <span className={`status-pill status-pill--${document.status}`}>
              {document.status}
            </span>
            <time dateTime={document.created_at}>
              {formatRelativeTime(document.created_at)}
            </time>
          </article>
        ))}

        {tab === "outputs" && outputs.map((output) => (
          <a
            className="file-row file-row--link"
            href={output.download_url}
            key={`${output.run_id}-${output.relative_path}`}
          >
            <span className="file-row__type">{fileExtension(output.filename)}</span>
            <div className="file-row__main">
              <strong>{output.filename}</strong>
              <span>Run #{output.run_id.slice(0, 8)}</span>
            </div>
            <span>{formatBytes(output.bytes)}</span>
            <Download size={14} aria-hidden="true" />
          </a>
        ))}

        {tab === "documents" && documents.length === 0 && (
          <div className="file-panel__empty">
            <Upload size={20} />
            <strong>Upload your first document</strong>
            <span>Drop files here or use the upload button.</span>
          </div>
        )}
        {tab === "outputs" && outputs.length === 0 && (
          <div className="file-panel__empty">
            <FileText size={20} />
            <strong>No produced files yet</strong>
            <span>Files created by completed runs will appear here.</span>
          </div>
        )}
        {isDragging && <div className="file-panel__drop">Drop files to upload</div>}
      </div>
    </section>
  );
}

function statusLabel(status: string) {
  if (status === "queued") return "Waiting to be processed";
  if (status === "ready") return "Ready for runs";
  return "Document processing";
}

function fileExtension(filename: string) {
  return filename.split(".").pop()?.toUpperCase().slice(0, 5) ?? "FILE";
}
