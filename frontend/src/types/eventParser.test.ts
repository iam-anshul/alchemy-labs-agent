import { describe, expect, it } from "vitest";

import {
  groupRunEvents,
  getPendingQuestion,
  getRunQuery,
  parseRunEvent,
} from "./eventParser";

describe("parseRunEvent", () => {
  it("normalizes artifact arrays and preserves event data", () => {
    const event = parseRunEvent(
      "artifact_ready",
      JSON.stringify({
        run_id: "run-1",
        agent_type: "office",
        stage: "writing_file",
        status: "progress",
        message: "File ready",
        timestamp: 10,
        data: { sections: 2 },
        artifacts: [{
          kind: "file",
          filename: "brief.docx",
          path: "outputs/brief.docx",
          metadata: {},
        }],
      }),
      0,
    );

    expect(event.artifacts).toHaveLength(1);
    expect(event.artifacts[0]?.filename).toBe("brief.docx");
    expect(event.data.sections).toBe(2);
  });

  it("parses multiple-choice questions", () => {
    const event = parseRunEvent(
      "awaiting_user_input",
      JSON.stringify({
        status: "waiting",
        message: "Choose scope",
        data: {
          question: "Which files?",
          options: ["Affected only", "All files"],
          recommended_option: 0,
        },
      }),
      1,
    );

    expect(getPendingQuestion(event)).toEqual({
      question: "Which files?",
      kind: "mcq",
      options: ["Affected only", "All files"],
      recommendedOption: 0,
    });
  });
});

describe("getRunQuery", () => {
  it("prefers the query passed while navigating to a newly accepted run", () => {
    expect(getRunQuery([], "  Prepare a risk brief  ")).toBe(
      "Prepare a risk brief",
    );
  });

  it("recovers the query from a run_started event", () => {
    const event = parseRunEvent(
      "run_started",
      JSON.stringify({
        status: "started",
        message: "Run started",
        data: { query: "Compare the uploaded contracts" },
      }),
      0,
    );

    expect(getRunQuery([event])).toBe("Compare the uploaded contracts");
  });
});

describe("groupRunEvents", () => {
  it("keeps every update inside one agent execution", () => {
    const first = parseRunEvent(
      "agent_progress",
      JSON.stringify({
        task_id: "task-1",
        agent_type: "web_search",
        stage: "searching",
        message: "Searching",
      }),
      0,
    );
    const latest = parseRunEvent(
      "agent_progress",
      JSON.stringify({
        task_id: "task-1",
        agent_type: "web_search",
        stage: "searching",
        message: "Found 8 sources",
      }),
      1,
    );

    const groups = groupRunEvents([first, latest]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.events).toEqual([first, latest]);
    expect(groups[0]?.latestEvent).toBe(latest);
  });

  it("keeps user questions as separate actionable updates", () => {
    const first = parseRunEvent(
      "awaiting_user_input",
      JSON.stringify({ message: "Choose scope", data: { question: "Scope?" } }),
      0,
    );
    const second = parseRunEvent(
      "awaiting_user_input",
      JSON.stringify({ message: "Choose format", data: { question: "Format?" } }),
      1,
    );

    expect(groupRunEvents([first, second])).toHaveLength(2);
  });

  it("keeps planner cycles separate after each agent end", () => {
    const started = parseRunEvent(
      "agent_started",
      JSON.stringify({ agent_type: "planner", message: "Planning" }),
      0,
    );
    const ended = parseRunEvent(
      "agent_ended",
      JSON.stringify({ agent_type: "planner", message: "Plan ready" }),
      1,
    );
    const replanning = parseRunEvent(
      "agent_started",
      JSON.stringify({ agent_type: "planner", message: "Replanning" }),
      2,
    );

    const groups = groupRunEvents([started, ended, replanning]);

    expect(groups).toHaveLength(2);
    expect(groups[0]?.events).toEqual([started, ended]);
    expect(groups[1]?.events).toEqual([replanning]);
  });
});
