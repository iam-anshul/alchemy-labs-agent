from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from qwen_compat import QwenChatModel
from system_prompts import web_system_prompt
from dataclasses import dataclass, field
from api.events import EventSink, file_artifact
from browser_agent import ExecutorResult
from typing import Literal
import mimetypes
import asyncio
import hashlib
import re

from exa_rotation import exa_client

from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")
# Exa keys are loaded and rotated by exa_rotation.exa_client (round-robin across
# all EXA_API_KEY* values in .env, with failover on rate-limit/auth errors).

@dataclass
class WebDeps:
    workspace: Path
    sink: EventSink = field(default_factory=EventSink)
    page_cache: dict[str, "CachedPage"] = field(default_factory=dict)

@dataclass
class CachedPage:
    url: str
    content: str

class WebSubmission(BaseModel):
    produced: list[str]
    notes: str


model = QwenChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

theWebAgent = Agent[WebDeps, WebSubmission](
    model,
    deps_type=WebDeps,
    output_type=ToolOutput(
        WebSubmission,
        name="submit",
        description=(
            "Finish the web-search task with the relative paths actually "
            "written and brief notes for the planner."
        ),
    ),
    system_prompt=web_system_prompt,
    retries=3,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False})
)

def _build_task_prompt(
    query: str,
    expects: str,
    dep_files: list[str],
    page_cache: dict[str, CachedPage],
) -> str:
    dep_section = "\n".join(f" - {p}" for p in dep_files) if dep_files else "none"
    page_section = "\n".join(
        f" - {page_id}: {page.url} ({len(page.content)} characters)"
        for page_id, page in page_cache.items()
    ) or "none"
    return f"""You are a web-search sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS (read these as needed using read_file):
{dep_section}

PAGES VISITED IN THIS RUN (search by page ID using search_page):
{page_section}

WORKSPACE:
All paths you pass to tools are relative to your workspace root.
Write your outputs under outputs/.

HOW TO WORK:
Read any upstream input files first. Then use `web_search` as your primary tool
to find information — it returns a synthesized, sourced answer plus citation
URLs. Use `fetch_url` only when you need the full contents of a specific page
(e.g. one of those citation URLs). It caches the page and returns a page ID;
use `search_page` to search the entire page without loading it into context.
Use the minimum number of web actions needed; do not search or fetch for sport.
Persist your findings with `write_file`, and carry a source URL for every
factual claim or figure you write — downstream tasks and the user rely on these.

WHEN DONE:
Return the terminal `submit` output with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, sources that were unavailable and what
    you used instead). Empty string if nothing. Do NOT recap what you did and
    do NOT claim to have used a tool you did not actually call — the planner
    can see the produced files.

Do not submit until you have written all expected files.
"""

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

async def _accept_submission(
    deps: WebDeps,
    produced: list[str],
    notes: str,
) -> ExecutorResult:
    workspace = deps.workspace
    normalized = []
    for path in produced:
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.relative_to(workspace)
            except ValueError:
                return ExecutorResult(
                    produced=[],
                    notes="",
                    error=f"path {path} is outside workspace; use relative paths",
                )
        absolute_path = workspace / p
        if not absolute_path.exists():
            return ExecutorResult(
                produced=[],
                notes="",
                error=f"claimed file {path} does not exist",
            )
        if absolute_path.stat().st_size == 0:
            return ExecutorResult(
                produced=[],
                notes="",
                error=f"claimed file {path} is empty",
            )
        normalized.append(str(p))

    return ExecutorResult(produced=normalized, notes=notes)


