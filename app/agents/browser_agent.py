import logfire
from browser_use import Agent, Tools, Browser, ChatOpenAI
from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.openai.serializer import OpenAIMessageSerializer
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any
import asyncio
import base64
import json
import mimetypes
from pydantic import BaseModel
from openai import AsyncOpenAI, APIConnectionError, RateLimitError
from app.api.events import EventSink, file_artifact
from app.agents.orchestrator import OPENAI_BASE_URL, OPENAI_KEY

try:
    from browser_use.browser.events import ScreenshotEvent
except Exception:  # pragma: no cover - depends on browser-use version
    ScreenshotEvent = None


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


@dataclass
class QwenToolCallChatOpenAI(QwenChatOpenAI):
    """OpenAI-compatible client that uses native tool calling for structured
    output. Modeled directly after browser-use's own Groq client
    (`llm/groq/chat.py::_invoke_with_tool_calling`), which routes Kimi and
    similar tool-call-capable Groq-hosted models through one tool +
    `tool_choice="required"`.

    Why this exists: response_format=json_schema is a *hint* on most non-OpenAI
    providers — they don't decode-enforce it, so weaker models produce
    truncated or wrong-shape JSON. The `tools=` parameter is a separate,
    older, more-widely-enforced protocol. On a model that actually supports
    tool calling on this provider (Qwen-Plus, Qwen-Max, Qwen3 — NOT
    qwen-vl-max, which silently ignores `tools=`), this gives
    schema-validated structured output via the same channel real tool calls
    use.

    For output_format=None (unstructured nudges), defers to the parent.
    """

    async def ainvoke(self, messages, output_format=None, **kwargs):
        if output_format is None:
            return await super().ainvoke(messages, output_format=None, **kwargs)

        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        schema = SchemaOptimizer.create_optimized_json_schema(
            output_format,
            remove_min_items=self.remove_min_items_from_schema,
            remove_defaults=self.remove_defaults_from_schema,
        )
        tool = {
            "type": "function",
            "function": {
                "name": output_format.__name__,
                "description": f"Extract information in the format of {output_format.__name__}",
                "parameters": schema,
            },
        }

        # Mirror the parent's model_params build so we don't drop temperature/etc.
        model_params: dict[str, Any] = {}
        if self.temperature is not None:           model_params["temperature"] = self.temperature
        if self.frequency_penalty is not None:     model_params["frequency_penalty"] = self.frequency_penalty
        if self.max_completion_tokens is not None: model_params["max_completion_tokens"] = self.max_completion_tokens
        if self.top_p is not None:                 model_params["top_p"] = self.top_p
        if self.seed is not None:                  model_params["seed"] = self.seed
        if self.service_tier is not None:          model_params["service_tier"] = self.service_tier

        try:
            response = await self.get_client().chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=[tool],
                tool_choice="required",
                **model_params,
            )
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIConnectionError as e:
            raise ModelProviderError(message=str(e), model=self.name) from e

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise ModelProviderError(message="Empty `choices`.", status_code=502, model=self.name)

        # Prefer tool_calls; fall back to content (Groq's client does the same —
        # some compat shims put the JSON in content even when tool_choice="required").
        args_json: str | None = None
        if choice.message.tool_calls:
            args_json = choice.message.tool_calls[0].function.arguments
        elif choice.message.content:
            args_json = choice.message.content

        if not args_json:
            raise ModelProviderError(
                message=f"Expected tool_call from {self.model}, got none.",
                status_code=500, model=self.name,
            )

        # Qwen models emit list/dict fields as JSON-encoded strings sometimes —
        # e.g. action='\n[{"done": ...}]\n' or plan_update='["a","b"]'. Decode
        # those before Pydantic sees them; anything that won't parse is left
        # for Pydantic to error on as usual.
        parsed = self._unstringify_collections(json.loads(args_json), output_format)

        # Qwen thinking-mode models sometimes dump <think>...</think> into the
        # optional `thinking` field and never populate the required ones, so
        # the tool call comes back with most of AgentOutput missing. Pad any
        # missing required field with a type-appropriate default so the step
        # validates and the loop survives the partial response; the agent will
        # try again next turn, and existing loop detection / retry / replan
        # layers handle persistent failure.
        parsed = self._pad_missing_required(parsed, output_format)

        return ChatInvokeCompletion(
            completion=output_format.model_validate(parsed),
            usage=self._get_usage(response),
            stop_reason=choice.finish_reason,
        )

    @staticmethod
    def _pad_missing_required(d: dict, output_format: type) -> dict:
        """For each required property absent from `d`, inject a default
        appropriate to its declared type: [] for arrays, {} for objects, ''
        for everything else (covers string and string|null unions)."""
        schema = output_format.model_json_schema()
        required = schema.get("required", [])
        props = schema.get("properties", {})
        for k in required:
            if k in d:
                continue
            fschema = props.get(k, {})
            types = {fschema.get("type"), *(b.get("type") for b in fschema.get("anyOf", []))}
            if "array" in types:
                d[k] = []
            elif "object" in types:
                d[k] = {}
            else:
                d[k] = ""
        return d

    @staticmethod
    def _unstringify_collections(d: dict, output_format: type) -> dict:
        """For each property whose schema allows array/object:
          - JSON-encoded string ('["a","b"]') → json.loads it
          - empty string ('')                 → drop the key (let the field's
                                                default — usually None — apply)
        Other values are left alone; Pydantic surfaces its own error."""
        props = output_format.model_json_schema().get("properties", {})
        for k in list(d.keys()):
            v = d[k]
            if not isinstance(v, str):
                continue
            schema = props.get(k, {})
            types = {schema.get("type"), *(b.get("type") for b in schema.get("anyOf", []))}
            if "array" not in types and "object" not in types:
                continue
            vs = v.strip()
            if not vs:
                d.pop(k)
                continue
            if vs[0] not in ("[", "{"):
                continue
            try:
                d[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                pass
        return d


class ExecutorResult(BaseModel):
    produced: list[str]  # relative paths to workspace
    notes: str
    error: str | None = None


class BrowserSubmission(BaseModel):
    produced: list[str]
    notes: str


class BrowserExecutor:
    """wraps browser_use Agent for use as a sub-agent in the planner system"""

    def __init__(
        self,
        workspace: Path,
        model: str,
        max_steps: int = 50,
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

    async def run(
        self,
        query: str,
        expects: str,
        dep_files: list[str],  # relative to workspace
        sink: EventSink = EventSink(),
        task_id: str | None = None,
        attempt: int | None = None,
    ) -> ExecutorResult:
        sink = sink.child(task_id=task_id, agent_type="browser", attempt=attempt)

        task_prompt = self._build_task_prompt(query, expects, dep_files)
        tools = self._build_tools(sink=sink)
        before_outputs = self._snapshot_outputs()
        await sink.publish_ui(
            "agent_started",
            stage="browsing",
            status="started",
            message="Browser agent started",
            data={
                "query": query,
                "expects": expects,
                "dep_files": dep_files,
                "headless": self.headless,
                "max_steps": self.max_steps,
                "max_failures": self.max_failures,
            },
        )
        browser = Browser(
            proxy={
                "server": "http://38.154.203.95:5863",
                "username": "kfaxvgga",
                "password": "3immlbucd1ys",
            },
            headless=self.headless,
            downloads_path=str(self.workspace / "outputs"),
            cross_origin_iframes=False,   # maintainers' OWN docstring: avoids "hanging"
            max_iframes=10,               # default 100
            max_iframe_depth=3,           # default 5
            paint_order_filtering=False,  # cuts serialization CPU cost
        )

        browser_llm = QwenToolCallChatOpenAI(
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
            max_failures=self.max_failures,
            flash_mode=True,
        )

        history = None
        try:
            # max_steps is a run() arg in browser-use, NOT a constructor arg.
            # Passing it to Agent(...) is silently ignored and run() falls back
            # to its default of 500. Pass it here so the cap is actually enforced.
            # browser-use isn't pydantic-ai, so it gets no auto-instrumentation;
            # this explicit span makes the browser agent visible in Logfire (its
            # LLM HTTP calls are still captured by instrument_httpx).
            with logfire.span(
                "browser_agent run",
                task_id=task_id,
                attempt=attempt,
                query=query,
                max_steps=self.max_steps,
            ):
                history = await browser_agent.run(
                    max_steps=self.max_steps,
                    on_step_start=self._make_step_hook(sink, "started"),
                    on_step_end=self._make_step_hook(sink, "progress"),
                )
        except Exception as e:
            error = self._format_run_exception(e)
            # Same rationale as the no-submission branch below: surface the full
            # error server-side in Logfire, since the UI message stays generic.
            logfire.error(
                "Browser agent run loop failed",
                task_id=task_id,
                attempt=attempt,
                query=query,
                error_class=type(e).__name__,
                error=error,
            )
            await sink.publish_ui(
                "agent_ended",
                stage="browsing",
                status="failed",
                message="Browser agent failed",
                data={"error_class": type(e).__name__, "error": error},
            )
            return ExecutorResult(
                produced=[],
                notes="",
                error=error,
            )
        finally:
            await browser.kill()

        try:
            final = history.final_result() if history is not None else None
            if not final:
                raise ValueError("browser agent produced no structured final result")
            submission = (
                BrowserSubmission.model_validate_json(final)
                if isinstance(final, str)
                else BrowserSubmission.model_validate(final)
            )
            result = self._validate_submission(submission)
            if result.error:
                raise ValueError(result.error)
        except Exception as e:
            detail = self._failure_detail(history)
            if str(e) and str(e) not in detail:
                detail = f"{detail} Validation: {e}"
            result = ExecutorResult(
                produced=[],
                notes="",
                error=detail,
            )
            # The UI 'message' is intentionally short/user-safe (per the event
            # streaming contract), so the full diagnostic — final_result, step
            # errors, validation message — would otherwise only live in the SSE
            # data payload. Log it to Logfire so the real reason the browser
            # agent failed to submit is captured server-side and searchable,
            # not just shown as the generic "ended without submitting" line.
            logfire.error(
                "Browser agent ended without submitting",
                task_id=task_id,
                attempt=attempt,
                query=query,
                error_class=type(e).__name__,
                failure_detail=detail,
            )
            await sink.publish_ui(
                "agent_ended",
                stage="browsing",
                status="failed",
                message="Browser agent ended without submitting",
                data={"error": result.error},
            )
            return result
        await self._emit_new_outputs(sink, before_outputs)
        await sink.publish_ui(
            "agent_ended",
            stage="browsing",
            status="completed",
            message="Browser agent completed",
            data={"produced": result.produced, "notes": result.notes},
        )
        return result

    def _validate_submission(self, submission: BrowserSubmission) -> ExecutorResult:
        normalized = []
        for path in submission.produced:
            produced_path = Path(path)
            if produced_path.is_absolute():
                try:
                    produced_path = produced_path.relative_to(self.workspace)
                except ValueError:
                    return ExecutorResult(
                        produced=[],
                        notes="",
                        error=f"path {path} is outside workspace; use relative paths",
                    )
            absolute_path = self.workspace / produced_path
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
            normalized.append(str(produced_path))
        return ExecutorResult(produced=normalized, notes=submission.notes)

    def _snapshot_outputs(self) -> set[str]:
        outputs = self.workspace / "outputs"
        if not outputs.exists():
            return set()
        return {
            str(p.relative_to(self.workspace))
            for p in outputs.rglob("*")
            if p.is_file()
        }

    async def _emit_new_outputs(self, sink: EventSink, before: set[str]) -> None:
        outputs = self.workspace / "outputs"
        if not outputs.exists():
            return
        for p in outputs.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.workspace))
            if rel in before:
                continue
            mime, _ = mimetypes.guess_type(str(p))
            suffix = p.suffix.lower()
            # Inline text content (md/txt/csv/json) so the UI can preview it with
            # no URL — same as the write_file tool. Without this, a saved .md
            # would render the "No preview URL is available" fallback. Binary
            # files carry no content and preview via the download route once
            # the run is persisted.
            content = None
            if suffix in {".md", ".txt", ".csv", ".json"}:
                try:
                    content = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    content = None
            await sink.publish_ui(
                "artifact_ready",
                stage="download",
                status="progress",
                message=f"Browser saved {p.name}",
                artifacts=[
                    file_artifact(
                        kind="markdown" if suffix == ".md" else "file",
                        path=rel,
                        filename=p.name,
                        type=suffix.lstrip(".") or None,
                        mime_type=mime,
                        bytes=p.stat().st_size,
                        content=content,
                    )
                ],
            )

    def _make_step_hook(self, sink: EventSink, status: str):
        async def hook(agent: Agent) -> None:
            step = self._history_len(agent)
            data: dict[str, Any] = {"step": step}
            try:
                data["url"] = await agent.browser_session.get_current_page_url()
            except Exception:
                try:
                    page = await agent.browser_session.get_current_page()
                    data["url"] = getattr(page, "url", None)
                except Exception:
                    pass
            try:
                data["title"] = await agent.browser_session.get_current_page_title()
            except Exception:
                try:
                    page = await agent.browser_session.get_current_page()
                    title = page.title()
                    data["title"] = await title if hasattr(title, "__await__") else title
                except Exception:
                    pass
            await sink.publish_ui(
                "agent_progress",
                stage="browsing",
                status="progress",
                message="Browser is working",
                data=data,
            )
            if status == "progress":
                screenshot = await self._capture_screenshot(agent, step, data)
                if screenshot is not None:
                    await sink.publish_ui(
                        "artifact_ready",
                        stage="screenshot",
                        status="progress",
                        message="Browser screenshot captured",
                        artifacts=[screenshot],
                    )

        return hook

    @staticmethod
    def _history_len(agent: Agent) -> int:
        try:
            return len(agent.history.urls())
        except Exception:
            return 0

    async def _capture_screenshot(
        self,
        agent: Agent,
        step: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            result = await agent.browser_session.take_screenshot(full_page=False)
        except Exception:
            if ScreenshotEvent is None:
                return None
            try:
                event = agent.browser_session.event_bus.dispatch(ScreenshotEvent(full_page=False))
                await event
                result = await event.event_result(raise_if_any=True, raise_if_none=True)
            except Exception:
                return None

        encoded = self._extract_screenshot_base64(result)
        if not encoded:
            return None

        rel_path = f"outputs/browser_events/step_{step:04d}.png"
        path = self.workspace / rel_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded))
            size = path.stat().st_size
        except Exception:
            size = None

        return file_artifact(
            kind="screenshot",
            path=rel_path,
            filename=f"step_{step:04d}.png",
            type="png",
            mime_type="image/png",
            bytes=size,
            content_base64=encoded,
            metadata=metadata,
        )

    @staticmethod
    def _extract_screenshot_base64(result: Any) -> str | None:
        if isinstance(result, bytes):
            return base64.b64encode(result).decode("ascii")
        if isinstance(result, str):
            return result.removeprefix("data:image/png;base64,")
        for attr in ("screenshot", "base64", "content_base64", "data"):
            value = getattr(result, attr, None)
            if isinstance(value, bytes):
                return base64.b64encode(value).decode("ascii")
            if isinstance(value, str):
                return value.removeprefix("data:image/png;base64,")
        return None

    @staticmethod
    def _format_run_exception(e: Exception) -> str:
        if isinstance(e, FileNotFoundError):
            return (
                "Browser launch failed because no local browser binary was available "
                "and browser-use could not run Playwright's browser installer. "
                "Install Chromium in the runtime image, e.g. `python -m playwright "
                "install --with-deps chromium`. Original error: "
                f"{type(e).__name__}: {e}"
            )
        return f"Agent loop failed: {type(e).__name__}: {e}"

    @staticmethod
    def _failure_detail(history) -> str:
        """Build a planner-readable failure string from the browser-use run
        history. The agent's final_result() holds its own account of what
        happened (e.g. 'screener.in search unresponsive, no alternatives tried'),
        which is exactly what the planner needs to make an informed rewrite."""
        base = "Agent ended without a valid structured output."
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
Call the terminal `done` action with its structured output:
  - produced: list of relative paths you wrote
  - notes: 1-2 sentences flagging anything the planner should know
    (judgment calls, data limitations, surprises). Empty string if nothing.

