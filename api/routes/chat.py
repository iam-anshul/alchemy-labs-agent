import os
import base64
import hashlib
import traceback
from pathlib import Path
import logfire
from orchestrator import (
    axisAgent,
    axisAppendPlannerAgent,
    plannerAgent,
    replanAgent,
    PlannerDeps,
)
from browser_agent import BrowserExecutor, ExecutorResult
from agent_schemas import QueryAnswer
from report_schemas import ReportResult
from office_agent import run_office_executor
from web_agent import CachedPage, run_web_executor
from api.ingest import start_workers, shutdown_workers
from api.routes.documents import ingest_local_file
from db import SessionLocal
from db import utils as db_utils
from formats_pydantic import (
    AxisPlanAddition,
    QueryRun,
    PlanOutput,
    TaskSpec,
    ChatAcceptedResponse,
    InternalDocAgentDeps,
)
from render_todo import render_todo
from time import time
import asyncio
import shutil
from uuid import uuid4, UUID
from shared import sse_stream


from report import draft_report
from agent import answer_query

from fastapi import Depends, APIRouter, HTTPException, Request
from supertokens_python.recipe.session.framework.fastapi import verify_session
from supertokens_python.recipe.session import SessionContainer

from api.events import EventSink, bus, file_artifact

from dotenv import load_dotenv
import os

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

chat_router = APIRouter()

# How many times to dispatch a task before marking it failed. Each attempt
# builds a fresh executor (and, for browser tasks, a fresh browser), so a retry
# sidesteps a wedged session rather than re-poking it. This catches transient
# failures (a one-off hang, a flaky page) without spending a replan.
MAX_DISPATCH_ATTEMPTS = 3
AXIS_EVIDENCE_FILE_CHAR_LIMIT = 20_000
AXIS_EVIDENCE_TOTAL_CHAR_LIMIT = 80_000
AXIS_APPEND_ATTEMPTS = 3
PLANNER_DOC_SUMMARY_CHAR_LIMIT = 1_200
PLANNER_DOC_INVENTORY_TOTAL_CHAR_LIMIT = 60_000
AXIS_READABLE_SUFFIXES = {
    ".csv",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

_pending_input: dict[str, asyncio.Future] = {}

def make_workspace(workspace_path):
    os.makedirs(workspace_path)
    return workspace_path


def workspace_dir(user_id: str, workspace_name: str) -> Path:
    """Absolute path to a workspace's directory on disk.

    Workspaces are namespaced under the owning user's id so different users'
    workspaces never share a parent folder:

        file_system_root/<user_id>/<workspace_name>

    This is the single source of truth for that layout — create, delete, and
    run all derive their paths from here so they can never drift apart.
    """
    return Path.cwd() / "file_system_root" / str(user_id) / workspace_name


def _read_artifacts(subdir: Path, produced: list[str], task_id: str) -> list[dict]:
    """Read a completed task's produced files into persistable artifact dicts.
    Each: {rel_path, content_b64, bytes, task_id}. Skips files that don't exist
    (validation runs separately) and is binary-safe (base64)."""
    artifacts: list[dict] = []
    for rel in produced:
        full = subdir / rel
        if not full.is_file():
            continue
        raw = full.read_bytes()
        artifacts.append({
            "rel_path": rel,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "bytes": len(raw),
            "task_id": task_id,
        })
    return artifacts


def _restore_artifacts(subdir: Path, artifacts: list[dict]) -> list[str]:
    """Materialize persisted artifacts into a run's subdir. Decodes base64 back
    to bytes at each artifact's rel_path. Never overwrites a file the current
    run already has. Returns the workspace-relative paths written."""
    written: list[str] = []
    for art in artifacts:
        rel = art.get("rel_path")
        content_b64 = art.get("content_b64")
        if not rel or content_b64 is None:
            continue
        dest = subdir / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(content_b64))
        written.append(rel)
    return written

def write_todo_atomic(run: QueryRun) -> None:
    todo_path = Path(run.workspace) / "todo.md"
    tmp = todo_path.with_suffix(".md.tmp")
    tmp.write_text(render_todo(run), encoding="utf-8")
    tmp.replace(todo_path)

def _merge_plan(old: PlanOutput, new: PlanOutput) -> PlanOutput:
    """Adopt a revised plan while preserving control-loop-owned state.

    The planner emits TaskSpec objects with default status='pending' and
    produced=[]. Without merging, replacing the plan would clobber the
    completion state of tasks we've already finished. We carry status and
    produced over from the old plan for any task id that survives.
    """
    old_by_id = {t.id: t for t in old.tasks}

    for t in new.tasks:
        if t.id in old_by_id:
            old_t = old_by_id[t.id]
            if old_t.axis_checkpoint and not t.axis_checkpoint:
                # Axis controls are intentionally absent from todo.md, so the
                # ordinary replanner may omit them when regenerating a full
                # plan. Preserve a pending checkpoint unless the task id itself
                # is removed from the revised plan.
                t.axis_checkpoint = True
                t.axis_focus = old_t.axis_focus
            rewritten = (t.query, t.expects, t.agent, t.doc_deps, t.deps) != (old_t.query, old_t.expects, old_t.agent, old_t.doc_deps, old_t.deps)
            if old_t.status == "failed" and rewritten:
                # planner changed approach for a failed task -> let it run again.
                # The old error/notes describe the abandoned approach, so clear
                # them; the task starts fresh.
                t.status = "pending"
                t.produced = []
                t.error = ""
                t.notes = ""
            else:
                # Preserve all control-loop-owned state. error and notes must be
                # carried too, or they're wiped on every replan — a failed task's
                # diagnostic would vanish before a second replan, and the planner
                # could re-propose the same doomed plan having forgotten why it
                # failed.
                t.status = old_t.status
                t.produced = old_t.produced
                t.error = old_t.error
                t.notes = old_t.notes

    # Mark routed-around failures as superseded. When a replan recovers from a
    # failed task by inserting a recovery sub-chain (e.g. t1 download failed ->
    # add t3 search + t4 re-download, and re-point t2's deps onto t4), the dead
    # task is left in the plan but NOTHING depends on it anymore. Left as
    # 'failed' it would (1) poison the final run status — any failed task marks
    # the whole run failed, so a fully-recovered run reads as failed — and (2)
    # risk the upstream-failure sweep killing a downstream task that still
    # happened to reference it. A failed task that no surviving task depends on
    # has been abandoned by the replan; record that distinctly as 'superseded'.
    # We only downgrade 'failed' (never completed/pending/dispatched), so a
    # genuinely-blocking failure that downstream tasks still depend on stays
    # 'failed' and keeps propagating.
    depended_on: set[str] = set()
    for t in new.tasks:
        depended_on.update(t.deps)
    for t in new.tasks:
        if t.status == "failed" and t.id not in depended_on:
            t.status = "superseded"
    return new


def _axis_evidence_task_ids(run: QueryRun, checkpoint_task: TaskSpec) -> list[str]:
    """Return the completed transitive dependency set for a checkpoint."""
    if run.plan is None:
        return []
    tasks_by_id = {task.id: task for task in run.plan.tasks}
    selected: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in selected:
            return
        task = tasks_by_id.get(task_id)
        if task is None or task.status != "completed":
            return
        for dep_id in task.deps:
            visit(dep_id)
        selected.add(task_id)

    visit(checkpoint_task.id)
    return [task.id for task in run.plan.tasks if task.id in selected]


