from browser_use import Agent, Tools, Browser, ChatOpenAI
from pathlib import Path
from typing import Literal
from pydantic import BaseModel
from planner_agent import OPENAI_BASE_URL, OPENAI_KEY

class ExecutorResult(BaseModel):
    produced: list[str] # relative paths to workspace
    notes: str
    error: str | None = None

class BrowserExecutor(BaseModel):
    """wraps browser use. Agent for use as a sub-agent in the planner system"""

    def __innit__(
            self,
            workspace: Path,
            model: str,
            max_steps: int = 30,
            headless: bool = True,
            use_cloud: bool = False
    ):
        self.workspace = workspace.resolve()
        self.model = model
        self.max_steps = max_steps,
        self.headless = headless,
        self.use_cloud = use_cloud

        # Mutable state captured by the submit tool
        self._submitted: ExecutorResult | None = None

        async def run(
                self,
                query: str,
                expects: str,
                dep_files: list[str] # relative to workspace
        ) -> ExecutorResult:
            self._submitted = None

            task_prompt = self._build_task_prompt(query, expects, dep_files)
            tools = self._build_tools()
            browser = Browser(
                headless=self.headless,
                model=self.model,
                cloud=self.use_cloud,
                downloads_path=str(self.workspace / "outputs")
            )

            browser_llm = ChatOpenAI(
                model=self.model,
                base_url=OPENAI_BASE_URL,
                api_key=OPENAI_KEY,
            )

            browser_agent = Agent(
                task=task_prompt,
                llm=browser_llm,
                tools=tools,
                max_steps=self.max_steps
            )

            try:
                await browser_agent.run()
            except Exception as e:
                return ExecutorResult(produced=[], notes="", error=f"Agent loop failed: {type(e).__name__}: {e}")
            finally:
                await browser.close()

            if self._submitted is None:
                # Agent finished without calling submit — treat as failure
                return ExecutorResult(
                    produced=[],
                    notes="",
                    error="Agent exhausted steps without calling submit",
                )
            return self._submitted

        def _build_task_prompt(
                self,
                query: str,
                expects: str,
                dep_files: list[str]
        ) -> str:
            dep_section = (
                "\n".join(f" -{p}" for p in dep_files)
                if dep_files else "none"
            )

            return f"""You are a browser-using sub-agent. Complete the task below and submit your result.

QUERY:
{query}

EXPECTED OUTPUT (files you must write):
{expects}

INPUT FILES FROM UPSTREAM TASKS (read these as needed using read_file):
{dep_section}

WORKSPACE:
All paths are relative to {self.workspace}.
Write your outputs under outputs/.
Use the write_file tool to save findings — do NOT just return text in your final message.

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
                # Normalize and validate paths
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
                return f"Result submitted successfully you may stop."
            return tools

















