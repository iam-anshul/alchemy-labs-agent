import type {
  Artifact,
  PendingQuestion,
  RunEvent,
  RunEventName,
  RunStatus,
} from "./events";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function parseArtifact(value: unknown): Artifact | null {
  if (!isRecord(value) || typeof value.kind !== "string") {
    return null;
  }
  return {
    kind: value.kind,
    path: optionalString(value.path),
    filename: optionalString(value.filename),
    type: optionalString(value.type),
    mime_type: optionalString(value.mime_type),
    bytes: typeof value.bytes === "number" ? value.bytes : null,
    content: optionalString(value.content),
    content_base64: optionalString(value.content_base64),
    url: optionalString(value.url),
    metadata: isRecord(value.metadata) ? value.metadata : {},
  };
}

const RUN_STATUSES = new Set<RunStatus>([
  "started",
  "progress",
  "waiting",
  "completed",
  "failed",
]);

export function parseRunEvent(
  eventName: RunEventName,
  rawData: string,
  sequence: number,
): RunEvent {
  const parsed: unknown = JSON.parse(rawData);
  if (!isRecord(parsed)) {
    throw new Error("Run event payload must be an object");
  }

  const artifactsValue = Array.isArray(parsed.artifacts)
    ? parsed.artifacts
    : parsed.artifact
      ? [parsed.artifact]
      : [];

  const artifacts = artifactsValue
    .map(parseArtifact)
    .filter((artifact): artifact is Artifact => artifact !== null);

  const status = typeof parsed.status === "string" && RUN_STATUSES.has(parsed.status as RunStatus)
    ? parsed.status as RunStatus
    : "progress";
  const timestamp = typeof parsed.timestamp === "number"
    ? parsed.timestamp
    : Date.now() / 1000;

  return {
    id: `${eventName}-${timestamp}-${sequence}`,
    event: eventName,
    query_id: optionalString(parsed.query_id),
    workspace_id: optionalString(parsed.workspace_id),
    run_id: optionalString(parsed.run_id),
    task_id: optionalString(parsed.task_id),
    agent_type: typeof parsed.agent_type === "string" ? parsed.agent_type : "system",
    stage: typeof parsed.stage === "string" ? parsed.stage : "",
    status,
    message: typeof parsed.message === "string" ? parsed.message : eventName,
    attempt: typeof parsed.attempt === "number" ? parsed.attempt : null,
    timestamp,
    data: isRecord(parsed.data) ? parsed.data : {},
    artifacts,
  };
}

export function getPendingQuestion(event: RunEvent): PendingQuestion | null {
  if (event.event !== "awaiting_user_input") {
    return null;
  }

  const question = typeof event.data.question === "string"
    ? event.data.question
    : event.message;
  const options = Array.isArray(event.data.options)
    ? event.data.options.filter((option): option is string => typeof option === "string")
    : [];
  const recommended = event.data.recommended_option;

  return {
    question,
    kind: options.length > 0 ? "mcq" : "text",
    options,
    recommendedOption: typeof recommended === "number" ? recommended : null,
  };
}

export function getRunQuery(
  events: RunEvent[],
  navigationQuery?: string,
): string | null {
  const trimmedNavigationQuery = navigationQuery?.trim();
  if (trimmedNavigationQuery) {
    return trimmedNavigationQuery;
  }

  const startedEvent = events.find((event) => event.event === "run_started");
  const eventQuery = startedEvent?.data.query;
  return typeof eventQuery === "string" && eventQuery.trim()
    ? eventQuery.trim()
    : null;
}