def _build_axis_evidence_bundle(run: QueryRun, checkpoint_task: TaskSpec) -> str:
    """Load bounded readable artifacts produced along the checkpoint path."""
    if run.plan is None:
        return "No completed evidence was available."

    workspace = Path(run.workspace).resolve()
    tasks_by_id = {task.id: task for task in run.plan.tasks}
    sections: list[str] = []
    remaining = AXIS_EVIDENCE_TOTAL_CHAR_LIMIT

    for task_id in _axis_evidence_task_ids(run, checkpoint_task):
        task = tasks_by_id[task_id]
        lines = [
            f"Task {task.id}: {task.title}",
            f"Task query: {task.query}",
            f"Executor notes: {task.notes or '(none)'}",
        ]
        for rel in task.produced:
            full = (workspace / rel).resolve()
            try:
                full.relative_to(workspace)
            except ValueError:
                lines.append(f"Artifact {rel}: omitted because it is outside the workspace.")
                continue
            if full.suffix.lower() not in AXIS_READABLE_SUFFIXES:
                lines.append(f"Artifact {rel}: binary or unsupported for inline review.")
                continue
            if not full.is_file():
                lines.append(f"Artifact {rel}: missing.")
                continue
            if remaining <= 0:
                lines.append("Further artifacts omitted because the evidence limit was reached.")
                break
            try:
                content = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                lines.append(f"Artifact {rel}: could not be decoded as UTF-8.")
                continue

            limit = min(AXIS_EVIDENCE_FILE_CHAR_LIMIT, remaining)
            excerpt = content[:limit]
            remaining -= len(excerpt)
            suffix = "\n[artifact truncated]" if len(content) > limit else ""
            lines.append(f"Artifact {rel}:\n{excerpt}{suffix}")

        sections.append("\n".join(lines))
        if remaining <= 0:
            break

    return "\n\n".join(sections) or "No completed readable evidence was available."


def _validate_axis_addition(
    run: QueryRun,
    checkpoint_task: TaskSpec,
    addition: AxisPlanAddition,
) -> str | None:
    """Validate append-only ids, dependency order, and checkpoint reachability."""
    if run.plan is None:
        return "the current plan is missing"

    existing_ids = {task.id for task in run.plan.tasks}
    known_ids = set(existing_ids)
    reaches_checkpoint: set[str] = set()
    new_ids: set[str] = set()
    checkpoint_positions = [
        index for index, task in enumerate(addition.tasks) if task.axis_checkpoint
    ]

    if len(checkpoint_positions) > 1:
        return "the appended segment contains more than one axis checkpoint"
    if checkpoint_positions and checkpoint_positions[0] != len(addition.tasks) - 1:
        return "an appended axis checkpoint must be the final task in the segment"
    if checkpoint_positions and run.axis_checkpoints_used + 1 >= run.axis_checkpoint_budget:
        return "the appended segment requests a checkpoint but no checkpoint budget remains"

    for task in addition.tasks:
        if task.id in known_ids or task.id in new_ids:
            return f"task id {task.id!r} is not a unique new id"
        unknown_deps = [dep for dep in task.deps if dep not in known_ids]
        if unknown_deps:
            return (
                f"task {task.id!r} depends on unknown or later task ids "
                f"{unknown_deps}"
            )
        if (
            checkpoint_task.id not in task.deps
            and not any(dep in reaches_checkpoint for dep in task.deps)
        ):
            return (
                f"task {task.id!r} is not downstream of checkpoint "
                f"{checkpoint_task.id!r}"
            )

        new_ids.add(task.id)
        known_ids.add(task.id)
        reaches_checkpoint.add(task.id)

    return None


def _append_axis_tasks(run: QueryRun, addition: AxisPlanAddition) -> None:
    """Append validated planner tasks while resetting model-owned run state."""
    if run.plan is None:
        raise ValueError("cannot append tasks without a current plan")
    for task in addition.tasks:
        task.status = "pending"
        task.produced = []
        task.notes = ""
        task.error = ""
        run.plan.tasks.append(task)
    if addition.notes:
        previous = (run.plan.notes or "").strip()
        run.plan.notes = "\n".join(part for part in (previous, addition.notes.strip()) if part)


def _truncate_planner_text(text: str | None, limit: int) -> str:
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + " [truncated]"


def _format_available_docs_for_planner(workspace_id: str) -> str:
    """Render ready workspace documents directly into planner context."""
    try:
        with SessionLocal() as db:
            docs = db_utils.list_ready_docs_for_planner(db, workspace_id)
    except Exception as e:
        return (
            "Available workspace documents: unavailable because the document "
            f"inventory query failed ({type(e).__name__}: {e})."
        )

    if not docs:
        return (
            "Available workspace documents: none are fully ingested and ready. "
            "Queued, processing, or failed documents are omitted because the "
            "document_answering agent cannot use them yet."
        )

    lines = [
        "Available workspace documents (ready only; queued/processing/failed docs are omitted):",
        "Use these exact doc_id values in doc_deps.doc_ids when the user names a specific ready document.",
        "Leave doc_deps.doc_ids=None for general questions over all ready documents.",
        "Each top_level_summary is the document-level root summary produced at ingestion time.",
    ]
    summary_budget = PLANNER_DOC_INVENTORY_TOTAL_CHAR_LIMIT

    for index, doc in enumerate(docs, start=1):
        source_name = Path(doc.source_path).name if doc.source_path else "(unknown source)"
        created_at = doc.created_at.isoformat() if doc.created_at else "(unknown)"
        if summary_budget > 0:
            summary_limit = min(PLANNER_DOC_SUMMARY_CHAR_LIMIT, summary_budget)
            summary = _truncate_planner_text(doc.doc_summary, summary_limit)
            summary_budget -= len(summary)
        elif doc.doc_summary:
            summary = "(summary omitted because the planner inventory summary budget was reached)"
        else:
            summary = ""
        entry = (
            f"{index}. doc_id={doc.doc_id}\n"
            f"   title={doc.title or '(untitled)'}\n"
            f"   source_file={source_name}\n"
            f"   pages={doc.n_pages if doc.n_pages is not None else 'unknown'}; "
            f"tables={doc.n_tables if doc.n_tables is not None else 'unknown'}; "
            f"created_at={created_at}\n"
            f"   top_level_summary={summary or '(no summary available)'}"
        )
        lines.append(entry)

    return "\n".join(lines)


def _with_planner_workspace_context(workspace_id: str, prompt: str, current_query_id=None) -> str:
    return (
        f"{_format_available_docs_for_planner(workspace_id)}\n\n"
        f"{_format_prior_runs_hint(workspace_id, current_query_id)}\n\n"
        f"Planner request:\n{prompt}"
    )


def _initial_axis_plan_error(plan: PlanOutput) -> str | None:
    checkpoint_positions = [
        index for index, task in enumerate(plan.tasks) if task.axis_checkpoint
    ]
    if len(checkpoint_positions) > 1:
        return "an initial plan may contain at most one axis checkpoint"
    if checkpoint_positions and checkpoint_positions[0] != len(plan.tasks) - 1:
        return "an initial axis checkpoint must be the final task in the plan segment"
    return None


def _replan_axis_error(run: QueryRun, plan: PlanOutput) -> str | None:
    """Validate the effective pending checkpoint after a full-plan replan."""
    if run.plan is None:
        return None
    old_by_id = {task.id: task for task in run.plan.tasks}
    checkpoint_positions: list[int] = []

    for index, task in enumerate(plan.tasks):
        old_task = old_by_id.get(task.id)
        effective_checkpoint = task.axis_checkpoint or bool(
            old_task
            and old_task.status == "pending"
            and old_task.axis_checkpoint
        )
        effective_status = old_task.status if old_task is not None else "pending"
        if effective_status == "pending" and effective_checkpoint:
            checkpoint_positions.append(index)

    if len(checkpoint_positions) > 1:
        return "a revised plan may contain at most one pending axis checkpoint"
    if checkpoint_positions and checkpoint_positions[0] != len(plan.tasks) - 1:
        return "a pending axis checkpoint must be the final task in the plan segment"
    if checkpoint_positions and run.axis_checkpoints_used >= run.axis_checkpoint_budget:
        return "the axis checkpoint budget is exhausted"
    return None


