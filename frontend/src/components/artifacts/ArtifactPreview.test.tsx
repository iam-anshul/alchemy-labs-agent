import { fireEvent, render, screen } from "@testing-library/react";
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

function webEvent(): RunEvent {
  return {
    ...fileEvent(null),
    id: "web-1",
    event: "agent_progress",
    agent_type: "web_search",
    stage: "fetching_page",
    message: "Fetched page",
    data: {
      url: "https://example.com/report",
      title: "Example report",
      content: "## Key finding\n\nThe result is **verified**.",
      sources: [{
        url: "https://example.com/source",
        title: "Primary source",
        snippet: "Supporting evidence",
      }],
    },
    artifacts: [],
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

  it("allows every artifact in one event to be selected", () => {
    const event = fileEvent("/artifacts/brief.docx");
    event.artifacts.push({
      ...event.artifacts[0]!,
      path: "outputs/appendix.pdf",
      filename: "appendix.pdf",
      type: "pdf",
      mime_type: "application/pdf",
      url: "/artifacts/appendix.pdf",
    });
    render(<ArtifactPreview event={event} />);

    fireEvent.click(screen.getByRole("button", { name: "appendix.pdf" }));

    expect(screen.getByTitle("appendix.pdf")).toHaveAttribute(
      "src",
      "/artifacts/appendix.pdf",
    );
  });

  it("renders fetched page content and source links in the live panel", () => {
    render(<ArtifactPreview event={webEvent()} />);

    expect(screen.getByRole("heading", { name: "Example report" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Key finding" })).toBeInTheDocument();
    expect(screen.getByText("verified")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Primary source" })).toHaveAttribute(
      "href",
      "https://example.com/source",
    );
  });
});
