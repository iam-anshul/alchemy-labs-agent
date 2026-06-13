import { describe, expect, it } from "vitest";

import type { WorkspaceOutput } from "./api";
import type { RunEvent } from "./events";
import { collectRunArtifacts } from "./runArtifacts";

function eventWithOutput(id: string, path: string): RunEvent {
  return {
    id,
    event: "artifact_ready",
    query_id: "run-1",
    workspace_id: "Research",
    run_id: "run-1",
    task_id: "t1",
    agent_type: "web_search",
    stage: "outputs",
    status: "progress",
    message: "Output ready",
    attempt: 1,
    timestamp: 1,
    data: {},
    artifacts: [{
      kind: "markdown",
      path,
      filename: path.split("/").pop() ?? null,
      type: "md",
      mime_type: "text/markdown",
      bytes: 10,
      content: "# Live",
      content_base64: null,
      url: null,
      metadata: {},
    }],
  };
}

describe("collectRunArtifacts", () => {
  it("combines outputs from different agents", () => {
    const entries = collectRunArtifacts([
      eventWithOutput("web", "outputs/research.md"),
      {
        ...eventWithOutput("office", "outputs/report.docx"),
        task_id: "t2",
        agent_type: "office",
      },
    ], []);

    expect(entries.map((entry) => entry.id)).toEqual([
      "outputs/research.md",
      "outputs/report.docx",
    ]);
  });

  it("uses the persisted output contract as the newest source for a path", () => {
    const savedOutput: WorkspaceOutput = {
      run_id: "run-1",
      task_id: "t1",
      filename: "research.md",
      relative_path: "outputs/research.md",
      bytes: 20,
      mime_type: "text/markdown",
      modified_at: "2026-06-14T10:00:00Z",
      preview_url: "/preview",
      download_url: "/download",
    };

    const entries = collectRunArtifacts(
      [eventWithOutput("web", "outputs/research.md")],
      [savedOutput],
    );

    expect(entries).toHaveLength(1);
    expect(entries[0]?.artifact.url).toBe("/preview");
    expect(entries[0]?.artifact.metadata.download_url).toBe("/download");
  });
});