async def run_axis_checkpoint(
    run: QueryRun,
    checkpoint_task: TaskSpec,
    sink: EventSink,
) -> AxisPlanAddition:
    """Critique checkpoint evidence, then obtain a validated append-only segment."""
    axis_sink = sink.child(task_id=checkpoint_task.id, agent_type="planner")
    with logfire.span(
        "axis checkpoint",
        workspace_id=run.workspace_id,
        task_id=checkpoint_task.id,
        task_title=checkpoint_task.title,
        checkpoints_used=run.axis_checkpoints_used,
        checkpoint_budget=run.axis_checkpoint_budget,
        produced_count=len(checkpoint_task.produced),
    ):
        await axis_sink.publish_ui(
            "agent_started",
            stage="replanning",
            status="started",
            message="Reviewing completed evidence",
            data={"phase": "evidence_review"},
        )

        evidence_bundle = _build_axis_evidence_bundle(run, checkpoint_task)
        critique_prompt = (
            f"USER QUESTION:\n{run.user_query}\n\n"
            f"CURRENT PLAN:\n{render_todo(run)}\n\n"
            f"CHECKPOINT TASK:\n"
            f"id: {checkpoint_task.id}\n"
            f"title: {checkpoint_task.title}\n"
            f"focus: {checkpoint_task.axis_focus}\n\n"
            f"COMPLETED EVIDENCE:\n{evidence_bundle}"
        )
        with logfire.span(
            "axis evidence critic",
            workspace_id=run.workspace_id,
            task_id=checkpoint_task.id,
            evidence_chars=len(evidence_bundle),
        ):
            critique_run = await axisAgent.run(user_prompt=critique_prompt)
        critique = critique_run.output.reasoning

        existing_ids = [task.id for task in run.plan.tasks] if run.plan else []
        remaining_budget = max(
            0,
            run.axis_checkpoint_budget - run.axis_checkpoints_used - 1,
        )
        base_append_prompt = (
            f"USER GOAL:\n{run.goal}\n\n"
            f"{_format_available_docs_for_planner(run.workspace_id)}\n\n"
            f"CURRENT PLAN:\n{render_todo(run)}\n\n"
            f"COMPLETED CHECKPOINT:\n"
            f"id: {checkpoint_task.id}\n"
            f"title: {checkpoint_task.title}\n"
            f"produced: {checkpoint_task.produced}\n\n"
            f"RESERVED EXISTING TASK IDS:\n{existing_ids}\n\n"
            f"REMAINING CHECKPOINT BUDGET AFTER THIS PASS:\n{remaining_budget}\n\n"
            f"INTERNAL EVIDENCE CRITIQUE:\n{critique}\n\n"
            "Append the next task segment. Return only new tasks."
        )

        validation_error: str | None = None
        for attempt in range(1, AXIS_APPEND_ATTEMPTS + 1):
            prompt = base_append_prompt
            if validation_error:
                prompt += (
                    "\n\nYour previous appended segment was rejected for this "
                    f"control-loop reason:\n{validation_error}\n"
                    "Correct the segment while preserving the append-only rules."
                )
            with logfire.span(
                "axis append planner",
                workspace_id=run.workspace_id,
                task_id=checkpoint_task.id,
                attempt=attempt,
                remaining_checkpoint_budget=remaining_budget,
            ):
                append_run = await axisAppendPlannerAgent.run(
                    user_prompt=prompt,
                    deps=PlannerDeps(workspace_name=run.workspace_id),
                )
            addition = append_run.output
            validation_error = _validate_axis_addition(run, checkpoint_task, addition)
            if validation_error is None:
                await axis_sink.publish_ui(
                    "agent_ended",
                    stage="replanning",
                    status="completed",
                    message="Plan updated from completed evidence",
                    data={"phase": "evidence_review", "tasks_added": len(addition.tasks)},
                )
                logfire.info(
                    "axis checkpoint appended tasks",
                    workspace_id=run.workspace_id,
                    task_id=checkpoint_task.id,
                    tasks_added=len(addition.tasks),
                    attempt=attempt,
                )
                return addition

            logfire.warning(
                "axis append planner rejected",
                workspace_id=run.workspace_id,
                task_id=checkpoint_task.id,
                attempt=attempt,
                validation_error=validation_error,
            )

    raise RuntimeError(
        "axis append planner could not produce a valid task segment after "
        f"{AXIS_APPEND_ATTEMPTS} attempts: {validation_error}"
    )
    
def _format_prior_runs_hint(workspace_id: str, current_query_id) -> str:
    """One-line awareness hint pushed into the planner's prompt context.

    History is now PULLED (the planner calls list_prior_runs / get_run_todo /
    list_prior_artifacts / fetch_prior_artifact on demand) rather than pushed
    as a synthetic message_history. But the planner can't decide to look back
    if it doesn't know history exists — so we always push this cheap one-liner:
    how many prior runs there are and what the most recent one was. The full
    todo.md timeline is no longer injected; the planner fetches the specific
    runs it judges relevant. Best-effort: a lookup failure degrades to a
    neutral note rather than breaking planning."""
    exclude = current_query_id if isinstance(current_query_id, UUID) else (
        UUID(str(current_query_id)) if current_query_id else UUID(int=0)
    )
    try:
        with SessionLocal() as db:
            count, latest = db_utils.count_prior_runs(
                db, workspace_id=workspace_id, exclude_query_id=exclude
            )
    except Exception as e:
        return (
            "Prior-run history: unavailable because the history lookup failed "
            f"({type(e).__name__}: {e}). Proceed as if this is the first run."
        )

    if count == 0:
        return (
            "Prior-run history: this is the first run in the workspace — there is "
            "no earlier work to continue."
        )

    latest_line = ""
    if latest is not None:
        latest_line = (
            f" Most recent: \"{_truncate_planner_text(latest.get('user_query'), 200)}\" "
            f"(status={latest.get('status')}, query_counter={latest.get('query_counter')})."
        )
    return (
        f"Prior-run history: this workspace has {count} prior run(s).{latest_line} "
        "If this query continues or builds on earlier work, use the look-back "
        "tools: list_prior_runs to browse, get_run_todo(query_id) to read a "
        "specific past plan, list_prior_artifacts to see prior files, and "
        "fetch_prior_artifact(query_id, rel_path) to copy a specific older file "
        "into this run for an executor to read. The MOST RECENT run's files are "
        "already restored into this run's outputs/ — fetch only when you need an "
        "OLDER run's output. Do not call these tools for a self-contained new "
        "request that does not build on prior work."
    )

