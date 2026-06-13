import { describe, expect, it } from "vitest";

import type { Artifact } from "../../types/events";
import { getPreviewKind } from "./ProducedFilePreview";

function artifact(filename: string, mimeType: string | null = null): Artifact {
  return {
    kind: "file",
    path: `outputs/${filename}`,
    filename,
    type: filename.split(".").pop() ?? null,
    mime_type: mimeType,
    bytes: 10,
    content: null,
    content_base64: null,
    url: `/outputs/${filename}`,
    metadata: {},
  };
}

describe("getPreviewKind", () => {
  it.each([
    ["report.md", "markdown"],
    ["data.json", "json"],
    ["data.csv", "csv"],
    ["workbook.xlsx", "xlsx"],
    ["photo.png", "image"],
    ["report.pdf", "pdf"],
    ["letter.docx", "docx"],
    ["slides.pptx", "pptx"],
    ["recording.mp3", "audio"],
    ["demo.mp4", "video"],
  ])("routes %s to the %s renderer", (filename, expectedKind) => {
    const mimeTypes: Record<string, string> = {
      "photo.png": "image/png",
      "recording.mp3": "audio/mpeg",
      "demo.mp4": "video/mp4",
    };

    expect(getPreviewKind(artifact(filename, mimeTypes[filename] ?? null))).toBe(
      expectedKind,
    );
  });
});
