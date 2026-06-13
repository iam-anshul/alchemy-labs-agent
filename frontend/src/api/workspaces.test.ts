import { describe, expect, it } from "vitest";

import { parseWorkspaceNames } from "./workspaces";

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
