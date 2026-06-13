import { Play } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { listDocuments, uploadDocument } from "../api/documents";
import { startRun } from "../api/runs";
import FileList from "../components/files/FileList";
import AppShell from "../components/layout/AppShell";
import BackendFeatureNotice from "../components/ui/BackendFeatureNotice";
import { AsyncState } from "../components/ui/AsyncState";
import { useAsyncData } from "../hooks/useAsyncData";
import "./WorkspacePage.css";

export default function WorkspacePage() {
  const { workspaceId = "" } = useParams();
  const decodedWorkspaceId = decodeURIComponent(workspaceId);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [isStarting, setIsStarting] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const documentsData = useAsyncData(
    (signal) => listDocuments(decodedWorkspaceId, signal),
    [decodedWorkspaceId],
  );

  async function handleStartRun() {
    const request = query.trim();
    if (!request) return;
    setIsStarting(true);
    setActionError(null);
    try {
      const accepted = await startRun(decodedWorkspaceId, request);
      navigate(
        `/workspaces/${encodeURIComponent(decodedWorkspaceId)}/runs/${accepted.query_id}`,
        {
          state: {
            queryText: request,
            streamUrl: accepted.stream_url,
          },
        },
      );
    } catch (requestError) {
      setActionError(
        requestError instanceof Error ? requestError.message : "Could not start run",
      );
    } finally {
      setIsStarting(false);
    }
  }

  async function handleUpload(files: File[]) {
    setIsUploading(true);
    setActionError(null);
    try {
      for (const file of files) {
        await uploadDocument(decodedWorkspaceId, file);
      }
      documentsData.reload();
    } catch (requestError) {
      setActionError(
        requestError instanceof Error ? requestError.message : "Could not upload files",
      );
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <AppShell
      crumbs={[
        { label: "Workspaces", to: "/workspaces" },
        { label: decodedWorkspaceId },
      ]}
    >
      <header className="workspace-heading">
        <span className="workspace-heading__mark">
          {decodedWorkspaceId.slice(0, 1).toUpperCase()}
        </span>
        <div>
          <p className="eyebrow">Workspace</p>
          <h1>{decodedWorkspaceId}</h1>
          {documentsData.data && (
            <p>{documentsData.data.length} files in scope</p>
          )}
        </div>
      </header>

      <section className="run-composer" aria-labelledby="run-composer-title">
        <label id="run-composer-title" htmlFor="run-query">What should Serca do?</label>
        <textarea
          id="run-query"
          rows={4}
          value={query}
          placeholder="Research, read the workspace documents, and produce a useful deliverable..."
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              void handleStartRun();
            }
          }}
        />
        <footer>
          <span>Uses workspace documents and the open web.</span>
          <button
            className="button button--primary"
            type="button"
            disabled={!query.trim() || isStarting}
            onClick={() => void handleStartRun()}
          >
            <Play size={14} />
            {isStarting ? "Starting..." : "Start run"}
          </button>
        </footer>
      </section>

      {actionError && <p className="form-error" role="alert">{actionError}</p>}

      {documentsData.isLoading && <AsyncState title="Loading workspace..." />}
      {documentsData.error && (
        <AsyncState
          title="Could not load this workspace"
          detail={documentsData.error}
          actionLabel="Try again"
          onAction={documentsData.reload}
        />
      )}

      {documentsData.data && (
        <>
          <section className="workspace-section">
            <div className="section-heading">
              <p className="eyebrow">Recent runs</p>
            </div>
            <BackendFeatureNotice
              title="Run history is not available yet"
              detail="The current backend can start and stream live runs, but it does not expose a workspace run-list endpoint."
            />
          </section>

          <section className="workspace-section">
            <div className="section-heading">
              <p className="eyebrow">Files</p>
            </div>
            <FileList
              documents={documentsData.data}
              isUploading={isUploading}
              onUpload={handleUpload}
            />
          </section>

          <section className="workspace-section">
            <div className="section-heading">
              <p className="eyebrow">Produced files</p>
            </div>
            <BackendFeatureNotice
              title="Saved output downloads are not available yet"
              detail="Artifacts can be previewed while a run is live. Listing and downloading saved outputs requires additional backend endpoints."
            />
          </section>
        </>
      )}
    </AppShell>
  );
}
