import { apiRequest } from "./client";

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

export function createWorkspace(workspaceName: string) {
  const query = new URLSearchParams({ workspace_name: workspaceName });
  return apiRequest<string>(`/workspace/create_workspace?${query}`, {
    method: "POST",
  });
}
