import { FileText, Upload } from "lucide-react";
import { useRef, useState } from "react";

import type { WorkspaceDocument } from "../../types/api";
import { formatRelativeTime } from "../../utils/format";
import "./FileList.css";

interface FileListProps {
  documents: WorkspaceDocument[];
  isUploading: boolean;
  onUpload: (files: File[]) => Promise<void>;
}

export default function FileList({
  documents,
  isUploading,
  onUpload,
}: FileListProps) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function upload(fileList: FileList | null) {
    const files = Array.from(fileList ?? []);
    if (files.length > 0) await onUpload(files);
  }

  return (
    <section className="file-panel">
      <header className="file-panel__header">
        <strong>Uploaded documents</strong>
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
          void upload(event.dataTransfer.files);
        }}
      >
        {documents.map((document) => (
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

        {documents.length === 0 && (
          <div className="file-panel__empty">
            <Upload size={20} />
            <strong>Upload your first document</strong>
            <span>Drop files here or use the upload button.</span>
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
