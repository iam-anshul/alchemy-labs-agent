import type {
  ChatAcceptedResponse,
} from "../types/api";
import { apiRequest } from "./client";

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
