export const RUN_EVENT_NAMES = [
  "run_started",
  "agent_started",
  "agent_progress",
  "artifact_ready",
  "awaiting_user_input",
  "agent_ended",
  "run_ended",
] as const;

export type RunEventName = (typeof RUN_EVENT_NAMES)[number];

export type RunStatus =
  | "started"
  | "progress"
  | "waiting"
  | "completed"
  | "failed";

export type ArtifactKind =
  | "file"
  | "screenshot"
  | "markdown"
  | "extracted_content"
  | "final_answer";

export interface Artifact {
  kind: ArtifactKind | string;
  path: string | null;
  filename: string | null;
  type: string | null;
  mime_type: string | null;
  bytes: number | null;
  content: string | null;
  content_base64: string | null;
  url: string | null;
  metadata: Record<string, unknown>;
}

export interface RunEvent {
  id: string;
  event: RunEventName;
  query_id: string | null;
  workspace_id: string | null;
  run_id: string | null;
  task_id: string | null;
  agent_type: string;
  stage: string;
  status: RunStatus;
  message: string;
  attempt: number | null;
  timestamp: number;
  data: Record<string, unknown>;
  artifacts: Artifact[];
}

export interface PendingQuestion {
  question: string;
  kind: "text" | "mcq";
  options: string[];
  recommendedOption: number | null;
}

export interface AgentExecutionGroup {
  id: string;
  agentType: string;
  taskId: string | null;
  attempt: number | null;
  events: RunEvent[];
  latestEvent: RunEvent;
}
