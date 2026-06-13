import { describe, expect, it } from "vitest";

import type { WorkspaceRun } from "../types/api";
import { getDisplayEvents } from "./RunPage";

describe("getDisplayEvents", () => {
  it("turns a saved todo into a renderable markdown artifact", () => {
    const savedRun: WorkspaceRun = {
      query_id: "run-1",
      workspace_id: "Research",
      user_query: "Prepare a brief",
      status: "completed",
      started_at: "2026-06-14T10:00:00Z",
      query_counter: 1,
      todo_md: "- [x] Research\n- [x] Write",
    };

    const events = getDisplayEvents([], savedRun);

    expect(events).toHaveLength(1);
    expect(events[0]?.artifacts[0]).toMatchObject({
      kind: "markdown",
      filename: "todo.md",
      content: savedRun.todo_md,
    });
  });

  it("prefers live events while a restored run is streaming", () => {
    const liveEvent = {
      ...getDisplayEvents([], {
        query_id: "run-1",
        workspace_id: "Research",
        user_query: "Prepare a brief",
        status: "completed" as const,
        started_at: "2026-06-14T10:00:00Z",
        query_counter: 1,
        todo_md: null,
      })[0]!,
      id: "live-1",
    };

    expect(getDisplayEvents([liveEvent], null)).toEqual([liveEvent]);
  });
});