async def _publish_submission(
    deps: WebDeps,
    submission: WebSubmission,
) -> None:
    """Publish the validated final artifact set exactly once."""
    artifacts = []
    for rel in submission.produced:
        full = deps.workspace / rel
        suffix = Path(rel).suffix.lower()
        content = None
        if suffix in {".md", ".txt", ".csv", ".json"}:
            try:
                content = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                content = None
        artifacts.append(
            file_artifact(
                kind="markdown" if suffix == ".md" else "file",
                path=rel,
                filename=Path(rel).name,
                type=suffix.lstrip(".") or None,
                mime_type=mimetypes.guess_type(str(full))[0],
                bytes=full.stat().st_size,
                content=content,
            )
        )
    await deps.sink.publish_ui(
        "artifact_ready",
        stage="web_search",
        status="progress",
        message=f"Web search agent produced {len(submission.produced)} file(s)",
        data={"produced": submission.produced, "notes": submission.notes},
        artifacts=artifacts,
    )


@theWebAgent.output_validator
async def validate_web_submission(
    ctx: RunContext[WebDeps],
    submission: WebSubmission,
) -> WebSubmission:
    result = await _accept_submission(
        ctx.deps,
        submission.produced,
        submission.notes,
    )
    if result.error:
        raise ModelRetry(result.error)
    return WebSubmission(produced=result.produced, notes=result.notes)


