import functools
import inspect
import json
import logging
import os
import sys
import traceback
from pydantic import BeforeValidator
from pydantic_ai import Agent, RunContext
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from dotenv import load_dotenv
from pathlib import Path
from typing import Annotated
from dataclasses import dataclass, field
from browser_agent import ExecutorResult
from system_prompts import doc_system_prompt
from api.routes.documents import ingest_local_file, list_local_documents, get_local_document
from api.routes.queries import ask_local_query, list_local_queries, get_local_query
from api.routes.reports import draft_local_report, list_local_reports, get_local_report

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

#Note: this dataclass is similar to OfficeDeps in office_agent.py. In a larger project we would deduplicate, but for the sake of keeping each agent self-contained I'm leaving it as-is in both places.
@dataclass
class DocDeps:
    """Per-run state passed into the doc agent via RunContext.

    workspace is the absolute root the tools resolve relative paths against;
    the LLM never sees it. workspace_id and user_id are needed to call into
    doc-reasoner's ingest path (it scopes documents by workspace_id and tags
    them with an uploader). docs accumulates the doc_ids returned by
    ingest_documents so we can scope ask() calls. submitted is the mutable
    holder the submit tool writes into so the caller can recover the
    ExecutorResult after the agent loop ends — pydantic-ai gives no other
    channel to lift state out of a tool call.
    """
    workspace: Path
    workspace_id: str
    user_id: str
    docs: list[str] = field(default_factory=list)
    # Workspace-relative paths already ingested this dispatch. Used by
    # ingest_documents to skip re-ingesting the same PDF — re-ingestion is
    # expensive (LlamaParse + tree build) and silently creates duplicate
    # doc_ids in the index, which then corrupts ask() citations.
    ingested_paths: set[str] = field(default_factory=set)
    submitted: ExecutorResult | None = None

model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

theDocAgent = Agent[DocDeps, str](
    model,
    deps_type=DocDeps,
    system_prompt=doc_system_prompt,
    retries=3,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)


# Logging setup so we can actually see what's going on in the terminal.
#
# We attach DEDICATED handlers to our two loggers (instead of relying on the
# root logger). Reason: api.routes.* indirectly imports uvicorn, which sets
# up root handlers at INFO before our code runs. basicConfig is a no-op when
# handlers already exist, and any DEBUG messages we emit get filtered by
# uvicorn's handler level on the way out. Dedicated handlers with their own
# levels bypass that.
#
# Two layers:
#   1. "pydantic_ai" — DEBUG. Surfaces tool-argument VALIDATION failures and
#      retry prompts. These fire BEFORE our tool body runs, so _surface_errors
#      can't catch them — this is the only channel that does.
#   2. "doc_agent" — INFO. One line per tool call (with args) and one line per
#      tool body exception (with traceback). _surface_errors writes here.
#
# propagate=False on both so messages don't ALSO bubble up to the root
# handler (which would print them a second time in uvicorn's format).

def _attach_handler(logger: logging.Logger, level: int) -> None:
    """Attach a stderr handler at the given level, exactly once, even if this
    module is re-imported."""
    marker = "_doc_agent_owned"
    if any(getattr(h, marker, False) for h in logger.handlers):
        return
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(level)
    h.setFormatter(logging.Formatter("[%(name)s %(levelname)s] %(message)s"))
    setattr(h, marker, True)
    logger.addHandler(h)

_pa_logger = logging.getLogger("pydantic_ai")
_pa_logger.setLevel(logging.DEBUG)
_pa_logger.propagate = False
_attach_handler(_pa_logger, logging.DEBUG)

_da_logger = logging.getLogger("doc_agent")
_da_logger.setLevel(logging.INFO)
_da_logger.propagate = False
_attach_handler(_da_logger, logging.INFO)


