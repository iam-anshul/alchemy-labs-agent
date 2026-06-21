import json
import os
import shutil
import subprocess
import asyncio
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

import logfire
from app.api.events import EventSink, file_artifact
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from app.core.qwen_compat import QwenChatModel

from app.agents.browser_agent import ExecutorResult
from app.core.config import get_settings
from app.prompts.system_prompts import office_system_prompt

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
    the LLM never sees it.
    """
    workspace: Path
    sink: EventSink = field(default_factory=EventSink)


class OfficeSubmission(BaseModel):
    produced: list[str]
    notes: str


model = QwenChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

theOfficeAgent = Agent[OfficeDeps, OfficeSubmission](
    model,
    deps_type=OfficeDeps,
    output_type=ToolOutput(
        OfficeSubmission,
        name="submit",
        description=(
            "Finish the office task with the relative paths actually written "
            "and brief notes for the planner."
        ),
    ),
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
def write_file(ctx: RunContext[OfficeDeps], path: str, content: str) -> str:
    """Write text content to a workspace path (overwrites). Use for markdown,
    CSV, JSON, plain text, or python build scripts. For binary artifacts
    (xlsx, docx, pptx, png), generate them via run_command with a python
    script instead."""
    resolved = _resolve_inside(ctx.deps.workspace, path)
    if isinstance(resolved, str):
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    # No UI artifact is published here. In the office workflow write_file is used
    # for INTERMEDIATES — the officecli batch ops JSON, helper build scripts,
    # scratch CSV/JSON — not the deliverable (the .pptx/.docx/.xlsx is built by
    # officecli/run_command). Streaming every write_file leaked those internals
    # into the UI. The final deliverables are published exactly once, at the end,
    # by _publish_submission from the validated `produced` list — so suppressing
    # the live publish here hides the scratch files without losing the result.
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


# officecli subcommands that mutate the document on disk. After one of these
# runs, the file's edits live only in officecli's in-memory "resident" process
# (it keeps docs open in the background for speed) and are NOT flushed to disk
# until the resident is closed. If the agent never closes it, the file on disk
# stays as it was at `create` time — a blank shell that still has bytes, so it
# passes the non-empty submission check while opening completely empty. We flush
# after every mutating command so the disk file always reflects the edits.
_OFFICECLI_MUTATING_COMMANDS = {
    "create", "add", "set", "delete", "batch", "import", "merge", "move",
}


def _run_officecli(workspace: Path, cmd_args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_OFFICECLI_PATH, *cmd_args],
        capture_output=True,
        cwd=workspace,
    )


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
    result = _run_officecli(ctx.deps.workspace, cmd_args)
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        return f"ERROR (exit {result.returncode}):\nstderr:\n{stderr}\nstdout:\n{stdout}"

    # Flush the resident to disk after a successful mutating command. The file
    # path is the second positional token (e.g. ['add', 'outputs/x.pptx', ...]);
    # `close` is a cheap no-op (exit 0) when no resident is running for it, so
    # this is safe to call unconditionally. We swallow its output — it's
    # bookkeeping the agent doesn't need to see.
    if args and args[0] in _OFFICECLI_MUTATING_COMMANDS and len(args) >= 2:
        target = args[1]
        _run_officecli(ctx.deps.workspace, ["close", target, "--json"])

    if not stdout.strip():
        return "(no output)"
    # Pretty-print JSON so the agent sees structured data cleanly; fall back
    # to raw stdout for commands whose --json output isn't parseable JSON.
    try:
        return json.dumps(json.loads(stdout), indent=2)
    except json.JSONDecodeError:
        return stdout


# Office file extensions whose "non-empty" cannot be judged by byte size: a
# freshly-`create`d .pptx/.docx/.xlsx is a valid ZIP of boilerplate XML and is
# several KB on disk while containing zero slides/paragraphs/cells. We probe
# their real content with `officecli view <file> stats` instead.
_OFFICE_CONTENT_EXTS = {".pptx", ".docx", ".xlsx"}


def _office_content_error(workspace: Path, rel_path: Path) -> str | None:
    """Return a user-facing error if an Office file is structurally empty, else
    None. Uses `officecli view <file> stats`: for pptx/docx an empty file has
    words==0, for xlsx totalCells==0. Returns None (i.e. "assume OK") if
    officecli is unavailable or stats can't be parsed — we don't want a probe
    failure to block an otherwise-valid submission."""
    if _OFFICECLI_PATH is None:
        return None
    result = _run_officecli(workspace, ["view", str(rel_path), "stats", "--json"])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.decode(errors="replace")).get("data", {})
    except (json.JSONDecodeError, AttributeError):
        return None

    suffix = rel_path.suffix.lower()
    if suffix == ".xlsx":
        if data.get("totalCells", 1) == 0:
            return (
                f"file {rel_path} is an empty workbook (0 cells) — the officecli "
                f"edits did not land. Re-add the data and verify with "
                f"`officecli view {rel_path} stats` before submitting."
            )
    else:  # .pptx / .docx
        slides = data.get("slides")
        if data.get("words", 1) == 0 and (slides is None or slides == 0):
            kind = "presentation" if suffix == ".pptx" else "document"
            return (
                f"file {rel_path} is an empty {kind} (no text content) — the "
                f"officecli edits did not land (a common cause is a `batch` whose "
                f"ops all failed). Re-add the content with individual "
                f"`officecli add` commands and verify with "
                f"`officecli view {rel_path} stats` before submitting."
            )
    return None


def _validate_submission(
    workspace: Path,
    submission: OfficeSubmission,
) -> ExecutorResult:
    normalized = []
    for path in submission.produced:
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
        # Byte size isn't enough for Office files — a blank deck/doc/workbook is
        # still several KB. Probe real content so a silently-empty artifact is
        # rejected (→ ModelRetry) instead of shipped.
        if p.suffix.lower() in _OFFICE_CONTENT_EXTS:
            content_error = _office_content_error(workspace, p)
            if content_error:
                return ExecutorResult(produced=[], notes="", error=content_error)
        normalized.append(str(p))

    return ExecutorResult(produced=normalized, notes=submission.notes)


async def _publish_submission(
    deps: OfficeDeps,
    submission: OfficeSubmission,
) -> None:
    """Publish the validated final artifact set exactly once."""
    artifacts = []
    for rel in submission.produced:
        full = deps.workspace / rel
        suffix = full.suffix.lower()
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
                filename=full.name,
                type=suffix.lstrip(".") or None,
                mime_type=mimetypes.guess_type(str(full))[0],
                bytes=full.stat().st_size,
                content=content,
            )
        )
    await deps.sink.publish_ui(
        "artifact_ready",
        stage="office",
        status="progress",
        message=f"Office agent produced {len(submission.produced)} file(s)",
        data={"produced": submission.produced, "notes": submission.notes},
        artifacts=artifacts,
    )


@theOfficeAgent.output_validator
def validate_office_submission(
    ctx: RunContext[OfficeDeps],
    submission: OfficeSubmission,
) -> OfficeSubmission:
    result = _validate_submission(ctx.deps.workspace, submission)
    if result.error:
        raise ModelRetry(result.error)
    return OfficeSubmission(produced=result.produced, notes=result.notes)


# Map an artifact type (inferred from the EXPECTED OUTPUT text) to the OfficeCLI
# skill we force-load for that run. Order matters: more specific extensions win.
_SKILL_FOR_EXT = {
    ".pptx": "pitch-deck",  # the pptx baseline most decks want; covers generic pptx too
    ".xlsx": "data-dashboard",
    ".docx": "academic-paper",
}


def _detect_office_ext(expects: str) -> str | None:
    """Pick the dominant Office extension named in the EXPECTED OUTPUT text."""
    lowered = expects.lower()
    for ext in (".pptx", ".xlsx", ".docx"):
        if ext in lowered:
            return ext
    return None


def _load_skill_text(skill: str) -> str | None:
    """Fetch a skill's SKILL.md via officecli, or None if unavailable."""
    if _OFFICECLI_PATH is None:
        return None
    result = subprocess.run(
        [_OFFICECLI_PATH, "load_skill", skill],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.decode(errors="replace").strip()
    return text or None


# Verified officecli recipe for building a .pptx that ACTUALLY persists text AND
# stays within the per-run request budget. Use `batch` (one request per slide-ish
# group), NOT one `add` call per shape — a 15-slide deck built one shape at a time
# blows past the request_limit and the run is killed mid-build. The batch op
# schema below is the one the loaded SKILL.md uses and is grep-verified to work:
# top-level command/parent/type, with everything else (text + geometry) under
# `props`. Each text shape needs explicit geometry (x/y/width/height).
_PPTX_RECIPE = """VERIFIED PPTX RECIPE — follow this exactly; it is known to work
and to stay within your request budget:
1. Create the deck: officecli(['create', 'outputs/<name>.pptx'])
2. Build slides with `batch` — ONE batch call per slide (or per few slides), NOT
   one officecli call per shape. Building a 15-slide deck with an `add` call per
   shape will exceed the run's request limit and the build will be killed before
   it finishes. Write the ops JSON with write_file, then run the batch:
     officecli(['batch', 'outputs/<name>.pptx', '--input', 'outputs/ops_slide1.json'])
3. Batch op schema (verified — this exact shape works): command/parent/type at
   the TOP level, and text + geometry under `props`. Every text shape needs
   explicit geometry (x/y/width/height) or it can render empty:
     [
       {"command":"add","parent":"/","type":"slide","props":{"layout":"blank","background":"1E2761"}},
       {"command":"add","parent":"/slide[1]","type":"shape",
        "props":{"text":"Your Title","x":"2cm","y":"5cm","width":"29cm","height":"3cm",
                 "font":"Georgia","size":"44","bold":"true","color":"FFFFFF","align":"center"}}
     ]
   Common mistakes that make ops fail (and leave the file empty): putting x/y/
   width/height at the TOP level instead of under `props`; omitting `type`;
   using `prop` instead of `props`. If batch returns success=false, read the
   per-op error and fix the JSON — do NOT fall back to one add-per-shape.
4. VERIFY before submitting: officecli(['view', 'outputs/<name>.pptx', 'stats'])
   — confirm `slides` and `words` are both > 0. If `words` is 0 the text did not
   land; fix it before you submit. The submission check will reject an empty deck.
"""


def _build_task_prompt(
    query: str,
    expects: str,
    dep_files: list[str],
    skill_text: str | None = None,
) -> str:
    dep_section = "\n".join(f" - {p}" for p in dep_files) if dep_files else "none"
    ext = _detect_office_ext(expects)
    recipe_section = _PPTX_RECIPE if ext == ".pptx" else ""
    skill_section = (
        f"\nLOADED OFFICECLI SKILL (design + command conventions — follow it):\n{skill_text}\n"
        if skill_text
        else ""
    )
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
{recipe_section}{skill_section}
WHEN DONE:
Return the terminal `submit` output with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, surprises). Empty string if nothing.
    Do NOT recap what you did and do NOT claim to have used a tool you did
    not actually call — the planner can see the produced files.