@theWebAgent.tool(retries=1)
async def write_file(ctx: RunContext[WebDeps], path: str, content: str) -> str:
    """Write text content to a workspace path (overwrites). Use for markdown,
    CSV, JSON, plain text, or python build scripts. For binary artifacts
    (xlsx, docx, pptx, png), generate them via run_command with a python
    script instead."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    rel = str(resolved.relative_to(ctx.deps.workspace))
    await ctx.deps.sink.publish_ui(
        "artifact_ready",
        stage="writing_file",
        status="progress",
        message=f"Web Search agent wrote {resolved.name}",
        artifacts=[
            file_artifact(
                kind="markdown" if resolved.suffix.lower() == ".md" else "file",
                path=rel,
                filename=resolved.name,
                type=resolved.suffix.lstrip(".").lower() or None,
                mime_type=mimetypes.guess_type(str(resolved))[0],
                bytes=resolved.stat().st_size,
                content=content if resolved.suffix.lower() in {".md", ".txt", ".csv", ".json"} else None,
            )
        ],
    )
    return f"Wrote {resolved.stat().st_size} bytes to {path}"

@theWebAgent.tool(retries=1)
def read_file(ctx: RunContext[WebDeps], path: str) -> str:
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


@theWebAgent.tool(retries=3)
async def web_search(ctx: RunContext[WebDeps], query: str, depth: Literal["standard", "deep"]):
    """Search the live web and get back a sourced answer.

    Use this as your primary way to find information, look up current facts or
    data, and discover authoritative source URLs. It does NOT return a raw list
    of links — Exa reads the web and returns a synthesized `answer` plus a list
    of `citations`, each with a url, title, and the source text it drew from. If
    the answer you need is fully in that response, you may not need fetch_url at
    all; if you need a page's full contents, take a url from `citations` and
    pass it to fetch_url.

    Args:
        query: A specific, instruction-style natural-language query — not bare
            keywords. State what you want and, where relevant, where to look.
            For example, "Find Acme Corp's FY2025 annual report and return the
            PDF URL and headline revenue and net income" works far better than
            "Acme financials". Specific queries return better answers.
        depth: Search effort. Use "standard" for ordinary lookups — fast and
            cheap, handles most questions. Use "deep" only when the question
            genuinely needs multi-step reasoning that one pass won't satisfy; it
            is markedly slower and more expensive, so do not reach for it by
            default.
    """
    model = "exa-pro" if depth == "deep" else "exa"
    # The Exa SDK is blocking; run it off the event loop so the agent loop (and
    # the event publish below) isn't stalled.
    response = await asyncio.to_thread(exa_client.answer, query, text=True, model=model)
    citations = [
        {"url": c.url, "title": c.title, "text": c.text}
        for c in response.citations
    ]
    # Surface the search to the event log so the UI can show what the agent
    # looked up and which sources it found — mirrors how write_file emits an
    # artifact_ready event. Awaited directly (not create_task) because this tool
    # runs on the event loop; create_task from a worker thread would have raised
    # "no running event loop" and been silently dropped.
    await ctx.deps.sink.publish_ui(
        "agent_progress",
        stage="web_search",
        status="progress",
        message=f"Searched the web: {query}",
        data={
            "query": query,
            "depth": depth,
            "sources": [
                {"url": c["url"], "title": c["title"]} for c in citations
            ],
        },
    )
    return {
        "answer": response.answer,
        "citations": citations,
    }

@theWebAgent.tool(retries=3)
async def fetch_url(ctx: RunContext[WebDeps], url: str):
    """Fetch a page into the run cache and return its page ID.

    Use this when you already have a specific URL — typically one surfaced in
    the `citations` of a web_search result. The full content stays outside model
    context; call search_page with the returned page ID to inspect it.

    Args:
        url: The full, absolute URL of the page to fetch, including the scheme
            (e.g. "https://example.com/report"). Relative URLs or bare domains
            will not work.
    """
    page_id = f"page_{hashlib.sha256(url.encode()).hexdigest()[:12]}"
    cached = ctx.deps.page_cache.get(page_id)
    if cached is not None:
        return {
            "page_id": page_id,
            "url": cached.url,
            "characters": len(cached.content),
            "cached": True,
        }

    await ctx.deps.sink.publish_ui(
        "agent_progress",
        stage="web_search",
        status="progress",
        message=f"Fetched page: {url}",
        data={"url": url},
    )
    response = await asyncio.to_thread(
        exa_client.get_contents, [url], text=True, livecrawl="always"
    )
    if not response.results:
        return f"ERROR: no content retrieved for {url}"
    result = response.results[0]
    content = result.text or ""
    ctx.deps.page_cache[page_id] = CachedPage(
        url=url,
        content=content,
    )
    return {
        "page_id": page_id,
        "url": url,
        "characters": len(content),
        "cached": False,
    }

@theWebAgent.tool(retries=1)
def search_page(
    ctx: RunContext[WebDeps],
    page_id: str,
    pattern: str,
    max_matches: int = 20,
):
    """Search every line of a cached page with a case-insensitive regex.

    Only bounded matching excerpts enter model context. Use `|` to search for
    multiple terms.
    """
    page = ctx.deps.page_cache.get(page_id)
    if page is None:
        return f"ERROR: unknown page_id {page_id}"
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"ERROR: invalid regular expression: {e}"

    limit = max(1, min(max_matches, 50))
    matches = []
    total_matches = 0
    for line_number, line in enumerate(page.content.splitlines(), start=1):
        for match in regex.finditer(line):
            total_matches += 1
            if len(matches) < limit:
                start = max(0, match.start() - 200)
                end = min(len(line), match.end() + 300)
                matches.append({
                    "line": line_number,
                    "text": line[start:end].strip(),
                })
    return {
        "page_id": page_id,
        "url": page.url,
        "pattern": pattern,
        "total_matches": total_matches,
        "matches": matches,
        "truncated": total_matches > len(matches),
    }

async def run_web_executor(
        workspace_subdir_path: Path,
        query: str,
        expects: str,
        dep_files: list[str],
        page_cache: dict[str, CachedPage],
        sink: EventSink = EventSink()
) -> ExecutorResult:
    deps = WebDeps(
        workspace=workspace_subdir_path.resolve(),
        sink=sink.child(agent_type="web_search"),
        page_cache=page_cache,
    )
    task_prompt = _build_task_prompt(query, expects, dep_files, page_cache)

    try:
        run_result = await theWebAgent.run(user_prompt=task_prompt, deps=deps)
    except Exception as e:
        return ExecutorResult(
            produced=[],
            notes="",
            error=f"Agent loop failed {type(e).__name__}: {e}"
        )

    submission = run_result.output
    await _publish_submission(deps, submission)
    return ExecutorResult(produced=submission.produced, notes=submission.notes)
