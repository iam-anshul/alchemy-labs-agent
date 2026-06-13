import {
  Check,
  CircleAlert,
  Clock3,
  FileOutput,
  LoaderCircle,
} from "lucide-react";

import type { PendingQuestion, RunEvent } from "../../types/events";
import { formatClock } from "../../utils/format";
import QuestionCard from "./QuestionCard";
import {
  getActivityPresentation,
  getEventStateLabel,
} from "./activityPresentation";
import "./RunComponents.css";

interface EventTimelineProps {
  events: RunEvent[];
  selectedEventId: string | null;
  pendingQuestion: PendingQuestion | null;
  answeredQuestionEventIds: Set<string>;
  isSubmittingAnswer: boolean;
  onSelectEvent: (eventId: string) => void;
  onSubmitAnswer: (answer: string) => Promise<void>;
}

export default function EventTimeline({
  events,
  selectedEventId,
  pendingQuestion,
  answeredQuestionEventIds,
  isSubmittingAnswer,
  onSelectEvent,
  onSubmitAnswer,
}: EventTimelineProps) {
  if (events.length === 0) {
    return (
      <div className="timeline-empty">
        <LoaderCircle size={20} className="spin" />
        <strong>Connecting to the run...</strong>
        <span>Events will appear here as work begins.</span>
      </div>
    );
  }

  return (
    <ol className="event-timeline">
      {events.map((event) => {
        const activity = getActivityPresentation(event);
        const isQuestion = event.event === "awaiting_user_input";
        const showQuestion = isQuestion
          && pendingQuestion
          && !answeredQuestionEventIds.has(event.id);

        return (
          <li className="event-row" key={event.id}>
            <div className="event-row__rail">
              <span className={`event-node event-node--${event.status}`}>
                {event.status === "completed" ? <Check size={11} />
                  : event.status === "failed" ? <CircleAlert size={11} />
                  : event.status === "waiting" ? <Clock3 size={11} />
                  : <i />}
              </span>
            </div>
            <button
              className="event-row__content"
              data-active={selectedEventId === event.id}
              type="button"
              onClick={() => onSelectEvent(event.id)}
            >
              <span className="event-row__meta">
                <time>{formatClock(event.timestamp)}</time>
                <span className={`activity-label activity-label--${activity.tone}`}>
                  {activity.label}
                </span>
                <span className={`event-state event-state--${event.status}`}>
                  {getEventStateLabel(event)}
                </span>
                {event.attempt && event.attempt > 1 && <span>retry {event.attempt}</span>}
              </span>
              <span className="event-row__message">{event.message}</span>
              {event.artifacts.length > 0 && (
                <span className="event-artifacts">
                  {event.artifacts.map((artifact, index) => (
                    <span key={`${artifact.filename ?? artifact.kind}-${index}`}>
                      <FileOutput size={12} />
                      {artifact.filename ?? artifact.kind}
                    </span>
                  ))}
                </span>
              )}
            </button>
            {showQuestion && (
              <div className="event-row__question">
                <QuestionCard
                  question={pendingQuestion}
                  isSubmitting={isSubmittingAnswer}
                  onSubmit={onSubmitAnswer}
                />
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