async def planner(run: QueryRun, sink: EventSink) -> PlanOutput | None:
    """Run the planner LLM.

    Initial call (no plan on the run yet): generate the plan from the user's
    goal alone, and return it (always a PlanOutput with >=1 task). If this run
    is part of a persistent workspace, prior runs' todo.mds are reconstructed as
    message_history so the planner sees the conversation timeline.

    Subsequent calls (replan): render the current todo.md and ask the replan
    agent whether the plan needs revision. It returns a ReplanDecision; we
    return the COMPLETE revised plan when needs_change is True, or None when the
    plan should stay as-is (the common case). Returning None means the caller
    leaves run.plan untouched and does not spend replan budget. NO history
    injection — replans are scoped to the in-flight run only, per design.
    """

    if not run.plan:
        await sink.child(agent_type="planner").publish_ui(
            "agent_started",
            stage="planning",
            status="started",
            message="Planning the work",
            data={"phase": "initial"},
        )
        # Cross-run history is no longer pushed as message_history. Instead a
        # one-line awareness hint is folded into the workspace context, and the
        # planner PULLS prior plans/artifacts on demand via its look-back tools
        # (list_prior_runs / get_run_todo / list_prior_artifacts /
        # fetch_prior_artifact). Those tools need this run's subdir + identity,
        # supplied through PlannerDeps below.
        validation_error: str | None = None
        for _ in range(3):
            initial_prompt = run.goal
            if validation_error:
                initial_prompt += (
                    "\n\nYour previous plan violated this internal checkpoint "
                    f"rule: {validation_error}. Correct the plan. If you use an "
                    "axis checkpoint, the current task segment must end there."
                )
            planner_run = await plannerAgent.run(
                user_prompt=_with_planner_workspace_context(
                    run.workspace_id, initial_prompt, run.query_id
                ),
                deps=PlannerDeps(
                    workspace_name=run.workspace_id,
                    subdir_path=run.workspace,
                    current_query_id=str(run.query_id) if run.query_id else None,
                ),
            )
            validation_error = _initial_axis_plan_error(planner_run.output)
            if validation_error is None:
                run.planner_messages = planner_run.all_messages()
                return planner_run.output
        raise RuntimeError(
            "planner could not produce a valid checkpoint segment after "
            f"3 attempts: {validation_error}"
        )

    current_todo = render_todo(run)

    # Executor errors are deliberately NOT rendered into todo.md (that file is
    # user-facing; raw failure diagnostics would only clutter it). We surface
    # them to the planner here instead, on a separate internal channel, so it
    # can decide whether to re-route a failed task — without the user seeing it.
    failures = [
        f"- {t.id} ({t.title}): {t.error}"
        for t in run.plan.tasks
        if t.status == "failed" and t.error
    ]
    failure_section = (
        "\n\nExecutor failure details (internal — not shown to the user). Use "
        "these to decide whether a failed task should be re-routed with a "
        "different approach, and avoid re-proposing an approach that already "
        "failed for the stated reason:\n" + "\n".join(failures)
    ) if failures else ""

    pending_checkpoints = [
        task for task in run.plan.tasks
        if task.status == "pending" and task.axis_checkpoint
    ]
    checkpoint_section = (
        "\n\nInternal checkpoint state (never shown to the user):\n"
        f"- passes used: {run.axis_checkpoints_used} / {run.axis_checkpoint_budget}\n"
        + (
            "\n".join(
                f"- pending checkpoint {task.id}: {task.axis_focus}"
                for task in pending_checkpoints
            )
            if pending_checkpoints
            else "- pending checkpoint: none"
        )
        + "\nPreserve any pending checkpoint on the same task id. A pending "
        "checkpoint must remain the final task in the current plan segment."
    )

    base_replan_prompt = (
        f"Goal: {run.goal}\n\n"
        f"{_format_available_docs_for_planner(run.workspace_id)}\n\n"
        f"Current plan state:\n\n"
        f"{current_todo}{failure_section}{checkpoint_section}\n\n"
        "Review the plan above. If executor notes or completed task results "
        "warrant a change, return the revised plan. Otherwise return the "
        "plan unchanged."
    )
    await sink.child(agent_type="planner").publish_ui(
        "agent_started",
        stage="replanning",
        status="started",
        message="Checking whether the plan needs updates",
        data={"phase": "replan", "replans_used": run.replans_used, "replan_budget": run.replan_budget},
    )
    validation_error: str | None = None
    for _ in range(3):
        replan_prompt = base_replan_prompt
        if validation_error:
            replan_prompt += (
                "\n\nYour previous revision violated this internal checkpoint "
                f"rule: {validation_error}. Correct the complete revised plan."
            )
        replan_run = await replanAgent.run(
            user_prompt=replan_prompt,
            deps=PlannerDeps(workspace_name=run.workspace_id),
        )
        decision = replan_run.output
        # No change wanted, or the model said "change" but gave no tasks -> treat as
        # a no-op (don't adopt anything, don't spend budget). Assembling the revised
        # PlanOutput from the flat decision fields ourselves (rather than nesting a
        # PlanOutput in the schema) avoids the model stringifying a nested object.
        if not decision.needs_change or not decision.tasks:
            return None
        candidate = PlanOutput(
            goal=decision.goal,
            tasks=decision.tasks,
            needs_user_feedback=decision.needs_user_feedback,
            feedback_question=decision.feedback_question,
            notes=decision.notes,
        )
        validation_error = _replan_axis_error(run, candidate)
        if validation_error is None:
            return candidate
    raise RuntimeError(
        "replanner could not preserve a valid checkpoint segment after "
        f"3 attempts: {validation_error}"
    )

async def publish_todo_artifact(run: QueryRun, sink: EventSink, *, phase: str) -> None:
    todo_path = Path(run.workspace) / "todo.md"
    content = todo_path.read_text(encoding="utf-8") if todo_path.exists() else render_todo(run)
    artifact = file_artifact(
        kind="markdown",
        path="todo.md",
        filename="todo.md",
        type="md",
        mime_type="text/markdown",
        bytes=todo_path.stat().st_size if todo_path.exists() else len(content.encode("utf-8")),
        content=content,
        metadata={"phase": phase},
    )
    planner_sink = sink.child(agent_type="planner")
    await planner_sink.publish_ui(
        "artifact_ready",
        stage=phase,
        status="progress",
        message="Plan file is ready",
        artifacts=[artifact],
    )
    await planner_sink.publish_ui(
        "agent_ended",
        stage=phase,
        status="completed",
        message="Planning complete",
        data={
            "phase": phase,
            "n_tasks": len(run.plan.tasks) if run.plan else 0,
            "needs_user_feedback": bool(run.plan and run.plan.needs_user_feedback),
        },
        artifacts=[artifact],
    )
    
# Terminal states for a Doc row's ingest pipeline (queued -> building_tree ->
# ready | failed). We poll for these because ingestion runs async on the shared
# worker queue; there is no per-doc await, and _ingest_queue.join() would block
# on unrelated docs too.
_INGEST_TERMINAL = {"ready", "failed"}
INGEST_POLL_TIMEOUT_SECONDS = 3600
INGEST_POLL_INTERVAL_SECONDS = 2


async def ingest_dep_pdfs(
    dep_files: list[str],
    subdir_path: Path,
    workspace_id: str,
    user_id: UUID,
    sink: EventSink,
) -> list[str]:
    pdfs = [p for p in dep_files if Path(p).suffix.lower() == ".pdf"]
    if not pdfs:
        return []

    ready_ids: list[str] = []
    for rel in pdfs:
        full = subdir_path / rel
        if not full.exists():
            await sink.publish_ui(
                "agent_progress",
                stage="ingesting",
                status="progress",
                message=f"Skipping missing dep PDF {rel}",
                data={"path": rel},
            )
            continue

        # Reuse an already-ingested copy if this workspace has one. Scope is the
        # workspace (which spans every chat run / subdir), not this run's subdir,
        # and the match is by content hash (sha256 of the bytes) — ingest_local_file
        # randomizes the saved source_path by doc_id so path can't be the key, and
        # a filename can collide across genuinely different PDFs. Hashing skips the
        # expensive re-ingest when the same PDF was ingested in a prior run, an
        # uploaded doc the planner already resolved, or a retry of this task.
        content_hash = hashlib.sha256(full.read_bytes()).hexdigest()
        with SessionLocal() as db:
            existing_id = db_utils.get_ready_docID_by_hash(db, workspace_id, content_hash)
        if existing_id is not None:
            ready_ids.append(existing_id)
            await sink.publish_ui(
                "agent_progress",
                stage="ingesting",
                status="progress",
                message=f"Reusing already-ingested {full.name}",
                data={"doc_id": existing_id, "path": rel},
            )
            continue

        doc_id = ingest_local_file(full, workspace_id, str(user_id))

        await sink.publish_ui(
            "agent_progress",
            stage="ingesting",
            status="progress",
            message=f"Ingesting {full.name}",
            data={"doc_id": doc_id, "path": rel},
        )

        # Poll the Doc row until ingestion reaches a terminal state.
        waited = 0.0
        status = "queued"
        while waited < INGEST_POLL_TIMEOUT_SECONDS:
            with SessionLocal() as db:
                doc = db_utils.get_doc(db, doc_id)
                status = doc.status if doc is not None else "failed"
            if status in _INGEST_TERMINAL:
                break
            await asyncio.sleep(INGEST_POLL_INTERVAL_SECONDS)
            waited += INGEST_POLL_INTERVAL_SECONDS

        if status == "ready":
            ready_ids.append(doc_id)
        else:
            await sink.publish_ui(
                "agent_progress",
                stage="ingesting",
                status="progress",
                message=f"Ingest of {full.name} did not complete (status={status})",
                data={"doc_id": doc_id, "path": rel, "status": status},
            )

    return ready_ids


