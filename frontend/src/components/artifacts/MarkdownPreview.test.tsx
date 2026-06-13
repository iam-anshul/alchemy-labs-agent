import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MarkdownPreview from "./MarkdownPreview";

describe("MarkdownPreview", () => {
  it("presents todo markdown as a readable run plan", () => {
    render(
      <MarkdownPreview
        filename="todo.md"
        content={"# Research plan\n\n- [x] Find sources\n- [ ] Write summary"}
      />,
    );

    expect(screen.getByText("Run plan")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Research plan" })).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.getAllByRole("checkbox")[0]).toBeChecked();
  });

  it("supports GitHub-flavored tables", () => {
    render(
      <MarkdownPreview
        content={"| Name | Status |\n| --- | --- |\n| Report | Ready |"}
      />,
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });
});
