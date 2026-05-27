from browser_use import Agent, Tools, Browser, ChatOpenAI
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from pydantic import BaseModel
from openai import AsyncOpenAI
from orchestrator import OPENAI_BASE_URL, OPENAI_KEY


@dataclass
class QwenChatOpenAI(ChatOpenAI):
    """ChatOpenAI variant that injects extra_body into every chat.completions
    call. DashScope's Qwen3 models default to thinking-mode-on, which leaves
    the OpenAI-compatible `content` field empty (the answer lands in
    `reasoning_content` instead). browser-use only reads `content` and fails
    to parse `""` as JSON. browser-use's ChatOpenAI doesn't forward
    extra_body, so we patch it onto the OpenAI client here.
    """
    enable_thinking: bool = False

    def get_client(self) -> AsyncOpenAI:
        client = super().get_client()
        original = client.chat.completions.create
        enable_thinking = self.enable_thinking

        @wraps(original)
        async def patched(*args, **kwargs):
            extra = dict(kwargs.get("extra_body") or {})
            extra.setdefault("enable_thinking", enable_thinking)
            kwargs["extra_body"] = extra
            return await original(*args, **kwargs)

        client.chat.completions.create = patched
        return client


class ExecutorResult(BaseModel):
    produced: list[str]  # relative paths to workspace
    notes: str
    error: str | None = None


