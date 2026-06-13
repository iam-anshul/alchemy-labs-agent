export interface WorkspaceSummary {
  workspace_id: string;
  document_count: number;
  run_count: number;
  running_run_count: number;
  created_at: string;
  last_activity_at: string;
}

export interface WorkspaceRun {
  query_id: string;
  workspace_id: string;
  user_query: string;
  status: "running" | "completed" | "failed";
  started_at: string;
  query_counter: number;
  todo_md: string | null;
}

export interface WorkspaceDocument {
  doc_id: string;
  workspace_id: string;
  uploaded_by_user_id: string;
  title: string | null;
  source_path: string | null;
  n_pages: number | null;
  n_tables: number | null;
  doc_summary: string | null;
  status: string;
  created_at: string;
}

export interface WorkspaceOutput {
  run_id: string;
  filename: string;
  relative_path: string;
  bytes: number;
  modified_at: string;
  download_url: string;
}

export interface ChatAcceptedResponse {
  query_id: string;
  stream_url: string;
}

export interface DocumentAcceptedResponse {
  doc_id: string;
  status: string;
  stream_url: string;
}
