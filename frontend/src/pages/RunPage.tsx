import { AlertCircle, Radio, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useLocation, useParams } from "react-router-dom";

import { submitRunAnswer } from "../api/runs";
import ArtifactPreview from "../components/artifacts/ArtifactPreview";
import AppShell from "../components/layout/AppShell";
import EventTimeline from "../components/runs/EventTimeline";
import { useRunStream } from "../hooks/useRunStream";
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
  const { events, streamState, error: streamError } = useRunStream(
    runId,
    locationState?.streamUrl,
  );
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [answeredQuestionEventIds, setAnsweredQuestionEventIds] = useState(
    () => new Set<string>(),
  );
  const [isSubmittingAnswer, setIsSubmittingAnswer] = useState(false);
  const [answerError, setAnswerError] = useState<string | null>(null);

  const selectedEvent = useMemo(
    () => selectFocusedEvent(events, selectedEventId),
    [events, selectedEventId],
  );
  const pendingQuestionEvent = [...events]
    .reverse()
    .find((event) =>
      event.event === "awaiting_user_input"
      && !answeredQuestionEventIds.has(event.id)
    ) ?? null;
  const pendingQuestion = pendingQuestionEvent
    ? getPendingQuestion(pendingQuestionEvent)
    : null;
  const runQuery = getRunQuery(events, locationState?.queryText);

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
      actions={<RunStatus status={streamState} />}
    >
      <div className="run-query">
        <span>query</span>
        <strong>{runQuery ?? "Waiting for run details..."}</strong>
      </div>
      {(streamError || answerError) && (
        <div className="run-alert" role="alert">
          <AlertCircle size={15} />
          <span>{answerError ?? streamError}</span>
        </div>
      )}
      <div className="run-workspace">
        <section className="timeline-pane">
          <header className="timeline-pane__header">
            <div>
              <strong>Activity</strong>
              <span>{events.length} events</span>
            </div>
            {streamState === "disconnected" && (
              <button className="button button--ghost" type="button" onClick={() => window.location.reload()}>
                <RefreshCw size={13} /> Reconnect
              </button>
            )}
          </header>
          <div className="timeline-pane__body">
            <EventTimeline
              events={events}
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
    return events.find((event) => event.id === selectedEventId) ?? null;
  }
  const artifactEvent = [...events]
    .reverse()
    .find((event) => event.artifacts.length > 0);
  return artifactEvent ?? events.at(-1) ?? null;
}
