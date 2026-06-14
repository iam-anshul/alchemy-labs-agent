import os
import base64
import hashlib
import traceback
from pathlib import Path
from orchestrator import plannerAgent, replanAgent, PlannerDeps
from browser_agent import BrowserExecutor, ExecutorResult
from agent_schemas import QueryAnswer
from report_schemas import ReportResult
from office_agent import run_office_executor
from web_agent import CachedPage, run_web_executor
from api.ingest import start_workers, shutdown_workers
from api.routes.documents import ingest_local_file
from db import SessionLocal
from db import utils as db_utils
from formats_pydantic import QueryRun, PlanOutput, TaskSpec, ChatAcceptedResponse, InternalDocAgentDeps
from render_todo import render_todo
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart
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
USER_FEEDBACK_TIMEOUT_SECONDS = 300

_pending_input: dict[str, asyncio.Future] = {}

def make_workspace(workspace_path):
    os.makedirs(workspace_path)
    return workspace_path


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
        if current_run_id is not None and r.query_id == current_run_id:
            continue
        if not r.todo_md:
            # Skip runs that never completed enough to render a todo.md —
            # they'd be noise without signal.
            continue
        messages.append(ModelRequest(parts=[UserPromptPart(content=r.user_query)]))
        messages.append(ModelResponse(parts=[TextPart(content=r.todo_md)]))
    return messages

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
        message_history: list[ModelMessage] = []
        if run.query_counter > 1:
            message_history = _build_message_history_from_prior_runs(
                workspace_id=run.workspace_id,
                current_run_id=run.query_id,
            )
        planner_run = await plannerAgent.run(
            user_prompt=run.goal,
            message_history=message_history or None,
            deps=PlannerDeps(workspace_name=run.workspace_id)
        )
        run.planner_messages = planner_run.all_messages()
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
    await sink.child(agent_type="planner").publish_ui(
        "agent_started",
        stage="replanning",
        status="started",
        message="Checking whether the plan needs updates",
        data={"phase": "replan", "replans_used": run.replans_used, "replan_budget": run.replan_budget},
    )
    replan_run = await replanAgent.run(user_prompt=replan_prompt, deps=PlannerDeps(workspace_name=run.workspace_id))
    decision = replan_run.output
    # No change wanted, or the model said "change" but gave no tasks -> treat as
    # a no-op (don't adopt anything, don't spend budget). Assembling the revised
    # PlanOutput from the flat decision fields ourselves (rather than nesting a
    # PlanOutput in the schema) avoids the model stringifying a nested object.
    if not decision.needs_change or not decision.tasks:
        return None
    return PlanOutput(
        goal=decision.goal,
        tasks=decision.tasks,
        needs_user_feedback=decision.needs_user_feedback,
        feedback_question=decision.feedback_question,
        notes=decision.notes,
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
    # Planner should have the tools to list document and find the doc id with its name
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

    absolute_workspace_path = f"{Path.cwd()}/file_system_root/{workspace_name}"
    workspace_path = Path(f"{Path.cwd()}/file_system_root/{workspace_name}")

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
        thisRun.plan = await planner(thisRun, sink)

        # Per-run filesystem subdir under the chat's workspace folder. Sub-agents'
        # _resolve_inside guard keeps them confined to THIS subdir, so cross-run
        # contamination is impossible — but the DB layer captures the chat identity
        # so the planner still sees prior runs via message_history.
        # Naming works like this------>
        # 1. we need to create this worksapce subdir after the first planner run, reason being we can use goal field of todo.md from planner as its name.
        # 2. we need to have a query counter maintainer in db, all the names of the chat run will be prefixed with this counter value, so the naming scheme will look something like this: f"{query_counter}_{plan.goal}""
        workspace_sub_dir = f"{thisRun.query_counter}_{str(thisRun.query_id)}" # define query counter in db
        absolute_workspace_path_with_subdir = f"{absolute_workspace_path}/{workspace_sub_dir}"
        thisRun.workspace = absolute_workspace_path_with_subdir
        make_workspace(Path(absolute_workspace_path_with_subdir))

        # Seed this run's outputs/ from the most recent prior run's produced
        # files. Executors are sandboxed to this subdir, so a "continue the
        # work" run otherwise can't see what an earlier run made; restoring the
        # prior artifacts lets the new plan build on them by path. No-op when
        # there is no prior run (a fresh workspace's first run).
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
            try:
                answer = await asyncio.wait_for(future, timeout=USER_FEEDBACK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await sink.child(agent_type="planner").publish_ui(
                    "agent_progress",
                    stage="planning",
                    status="failed",
                    message="User input timed out",
                    data={"question": thisRun.plan.feedback_question, "scope": "planner"},
                )
                break
            finally:
                _pending_input.pop(str(query_id), None)

            planner_run = await plannerAgent.run(
                user_prompt=answer,
                message_history=thisRun.planner_messages,
                deps=PlannerDeps(workspace_name=thisRun.workspace_id)
            )
            thisRun.plan = planner_run.output
            thisRun.planner_messages = planner_run.all_messages()

            write_todo_atomic(thisRun)
            await publish_todo_artifact(thisRun, sink, phase="planning")

        # let's fore the tasks ony by one and not concurrently.
        while True:
            if all(task.status == "completed" for task in thisRun.plan.tasks):
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
                        try:
                            human_answer = await asyncio.wait_for(future, timeout=USER_FEEDBACK_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            await sink.child(task_id=task.id, agent_type=task.agent).publish_ui(
                                "agent_progress",
                                stage="task",
                                status="failed",
                                message="User input timed out",
                                data={"question": task.query_for_human_in_the_loop, "scope": "task"},
                            )
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

                # We can add write_todo_atomic here so that user can see the changes as well, this can be done in future versions

                # ask the planner whether the plan needs revision, but only while
                # we still have replan budget
                # persist *and* replan regardless of success/failure — a failure is
                # exactly when the planner most needs to see the state and decide
                # whether to revise.
                if thisRun.replans_used < thisRun.replan_budget:
                    new_plan = await planner(thisRun, sink)
                    if new_plan is not None:
                        thisRun.plan = _merge_plan(thisRun.plan, new_plan)
                        thisRun.replans_used += 1

                # always rewrite todo.md so status, produced, and any replan land on disk
                write_todo_atomic(thisRun)
                await publish_todo_artifact(thisRun, sink, phase="replanning" if thisRun.replans_used else "planning")
    finally:

        # Increment query_counter by 1
        thisRun.query_counter +=1

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
