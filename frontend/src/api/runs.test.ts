import { afterEach, describe, expect, it, vi } from "vitest";

import { listOutputs, listRuns } from "./runs";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("persisted run APIs", () => {
  it("marks run history unavailable when the optional endpoint returns 404", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("Not found", { status: 404 }),
    ));

    await expect(listRuns("Research")).resolves.toEqual({
      data: [],
      isAvailable: false,
    });
  });

  it("loads produced files when the backend endpoint is available", async () => {
    const outputs = [{
      workspace_id: "Research",
      run_id: "run-1",
      relative_path: "brief.md",
      filename: "brief.md",
      bytes: 42,
      created_at: "2026-06-14T10:00:00Z",
      download_url: "/workspace/Research/runs/run-1/outputs/brief.md",
    }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      Response.json(outputs),
    ));

    await expect(listOutputs("Research")).resolves.toEqual({
      data: outputs,
      isAvailable: true,
    });
  });
});
