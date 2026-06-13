import type {
  DocumentAcceptedResponse,
  WorkspaceDocument,
} from "../types/api";
import { apiRequest } from "./client";

export function listDocuments(workspaceId: string, signal?: AbortSignal) {
  return apiRequest<WorkspaceDocument[]>(
    `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`,
    { signal },
  );
}

export function uploadDocument(workspaceId: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  return apiRequest<DocumentAcceptedResponse>(
    `/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`,
    { method: "POST", body },
  );
}
