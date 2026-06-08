import os
import uuid
from pathlib import Path
from orchestrator import plannerAgent
from browser_agent import BrowserExecutor
from office_agent import run_office_executor
from api.ingest import start_workers, shutdown_workers
from db import SessionLocal
from db import utils as db_utils
from formats_pydantic import Run, PlanOutput, TaskSpec
from render_todo import render_todo
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart
from time import time
import asyncio
import shutil

from report import draft_report
from agent import answer_query

from dotenv import load_dotenv
import os

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

# How many times to dispatch a task before marking it failed. Each attempt
# builds a fresh executor (and, for browser tasks, a fresh browser), so a retry
# sidesteps a wedged session rather than re-poking it. This catches transient
# failures (a one-off hang, a flaky page) without spending a replan.
MAX_DISPATCH_ATTEMPTS = 3

def make_workspace(workspace_path):
    os.makedirs(workspace_path)
    return workspace_path

def write_todo_atomic(run: Run) -> None:
    todo_path = Path(run.workspace) / "todo.md"
    tmp = todo_path.with_suffix(".md.tmp")
    tmp.write_text(render_todo(run), encoding="utf-8")
    tmp.replace(todo_path)

def _plan_signature(plan: PlanOutput) -> tuple:
    """Comparison signature covering only the planner-owned fields of a plan.

    Used to detect meaningful changes (new/removed tasks, edited queries,
    revised deps, etc.) while ignoring control-loop-owned state (status,
    produced). Without this, status flips would always read as 'plan
    changed' and burn the replan budget on no-op revisions.
    """
    return (
        tuple(
            (
                t.id,
                t.title,
                t.agent,
                tuple(getattr(d, "id", d) for d in t.deps),
                t.query,
                t.expects,
            )
            for t in plan.tasks
        ),
        plan.notes,
    )

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
            rewritten = (t.query, t.expects, t.agent) != (old_t.query, old_t.expects, old_t.agent)
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
    return new
    
def _build_message_history_from_prior_runs(workspace_id: str, current_run_id: str | None) -> list[ModelMessage]:
    """Reconstruct a synthetic conversation history from this workspace's prior
    runs so the planner can see what was tried before (Option B per the design
    discussion). For each prior run we emit a ModelRequest(user_goal) + a
    ModelResponse(todo_md) pair — pydantic-ai treats this as a real multi-turn
    conversation when passed via the `message_history` parameter.

    Why prior runs instead of just the current one's state: this is the
    "learn from prior conversation turns" feature. Within a single run, replan
    calls deliberately skip this — they should only reason about the in-flight
    todo.md, not get distracted by past conversation.

    The current in-flight run is excluded by run_id even if its row was
    created at run-start (status='running' with empty todo_md); otherwise the
    planner would see itself in its own history."""
    with SessionLocal() as db:
        runs = db_utils.list_recent_runs(db, workspace_id=workspace_id, limit=5)
    messages: list[ModelMessage] = []
    for r in runs:
        if current_run_id is not None and r.run_id == current_run_id:
            continue
        if not r.todo_md:
            # Skip runs that never completed enough to render a todo.md —
            # they'd be noise without signal.
            continue
        messages.append(ModelRequest(parts=[UserPromptPart(content=r.user_goal)]))
        messages.append(ModelResponse(parts=[TextPart(content=r.todo_md)]))
    return messages


async def planner(run: Run) -> PlanOutput:
    """Run the planner LLM.

    Initial call (no plan on the run yet): generate the plan from the user's
    goal alone. If this run is part of a persistent workspace, prior runs'
    todo.mds are reconstructed as message_history so the planner sees the
    conversation timeline.

    Subsequent calls (replan): render the current todo.md and pass it to the
    agent so it can review executor results and either return the plan
    unchanged or a revised version. NO history injection — replans are
    scoped to the in-flight run only, per design.
    """
    if not run.plan:
        message_history: list[ModelMessage] = []
        if run.workspace_id:
            message_history = _build_message_history_from_prior_runs(
                workspace_id=run.workspace_id,
                current_run_id=run.run_id,
            )
        planner_run = await plannerAgent.run(
            user_prompt=run.goal,
            message_history=message_history or None,
        )
        return planner_run.output

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

    replan_prompt = (
        f"Goal: {run.goal}\n\n"
        f"Current plan state:\n\n{current_todo}{failure_section}\n\n"
        "Review the plan above. If executor notes or completed task results "
        "warrant a change, return the revised plan. Otherwise return the "
        "plan unchanged."
    )
    planner_run = await plannerAgent.run(user_prompt=replan_prompt)
    return planner_run.output
    
