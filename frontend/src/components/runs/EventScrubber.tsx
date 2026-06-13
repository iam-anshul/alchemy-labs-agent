import type { RunEvent } from "../../types/events";
import { formatClock } from "../../utils/format";
import { getEventStateLabel } from "./activityPresentation";
import "./EventScrubber.css";

interface EventScrubberProps {
  events: RunEvent[];
  selectedEventId: string | null;
  onSelectEvent: (eventId: string) => void;
}

export default function EventScrubber({
  events,
  selectedEventId,
  onSelectEvent,
}: EventScrubberProps) {
  if (events.length <= 1) return null;

  return (
    <nav className="event-scrubber" aria-label="Agent event history">
      <div className="event-scrubber__track">
        {events.map((event, index) => (
          <button
            className="event-scrubber__item"
            data-active={selectedEventId === event.id}
            type="button"
            key={event.id}
            onClick={() => onSelectEvent(event.id)}
          >
            <span>{index + 1}</span>
            <strong>{event.message}</strong>
            <small>
              {formatClock(event.timestamp)} · {getEventStateLabel(event)}
            </small>
          </button>
        ))}
      </div>
    </nav>
  );
}
