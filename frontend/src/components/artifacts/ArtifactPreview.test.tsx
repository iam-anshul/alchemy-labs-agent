import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RunEvent } from "../../types/events";
import ArtifactPreview from "./ArtifactPreview";

function fileEvent(url: string | null): RunEvent {
  return {
    id: "artifact-1",
    event: "artifact_ready",
    query_id: "query-1",
    workspace_id: "workspace",
    run_id: "run-1",
    task_id: "task-1",
    agent_type: "office",
    stage: "writing_file",
    status: "progress",
    message: "File ready",
    attempt: 1,
    timestamp: 1,
    data: {},
    artifacts: [{
      kind: "file",
      path: "outputs/brief.docx",
      filename: "brief.docx",
      type: "docx",
      mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      bytes: 1200,
      content: null,
      content_base64: null,
      url,
      metadata: {},
    }],
  };
}

describe("ArtifactPreview", () => {
  it("does not create a download link when the backend provides no URL", () => {
    render(<ArtifactPreview event={fileEvent(null)} />);

    expect(screen.queryByRole("link", { name: /download/i })).not.toBeInTheDocument();
  });

  it("uses a backend-provided artifact URL", () => {
    render(<ArtifactPreview event={fileEvent("/artifacts/brief.docx")} />);

    expect(screen.getByRole("link", { name: /download/i })).toHaveAttribute(
      "href",
      "/artifacts/brief.docx",
    );
  });
});