class BrowserExecutor:
    """wraps browser_use Agent for use as a sub-agent in the planner system"""

    def __init__(
        self,
        workspace: Path,
        model: str,
        max_steps: int = 30,
        headless: bool = True,
        use_cloud: bool = False,
        max_failures: int = 8,
    ):
        self.workspace = workspace.resolve()
        self.model = model
        self.max_steps = max_steps
        self.headless = headless
        self.use_cloud = use_cloud
        self.max_failures = max_failures

        # Mutable state captured by the submit tool
        self._submitted: ExecutorResult | None = None

    async def run(
        self,
        query: str,
        expects: str,
        dep_files: list[str],  # relative to workspace
    ) -> ExecutorResult:
        self._submitted = None

        task_prompt = self._build_task_prompt(query, expects, dep_files)
        tools = self._build_tools()
        browser = Browser(
            headless=self.headless,
            downloads_path=str(self.workspace / "outputs"),
            cross_origin_iframes=False,   # maintainers' OWN docstring: avoids "hanging"
            max_iframes=10,               # default 100
            max_iframe_depth=3,           # default 5
            paint_order_filtering=False,  # cuts serialization CPU cost
        )

        browser_llm = QwenChatOpenAI(
            model=self.model,
            base_url=OPENAI_BASE_URL,
            api_key=OPENAI_KEY,
        )

        browser_agent = Agent(
            task=task_prompt,
            llm=browser_llm,
            tools=tools,
            use_cloud=self.use_cloud,
            browser=browser,
            max_failures=self.max_failures
        )

        history = None
        try:
            # max_steps is a run() arg in browser-use, NOT a constructor arg.
            # Passing it to Agent(...) is silently ignored and run() falls back
            # to its default of 500. Pass it here so the cap is actually enforced.
            history = await browser_agent.run(max_steps=self.max_steps)
        except Exception as e:
            return ExecutorResult(
                produced=[],
                notes="",
                error=f"Agent loop failed: {type(e).__name__}: {e}",
            )
        finally:
            await browser.kill()

        if self._submitted is None:
            # The agent ended (called done, exhausted steps, or stopped) without
            # calling our submit tool. Surface its OWN final result and errors so
            # the planner can see WHY it failed and reroute on replan, instead of
            # a generic "no submit" string.
            return ExecutorResult(
                produced=[],
                notes="",
                error=self._failure_detail(history),
            )
        return self._submitted

    @staticmethod
    def _failure_detail(history) -> str:
        """Build a planner-readable failure string from the browser-use run
        history. The agent's final_result() holds its own account of what
        happened (e.g. 'screener.in search unresponsive, no alternatives tried'),
        which is exactly what the planner needs to make an informed rewrite."""
        base = "Agent ended without calling submit."
        if history is None:
            return base
        parts = [base]
        try:
            final = history.final_result()
            if final:
                parts.append(f"Final result: {final}")
        except Exception:
            pass
        try:
            errs = history.errors()
            # errors() returns one entry per step, mostly None; keep the real ones.
            real = [e for e in errs if e] if errs else []
            if real:
                parts.append(f"Step errors: {real[-3:]}")  # last few are most relevant
        except Exception:
            pass
        return " ".join(parts)

    def _build_task_prompt(
        self,
        query: str,
        expects: str,
        dep_files: list[str],
    ) -> str:
        dep_section = (
            "\n".join(f" -{p}" for p in dep_files) if dep_files else "none"
        )

        return f"""You are a browser-using sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS (read these as needed using read_file):
{dep_section}

WORKSPACE:
All paths are relative to your workspace root.
Write your outputs under outputs/.
Use the write_file tool to save findings — do NOT just return text in your final message.

IF YOU ARE BLOCKED:
Do NOT attempt to solve CAPTCHAs, "I'm not a robot" checks, or bot-detection
challenges — you cannot solve them and trying wastes the entire run. Do not click
CAPTCHA images or checkboxes. If a page blocks you with a CAPTCHA, consent wall,
or login wall, leave it immediately and try a different source. Prefer primary
and official sources (company investor-relations pages, regulatory filings,
reputable data providers) over Google search, which frequently challenges
automated browsers. If after one or two alternative sources you still cannot get
the required data, stop and submit: write whatever partial findings you have to
the expected output file, clearly marking what is missing and that you were
blocked, and put the reason in your submit notes (e.g. "Google hit a reCAPTCHA;
retrieved figures from screener.in instead" or "all sources blocked, no data").

WHEN DONE:
Call the submit tool with:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, surprises). Empty string if nothing.

Do not call submit until you have written all expected files.
"""

    def _build_tools(self) -> Tools:
        tools = Tools()
        workspace = self.workspace

        @tools.action(
            description=(
                "Read a file from the workspace. Pass a path relative to the workspace root, e.g. 'outputs/t1_sources.md'."
            )
        )
        def read_file(path: str) -> str:
            absolute_path = (workspace / path).resolve()
            if not str(absolute_path).startswith(str(workspace)):
                return f"ERROR: path {path} is outside workspace"
            if not absolute_path.exists():
                return f"ERROR: file {path} does not exist"
            try:
                return absolute_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return f"ERROR: file {path} is not text; use a different tool"

        @tools.action(
            description=(
                "Write content to a file in the workspace. Pass a path relative "
                "to the workspace root, e.g. 'outputs/t1_sources.md'. Overwrites "
                "if the file exists."
            )
        )
        def write_file(path: str, content: str) -> str:
            absolute_path = (workspace / path).resolve()
            if not str(absolute_path).startswith(str(workspace)):
                return f"ERROR: path {path} is outside workspace"
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_text(content, encoding="utf-8")
            return f"Wrote {absolute_path.stat().st_size} bytes to {path}"

        @tools.action(
            description=(
                "Submit your final result. Call this exactly once, after all expected files have been written. Ends your turn."
            )
        )
        def submit(produced: list[str], notes: str) -> str:
            normalized = []
            for path in produced:
                path_produced = Path(path)
                if path_produced.is_absolute():
                    try:
                        path_produced = path_produced.relative_to(workspace)
                    except ValueError:
                        return f"ERROR: path {path} is outside workspace; use relative paths"
                absolute_path = workspace / path_produced
                if not absolute_path.exists():
                    return f"ERROR: claimed file {path} does not exist"
                if absolute_path.stat().st_size == 0:
                    return f"ERROR: claimed file {path} is empty"
                normalized.append(str(path_produced))

            self._submitted = ExecutorResult(produced=normalized, notes=notes)
            return "Result submitted successfully you may stop."

        return tools