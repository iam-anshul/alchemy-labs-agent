import { afterEach, describe, expect, it, vi } from "vitest";

import { deleteWorkspace, parseWorkspaceNames } from "./workspaces";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseWorkspaceNames", () => {
  it("accepts the current backend workspace-name response", () => {
    expect(parseWorkspaceNames(["Research", "Vendor review"])).toEqual([
      "Research",
      "Vendor review",
    ]);
  });

  it("rejects an unexpected workspace response", () => {
    expect(() => parseWorkspaceNames([{ workspace_id: "Research" }])).toThrow(
      "unexpected format",
    );
  });
});

describe("deleteWorkspace", () => {
  it("uses the current backend delete route and encodes the workspace name", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ status: "deleted" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await deleteWorkspace("Vendor risk");

    expect(fetchMock).toHaveBeenCalledWith(
      "/workspace/delete_workspace?workspace_name=Vendor+risk",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
