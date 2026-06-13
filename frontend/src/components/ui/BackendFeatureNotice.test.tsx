import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import BackendFeatureNotice from "./BackendFeatureNotice";

afterEach(cleanup);

describe("BackendFeatureNotice", () => {
  it.each([
    ["Run history is not available yet", "workspace run-list endpoint"],
    ["Saved output downloads are not available yet", "additional backend endpoints"],
  ])("marks %s as unavailable", (title, detail) => {
    render(<BackendFeatureNotice title={title} detail={detail} />);

    expect(screen.getByText(title).closest("[aria-disabled='true']")).toBeInTheDocument();
    expect(screen.getByText(detail)).toBeInTheDocument();
    expect(screen.getByText("Backend API required")).toBeInTheDocument();
  });
});
