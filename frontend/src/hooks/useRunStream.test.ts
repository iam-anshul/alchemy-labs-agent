import { describe, expect, it } from "vitest";

import { getRunStreamUrl } from "./useRunStream";

describe("getRunStreamUrl", () => {
  it("uses the stream URL returned when the backend accepts a run", () => {
    expect(getRunStreamUrl("run-1", "/chat/custom-run/stream")).toBe(
      "/chat/custom-run/stream",
    );
  });

  it("falls back to the documented chat stream route", () => {
    expect(getRunStreamUrl("run with spaces")).toBe(
      "/chat/run%20with%20spaces/stream",
    );
  });
});
