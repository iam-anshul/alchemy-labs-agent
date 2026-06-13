import { AlertCircle, Radio, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useLocation, useParams } from "react-router-dom";

import { getRun, submitRunAnswer } from "../api/runs";
import ArtifactPreview from "../components/artifacts/ArtifactPreview";
import AppShell from "../components/layout/AppShell";
import EventTimeline from "../components/runs/EventTimeline";
import { useAsyncData } from "../hooks/useAsyncData";
import { useRunStream } from "../hooks/useRunStream";
import type { WorkspaceRun } from "../types/api";
import type { RunEvent } from "../types/events";
import { getPendingQuestion, getRunQuery } from "../types/eventParser";
import "./RunPage.css";

interface RunLocationState {
  queryText?: string;
  streamUrl?: string;
}

export default function RunPage() {
  const { workspaceId = "", runId = "" } = useParams();
  const decodedWorkspaceId = decodeURIComponent(workspaceId);
  const location = useLocation();
  const locationState = location.state as RunLocationState | null;
  const persistedRun = useAsyncData(
    (signal) => getRun(decodedWorkspaceId, runId, signal),
    [decodedWorkspaceId, runId],
  );
  const shouldStream = Boolean(locationState?.streamUrl || locationState?.queryText)
    || persistedRun.data?.data?.status === "running";
  const { events, streamState, error: streamError } = useRunStream(
    runId,
    locationState?.streamUrl,
    shouldStream,
  );
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [answeredQuestionEventIds, setAnsweredQuestionEventIds] = useState(
    () => new Set<string>(),
  );
  const [isSubmittingAnswer, setIsSubmittingAnswer] = useState(false);
  const [answerError, setAnswerError] = useState<string | null>(null);

  const displayEvents = useMemo(
    () => getDisplayEvents(events, persistedRun.data?.data ?? null),
    [events, persistedRun.data?.data],
  );
  const selectedEvent = useMemo(
    () => selectFocusedEvent(displayEvents, selectedEventId),
    [displayEvents, selectedEventId],
  );
  const pendingQuestionEvent = [...displayEvents]
    .reverse()
    .find((event) =>
      event.event === "awaiting_user_input"
      && !answeredQuestionEventIds.has(event.id)
    ) ?? null;
  const pendingQuestion = pendingQuestionEvent
    ? getPendingQuestion(pendingQuestionEvent)
    : null;
  const runQuery = persistedRun.data?.data?.user_query
    ?? getRunQuery(displayEvents, locationState?.queryText);
  const displayStatus = persistedRun.data?.data?.status === "completed"
    || persistedRun.data?.data?.status === "failed"
    ? persistedRun.data.data.status
    : streamState;
  const historyUnavailable = persistedRun.data
    && !persistedRun.data.isAvailable
    && !shouldStream;

  async function handleSubmitAnswer(answer: string) {
    if (!pendingQuestionEvent) return;
    setIsSubmittingAnswer(true);
    setAnswerError(null);
    try {
      await submitRunAnswer(decodedWorkspaceId, runId, answer);
      setAnsweredQuestionEventIds((current) => {
        const next = new Set(current);
        next.add(pendingQuestionEvent.id);
        return next;
      });
    } catch (requestError) {
      setAnswerError(
        requestError instanceof Error ? requestError.message : "Could not submit answer",
      );
    } finally {
      setIsSubmittingAnswer(false);
    }
  }

  return (
    <AppShell
      contentClassName="run-page-shell"
      crumbs={[
        { label: "Workspaces", to: "/workspaces" },
        {
          label: decodedWorkspaceId,
          to: `/workspaces/${encodeURIComponent(decodedWorkspaceId)}`,
        },
        { label: `Run #${runId.slice(0, 8)}` },
      ]}
      actions={<RunStatus status={displayStatus} />}
    >
      <div className="run-query">
        <span>query</span>
        <strong>{runQuery ?? "Waiting for run details..."}</strong>
      </div>
      {(streamError || answerError || historyUnavailable) && (
        <div className="run-alert" role="alert">
          <AlertCircle size={15} />
          <span>
            {answerError
              ?? streamError
              ?? "This backend cannot load saved run details yet."}
          </span>
        </div>
      )}
      <div className="run-workspace">
        <section className="timeline-pane">
          <header className="timeline-pane__header">
            <div>
              <strong>Activity</strong>
              <span>{displayEvents.length} updates</span>
            </div>
            {selectedEventId && (
              <button
                className="button button--ghost"
                type="button"
                onClick={() => setSelectedEventId(null)}
              >
                <Radio size={13} /> Follow live
              </button>
            )}
            {displayStatus === "disconnected" && (
              <button className="button button--ghost" type="button" onClick={() => window.location.reload()}>
                <RefreshCw size={13} /> Reconnect
              </button>
            )}
          </header>
          <div className="timeline-pane__body">
            <EventTimeline
              events={displayEvents}
              selectedEventId={selectedEvent?.id ?? null}
              pendingQuestion={pendingQuestion}
              answeredQuestionEventIds={answeredQuestionEventIds}
              isSubmittingAnswer={isSubmittingAnswer}
              onSelectEvent={(eventId) => {
                setSelectedEventId((current) => current === eventId ? null : eventId);
              }}
              onSubmitAnswer={handleSubmitAnswer}
            />
          </div>
        </section>
        <section className="focus-pane">
          <ArtifactPreview event={selectedEvent} />
        </section>
      </div>
    </AppShell>
  );
}

function RunStatus({ status }: { status: string }) {
  return (
    <span className={`run-status run-status--${status}`}>
      <Radio size={12} />
      {status === "live" ? "running" : status}
    </span>
  );
}

function selectFocusedEvent(
  events: RunEvent[],
  selectedEventId: string | null,
) {
  if (selectedEventId) {
    const selectedEvent = events.find((event) => event.id === selectedEventId);
    if (selectedEvent) return selectedEvent;
  }
  const artifactEvent = [...events]
    .reverse()
    .find((event) =>
      event.artifacts.length > 0
      || event.agent_type === "web_search"
      || event.agent_type === "browser"
    );
  return artifactEvent ?? events.at(-1) ?? null;
}

export function getDisplayEvents(
  liveEvents: RunEvent[],
  persistedRun: WorkspaceRun | null,
): RunEvent[] {
  if (liveEvents.length > 0 || !persistedRun) {
    return liveEvents;
  }

  const todoArtifact = persistedRun.todo_md
    ? [{
        kind: "markdown",
        path: "todo.md",
        filename: "todo.md",
        type: "md",
        mime_type: "text/markdown",
        bytes: new TextEncoder().encode(persistedRun.todo_md).length,
        content: persistedRun.todo_md,
        content_base64: null,
        url: null,
        metadata: { historical: true },
      }]
    : [];

  return [{
    id: `saved-run-${persistedRun.query_id}`,
    event: "run_ended",
    query_id: persistedRun.query_id,
    workspace_id: persistedRun.workspace_id,
    run_id: persistedRun.query_id,
    task_id: null,
    agent_type: "system",
    stage: "done",
    status: persistedRun.status === "failed" ? "failed" : "completed",
    message: persistedRun.status === "failed"
      ? "Saved run failed"
      : "Saved run completed",
    attempt: null,
    timestamp: new Date(persistedRun.started_at).getTime() / 1000,
    data: {},
    artifacts: todoArtifact,
  }];
}