async def dispatch_executor_agent(
    task_spec: TaskSpec,
    dep_files: list[str],
    workspace: Path,
    workspace_id: str,
    user_id: str,
) -> str:

    match task_spec.agent:
        case "browser":
            browser = BrowserExecutor( workspace=workspace, model=MODEL, headless=True, max_failures=8)
            browser_result = await browser.run(
                query=task_spec.query,
                expects=task_spec.expects,
                dep_files=dep_files
            )
            return browser_result
        case "office":
            office_result = await run_office_executor(
                workspace=workspace,
                query=task_spec.query,
                expects=task_spec.expects,
                dep_files=dep_files
                )
            return office_result
        case "document_answering":
            if task_spec.doc_answering_mode == "ASK":
                answer_query()
            elif task_spec.doc_answering_mode == "REPORT":
                draft_report()

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

async def main():
    # Chat identity comes first. Each workspace_id is one persistent chat;
    # a fresh name creates the chat, an existing one resumes it. The user_id
    # is hardcoded for the dev CLI — when this gets a FastAPI front-end the
    # request-auth layer will provide it.
    USER_ID = "user_dev"
    workspace_id_input = input("Workspace id (chat name; blank for a fresh one): ").strip()
    if not workspace_id_input:
        workspace_id_input = f"chat_{uuid.uuid4().hex[:8]}"
        print(f"Starting new chat: {workspace_id_input}")

    userGoal = input("What's your goal: ")
    files_attached = input("Any files to attach? Give a path: ")

    # Persistent state: ensure the workspace exists in the DB, allocate a run_id
    # for this turn. We create the workspace_runs row BEFORE the planner fires
    # so the row exists for observability even if the run crashes mid-flight
    # (status stays 'running' and we can tell what happened). Status flips to
    # 'completed' / 'failed' in the finally block at the end of this function.
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db_utils.get_or_create_workspace(
            db,
            workspace_id=workspace_id_input,
            user_id=USER_ID,
            title=userGoal[:80],  # first goal becomes the chat label
        )
        db_utils.create_workspace_run(
            db,
            run_id=run_id,
            workspace_id=workspace_id_input,
            user_goal=userGoal,
            status="running",
        )

    # Per-run filesystem subdir under the chat's workspace folder. Sub-agents'
    # _resolve_inside guard keeps them confined to THIS subdir, so cross-run
    # contamination is impossible — but the DB layer captures the chat identity
    # so the planner still sees prior runs via message_history.
    workspace = make_workspace(f"{Path.cwd()}/workspace/{workspace_id_input}/{run_id}")

    if files_attached.strip():
        path = Path(files_attached.strip())
        if path.exists():
            shutil.copy(path, workspace)
            print("File found and attached to workspace")
            userGoal += f"""

The user provided these files; they have been copied into the workspace and
are already present at the relative paths below — no upstream task needs to
produce them. When designing tasks, reference these paths in the relevant
task's `query` so the executor knows to read or ingest them.
  - {path.name}
"""
        else:
            raise FileNotFoundError(f"File {files_attached} does not exist")


    thisRun = Run(
            workspace=workspace,
            replans_used=0,
            goal=userGoal,
            user_query=userGoal,
            timestamp=time(),
            started_at=time(),
            plan=None,
            workspace_id=workspace_id_input,
            run_id=run_id,
        )

    # Start the doc-reasoner ingest workers BEFORE the planner runs. They drain
    # the asyncio queue that ingest_local_file pushes onto, so PDFs uploaded
    # via the doc agent's ingest_documents tool actually get parsed and indexed
    # within this run instead of sitting at status='queued' forever (which is
    # what happens when main.py runs without the FastAPI lifespan hook firing).
    # shutdown_workers in finally drains any remaining ingests before exit so
    # a doc the planner kicked off late still finishes before the process dies.
    start_workers(n=2)
    try:
        thisRun.plan = await planner(thisRun)

        # write todo
        write_todo_atomic(thisRun)

        while True:
            if all(task.status == "completed" for task in thisRun.plan.tasks):
                break

            ready = []
            tasks_by_id = {t.id: t for t in thisRun.plan.tasks}

            for task in thisRun.plan.tasks:
                if task.status == "pending":
                    if all(tasks_by_id[dep].status == "completed" for dep in task.deps):
                        ready.append(task)

            if not ready:
                # Either deadlocked or waiting on running tasks.
                # In a parallel version this is where you'd join on a running task.
                # No pending task can run — every remaining pending task is blocked
                # by a failed upstream. Mark them and exit.

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
                result = None
                for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
                    result = await dispatch_executor_agent(
                        task,
                        dep_files,
                        Path(workspace),
                        workspace_id=workspace_id_input,
                        user_id=USER_ID,
                    )
                    if not result.error:
                        break
                    print(f"[{task.id}] attempt {attempt}/{MAX_DISPATCH_ATTEMPTS} failed: {result.error}")
                    if attempt < MAX_DISPATCH_ATTEMPTS:
                        await asyncio.sleep(2 * attempt)  # linear backoff: 2s, 4s

                if result.error:
                    task.status = "failed"
                    task.error = f"after {MAX_DISPATCH_ATTEMPTS} attempts: {result.error}"
                else:
                    # files were written by the executor's write_file tool during
                    # its run; we only verify they exist and are non-empty.
                    ok, status = validate_files_exist(workspace, result.produced)
                    if not ok:
                        raise RuntimeError(status)
                    task.status = "completed"
                    task.produced = result.produced
                    task.notes = result.notes

                # ask the planner whether the plan needs revision, but only while
                # we still have replan budget
                # persist *and* replan regardless of success/failure — a failure is
                # exactly when the planner most needs to see the state and decide
                # whether to revise.
                if thisRun.replans_used < thisRun.replan_budget:
                    new_plan = await planner(thisRun)
                    if _plan_signature(new_plan) != _plan_signature(thisRun.plan):
                        thisRun.plan = _merge_plan(thisRun.plan, new_plan)
                        thisRun.replans_used += 1

                # always rewrite todo.md so status, produced, and any replan land on disk
                write_todo_atomic(thisRun)
    finally:
        # Persist final state to workspace_runs so this turn becomes part of
        # the chat's history for the next run's planner. todo_md gets the
        # rendered final state; status reflects what happened. We compute
        # status from the plan's tasks: any failed task → 'failed' overall,
        # otherwise 'completed'. If the plan never got built (planner crashed
        # on the initial call) we mark 'failed' with no todo_md.
        final_status = "failed"
        final_todo: str | None = None
        if thisRun.plan is not None:
            final_todo = render_todo(thisRun)
            if any(t.status == "failed" for t in thisRun.plan.tasks):
                final_status = "failed"
            elif all(t.status == "completed" for t in thisRun.plan.tasks):
                final_status = "completed"
            else:
                # Mid-flight interruption — leave as 'failed' so future planner
                # runs see a clear signal that the previous turn didn't finish
                # what it set out to do.
                final_status = "failed"
        with SessionLocal() as db:
            db_utils.update_workspace_run(
                db, run_id=run_id, todo_md=final_todo, status=final_status
            )

        # Drain any ingest queued during the run (a doc agent may have kicked
        # off a slow LlamaParse + tree build right before submit) and cancel
        # the workers so the process exits cleanly. Errors here are swallowed
        # by shutdown_workers via gather(return_exceptions=True).
        await shutdown_workers()


if __name__ == "__main__":
    asyncio.run(main())