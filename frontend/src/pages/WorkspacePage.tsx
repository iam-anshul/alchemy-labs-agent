import { ArrowRight, Play, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { listDocuments, uploadDocument } from "../api/documents";
import { listOutputs, listRuns, startRun } from "../api/runs";
import { deleteWorkspace } from "../api/workspaces";
import FileList from "../components/files/FileList";
import AppShell from "../components/layout/AppShell";
import BackendFeatureNotice from "../components/ui/BackendFeatureNotice";
import { AsyncState } from "../components/ui/AsyncState";
import Modal from "../components/ui/Modal";
import { useAsyncData } from "../hooks/useAsyncData";
import { formatRelativeTime } from "../utils/format";
import "./WorkspacePage.css";

export default function WorkspacePage() {
  const { workspaceId = "" } = useParams();
  const decodedWorkspaceId = decodeURIComponent(workspaceId);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [isStarting, setIsStarting] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDeleteOpen, setIsDeleteOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const workspaceData = useAsyncData(
    async (signal) => {
      const [documents, runs, outputs] = await Promise.all([
        listDocuments(decodedWorkspaceId, signal),
        listRuns(decodedWorkspaceId, signal),
        listOutputs(decodedWorkspaceId, signal),
      ]);
      return { documents, runs, outputs };
    },
    [decodedWorkspaceId],
    // Silently refresh every 5s so a run started elsewhere (e.g. the user
    // navigated back from a chat that is still in flight) appears in "Recent
    // runs", and a running run flips to completed/failed, without a manual
    // reload. Also keeps the document/output lists current (e.g. an upload
    // finishing ingestion).
    5000,
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
      workspaceData.reload();
    } catch (requestError) {
      setActionError(
        requestError instanceof Error ? requestError.message : "Could not upload files",
      );
    } finally {
      setIsUploading(false);
    }
  }

  async function handleDeleteWorkspace() {
    setIsDeleting(true);
    setActionError(null);
    try {
      await deleteWorkspace(decodedWorkspaceId);
      navigate("/workspaces", { replace: true });
    } catch (requestError) {
      setActionError(
        requestError instanceof Error
          ? requestError.message
          : "Could not delete workspace",
      );
      setIsDeleteOpen(false);
    } finally {
      setIsDeleting(false);
    }
  }

  return (
    <AppShell
      crumbs={[
        { label: "Workspaces", to: "/workspaces" },
        { label: decodedWorkspaceId },
      ]}
      actions={(
        <button
          className="button button--ghost button--danger"
          type="button"
          onClick={() => setIsDeleteOpen(true)}
        >
          <Trash2 size={14} />
          Delete workspace
        </button>
      )}
    >
      <header className="workspace-heading">
        <span className="workspace-heading__mark">
          {decodedWorkspaceId.slice(0, 1).toUpperCase()}
        </span>
        <div>
          <p className="eyebrow">Workspace</p>
          <h1>{decodedWorkspaceId}</h1>
          {workspaceData.data && (
            <p>
              {workspaceData.data.documents.length} files in scope
              {workspaceData.data.runs.isAvailable
                ? ` · ${workspaceData.data.runs.data.length} runs`
                : ""}
            </p>
          )}
        </div>
      </header>

      <section className="run-composer" aria-labelledby="run-composer-title">
        <label id="run-composer-title" htmlFor="run-query">What should Alchemy Labs do?</label>
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

      {workspaceData.isLoading && <AsyncState title="Loading workspace..." />}
      {workspaceData.error && (
        <AsyncState
          title="Could not load this workspace"
          detail={workspaceData.error}
          actionLabel="Try again"
          onAction={workspaceData.reload}
        />
      )}

      {workspaceData.data && (
        <>
          <section className="workspace-section">
            <div className="section-heading">
              <p className="eyebrow">Recent runs</p>
            </div>
            {workspaceData.data.runs.isAvailable ? (
              <div className="run-list">
                {workspaceData.data.runs.data.map((run) => (
                  <button
                    className="run-row"
                    type="button"
                    key={run.query_id}
                    onClick={() => navigate(
                      `/workspaces/${encodeURIComponent(decodedWorkspaceId)}/runs/${run.query_id}`,
                    )}
                  >
                    <span className={`run-row__dot run-row__dot--${run.status}`} />
                    <span className="run-row__title">{run.user_query}</span>
                    <span className={`status-pill status-pill--${run.status}`}>
                      {run.status}
                    </span>
                    <time dateTime={run.started_at}>
                      {formatRelativeTime(run.started_at)}
                    </time>
                    <ArrowRight size={14} />
                  </button>
                ))}
                {workspaceData.data.runs.data.length === 0 && (
                  <div className="run-list__empty">
                    No runs yet. Start one above.
                  </div>
                )}
              </div>
            ) : (
              <BackendFeatureNotice
                title="Run history is not available yet"
                detail="The active backend does not expose the workspace run-list endpoint."
              />
            )}
          </section>

          <section className="workspace-section">
            <div className="section-heading">
              <p className="eyebrow">Files</p>
            </div>
            <FileList
              documents={workspaceData.data.documents}
              outputs={workspaceData.data.outputs.data}
              areOutputsAvailable={workspaceData.data.outputs.isAvailable}
              isUploading={isUploading}
              onUpload={handleUpload}
            />
          </section>

          {!workspaceData.data.outputs.isAvailable && (
            <BackendFeatureNotice
              title="Saved output downloads are not available yet"
              detail="Artifacts can be previewed live, but this backend does not expose saved output listing and download endpoints."
            />
          )}
        </>
      )}

      {isDeleteOpen && (
        <Modal title="Delete workspace" onClose={() => setIsDeleteOpen(false)}>
          <p className="delete-confirmation">
            Delete <strong>{decodedWorkspaceId}</strong> and all of its runs,
            documents, and produced files? This cannot be undone.
          </p>
          <footer className="modal__actions">
            <button
              className="button button--ghost"
              type="button"
              disabled={isDeleting}
              onClick={() => setIsDeleteOpen(false)}
            >
              Cancel
            </button>
            <button
              className="button button--danger-solid"
              type="button"
              disabled={isDeleting}
              onClick={() => void handleDeleteWorkspace()}
            >
              {isDeleting ? "Deleting..." : "Delete workspace"}
            </button>
          </footer>
        </Modal>
      )}
    </AppShell>
  );
}
