import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RunEvent } from "../../types/events";
import EventScrubber from "./EventScrubber";

function event(id: string, message: string): RunEvent {
  return {
    id,
    event: "agent_progress",
    query_id: "run-1",
    workspace_id: "Research",
    run_id: "run-1",
    task_id: "t1",
    agent_type: "browser",
    stage: "browsing",
    status: "progress",
    message,
    attempt: 1,
    timestamp: 1,
    data: {},
    artifacts: [],
  };
}

describe("EventScrubber", () => {
  it("keeps every agent event selectable", () => {
    const onSelectEvent = vi.fn();
    render(
      <EventScrubber
        events={[event("first", "Opened page"), event("second", "Read page")]}
        selectedEventId="second"
        onSelectEvent={onSelectEvent}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /opened page/i }));

    expect(onSelectEvent).toHaveBeenCalledWith("first");
    expect(screen.getByRole("button", { name: /read page/i })).toHaveAttribute(
      "data-active",
      "true",
    );
  });
});
