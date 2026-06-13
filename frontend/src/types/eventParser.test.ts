import { describe, expect, it } from "vitest";

import {
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
