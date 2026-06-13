import type {
  ChatAcceptedResponse,
  WorkspaceOutput,
  WorkspaceRun,
} from "../types/api";
import { ApiError, apiRequest } from "./client";

export interface PersistedFeature<T> {
  data: T;
  isAvailable: boolean;
}

async function requestOptionalFeature<T>(
  path: string,
  emptyValue: T,
  signal?: AbortSignal,
): Promise<PersistedFeature<T>> {
  try {
    return {
      data: await apiRequest<T>(path, { signal }),
      isAvailable: true,
    };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { data: emptyValue, isAvailable: false };
    }
    throw error;
  }
}

export function listRuns(workspaceId: string, signal?: AbortSignal) {
  return requestOptionalFeature<WorkspaceRun[]>(
    `/workspace/${encodeURIComponent(workspaceId)}/runs`,
    [],
    signal,
  );
}

export function getRun(
  workspaceId: string,
  runId: string,
  signal?: AbortSignal,
) {
  return requestOptionalFeature<WorkspaceRun | null>(
    `/workspace/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}`,
    null,
    signal,
  );
}

export function listOutputs(workspaceId: string, signal?: AbortSignal) {
  return requestOptionalFeature<WorkspaceOutput[]>(
    `/workspace/${encodeURIComponent(workspaceId)}/outputs`,
    [],
    signal,
  );
}

export function startRun(workspaceId: string, queryText: string) {
  const query = new URLSearchParams({
    workspace_name: workspaceId,
    query: queryText,
  });
  return apiRequest<ChatAcceptedResponse>(`/chat/user_chat?${query}`, {
    method: "POST",
  });
}

export function submitRunAnswer(
  workspaceId: string,
  runId: string,
  answer: string,
) {
  const query = new URLSearchParams({
    workspace_name: workspaceId,
    query_id: runId,
    answer,
  });
  return apiRequest<{ status: string }>(`/chat/user_chat?${query}`, {
    method: "POST",
  });
}
