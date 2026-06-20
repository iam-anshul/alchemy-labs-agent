import os
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext, ToolOutput
from formats_pydantic import (
    AxisPlanAddition,
    AxisReasoningOutput,
    PlanOutput,
    ReplanDecision,
)
from system_prompts import (
    axis_append_planner_system_prompt,
    axis_reasoning_system_prompt,
    planner_system_prompt,
)
from pydantic_ai.models.openai import OpenAIChatModelSettings
from qwen_compat import QwenChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from dataclasses import dataclass
import base64
from pathlib import Path
from uuid import UUID
from db.utils import (
    get_reportID_by_name,
    list_prior_runs_meta,
    get_run_todo_md,
    list_prior_artifact_manifest,
    get_run_artifacts_by_query_id,
)
from db import SessionLocal
#from source.models import agentDeps

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

model = QwenChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

@dataclass
class PlannerDeps:
    workspace_name: str
    # Current run identity + filesystem subdir, needed by the planner's prior-run
    # look-back tools. subdir_path is the absolute path to THIS run's workspace
    # subdir; fetch_prior_artifact copies prior artifacts into it. current_query_id
    # excludes the in-flight run from history/manifest listings so the planner
    # never sees itself. Optional/defaulted so the replan + axis-append agents
    # (which do NOT get the history tools) can construct PlannerDeps with just
    # workspace_name as before.
    subdir_path: str | None = None
    current_query_id: str | None = None

# Initial planning: produce the full plan from the user's goal.
plannerAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=ToolOutput(PlanOutput, name="submit_plan"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": True}),
)

# Replanning: decide whether the in-flight plan needs revision, and only then
# emit a full revised plan. Separate agent because its output_type differs
# (ReplanDecision, not PlanOutput); it shares the same system prompt so the
# planning rules are identical. Returning needs_change=false on a no-op replan
# means the model never regenerates the task list unnecessarily — which is what
# previously let it occasionally collapse the plan to zero tasks.
replanAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=ToolOutput(ReplanDecision, name="submit_replan_decision"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": True}),
)

