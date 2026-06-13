import { fireEvent, render, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import FileList from "./FileList";

describe("FileList", () => {
  it("renders produced files using the backend download URL", async () => {
    const view = render(
      <FileList
        documents={[]}
        outputs={[{
          run_id: "run-123456",
          task_id: "t1",
          relative_path: "reports/brief.md",
          filename: "brief.md",
          bytes: 42,
          mime_type: "text/markdown",
          modified_at: "2026-06-14T10:00:00Z",
          preview_url: "/workspace/Research/runs/run-123456/outputs/reports/brief.md?disposition=inline",
          download_url: "/workspace/Research/runs/run-123456/outputs/reports/brief.md",
        }]}
        areOutputsAvailable
        isUploading={false}
        onUpload={vi.fn()}
      />,
    );

    fireEvent.click(within(view.container).getByRole("button", { name: /produced/i }));

    expect(within(view.container).getByRole("link", { name: /brief\.md/i })).toHaveAttribute(
      "href",
      "/workspace/Research/runs/run-123456/outputs/reports/brief.md",
    );
  });

  it("disables produced files when the backend API is unavailable", () => {
    const view = render(
      <FileList
        documents={[]}
        outputs={[]}
        areOutputsAvailable={false}
        isUploading={false}
        onUpload={vi.fn()}
      />,
    );

    expect(within(view.container).getByRole("button", { name: /produced/i })).toBeDisabled();
  });
});