def _surface_errors(fn):
    """Wrap a tool body so it logs (1) every call with its args, and (2) any
    unhandled exception with its full traceback, then returns the exception
    as a readable ERROR string back to the LLM. This makes tool execution
    visible in the terminal via the "doc_agent.<tool_name>" logger and
    prevents pydantic-ai's retry wrapper from swallowing the underlying
    error as "exceeded max retries count of N".

    What this DOES catch: any Exception raised inside the tool body
    (TypeError, ValueError, custom failures from answer_query, etc.).

    What this does NOT catch: pydantic-ai's tool ARGUMENT VALIDATION
    failures — those fire before the tool body runs. Those land in the
    "pydantic_ai" logger instead (bumped to DEBUG at module load).

    Side effect: pydantic-ai's per-tool retry=N becomes effectively inert
    for body-level exceptions because tools now always 'succeed' from its
    perspective. Validation-layer retries still happen as before."""
    log = logging.getLogger(f"doc_agent.{fn.__name__}")

    def _call_repr(kwargs: dict) -> str:
        # Drop ctx and clip long values so tracing stays readable.
        kw = {}
        for k, v in kwargs.items():
            if k == "ctx":
                continue
            s = repr(v)
            kw[k] = s if len(s) <= 200 else s[:200] + "...<clipped>"
        return ", ".join(f"{k}={v}" for k, v in kw.items())

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            log.info("call(%s)", _call_repr(kwargs))
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                tb = traceback.format_exc()
                log.error("%s: %s\n%s", type(e).__name__, e, tb)
                return f"ERROR in {fn.__name__}: {type(e).__name__}: {e}\n\n{tb}"
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        log.info("call(%s)", _call_repr(kwargs))
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("%s: %s\n%s", type(e).__name__, e, tb)
            return f"ERROR in {fn.__name__}: {type(e).__name__}: {e}\n\n{tb}"
    return sync_wrapper


def _decode_json_if_string(v):
    """Qwen-class models on OpenAI-compatible providers sometimes emit
    list-typed tool arg fields as JSON-encoded STRINGS ('["foo","bar"]')
    instead of native arrays. Pydantic then rejects them as list_type
    errors, the LLM gets a retry prompt, the model re-emits the exact same
    encoding (because it's a serialization-layer issue not a reasoning
    one), and the tool blows up after N retries.

    This validator runs BEFORE pydantic's main validation pass via
    BeforeValidator. If the incoming value is a JSON-array-shaped string,
    decode it; otherwise pass through unchanged so the real type check
    fires on genuinely-wrong inputs. Mirrors the same workaround that
    QwenToolCallChatOpenAI._unstringify_collections does for the browser
    agent (browser_agent.py:122-127), but injected at the pydantic-ai
    tool-validation layer instead of inside a custom LLM client."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return None
    if s[0] not in "[{":
        return v  # not list/object shaped — let downstream validate as-is
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return v  # bad JSON — let downstream surface the real error


# Type aliases used on every list[str]-shaped tool arg. The JSON schema
# pydantic-ai emits for these is identical to plain list[str] / list[str] |
# None — the BeforeValidator metadata only affects runtime decoding, not
# schema generation, so the LLM still sees a clean array type.
QwenStrList = Annotated[list[str], BeforeValidator(_decode_json_if_string)]
QwenStrListOpt = Annotated[list[str] | None, BeforeValidator(_decode_json_if_string)]


#Note: this exact same funtion is in use with office agent as well. In a larger project we would want to deduplicate this, but for the sake of keeping each agent self-contained I'm leaving it as-is in both places.
def _resolve_inside(workspace: Path, path: str) -> Path | str:
    """Resolve `path` against workspace and reject anything outside it.
    Returns the absolute Path on success, or a user-facing ERROR string
    on failure (so tools can return it directly to the LLM)."""
    absolute_path = (workspace / path).resolve()
    try:
        absolute_path.relative_to(workspace)
    except ValueError:
        return f"ERROR: path {path} is outside the workspace"
    return absolute_path

@theDocAgent.tool(retries=1)
@_surface_errors
def read_file(ctx: RunContext[DocDeps], path: str) -> str:
    """Read a text file. `path` is relative to the workspace root,
    e.g. 'outputs/t1_sources.md'."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    if not resolved.exists():
        return f"ERROR: file {path} does not exist"
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: file {path} is not text; read it via a python script with run_command instead"

