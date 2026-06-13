import type { WorkspaceSummary } from "../types/api";
import { ApiError, apiRequest } from "./client";

export function parseWorkspaceNames(value: unknown): string[] {
  if (!Array.isArray(value) || value.some((name) => typeof name !== "string")) {
    throw new Error("Workspace list response has an unexpected format");
  }
  return value;
}

export async function listWorkspaces(signal?: AbortSignal) {
  const response = await apiRequest<unknown>("/workspace/list_workspace", {
    signal,
  });
  return parseWorkspaceNames(response);
}

export async function listWorkspaceSummaries(signal?: AbortSignal) {
  try {
    return await apiRequest<WorkspaceSummary[]>("/workspace/summaries", {
      signal,
    });
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) {
      throw error;
    }

    const workspaceNames = await listWorkspaces(signal);
    return workspaceNames.map((workspaceId): WorkspaceSummary => ({
      workspace_id: workspaceId,
      document_count: null,
      run_count: null,
      running_run_count: null,
      created_at: null,
      last_activity_at: null,
    }));
  }
}

export function createWorkspace(workspaceName: string) {
  const query = new URLSearchParams({ workspace_name: workspaceName });
  return apiRequest<string>(`/workspace/create_workspace?${query}`, {
    method: "POST",
  });
}

export function deleteWorkspace(workspaceName: string) {
  const query = new URLSearchParams({ workspace_name: workspaceName });
  return apiRequest<string>(`/workspace/delete_workspace?${query}`, {
    method: "DELETE",
  });
}
