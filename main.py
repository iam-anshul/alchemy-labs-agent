import os
from pathlib import Path
from orchestrator import plannerAgent
from browser_agent import BrowserExecutor, ExecutorResult
from office_agent import run_office_executor
from formats_pydantic import Run, PlanOutput, TaskSpec
from render_todo import render_todo
from time import time
import random
import asyncio

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
    # for t in new.tasks:
    #     if t.id in old_by_id:
    #         t.status = old_by_id[t.id].status
    #         t.produced = old_by_id[t.id].produced
    # return new

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
    
async def planner(run: Run) -> PlanOutput:
    """Run the planner LLM.

    Initial call (no plan on the run yet): generate the plan from the
    user's goal alone.
    Subsequent calls: render the current todo.md and pass it to the agent
    so it can review executor results and either return the plan unchanged
    or a revised version.
    """
    if not run.plan:
        planner_run = await plannerAgent.run(user_prompt=run.goal)
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
    
async def dispatch_executor_agent(task_spec: TaskSpec, dep_files: list[str], workspace: Path) -> str:

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
            ...

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
    userGoal = input("What's your goal: ")
    workspace = make_workspace(f"{Path.cwd()}/workspace/{random.randint(0, 99)}{userGoal[:10]}")

    thisRun = Run(
            workspace=workspace,
            replans_used=0,
            goal=userGoal,
            user_query=userGoal,
            timestamp=time(),
            started_at=time(),
            plan=None
        )
    
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
                result = await dispatch_executor_agent(task, dep_files, Path(workspace))
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
    

if __name__ == "__main__":
    asyncio.run(main())