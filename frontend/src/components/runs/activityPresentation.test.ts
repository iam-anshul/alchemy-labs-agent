import { describe, expect, it } from "vitest";

import type { RunEvent } from "../../types/events";
import { getActivityPresentation } from "./activityPresentation";

function event(overrides: Partial<RunEvent>): RunEvent {
  return {
    id: "event-1",
    event: "agent_progress",
    query_id: "query-1",
    workspace_id: "workspace",
    run_id: "run-1",
    task_id: "t1",
    agent_type: "system",
    stage: "validating",
    status: "progress",
    message: "Working",
    attempt: 1,
    timestamp: 1,
    data: {},
    artifacts: [],
    ...overrides,
  };
}

describe("getActivityPresentation", () => {
  it("hides internal system terminology", () => {
    expect(getActivityPresentation(event({})).label).toBe("Checking results");
  });

  it("describes browser work as user-facing activity", () => {
    expect(
      getActivityPresentation(event({ agent_type: "browser", stage: "browsing" })).label,
    ).toBe("Searching the web");
  });
});