Do not finish until you have written all expected files.
"""

    def _build_tools(self, sink: EventSink = EventSink()) -> Tools:
        tools = Tools(output_model=BrowserSubmission)
        workspace = self.workspace

        def emit_artifact(stage: str, message: str, artifact: dict[str, Any]) -> None:
            try:
                asyncio.create_task(
                    sink.publish_ui(
                        "artifact_ready",
                        stage=stage,
                        status="progress",
                        message=message,
                        artifacts=[artifact],
                    )
                )
            except RuntimeError:
                pass

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
                content = absolute_path.read_text(encoding="utf-8")
                return content
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
            rel = str(absolute_path.relative_to(workspace))
            emit_artifact(
                "writing_file",
                f"Browser wrote {Path(path).name}",
                file_artifact(
                    kind="markdown" if absolute_path.suffix.lower() == ".md" else "file",
                    path=rel,
                    filename=absolute_path.name,
                    type=absolute_path.suffix.lstrip(".").lower() or None,
                    mime_type=mimetypes.guess_type(str(absolute_path))[0],
                    bytes=absolute_path.stat().st_size,
                    content=content if absolute_path.suffix.lower() in {".md", ".txt", ".csv", ".json"} else None,
                ),
            )
            return f"Wrote {absolute_path.stat().st_size} bytes to {path}"

        return tools
