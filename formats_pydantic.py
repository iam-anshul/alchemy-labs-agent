from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime

class TaskSpec(BaseModel):
    id: str
    title: str
    agent: Literal["browser", "office", "document_answering"]
    deps: list[TaskSpec] = Field(default_factory=list)
    query: str
    expects: str
    produced: list[str] = Field(default_factory=list)
    status: Literal["pending", "dispatched", "completed", "failed"] = "pending"
    notes: str = Field(
        default="",
        description="filled in by the control loop from the executor's submit notes. It isfree-form text the executor wanted the planner to see"
        )

class ProducedFile(BaseModel):
    path: str       # relative path under the workspace, e.g. "outputs/t1_sources.md"
    content: str    # the contents to write to that path

class BrowserTaskOutput(BaseModel):
    produced: list[ProducedFile] = Field(default_factory=list)

class PlanOutput(BaseModel):
    tasks: list[TaskSpec]

    notes: str | None = Field(
        default=None,
        description="Optional free-form notes the planner appends when replanning, explaining what changed and why." \
        "Notes is the planner's scratchpad. It's free-form text the planner writes to itself across calls, explaining decisions."
    )

class PlanRevision(BaseModel):
    decision: Literal["continue", "revise"]
    revised_tasks: list[TaskSpec] = []   # only populated if decision == "revise"
    notes_to_append: str = ""             # appended to the Notes section

class Run(BaseModel):
    user_query: str        # the raw user input that kicked off the run
    goal: str              # the distilled intent the planner reasons about
    workspace: str
    started_at: datetime
    replans_used: int = 0
    replan_budget: int = 3  # max number of times the planner may revise the plan
    plan: PlanOutput | None = None


