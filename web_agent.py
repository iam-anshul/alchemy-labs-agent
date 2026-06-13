from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from system_prompts import web_system_prompt
from dataclasses import dataclass, field
from api.events import EventSink, file_artifact
from browser_agent import ExecutorResult
from typing import Literal
import mimetypes
import asyncio

from linkup import LinkupClient

from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")
LINKUP_API_KEY = os.getenv("LINKUP_API_KEY")

linkup_client = LinkupClient(api_key=LINKUP_API_KEY)

@dataclass
class WebDeps:
    workspace: Path
    sink: EventSink = field(default_factory=EventSink)
    submitted: ExecutorResult | None = None

model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

theWebAgent = Agent[WebDeps](
    model,
    deps_type=WebDeps,
    system_prompt=web_system_prompt,
    retries=3,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False})
)

def _build_task_prompt(query: str, expects: str, dep_files: list[str]) -> str:
    dep_section = "\n".join(f" - {p}" for p in dep_files) if dep_files else "none"
    return f"""You are a web-search sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS (read these as needed using read_file):
{dep_section}

WORKSPACE:
All paths you pass to tools are relative to your workspace root.
Write your outputs under outputs/.

HOW TO WORK:
Read any upstream input files first. Then use `web_search_with_linkup` as your
primary tool to find information — it returns a synthesized, sourced answer
plus source URLs. Use `fetch_url` only when you need the full contents of a
specific page (e.g. one of those source URLs). Use the minimum number of web
actions needed; do not search or fetch for sport. Persist your findings with
`write_file`, and carry a source URL for every factual claim or figure you
write — downstream tasks and the user rely on these.

WHEN DONE:
Call the submit tool with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, sources that were unavailable and what
    you used instead). Empty string if nothing. Do NOT recap what you did and
    do NOT claim to have used a tool you did not actually call — the planner
    can see the produced files.

Do not call submit until you have written all expected files.
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

@theWebAgent.tool(retries=1)
def submit(ctx: RunContext[WebDeps], produced: list[str], notes: str) -> str:
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


@theWebAgent.tool(retries=1)
def write_file(ctx: RunContext[WebDeps], path: str, content: str) -> str:
    """Write text content to a workspace path (overwrites). Use for markdown,
    CSV, JSON, plain text, or python build scripts. For binary artifacts
    (xlsx, docx, pptx, png), generate them via run_command with a python
    script instead."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    try:
        rel = str(resolved.relative_to(ctx.deps.workspace))
        asyncio.create_task(
            ctx.deps.sink.publish_ui(
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
        )
    except RuntimeError:
        pass
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


@theWebAgent.tool_plain(retries=3)
def web_search_with_linkup(query: str, depth: Literal["standard", "deep"]):
    """Search the live web and get back a sourced answer.

    Use this as your primary way to find information, look up current facts or
    data, and discover authoritative source URLs. It does NOT return a raw list
    of links — Linkup's agent reads the web and returns a synthesized `answer`
    plus a list of `sources`, each with a title, URL, and snippet. If the answer
    you need is fully in that response, you may not need to fetch_url at all; if
    you need a page's full contents, take a URL from `sources` and pass it to
    fetch_url.

    Args:
        query: A specific, instruction-style natural-language query — not bare
            keywords. State what you want and, where relevant, where to look;
            Linkup follows instructions in the query literally. For example,
            "Find Acme Corp's FY2025 annual report and return the PDF URL and
            headline revenue and net income" works far better than "Acme
            financials". Specific queries return better answers and cost fewer
            calls.
        depth: Search effort. Use "standard" for ordinary lookups — a single,
            fast, cheap pass that handles most questions. Use "deep" only when
            the question genuinely needs multi-step, iterative research that one
            pass won't satisfy; it runs an iterative search-and-scrape loop that
            is markedly slower and roughly 10x the cost, so do not reach for it
            by default.
    """
    return linkup_client.search(
        query=query,
        depth=depth,
        output_type="sourcedAnswer"
    )

@theWebAgent.tool_plain(retries=3)
def fetch_url(url: str):
    """Fetch a single web page and return its full contents as clean markdown.

    JavaScript is rendered, so this works on dynamic / client-rendered pages too.
    Use this when you already have a specific URL — typically one surfaced in the
    `sources` of a web_search_with_linkup result — and you need the page's full
    text (e.g. to extract a table, read a full article, or get details the search
    answer only summarized), rather than the search engine's synthesized summary.
    Returns page text as markdown, not a saved binary file.

    Args:
        url: The full, absolute URL of the page to fetch, including the scheme
            (e.g. "https://example.com/report"). Relative URLs or bare domains
            will not work.
    """
    return linkup_client.fetch(url=url, render_js=True)

async def run_web_executor(
        workspace_subdir_path: Path,
        query: str,
        expects: str,
        dep_files: list[str],
        sink: EventSink = EventSink()
) -> ExecutorResult:
    deps = WebDeps(workspace=workspace_subdir_path.resolve(), sink=sink.child(agent_type="web_search"))
    task_prompt = _build_task_prompt(query, expects, dep_files)

    try:
        await theWebAgent.run(user_prompt=task_prompt, deps=deps)
    except Exception as e:
        return ExecutorResult(
            produced=[],
            notes="",
            error=f"Agent loop failed {type(e).__name__}: {e}"
        )

    if deps.submitted is None:
        return ExecutorResult(
            produced=[],
            notes="",
            error="Agent finished without calling submit"
        )
    return deps.submitted