@theDocAgent.tool(retries=1)
@_surface_errors
def write_file(ctx: RunContext[DocDeps], path: str, content: str) -> str:
    """Write text content to a workspace path (overwrites). Use for markdown,
    CSV, JSON, plain text, or python build scripts. For binary artifacts
    (xlsx, docx, pptx, png), generate them via run_command with a python
    script instead."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Wrote {resolved.stat().st_size} bytes to {path}"

@theDocAgent.tool(retries=1)
@_surface_errors
def submit(ctx: RunContext[DocDeps], produced: QwenStrList, notes: str) -> str:
    """Submit the final result. Call exactly once, after all expected files
    are written. Validates each produced path exists in the workspace and is
    non-empty before accepting."""
    workspace = ctx.deps.workspace
    normalized = []
    for path in produced:
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.relative_to(workspace)
            except ValueError:
                return f"ERROR: path {path} is outside workspace; use relative paths"
        absolute_path = workspace / p
        if not absolute_path.exists():
            return f"ERROR: claimed file {path} does not exist"
        if absolute_path.stat().st_size == 0:
            return f"ERROR: claimed file {path} is empty"
        normalized.append(str(p))

    ctx.deps.submitted = ExecutorResult(produced=normalized, notes=notes)
    return "Result submitted successfully. You may stop."

def _build_task_prompt(query: str, expects: str, dep_files: list[str]) -> str:
    dep_section = "\n".join(f" - {p}" for p in dep_files) if dep_files else "none"
    return f"""You are a document-answering sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS:
{dep_section}

WORKSPACE:
All paths you pass to tools are relative to your workspace root.
Write your outputs under outputs/.

GROUNDING (mandatory):
You have NO web access. Every factual claim in your output files must come from
an `ask` result, and you must preserve the citations `ask` returns. If `ask`
cannot ground an answer (low confidence, or it says the documents do not contain
it), record the gap honestly in the output — DO NOT fall back to prior knowledge
to fill it.

HOW TO PROCEED:
1. read_file any upstream markdown handoffs first (source lists, notes) to
   understand what each PDF is — ticker, fiscal period, company.
2. ingest_documents once with ALL PDFs from INPUT FILES in a single call. Keep
   the returned doc_ids; you will use them to scope `ask`. Skip non-PDF deps.
3. Decompose the QUERY into focused, self-contained questions and call `ask` for
   each. Pass `doc_ids=[...]` when the question is about a specific document so
   the router does not waste budget on other docs; leave unscoped for
   cross-document questions ("which competitor had the highest growth").
4. Trust `ask`'s outputs verbatim: `table_findings` numbers are pandas-computed
   and authoritative — do NOT reword or recompute them. Preserve `citations`
   exactly as returned.
5. Assemble the EXPECTED OUTPUT files yourself with write_file, stitching the
   `ask` answers and citations together. Use draft_report only when EXPECTED
   OUTPUT calls for a multi-section narrative report — for single-question or
   extract tasks it is overkill.

DO NOT try to parse PDFs by reading them as text with read_file — that will
fail or return garbage. PDFs always go through ingest_documents + ask.

WHEN DONE:
Call the submit tool with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging data gaps the engine could not ground,
    low-confidence answers, structural surprises in the inputs, or judgment
    calls about scope. Empty string if nothing notable. Do NOT recap what you
    did — the planner can see your produced files.

