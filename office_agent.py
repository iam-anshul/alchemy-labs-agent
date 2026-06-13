import json
import os
import shutil
import subprocess
import asyncio
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from api.events import EventSink, file_artifact
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from browser_agent import ExecutorResult
from system_prompts import office_system_prompt

# Resolved once at import so every officecli call doesn't re-walk PATH.
# None means the binary is not installed; the tool returns a clean error
# in that case rather than letting subprocess raise FileNotFoundError.
_OFFICECLI_PATH = shutil.which("officecli")

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")


#Note: this exact same dataclass is in use with doc agent as well. In a larger project we would want to deduplicate this, but for the sake of keeping each agent self-contained I'm leaving it as-is in both places.
@dataclass
class OfficeDeps:
    """Per-run state passed into the office agent via RunContext.

    workspace is the absolute root the tools resolve relative paths against;
    the LLM never sees it. submitted is the mutable holder the submit tool
    writes into so the caller can recover the ExecutorResult after the agent
    loop ends — pydantic-ai gives no other channel to lift state out of a
    tool call.
    """
    workspace: Path
    sink: EventSink = field(default_factory=EventSink)
    submitted: ExecutorResult | None = None


model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

theOfficeAgent = Agent[OfficeDeps, str](
    model,
    deps_type=OfficeDeps,
    system_prompt=office_system_prompt,
    retries=3,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)

#Note: this exact same funtion is in use with doc agent as well. In a larger project we would want to deduplicate this, but for the sake of keeping each agent self-contained I'm leaving it as-is in both places.
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


@theOfficeAgent.tool(retries=1)
def read_file(ctx: RunContext[OfficeDeps], path: str) -> str:
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


@theOfficeAgent.tool(retries=1)
async def write_file(ctx: RunContext[OfficeDeps], path: str, content: str) -> str:
    """Write text content to a workspace path (overwrites). Use for markdown,
    CSV, JSON, plain text, or python build scripts. For binary artifacts
    (xlsx, docx, pptx, png), generate them via run_command with a python
    script instead."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    # Awaited directly (not asyncio.create_task): pydantic-ai runs sync tools in
    # a worker thread with no event loop, so create_task there raises and the
    # event is silently dropped. An async tool runs on the loop, so the publish
    # actually reaches subscribers.
    rel = str(resolved.relative_to(ctx.deps.workspace))
    await ctx.deps.sink.publish_ui(
        "artifact_ready",
        stage="writing_file",
        status="progress",
        message=f"Office agent wrote {resolved.name}",
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


@theOfficeAgent.tool(retries=1)
def run_command(ctx: RunContext[OfficeDeps], command: str) -> str:
    """Run a shell command with the workspace as cwd. Relative paths in the
    command resolve against the workspace. Use for invoking python scripts,
    listing files, etc."""
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        cwd=ctx.deps.workspace,
    )
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    if result.returncode == 0:
        return stdout or "(no output)"
    return f"ERROR (exit {result.returncode}):\nstderr:\n{stderr}\nstdout:\n{stdout}"


@theOfficeAgent.tool(retries=1)
def officecli(ctx: RunContext[OfficeDeps], args: list[str]) -> str:
    """Invoke the OfficeCLI binary for purpose-built read/edit of .docx,
    .xlsx, .pptx files. Pass args as a list, e.g.
    ['create', 'outputs/report.docx'] or
    ['get', 'outputs/data.xlsx', '--cell', 'A1']. --json is appended
    automatically; on success returns the parsed JSON as pretty-printed
    text; on failure returns the stderr."""
    if _OFFICECLI_PATH is None:
        return (
            "ERROR: officecli binary not found on PATH. "
            "Install from https://github.com/iOfficeAI/OfficeCLI"
        )
    # Append --json by default so the agent sees structured output. Skip it for
    # commands that print plain markdown/help text and silently ignore the
    # following positional arg when --json is set (load_skill, help, ...).
    plain_text_commands = {"load_skill", "help", "--help", "-h"}
    cmd_args = list(args)
    if not cmd_args or cmd_args[0] not in plain_text_commands:
        cmd_args.append("--json")
    result = subprocess.run(
        [_OFFICECLI_PATH, *cmd_args],
        capture_output=True,
        cwd=ctx.deps.workspace,
    )
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        return f"ERROR (exit {result.returncode}):\nstderr:\n{stderr}\nstdout:\n{stdout}"
    if not stdout.strip():
        return "(no output)"
    # Pretty-print JSON so the agent sees structured data cleanly; fall back
    # to raw stdout for commands whose --json output isn't parseable JSON.
    try:
        return json.dumps(json.loads(stdout), indent=2)
    except json.JSONDecodeError:
        return stdout


@theOfficeAgent.tool(retries=1)
def submit(ctx: RunContext[OfficeDeps], produced: list[str], notes: str) -> str:
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
    return f"""You are an office sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS (read these as needed using read_file):
{dep_section}

WORKSPACE:
All paths you pass to tools are relative to your workspace root.
Write your outputs under outputs/.

TOOL CHOICE (mandatory):
Any creation or modification of a .docx / .xlsx / .pptx file MUST go through
the `officecli` tool. Do not write Python scripts that use openpyxl,
python-docx, or python-pptx to do these operations — those libraries are
present in the environment but using them for Office files violates this
contract. `run_command` + python is reserved for pandas analysis, matplotlib
charts, and CSV/JSON wrangling that officecli cannot do.

WHEN DONE:
Call the submit tool with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, surprises). Empty string if nothing.
    Do NOT recap what you did and do NOT claim to have used a tool you did
    not actually call — the planner can see the produced files.

Do not call submit until you have written all expected files.
"""


async def run_office_executor(
    workspace: Path,
    query: str,
    expects: str,
    dep_files: list[str],
    sink: EventSink = EventSink(),
) -> ExecutorResult:
    """Pattern-B entrypoint: build per-run deps, run the module-level agent,
    return the result captured by the submit tool.

    Plugs into dispatch_executor_agent in main.py for the 'office' branch."""
    deps = OfficeDeps(workspace=workspace.resolve(), sink=sink.child(agent_type="office"))
    task_prompt = _build_task_prompt(query, expects, dep_files)

    try:
        await theOfficeAgent.run(user_prompt=task_prompt, deps=deps)
    except Exception as e:
        return ExecutorResult(
            produced=[],
            notes="",
            error=f"Agent loop failed: {type(e).__name__}: {e}",
        )

    if deps.submitted is None:
        return ExecutorResult(
            produced=[],
            notes="",
            error="Agent finished without calling submit",
        )
    return deps.submitted
