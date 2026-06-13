import { ArrowRight, Plus } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { createWorkspace, listWorkspaces } from "../api/workspaces";
import AppShell from "../components/layout/AppShell";
import { AsyncState } from "../components/ui/AsyncState";
import Modal from "../components/ui/Modal";
import { useAsyncData } from "../hooks/useAsyncData";
import "./WorkspacesPage.css";

const WORKSPACE_COLORS = ["blue", "green", "brown", "purple", "red", "slate"];

export default function WorkspacesPage() {
  const { data, error, isLoading, reload } = useAsyncData(
    (signal) => listWorkspaces(signal),
    [],
  );
  const [isCreating, setIsCreating] = useState(false);
  const [workspaceName, setWorkspaceName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleCreateWorkspace() {
    const name = workspaceName.trim();
    if (!name) return;
    setIsSubmitting(true);
    setCreateError(null);
    try {
      await createWorkspace(name);
      setWorkspaceName("");
      setIsCreating(false);
      reload();
    } catch (requestError) {
      setCreateError(
        requestError instanceof Error ? requestError.message : "Could not create workspace",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <AppShell
      crumbs={[{ label: "Workspaces" }]}
      actions={(
        <button className="button button--primary" type="button" onClick={() => setIsCreating(true)}>
          <Plus size={14} /> New workspace
        </button>
      )}
    >
      <header className="page-heading">
        <p className="eyebrow">Knowledge work</p>
        <h1>Workspaces</h1>
        <p>Documents, research runs, and produced files stay together.</p>
      </header>

      {isLoading && <AsyncState title="Loading workspaces..." />}
      {error && (
        <AsyncState
          title="Could not load workspaces"
          detail={error}
          actionLabel="Try again"
          onAction={reload}
        />
      )}
      {!isLoading && !error && data?.length === 0 && (
        <AsyncState
          title="No workspaces yet"
          detail="Create one to upload documents and start your first run."
          actionLabel="Create workspace"
          onAction={() => setIsCreating(true)}
        />
      )}

      {data && data.length > 0 && (
        <section className="workspace-grid" aria-label="Workspaces">
          {data.map((workspaceName, index) => (
            <Link
              className="workspace-card"
              to={`/workspaces/${encodeURIComponent(workspaceName)}`}
              key={workspaceName}
            >
              <div className="workspace-card__top">
                <span
                  className={`workspace-card__mark workspace-card__mark--${WORKSPACE_COLORS[index % WORKSPACE_COLORS.length]}`}
                >
                  {workspaceName.slice(0, 1).toUpperCase()}
                </span>
                <span className="workspace-card__type">workspace</span>
              </div>
              <h2>{workspaceName}</h2>
              <div className="workspace-card__stats">
                <span>Open documents and start a run</span>
                <ArrowRight size={15} />
              </div>
            </Link>
          ))}
          <button
            className="workspace-card workspace-card--new"
            type="button"
            onClick={() => setIsCreating(true)}
          >
            <span><Plus size={18} /></span>
            New workspace
          </button>
        </section>
      )}

      {isCreating && (
        <Modal title="New workspace" onClose={() => setIsCreating(false)}>
          <label className="field-label" htmlFor="workspace-name">Workspace name</label>
          <input
            id="workspace-name"
            className="text-input"
            autoFocus
            value={workspaceName}
            placeholder="Vendor risk 2026"
            onChange={(event) => setWorkspaceName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void handleCreateWorkspace();
            }}
          />
          <p className="field-help">
            This name is also used as the workspace identifier.
          </p>
          {createError && <p className="form-error" role="alert">{createError}</p>}
          <footer className="modal__actions">
            <button className="button button--ghost" type="button" onClick={() => setIsCreating(false)}>
              Cancel
            </button>
            <button
              className="button button--primary"
              type="button"
              disabled={!workspaceName.trim() || isSubmitting}
              onClick={() => void handleCreateWorkspace()}
            >
              {isSubmitting ? "Creating..." : "Create workspace"}
            </button>
          </footer>
        </Modal>
      )}
    </AppShell>
  );
}