Do not call submit until you have written all expected files.
"""

@theDocAgent.tool(retries=1)
@_surface_errors
def list_documents(ctx: RunContext[DocDeps]) -> str:
    """List all documents currently available in this workspace's doc-reasoner
    index, with their fields (doc_id, title, page count, status, etc.). Use
    after ingest_documents to confirm what was indexed, or any time you need
    to discover which doc_ids exist before scoping an ask() call. Returns one
    JSON object per line; doc_summary is omitted from this view — use
    get_document for the full per-doc summary."""
    docs = list_local_documents(ctx.deps.workspace_id)
    if not docs:
        return "(no documents ingested in this workspace yet)"
    lines = []
    for d in docs:
        payload = {k: v for k, v in d.model_dump().items() if k != "doc_summary"}
        lines.append(json.dumps(payload, default=str))
    return "\n".join(lines)


@theDocAgent.tool(retries=1)
@_surface_errors
def get_document(ctx: RunContext[DocDeps], doc_id: str) -> str:
    """Get full details on a single document by doc_id, including the
    doc_summary (root-node summary written at ingest time). Use to confirm a
    doc is about what you expect before asking specific questions. Returns the
    document as pretty-printed JSON, or an ERROR string if the doc_id isn't
    found in this workspace."""
    doc = get_local_document(ctx.deps.workspace_id, doc_id)
    if doc is None:
        return f"ERROR: document {doc_id} not found in workspace"
    return doc.model_dump_json(indent=2)


@theDocAgent.tool(retries=1)
@_surface_errors
def ingest_documents(ctx: RunContext[DocDeps], paths: QwenStrList) -> list[str] | str:
    """Ingest one or more PDFs (or other supported docs) from the workspace
    into the doc-reasoner index. Returns the list of doc_ids that can be
    passed to ask(doc_ids=[...]).

    Per-path dedup is enforced: paths already ingested in this dispatch are
    silently skipped (you'll see only the doc_ids for newly-ingested files).
    If every path you passed was already ingested, the return value is a
    string telling you so plus the existing doc_ids — reuse those instead of
    calling again.

    Ingestion is expensive (LlamaParse + tree build). Call once per dispatch,
    up front, with all the PDFs from your INPUT FILES."""
    new_doc_ids: list[str] = []
    for path in paths:
        resolved = _resolve_inside(ctx.deps.workspace, path)
        if isinstance(resolved, str):
            return resolved  # surface workspace-escape error to the LLM
        if not resolved.exists():
            return f"ERROR: file {path} does not exist"

        rel = str(resolved.relative_to(ctx.deps.workspace))
        if rel in ctx.deps.ingested_paths:
            continue  # already ingested this dispatch — skip silently

        doc_id = ingest_local_file(
            local_path=resolved,
            workspace_id=ctx.deps.workspace_id,
            user_id=ctx.deps.user_id,
        )
        new_doc_ids.append(doc_id)
        ctx.deps.docs.append(doc_id)
        ctx.deps.ingested_paths.add(rel)

    if not new_doc_ids:
        return (
            f"All requested paths were already ingested this dispatch. "
            f"Reuse the existing doc_ids: {ctx.deps.docs}"
        )
    return new_doc_ids

@theDocAgent.tool(retries=1)
@_surface_errors
async def ask(
    ctx: RunContext[DocDeps],
    query: str,
    doc_ids: QwenStrListOpt = None,
) -> str:
    """Ask one focused, self-contained question against the doc-reasoner index
    for this workspace. Pass doc_ids=[...] to scope the query to specific
    documents you got back from ingest_documents or list_documents; leave None
    for cross-document questions. Returns the answer, citations,
    table_findings (pandas-computed numbers — authoritative), and confidence
    as pretty-printed JSON. Verbose internal trace fields (hop history, raw
    page targets, latency, query_id) are omitted to keep your context lean."""
    result = await ask_local_query(
        workspace_id=ctx.deps.workspace_id,
        user_id=ctx.deps.user_id,
        query=query,
        doc_ids=doc_ids,
    )
    return result.model_dump_json(
        exclude={"query_id", "latency_ms", "n_hops", "hops", "page_targets", "table_targets"},
        indent=2,
    )


@theDocAgent.tool(retries=1)
@_surface_errors
def list_queries(ctx: RunContext[DocDeps]) -> str:
    """List recent queries that have already been answered in this workspace
    (up to 50, newest first). Useful if you want to check whether a question
    has been asked and answered before during a prior dispatch — saves a
    round-trip if so. Returns one JSON object per line."""
    rows = list_local_queries(ctx.deps.workspace_id, limit=50)
    if not rows:
        return "(no prior queries in this workspace)"
    return "\n".join(
        r.model_dump_json(exclude={"hops", "page_targets", "table_targets"})
        for r in rows
    )


@theDocAgent.tool(retries=1)
@_surface_errors
def get_query(ctx: RunContext[DocDeps], query_id: str) -> str:
    """Fetch the full stored result of a past query by query_id (typically one
    you saw via list_queries). Returns the answer, citations, table_findings,
    and confidence as pretty-printed JSON, or an ERROR string if the query
    isn't found in this workspace or is still running."""
    row = get_local_query(ctx.deps.workspace_id, query_id)
    if row is None:
        return f"ERROR: query {query_id} not found in this workspace (or still running)"
    return row.model_dump_json(
        exclude={"hops", "page_targets", "table_targets"},
        indent=2,
    )


@theDocAgent.tool(retries=1)
@_surface_errors
async def draft_report(
    ctx: RunContext[DocDeps],
    brief: str,
    output_relpath: str,
    target_length: str = "standard",
    doc_ids: QwenStrListOpt = None,
) -> str:
    """Generate a multi-section markdown report against the doc-reasoner index
    for this workspace and write it directly into the workspace at
    output_relpath. Use ONLY when the EXPECTED OUTPUT calls for a structured,
    multi-section narrative — for single-question / extract tasks, use ask +
    write_file instead, which is much cheaper. Arguments:
      - brief: the prompt/brief the report should answer.
      - output_relpath: workspace-relative path to write the markdown to,
        e.g. 'outputs/t3_report.md'. The file is written for you — do NOT
        also call write_file with the same content afterwards.
      - target_length: one of 'brief' (3-4 sections), 'standard' (5-7), or
        'deep' (8-12).
      - doc_ids: optional list to scope the report to specific documents;
        leave None for cross-document briefs.
    Returns metadata only (report_id, doc-reasoner's internal output_path,
    stats) as pretty-printed JSON. The full draft_md is NOT returned, to keep
    your context lean — the file on disk at output_relpath is authoritative.
    If you need to inspect the body, read_file the path you just wrote to."""
    if target_length not in ("brief", "standard", "deep"):
        return f"ERROR: target_length must be 'brief', 'standard', or 'deep' (got {target_length!r})"

    resolved = _resolve_inside(ctx.deps.workspace, output_relpath)
    if isinstance(resolved, str):
        return resolved  # workspace-escape error

    result = await draft_local_report(
        workspace_id=ctx.deps.workspace_id,
        user_id=ctx.deps.user_id,
        brief=brief,
        doc_ids=doc_ids,
        target_length=target_length,
    )

    if not result.draft_md:
        return (
            f"ERROR: report generation finished but produced no markdown "
            f"(report_id={result.report_id}). Nothing was written to {output_relpath}."
        )

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(result.draft_md, encoding="utf-8")

    return result.model_dump_json(
        exclude={"outline", "sections", "draft_md"}, indent=2
    )


@theDocAgent.tool(retries=1)
@_surface_errors
def list_reports(ctx: RunContext[DocDeps]) -> str:
    """List recent reports already drafted in this workspace (up to 50, newest
    first). Useful for checking whether a report has been drafted before in a
    prior dispatch. Returns one JSON object per line; the full draft_md is
    omitted from the list view — use get_report for the full markdown."""
    rows = list_local_reports(ctx.deps.workspace_id, limit=50)
    if not rows:
        return "(no prior reports in this workspace)"
    return "\n".join(
        r.model_dump_json(exclude={"draft_md", "outline_json"})
        for r in rows
    )


@theDocAgent.tool(retries=1)
@_surface_errors
def get_report(ctx: RunContext[DocDeps], report_id: str) -> str:
    """Fetch the full stored result of a past report by report_id (typically
    one you saw via list_reports). Returns the full draft_md and metadata as
    pretty-printed JSON, or an ERROR string if the report isn't found in this
    workspace, is still running, or failed to complete."""
    row = get_local_report(ctx.deps.workspace_id, report_id)
    if row is None:
        return f"ERROR: report {report_id} not found in this workspace (or still running)"
    return row.model_dump_json(indent=2)


async def _debug_event_stream_handler(_ctx, events) -> None:
    """Log every event from the agent run to "doc_agent.events". This is our
    window into what the LLM is actually trying — most importantly the raw
    args on FunctionToolCallEvent, which is what we need to see for the
    `ask`-validation-failure debugging. pydantic-ai's standard logging
    channel does NOT surface tool-arg validation failures (they're routed
    back to the LLM as RetryPromptPart messages and never logged), so this
    stream is the only way to observe them in real time."""
    del _ctx  # required by pydantic-ai's handler signature, intentionally unused
    log = logging.getLogger("doc_agent.events")
    async for event in events:
        log.info("%s: %r", type(event).__name__, event)


async def run_doc_executor(
        workspace: Path,
        workspace_id: str,
        user_id: str,
        query: str,
        expects: str,
        dep_files: list[str],
) -> ExecutorResult:
    deps = DocDeps(
        workspace=workspace.resolve(),
        workspace_id=workspace_id,
        user_id=user_id,
    )
    task_prompt = _build_task_prompt(query, expects, dep_files)
    log = logging.getLogger("doc_agent.executor")

    try:
        await theDocAgent.run(
            user_prompt=task_prompt,
            deps=deps,
            event_stream_handler=_debug_event_stream_handler,
        )
    except TypeError as e:
        # Older pydantic-ai versions don't accept event_stream_handler. Fall
        # back to a vanilla run so we don't fail just because the diagnostic
        # channel isn't supported.
        if "event_stream_handler" not in str(e):
            raise
        log.warning("event_stream_handler not supported by this pydantic-ai version; running without it")
        try:
            await theDocAgent.run(user_prompt=task_prompt, deps=deps)
        except Exception as inner:
            _log_agent_exception(log, inner)
            return ExecutorResult(produced=[], notes="", error=f"Agent loop failed: {type(inner).__name__}: {inner}")
    except Exception as e:
        _log_agent_exception(log, e)
        return ExecutorResult(produced=[], notes="", error=f"Agent loop failed: {type(e).__name__}: {e}")

    if deps.submitted is None:
        return ExecutorResult(produced=[], notes="", error="Agent loop ended without calling submit")
    return deps.submitted


def _log_agent_exception(log: logging.Logger, e: Exception) -> None:
    """Dump every channel that might carry the underlying cause of an
    agent-loop failure. UnexpectedModelBehavior often has a `body` attr with
    the failing payload; some other pydantic-ai exceptions carry context
    on __cause__ or in their __dict__. Belt-and-braces — log all of them."""
    log.error("Agent loop failed: %s: %s", type(e).__name__, e)
    log.error("Exception __dict__: %r", getattr(e, "__dict__", None))
    for attr in ("body", "message", "model_response", "tool_call"):
        v = getattr(e, attr, None)
        if v is not None:
            log.error("Exception .%s: %r", attr, v)
    if e.__cause__ is not None:
        log.error("Exception __cause__: %r", e.__cause__)
    log.error("Traceback:\n%s", traceback.format_exc())