Do not submit until you have written all expected files and verified each
Office file is non-empty with `officecli view <file> stats`.
"""


async def run_office_executor(
    workspace: Path,
    query: str,
    expects: str,
    dep_files: list[str],
    sink: EventSink = EventSink(),
) -> ExecutorResult:
    """Pattern-B entrypoint: build per-run deps, run the module-level agent,
    return the agent's validated structured output.

    Plugs into dispatch_executor_agent in main.py for the 'office' branch."""
    deps = OfficeDeps(workspace=workspace.resolve(), sink=sink.child(agent_type="office"))

    # Force-load the matching OfficeCLI skill and inject it into the prompt. The
    # system prompt tells the agent to `load_skill` first, but in practice it
    # often skips that step and then guesses wrong command forms (a major cause
    # of malformed/empty artifacts). Loading it here makes the verified design +
    # command conventions unconditionally present in the agent's context.
    ext = _detect_office_ext(expects)
    skill_text = _load_skill_text(_SKILL_FOR_EXT[ext]) if ext else None
    task_prompt = _build_task_prompt(query, expects, dep_files, skill_text=skill_text)

    # Multi-slide decks need many tool calls; the pydantic-ai default request
    # limit of 50 kills large builds mid-way. Use the configured office budget.
    usage_limits = UsageLimits(
        request_limit=get_settings().agent_office_request_limit
    )

    try:
        run_result = await theOfficeAgent.run(
            user_prompt=task_prompt, deps=deps, usage_limits=usage_limits
        )
    except Exception as e:
        error = f"Agent loop failed: {type(e).__name__}: {e}"
        # The control loop surfaces only a short, user-safe "Office agent failed"
        # message to the UI, and this branch (unlike the browser agent) doesn't
        # publish the error itself — so without this log the real reason (model
        # error, officecli crash, request-limit exhaustion, timeout) would be
        # invisible server-side. Log it to Logfire so it's captured/searchable.
        logfire.error(
            "Office agent loop failed",
            query=query,
            expects=expects,
            error_class=type(e).__name__,
            error=error,
        )
        return ExecutorResult(
            produced=[],
            notes="",
            error=error,
        )

    submission = run_result.output
    await _publish_submission(deps, submission)
    return ExecutorResult(produced=submission.produced, notes=submission.notes)
