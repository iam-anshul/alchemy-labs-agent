import {
  Check,
  CircleAlert,
  Clock3,
  FileOutput,
  LoaderCircle,
} from "lucide-react";

import type { AgentExecutionGroup, PendingQuestion } from "../../types/events";
import { formatClock } from "../../utils/format";
import QuestionCard from "./QuestionCard";
import {
  getActivityPresentation,
  getEventStateLabel,
} from "./activityPresentation";
import "./RunComponents.css";

interface EventTimelineProps {
  groups: AgentExecutionGroup[];
  selectedGroupId: string | null;
  pendingQuestion: PendingQuestion | null;
  answeredQuestionEventIds: Set<string>;
  isSubmittingAnswer: boolean;
  onSelectEvent: (eventId: string) => void;
  onSubmitAnswer: (answer: string) => Promise<void>;
}

export default function EventTimeline({
  groups,
  selectedGroupId,
  pendingQuestion,
  answeredQuestionEventIds,
  isSubmittingAnswer,
  onSelectEvent,
  onSubmitAnswer,
}: EventTimelineProps) {
  if (groups.length === 0) {
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
      {groups.map((group) => {
        const event = group.latestEvent;
        const activity = getActivityPresentation(event);
        const questionEvent = group.events.find(
          (groupEvent) => groupEvent.event === "awaiting_user_input",
        );
        const isQuestion = Boolean(questionEvent);
        const showQuestion = isQuestion
          && pendingQuestion
          && questionEvent
          && !answeredQuestionEventIds.has(questionEvent.id);
        const artifacts = group.events.flatMap(
          (groupEvent) => groupEvent.artifacts,
        );

        return (
          <li className="event-row" key={group.id}>
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
              data-active={selectedGroupId === group.id}
              type="button"
              onClick={() => onSelectEvent(group.id)}
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
              <span className="event-row__history">
                {group.events.length} update{group.events.length === 1 ? "" : "s"}
              </span>
              {artifacts.length > 0 && (
                <span className="event-artifacts">
                  {artifacts.slice(-3).map((artifact, index) => (
                    <span key={`${artifact.filename ?? artifact.kind}-${index}`}>
                      <FileOutput size={12} />
                      {artifact.filename ?? artifact.kind}
                    </span>
                  ))}
                  {artifacts.length > 3 && <span>+{artifacts.length - 3} more</span>}
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