async def dispatch_executor_agent(
    task_spec: TaskSpec,
    dep_files: list[str],
    subdir_path: Path,
    page_cache: dict[str, CachedPage],
    workspace_id: str,
    user_id: UUID,
    query_id: str,
    sink: EventSink,
    attempt: int | None = None,
) -> ExecutorResult:
    task_sink = sink.child(task_id=task_spec.id, agent_type=task_spec.agent, attempt=attempt)
    # Planner sees ready document ids in prompt context; execution routing happens here.
    match task_spec.agent:
        case "browser":
            browser = BrowserExecutor( workspace=subdir_path, model=MODEL, headless=True, max_failures=8)
            browser_result = await browser.run(
                query=task_spec.query,
                expects=task_spec.expects,
                dep_files=dep_files,
                sink=task_sink,
                task_id=task_spec.id,
                attempt=attempt,
            )
            return browser_result
        case "office":
            await task_sink.publish_ui(
                "agent_started",
                stage="office",
                status="started",
                message="Office agent started",
                data={"expects": task_spec.expects, "dep_files": dep_files},
            )
            office_result = await run_office_executor(
                workspace=subdir_path,
                query=task_spec.query,
                expects=task_spec.expects,
                dep_files=dep_files,
                sink=task_sink,
                )
            await task_sink.publish_ui(
                "agent_ended",
                stage="done",
                status="failed" if office_result.error else "completed",
                message="Office agent finished" if not office_result.error else "Office agent failed",
                data={
                    "produced": office_result.produced,
                    "notes": office_result.notes,
                    "error": office_result.error,
                },
            )
            return office_result
        case "document_answering":
            this_sink = task_sink.child(agent_type="document_answering")
            # Resolve doc_deps with a sane default instead of failing. A
            # structured-output planner frequently drops this optional nested
            # object (or leaves doc_answering_mode unset) even when it intends a
            # plain Q&A. ASK with doc_ids=None is the overwhelmingly common case
            # — a focused question over the task's dep docs — and the control
            # loop supplies the real doc_ids from ingestion below, so a missing
            # doc_deps does not actually lose information. Only a genuine REPORT
            # intent needs the field, and that mode is always stated explicitly;
            # so we treat anything that isn't an explicit REPORT as ASK, while
            # preserving any doc_ids/report_id/target_length the planner did set.
            doc_deps = task_spec.doc_deps or InternalDocAgentDeps()
            if doc_deps.doc_answering_mode not in ("ASK", "REPORT"):
                doc_deps = doc_deps.model_copy(update={"doc_answering_mode": "ASK"})

            # A dep browser task may have downloaded PDFs that no one has
            # ingested yet. Ingest them now and combine the fresh doc_ids with
            # any the planner pre-resolved (an already-ingested doc the user
            # named). doc_ids=None scopes answer_query to all workspace docs;
            # an explicit list scopes it to exactly these. We pass the explicit
            # union when we ingested something, so a freshly-downloaded PDF is
            # actually in scope instead of triggering "no documents found".
            ingested_ids = await ingest_dep_pdfs(
                dep_files=dep_files,
                subdir_path=subdir_path,
                workspace_id=workspace_id,
                user_id=user_id,
                sink=this_sink,
            )

            # Guard: if this task depended on PDFs but NONE of them ingested
            # successfully, do not silently fall through to doc_ids=None — that
            # would scope the engine to every doc in the workspace and answer
            # from the wrong sources (or "no documents found") instead of
            # surfacing that the intended source never made it into the index.
            # Fail the task so the loop retries / the planner can re-route.
            dep_pdf_count = sum(1 for p in dep_files if Path(p).suffix.lower() == ".pdf")
            if dep_pdf_count and not ingested_ids:
                return ExecutorResult(
                    produced=[],
                    notes="",
                    error=(
                        f"none of the {dep_pdf_count} dependency PDF(s) could be "
                        "ingested into the doc index; cannot answer without them"
                    ),
                )

            planner_ids = doc_deps.doc_ids or []
            combined_ids = list(dict.fromkeys([*planner_ids, *ingested_ids])) or None

            if doc_deps.doc_answering_mode == "ASK":

                doc_ask_result = await answer_query(
                    workspace_subdir_path=subdir_path,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    query=task_spec.query,
                    doc_ids=combined_ids,
                    sink=this_sink
                )
                return ExecutorResult(
                    produced=[str(Path(doc_ask_result.output_path).relative_to(subdir_path))],
                    notes=f"Page targets: {doc_ask_result.page_targets} with confidence: {doc_ask_result.confidence} \n citations: {doc_ask_result.citations}"
                )

            else:  # doc_answering_mode == "REPORT" (guarded above to ASK | REPORT)

                doc_draft_result = await draft_report(
                    workspace_subdir_path=subdir_path,
                    workspace_id=workspace_id,
                    user_id=str(user_id),
                    brief=task_spec.query,
                    doc_ids=combined_ids,
                    target_length=doc_deps.target_length,
                    report_id=doc_deps.report_id,
                    sink=this_sink # "Compare ESG strategies..."
                )
                return ExecutorResult(
                    produced=[str(Path(doc_draft_result.output_path).relative_to(subdir_path))],
                    notes=doc_draft_result.brief
                )
        case "web_search":
            await task_sink.publish_ui(
                "agent_started",
                stage="web_search",
                status="started",
                message="Web search agent started",
                data={"expects": task_spec.expects, "dep_files": dep_files},
            )
            web_result = await run_web_executor(
                workspace_subdir_path=subdir_path,
                query=task_spec.query,
                expects=task_spec.expects,
                dep_files=dep_files,
                page_cache=page_cache,
                sink=task_sink,
            )
            await task_sink.publish_ui(
                "agent_ended",
                stage="done",
                status="failed" if web_result.error else "completed",
                message="Web search agent finished" if not web_result.error else "Web search agent failed",
                data={
                    "produced": web_result.produced,
                    "notes": web_result.notes,
                    "error": web_result.error,
                },
            )
            return web_result
def validate_files_exist(workspace: Path | str, produced: list[str]) -> tuple[bool, str]:
    """Verify each produced path exists under the workspace and is non-empty.

    Returns (True, "...") if every file exists and is non-empty.
    Returns (False, "...") on the first failure, with a message describing
    whether the file was missing or empty. The caller decides whether to
    raise.
    """
    workspace = Path(workspace)
    for rel_path in produced:
        full_path = workspace / rel_path
        if not full_path.exists():
            return False, f"File {rel_path!r} does not exist at {full_path}"
        if full_path.stat().st_size == 0:
            return False, f"File {rel_path!r} exists but is empty: {full_path}"
    return True, "All produced files exist and are non-empty"

