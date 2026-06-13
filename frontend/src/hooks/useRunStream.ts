import { useEffect, useRef, useState } from "react";

import {
  RUN_EVENT_NAMES,
  type RunEvent,
  type RunEventName,
} from "../types/events";
import { parseRunEvent } from "../types/eventParser";

export type StreamState =
  | "connecting"
  | "live"
  | "disconnected"
  | "completed"
  | "failed";

export function getRunStreamUrl(runId: string, acceptedStreamUrl?: string) {
  if (acceptedStreamUrl?.startsWith("/")) {
    return acceptedStreamUrl;
  }
  return `/chat/${encodeURIComponent(runId)}/stream`;
}

export function useRunStream(runId: string, acceptedStreamUrl?: string) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);

  useEffect(() => {
    setEvents([]);
    setStreamState("connecting");
    setError(null);
    sequence.current = 0;

    const source = new EventSource(getRunStreamUrl(runId, acceptedStreamUrl), {
      withCredentials: true,
    });

    const listeners = RUN_EVENT_NAMES.map((eventName) => {
      const listener = (message: MessageEvent<string>) => {
        try {
          const event = parseRunEvent(
            eventName as RunEventName,
            message.data,
            sequence.current++,
          );
          setEvents((current) => [...current, event]);
          setStreamState(
            event.event === "run_ended"
              ? event.status === "failed" ? "failed" : "completed"
              : "live",
          );
        } catch (parseError) {
          setError(
            parseError instanceof Error
              ? parseError.message
              : "Could not read a run event",
          );
        }
      };
      source.addEventListener(eventName, listener as EventListener);
      return { eventName, listener };
    });

    source.onopen = () => {
      setStreamState("live");
      setError(null);
    };
    source.onerror = () => {
      setStreamState((current) =>
        current === "completed" || current === "failed"
          ? current
          : "disconnected",
      );
    };

    return () => {
      listeners.forEach(({ eventName, listener }) => {
        source.removeEventListener(eventName, listener as EventListener);
      });
      source.close();
    };
  }, [acceptedStreamUrl, runId]);

  return { events, streamState, error };
}