# Hidden evidence critic. Its output is intentionally one detailed string: the
# normal planner consumes the critique, while users only see appended tasks.
axisAgent = Agent(
    model,
    system_prompt=axis_reasoning_system_prompt,
    retries=3,
    output_type=ToolOutput(AxisReasoningOutput, name="submit_axis_reasoning"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": True}),
)

# A separate planner output keeps checkpoint replanning append-only by schema.
# It cannot accidentally regenerate or modify the existing todo.
axisAppendPlannerAgent = Agent(
    model,
    system_prompt=axis_append_planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=ToolOutput(AxisPlanAddition, name="append_plan_tasks"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)


def _register_lookup_tools(agent: Agent) -> None:
    """Planning agents only need report lookup tools.

    Ready document ids are injected into the planner prompt as workspace
    context, so there is no document lookup tool for the planner to call.
    """

    @agent.tool(retries=1)
    def fetch_report_ids(ctx: RunContext[PlannerDeps], report_name: str) -> list[str]:
        with SessionLocal() as db:
            return get_reportID_by_name(db, ctx.deps.workspace_name, report_name)


def _register_history_tools(agent: Agent) -> None:
    """Prior-run look-back tools — PLANNER ONLY.

    The initial planner is the one agent that reasons about cross-run
    continuity ("continue the work", "build the deck from that research"). It is
    sandboxed away from other runs' subdirs, so these tools are its only window
    into prior work: it browses cheap metadata, pulls a specific run's final
    todo.md when relevant, and copies a specific prior artifact into THIS run's
    subdir for an executor to read. The replan and axis-append agents
    deliberately do NOT get these — replanning is scoped to the in-flight run.
    """

    @agent.tool(retries=1)
    def list_prior_runs(ctx: RunContext[PlannerDeps]) -> list[dict]:
        """List prior runs in this workspace (newest first), excluding the
        current run. Returns lightweight metadata only (query_id, user_query,
        status, started_at, query_counter, todo_md_chars) — NOT the todo.md
        content. Call this to discover what earlier runs exist before deciding
        whether this query continues earlier work. Then call get_run_todo for
        the specific run(s) you need."""
        exclude = UUID(ctx.deps.current_query_id) if ctx.deps.current_query_id else UUID(int=0)
        with SessionLocal() as db:
            return list_prior_runs_meta(db, ctx.deps.workspace_name, exclude)

    @agent.tool(retries=1)
    def get_run_todo(ctx: RunContext[PlannerDeps], query_id: str) -> str:
        """Return the full final todo.md of one prior run, identified by the
        query_id from list_prior_runs. Use this to see exactly what an earlier
        run planned and which task produced which files, when the current query
        builds on it. Returns a short '(not found / no plan)' message if the run
        has no rendered todo."""
        with SessionLocal() as db:
            todo = get_run_todo_md(db, ctx.deps.workspace_name, UUID(query_id))
        if not todo:
            return f"(no todo.md available for run {query_id})"
        return todo

    @agent.tool(retries=1)
    def list_prior_artifacts(ctx: RunContext[PlannerDeps]) -> list[dict]:
        """List the produced files of all prior runs in this workspace (newest
        run first), excluding the current run. Returns a content-free manifest:
        {query_id, run_started_at, task_id, rel_path, bytes}. Call this to see
        what files earlier runs produced before fetching any. To actually make a
        file available to an executor in THIS run, call fetch_prior_artifact."""
        exclude = UUID(ctx.deps.current_query_id) if ctx.deps.current_query_id else UUID(int=0)
        with SessionLocal() as db:
            return list_prior_artifact_manifest(db, ctx.deps.workspace_name, exclude)

    @agent.tool(retries=1)
    def fetch_prior_artifact(
        ctx: RunContext[PlannerDeps], query_id: str, rel_path: str
    ) -> str:
        """Copy ONE prior run's produced file into the CURRENT run's subdir so an
        executor in this run can read it by path. Identify the file by the
        query_id + rel_path from list_prior_artifacts. The file content does NOT
        enter your context — this returns only a short confirmation (the written
        path + byte size). After fetching, write a task that references the file
        BY PATH in its query/expects with EMPTY deps, and instruct the executor
        to read it. Only fetch what the current query genuinely needs.

        Isolation: reads bytes from the database (never from another run's
        directory) and writes only inside this run's subdir."""
        if not ctx.deps.subdir_path:
            return "error: the current run subdir is not available; cannot fetch."
        with SessionLocal() as db:
            artifacts = get_run_artifacts_by_query_id(
                db, ctx.deps.workspace_name, UUID(query_id)
            )
        if artifacts is None:
            return f"error: run {query_id} not found in this workspace."
        match = next((a for a in artifacts if a.get("rel_path") == rel_path), None)
        if match is None:
            return f"error: no artifact {rel_path!r} in run {query_id}."
        content_b64 = match.get("content_b64")
        if content_b64 is None:
            return f"error: artifact {rel_path!r} has no stored content."

        # Resolve the destination strictly inside the current subdir — guard
        # against a rel_path that tries to escape via .. or an absolute path.
        subdir = Path(ctx.deps.subdir_path).resolve()
        dest = (subdir / rel_path).resolve()
        try:
            dest.relative_to(subdir)
        except ValueError:
            return f"error: refusing to write {rel_path!r} outside the run subdir."

        raw = base64.b64decode(content_b64)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return (
            f"fetched {rel_path} ({len(raw)} bytes) from run {query_id} into this "
            f"run's subdir. Reference it by path '{rel_path}' with empty deps and "
            "tell the executor to read it."
        )


_register_lookup_tools(plannerAgent)
_register_lookup_tools(replanAgent)
_register_lookup_tools(axisAppendPlannerAgent)
_register_history_tools(plannerAgent)