#---------------------------------I am below a function---------------------------------

@chat_router.post("/user_chat", response_model=ChatAcceptedResponse | dict[str, str])
async def user_chat(
    workspace_name: str,
    query: str | None = None,
    query_id: str | None = None,
    answer: str | None = None,
    session: SessionContainer = Depends(verify_session())
):
    if query_id is not None and answer is not None:
        fut = _pending_input.get(query_id)
        if fut is None or fut.done():
            raise HTTPException(409, detail="no pending question for this query")
        fut.set_result(answer)
        channel_id = f"query:{query_id}"
        sink = EventSink(bus=bus, channel_id=channel_id, query_id=query_id, run_id=query_id)
        await sink.publish_ui(
            "agent_progress",
            agent_type="system",
            stage="user_input",
            status="progress",
            message="User input received",
        )
        return {"status": "ok"}
    elif query_id is None and answer is None and query is not None:
        query_id = uuid4()
        asyncio.create_task(
            execute_chat(
                workspace_name=workspace_name,
                query=query,
                query_id=query_id,
                user_id=session.get_user_id()
            )
        )
        stream_url = f"/chat/{query_id}/stream"
        return ChatAcceptedResponse(query_id=query_id, stream_url=stream_url)
    else:
        raise HTTPException(status_code=400, detail="Either the query_id or answer is missing otherwise query param is missing.")

async def execute_chat(
    workspace_name: str,
    query: str,
    query_id,
    user_id
):
    channel_id = f"query:{query_id}"
    sink = EventSink(
        bus=bus,
        channel_id=channel_id,
        query_id=str(query_id),
        workspace_id=workspace_name,
        run_id=str(query_id),
        agent_type="system",
    )
    try:
        await create_chat(
                workspace_name=workspace_name,
                query=query,
                query_id=query_id,
                user_id=user_id,
                sink=sink
            )
    finally:
        # Close the channel so the SSE stream terminates cleanly instead of
        # hanging on keep-alive pings forever after the work finishes.
        bus.close(channel_id)

