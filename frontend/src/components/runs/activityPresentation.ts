import type { RunEvent } from "../../types/events";

export interface ActivityPresentation {
  label: string;
  tone: "slate" | "blue" | "green" | "brown" | "purple";
}

export function getActivityPresentation(event: RunEvent): ActivityPresentation {
  if (event.agent_type === "browser" || event.agent_type === "web_search") {
    return { label: "Searching the web", tone: "blue" };
  }
  if (event.agent_type === "document_answering") {
    return { label: "Reading documents", tone: "green" };
  }
  if (event.agent_type === "office") {
    return { label: "Preparing files", tone: "brown" };
  }
  if (event.agent_type === "planner") {
    return event.event === "awaiting_user_input"
      ? { label: "Needs your input", tone: "purple" }
      : { label: "Planning", tone: "purple" };
  }
  if (event.event === "run_started") {
    return { label: "Run started", tone: "slate" };
  }
  if (event.event === "run_ended") {
    return { label: "Run complete", tone: "green" };
  }
  if (event.stage === "validating") {
    return { label: "Checking results", tone: "slate" };
  }
  if (event.artifacts.some((artifact) => artifact.kind === "final_answer")) {
    return { label: "Answer ready", tone: "green" };
  }
  return { label: "Working", tone: "slate" };
}

export function getEventStateLabel(event: RunEvent) {
  if (event.event === "artifact_ready") return "output";
  if (event.event === "awaiting_user_input") return "needs you";
  if (event.status === "completed") return "done";
  if (event.status === "failed") return "failed";
  if (event.status === "waiting") return "waiting";
  if (event.status === "started") return "started";
  return "working";
}