async def create_chat(
    workspace_name: str, # workspace name will be the workspace_id in db
    query: str,
    query_id: UUID,
    user_id: UUID,
    sink: EventSink = EventSink()
):

    await sink.publish_ui(
        "run_started",
        stage="chat",
        status="started",
        message="Chat run started",
        data={"query": query},
    )

    #Check if the workspace exists or not, check in local filesystem and in the database as well.

    #checking in local filesystem for the workspace

    workspace_path = workspace_dir(user_id, workspace_name)
    absolute_workspace_path = str(workspace_path)

    if not workspace_path.exists():
        # check in the database if the workspace exists
        with SessionLocal() as db:
            if db_utils.does_workspace_exist(db, workspace_id=workspace_name, user_id=user_id):
                # make workspace directory in local filesystem
                make_workspace(absolute_workspace_path)
            else:
                raise HTTPException(status_code=404, detail="Workspace not found")
            
    with SessionLocal() as db:
        # also fetch the query counter
        query_counter = db_utils.get_highest_query_counter(db, workspace_id=workspace_name, user_id=user_id)
        if query_counter is None:
            query_counter = 1

    thisRun = QueryRun(
            user_query=query,
            goal=query, # setting goal as user query for now,later in the flow we will set it to goal field from the planner output
            workspace=absolute_workspace_path, # Let's re-initiaze this appending sub dir path after the first planner run
            started_at=time(),
            replans_used=0,
            plan=None,
            workspace_id=workspace_name,
            query_id=query_id,
            user_id=user_id,
            status="running",
            query_counter=query_counter
        )

    # Start the doc-reasoner ingest workers BEFORE the planner runs. They drain
    # the asyncio queue that ingest_local_file pushes onto, so PDFs uploaded
    # via the doc agent's ingest_documents tool actually get parsed and indexed
    # within this run instead of sitting at status='queued' forever (which is
    # what happens when main.py runs without the FastAPI lifespan hook firing).
    # shutdown_workers in finally drains any remaining ingests before exit so
    # a doc the planner kicked off late still finishes before the process dies.
    start_workers(n=2)
    page_cache: dict[str, CachedPage] = {}
    try:
        # Per-run filesystem subdir under the chat's workspace folder. Sub-agents'
        # _resolve_inside guard keeps them confined to THIS subdir, so cross-run
        # contamination is impossible. The subdir name is {query_counter}_{query_id}
        # — independent of the plan's goal — so it can be (and now is) created
        # BEFORE the first planner call. The planner needs this path up front
        # because its fetch_prior_artifact tool copies prior-run files into it.
        workspace_sub_dir = f"{thisRun.query_counter}_{str(thisRun.query_id)}"
        absolute_workspace_path_with_subdir = f"{absolute_workspace_path}/{workspace_sub_dir}"
        thisRun.workspace = absolute_workspace_path_with_subdir
        make_workspace(Path(absolute_workspace_path_with_subdir))

        # Seed this run's outputs/ from the most recent prior run's produced
        # files (EAGER RESTORE). Executors are sandboxed to this subdir, so a
        # "continue the work" run otherwise can't see what an earlier run made;
        # restoring the prior artifacts lets the new plan build on them by path.
        # No-op when there is no prior run (a fresh workspace's first run).
        # This handles the COMMON case (most-recent run). For OLDER runs the
        # planner pulls on demand via its fetch_prior_artifact tool.
        with SessionLocal() as db:
            prior_artifacts = db_utils.get_latest_prior_run_artifacts(
                db, workspace_id=workspace_name, exclude_query_id=query_id
            )
        if prior_artifacts:
            restored = _restore_artifacts(Path(absolute_workspace_path_with_subdir), prior_artifacts)
            if restored:
                await sink.publish_ui(
                    "agent_progress",
                    stage="resuming",
                    status="progress",
                    message=f"Restored {len(restored)} file(s) from the previous run",
                    data={"restored": restored},
                )

        thisRun.plan = await planner(thisRun, sink)

        # write todo
        write_todo_atomic(thisRun)
        await publish_todo_artifact(thisRun, sink, phase="planning")

        while thisRun.plan.needs_user_feedback:

            await sink.child(agent_type="planner").publish_ui(
                "awaiting_user_input",
                stage="planning",
                status="waiting",
                message="Waiting for your input",
                data={"question": thisRun.plan.feedback_question, "scope": "planner"},
            )
            future = asyncio.get_event_loop().create_future()
            _pending_input[str(query_id)] = future
            # No timeout: the run parks here until the user answers the planner's
            # clarification. This keeps the loop's exit state consistent — the
            # ONLY way out is the planner returning a plan that no longer needs
            # feedback (its needs_user_feedback default is False), so the stale-
            # flag fall-through that a timeout+break path caused cannot happen.
            # Trade-off: an abandoned run holds its task/DB/SSE/ingest workers
            # open indefinitely (accepted limitation).
            try:
                answer = await future
            finally:
                _pending_input.pop(str(query_id), None)

            validation_error: str | None = None
            for _ in range(3):
                feedback_prompt = answer
                if validation_error:
                    feedback_prompt += (
                        "\n\nYour revised plan violated this internal checkpoint "
                        f"rule: {validation_error}. Correct it and ensure any "
                        "checkpoint is the final task in the current segment."
                    )
                planner_run = await plannerAgent.run(
                    user_prompt=_with_planner_workspace_context(
                        thisRun.workspace_id, feedback_prompt, thisRun.query_id
                    ),
                    message_history=thisRun.planner_messages,
                    deps=PlannerDeps(
                        workspace_name=thisRun.workspace_id,
                        subdir_path=thisRun.workspace,
                        current_query_id=str(thisRun.query_id) if thisRun.query_id else None,
                    ),
                )
                validation_error = _initial_axis_plan_error(planner_run.output)
                if validation_error is None:
                    thisRun.plan = planner_run.output
                    thisRun.planner_messages = planner_run.all_messages()
                    break
            else:
                raise RuntimeError(
                    "planner could not produce a valid checkpoint segment after "
                    f"user feedback: {validation_error}"
                )

            write_todo_atomic(thisRun)
            await publish_todo_artifact(thisRun, sink, phase="planning")

        # let's fore the tasks ony by one and not concurrently.
        while True:
            # 'superseded' is terminal-OK: a routed-around failed task is done
            # with, so it does not keep the loop from recognizing completion.
            if all(task.status in ("completed", "superseded") for task in thisRun.plan.tasks):
                break

            ready = []
            tasks_by_id = {t.id: t for t in thisRun.plan.tasks}

            # Guard dangling deps: a task may reference a dep id that isn't in
            # this plan (e.g. the planner reused a prior run's task id like "t1"
            # for a "continue" request). Looking it up below would KeyError and
            # crash the whole run. Fail such a task with a clear error instead,
            # so the loop falls through to the replan path and the planner can
            # rewrite it to use the restored prior-run files by path.
            dangling = False
            for task in thisRun.plan.tasks:
                if task.status == "pending":
                    missing = [dep for dep in task.deps if dep not in tasks_by_id]
                    if missing:
                        task.status = "failed"
                        task.error = (
                            f"task {task.id} depends on unknown task id(s) {missing} "
                            "not present in this plan; if you meant files produced by a "
                            "previous run, they have been restored into outputs/ — "
                            "reference them by path with empty deps instead"
                        )
                        dangling = True
            if dangling:
                write_todo_atomic(thisRun)
                if thisRun.replans_used < thisRun.replan_budget:
                    new_plan = await planner(thisRun, sink)
                    if new_plan is not None:
                        thisRun.plan = _merge_plan(thisRun.plan, new_plan)
                        thisRun.replans_used += 1
                    write_todo_atomic(thisRun)
                    await publish_todo_artifact(thisRun, sink, phase="replanning")
                    continue
                break

            for task in thisRun.plan.tasks:
                if task.status == "pending":
                    if all(tasks_by_id[dep].status == "completed" for dep in task.deps):
                        ready.append(task)

            if not ready:
                # Either deadlocked or waiting on running tasks.
                # In a parallel version this is where you'd join on a running task.
                # No pending task can run — every remaining pending task is blocked
                # by a failed upstream. Mark them and exit.

                # Only genuinely-failed tasks block downstream work. A
                # 'superseded' task was routed around by a replan and is NOT a
                # real blocker — excluding it here prevents a downstream task
                # (whose deps point at the recovery chain, not the dead task)
                # from being wrongly force-failed.
                failed_ids = {t.id for t in thisRun.plan.tasks if t.status == "failed"}
                # Mark only tasks blocked by a failed ancestor; leave others for a replan pass.
                progressed = False
                for t in thisRun.plan.tasks:
                    if t.status == "pending" and any(dep in failed_ids for dep in t.deps):
                        t.status = "failed"
                        t.error = f"upstream task failed: {[d for d in t.deps if d in failed_ids]}"
                        progressed = True
                write_todo_atomic(thisRun)
                if not progressed:
                    break # genuinly nothing left to do.
                continue

            for task in ready:
                task.status = "dispatched"
                dep_files = []
                for dep in task.deps:
                    dep_files.extend(tasks_by_id[dep].produced)

                # Retry the dispatch on error. Each attempt is a fresh executor, so
                # a wedged browser session or transient hang doesn't carry over.
                # An executor that *raises* (e.g. answer_query's "no documents
                # found" ValueError) is caught and turned into a result.error, so
                # the same retry/replan path handles it instead of the exception
                # escaping the loop and killing the whole run as an orphaned task.
                result = None
                for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
                    try:
                        result = await dispatch_executor_agent(
                            task,
                            dep_files,
                            Path(absolute_workspace_path_with_subdir),
                            page_cache,
                            workspace_id=workspace_name,
                            user_id=user_id,
                            query_id=str(query_id),
                            sink=sink,
                            attempt=attempt,
                        )
                    except Exception as e:
                        # Full traceback to stdout so the real source of a raised
                        # error (e.g. an IntegrityError and which DB call caused
                        # it) is visible; result.error keeps only the short string
                        # for the user/planner.
                        print(f"[{task.id}] attempt {attempt}/{MAX_DISPATCH_ATTEMPTS} executor raised:")
                        traceback.print_exc()
                        result = ExecutorResult(
                            produced=[],
                            notes="",
                            error=f"executor raised {type(e).__name__}: {e}",
                        )
                    if not result.error:
                        break
                    print(f"[{task.id}] attempt {attempt}/{MAX_DISPATCH_ATTEMPTS} failed: {result.error}")
                    if attempt < MAX_DISPATCH_ATTEMPTS:
                        await sink.child(task_id=task.id, agent_type=task.agent, attempt=attempt).publish_ui(
                            "agent_progress",
                            stage="retrying",
                            status="progress",
                            message="Retrying task",
                            data={"error": result.error, "max_attempts": MAX_DISPATCH_ATTEMPTS},
                        )
                        await asyncio.sleep(2 * attempt)  # linear backoff: 2s, 4s

                human_answer: str | None = None
                if result.error:
                    await sink.child(task_id=task.id, agent_type=task.agent).publish_ui(
                        "agent_ended",
                        stage="done",
                        status="failed",
                        message="Task failed",
                        data={"error": result.error},
                    )
                    task.status = "failed"
                    task.error = f"after {MAX_DISPATCH_ATTEMPTS} attempts: {result.error}"
                else:
                    # files were written by the executor's write_file tool during
                    # its run; we only verify they exist and are non-empty.
                    await sink.child(task_id=task.id, agent_type="system").publish_ui(
                        "agent_progress",
                        stage="validating",
                        status="progress",
                        message="Validating produced files",
                        data={"agent_type": task.agent, "produced": result.produced},
                    )
                    ok, status = validate_files_exist(absolute_workspace_path_with_subdir, result.produced)
                    if not ok:
                        await sink.child(task_id=task.id, agent_type="system").publish_ui(
                            "agent_ended",
                            stage="validating",
                            status="failed",
                            message="Produced file validation failed",
                            data={"agent_type": task.agent, "error": status},
                        )
                        raise HTTPException(status_code=500, detail=f"file validation for 'produced' of the task failed with status: {status}")

                    if task.human_in_the_loop and task.query_for_human_in_the_loop:
                        await sink.child(task_id=task.id, agent_type=task.agent).publish_ui(
                            "awaiting_user_input",
                            stage="task",
                            status="waiting",
                            message="Waiting for your input",
                            data={"question": task.query_for_human_in_the_loop, "scope": "task"},
                        )

                        future = asyncio.get_event_loop().create_future()
                        _pending_input[str(query_id)] = future
                        # No timeout: park until the user responds to this task's
                        # HITL question (consistent with the planner HITL wait).
                        # human_answer is therefore always set once we proceed.
                        try:
                            human_answer = await future
                        finally:
                            _pending_input.pop(str(query_id), None)

                        if human_answer is not None:
                            task.query = (
                                task.query
                                + "\n\nUser feedback on this task's result: "
                                + human_answer
                            )
                            await sink.child(task_id=task.id, agent_type=task.agent).publish_ui(
                                "agent_progress",
                                stage="task",
                                status="progress",
                                message="Re-running task with your feedback",
                            )
                            try:
                                result = await dispatch_executor_agent(
                                    task,
                                    dep_files,
                                    Path(absolute_workspace_path_with_subdir),
                                    page_cache,
                                    workspace_id=workspace_name,
                                    user_id=user_id,
                                    query_id=str(query_id),
                                    sink=sink,
                                    attempt=1,
                                )
                            except Exception as e:
                                print(f"[{task.id}] HITL re-run executor raised:")
                                traceback.print_exc()
                                result = ExecutorResult(
                                    produced=[],
                                    notes="",
                                    error=f"executor raised {type(e).__name__}: {e}",
                                )
                            if result.error:
                                await sink.child(task_id=task.id, agent_type=task.agent).publish_ui(
                                    "agent_ended",
                                    stage="done",
                                    status="failed",
                                    message="Task re-run failed",
                                    data={"error": result.error},
                                )
                                task.status = "failed"
                                task.error = f"re-run after user feedback failed: {result.error}"
                                continue
                            ok, status = validate_files_exist(absolute_workspace_path_with_subdir, result.produced)
                            if not ok:
                                await sink.child(task_id=task.id, agent_type="system").publish_ui(
                                    "agent_ended",
                                    stage="validating",
                                    status="failed",
                                    message="Re-run file validation failed",
                                    data={"agent_type": task.agent, "error": status},
                                )
                                raise HTTPException(status_code=500, detail=f"file validation for 're-run produced' of the task failed with status: {status}")

                    task.status = "completed"
                    task.produced = result.produced
                    task.notes = result.notes

                    # Persist this task's produced files to the DB immediately,
                    # so a crash later in the run (e.g. the next task's network
                    # dies) doesn't lose completed work — a subsequent "continue"
                    # run can restore them. Best-effort: a persistence error must
                    # not fail an otherwise-successful task.
                    try:
                        artifacts = _read_artifacts(
                            Path(absolute_workspace_path_with_subdir), result.produced, task.id
                        )
                        if artifacts:
                            with SessionLocal() as db:
                                db_utils.append_run_artifacts(
                                    db,
                                    query_id=query_id,
                                    workspace_id=workspace_name,
                                    user_id=user_id,
                                    artifacts=artifacts,
                                    row_defaults={
                                        "user_query": thisRun.user_query,
                                        "goal": thisRun.goal,
                                        "workspace": thisRun.workspace,
                                        "started_at": thisRun.started_at,
                                        "status": "running",
                                        "query_counter": thisRun.query_counter,
                                    },
                                )
                    except Exception as e:
                        print(f"[{task.id}] failed to persist artifacts: {type(e).__name__}: {e}")

                # A successful planner-selected checkpoint uses the hidden
                # evidence critic and an append-only planner. Ordinary tasks
                # retain the existing full-plan replan path for failures and
                # executor surprises.
                axis_segment_added = False
                if (
                    task.status == "completed"
                    and task.axis_checkpoint
                    and thisRun.axis_checkpoints_used < thisRun.axis_checkpoint_budget
                ):
                    addition = await run_axis_checkpoint(thisRun, task, sink)
                    _append_axis_tasks(thisRun, addition)
                    thisRun.axis_checkpoints_used += 1
                    axis_segment_added = True
                elif thisRun.replans_used < thisRun.replan_budget:
                    new_plan = await planner(thisRun, sink)
                    if new_plan is not None:
                        thisRun.plan = _merge_plan(thisRun.plan, new_plan)
                        thisRun.replans_used += 1

                # always rewrite todo.md so status, produced, and any replan land on disk
                write_todo_atomic(thisRun)
                await publish_todo_artifact(
                    thisRun,
                    sink,
                    phase="replanning"
                    if thisRun.replans_used or axis_segment_added
                    else "planning",
                )

                if axis_segment_added:
                    # `ready` was computed from the old graph. Restart task
                    # selection so the appended dependencies take effect before
                    # any stale ready task can run.
                    break
    finally:

        # Increment query_counter by 1
        thisRun.query_counter +=1

        # Persist final state to workspace_runs so this turn becomes part of
        # the chat's history for the next run's planner. todo_md gets the
        # rendered final state; status reflects what happened. We compute
        # status from the plan's tasks: any failed task → 'failed' overall,
        # otherwise 'completed'. If the plan never got built (planner crashed
        # on the initial call) we mark 'failed' with no todo_md.
        #
        # 'superseded' tasks are excluded from BOTH checks: a failure that a
        # replan recovered from (routed around) must neither mark the run failed
        # nor block it from being recognized as completed. A run whose only
        # non-completed tasks are superseded is a successful recovery.
        final_status = "failed"
        final_todo: str | None = None
        if thisRun.plan is not None:
            final_todo = render_todo(thisRun)
            if any(t.status == "failed" for t in thisRun.plan.tasks):
                final_status = "failed"
            elif all(t.status in ("completed", "superseded") for t in thisRun.plan.tasks):
                final_status = "completed"
            else:
                # Mid-flight interruption — leave as 'failed' so future planner
                # runs see a clear signal that the previous turn didn't finish
                # what it set out to do.
                final_status = "failed"
        thisRun.status = final_status
        with SessionLocal() as db:
            e = db_utils.register_query_run(db, run=thisRun, final_todo=final_todo)
            if e is not None:
                raise HTTPException(status_code=500, detail=f"Could not write query run to the database error: {e}")
        await sink.publish_ui(
            "run_ended",
            stage="done",
            status="completed" if final_status == "completed" else "failed",
            message="Chat run complete" if final_status == "completed" else "Chat run failed",
            data={"status": final_status},
            artifacts=[
                file_artifact(
                    kind="markdown",
                    path="todo.md",
                    filename="todo.md",
                    type="md",
                    mime_type="text/markdown",
                    content=final_todo,
                )
            ] if final_todo else [],
        )

        # Drain any ingest queued during the run (a doc agent may have kicked
        # off a slow LlamaParse + tree build right before submit) and cancel
        # the workers so the process exits cleanly. Errors here are swallowed
        # by shutdown_workers via gather(return_exceptions=True).
        await shutdown_workers()

    # Return the final plan as the response_model=PlanOutput body. Placed
    # OUTSIDE the finally so it never masks an in-flight exception. If the
    # planner crashed before building a plan, thisRun.plan is None — surface
    # that as a 500 rather than letting FastAPI fail response validation on None.
    if thisRun.plan is None:
        raise HTTPException(status_code=500, detail="Planner failed to produce a plan")
    return thisRun.plan

@chat_router.get("/{query_id}/stream")
async def stream_chat(
    query_id: str,
    request: Request,
    current_user: SessionContainer = Depends(verify_session())
):
    # The stream is keyed solely on the query_id (path param). No workspace_name
    # needed — dropping it lets the URL the client receives work as-is.
    return sse_stream(bus, f"query:{query_id}", request)

@chat_router.get("/get_docs_by_name")
async def get_docs_by_name(
        workspace_name: str, # workspace name will be the workspace_id in db
        doc_name: str,
        session: SessionContainer = Depends(verify_session())
    ) -> list[str]:
    with SessionLocal() as db:
            doc_ids = db_utils.get_docID_by_name(db, workspace_name, doc_name)
    return doc_ids

@chat_router.get("/get_reports_by_name")
async def get_reports_by_name(
    workspace_name: str,
    report_name: str,
    session: SessionContainer = Depends(verify_session())
) -> str:
    with SessionLocal() as db:
        report_ids = db_utils.get_reportID_by_name(db, workspace_name, report_name)
    return report_ids
