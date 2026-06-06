from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime
from uuid import UUID

class TaskSpec(BaseModel):
    id: str
    title: str
    agent: Literal["browser", "office", "document_answering"]
    doc_answering_mode: Literal["ASK", "REPORT"] | None = None # only for agent=document_answering, required in that case. ASK=focused Q&A, REPORT=multi-section narrative report 
    deps: list[str] = Field(default_factory=list) # refrences by id
    query: str
    expects: str
    produced: list[str] = Field(default_factory=list)
    status: Literal["pending", "dispatched", "completed", "failed"] = "pending"
    notes: str = ""
    error: str = ""

class PlanOutput(BaseModel):
    goal: str = Field(default="", description="The distilled intent the planner reasons about. This is what the planner tries to achieve through its tasks.")
    tasks: list[TaskSpec]

    notes: str | None = Field(
        default=None,
        description="Optional free-form notes the planner appends when replanning, explaining what changed and why." \
        "Notes is the planner's scratchpad. It's free-form text the planner writes to itself across calls, explaining decisions."
    )

class QueryRun(BaseModel): # the name should be changed to QueryRun in the upcoming version
    user_query: str        # the raw user input that kicked off the run
    goal: str | None = None             # the distilled intent the planner reasons about
    workspace: str         # this is the absolute path to the subdir of a workspace
    started_at: datetime
    replans_used: int = 0
    replan_budget: int = 3  # max number of times the planner may revise the plan
    plan: PlanOutput | None = None
    # Persistent identity for chat-style state. workspace_id groups multiple
    # runs into one conversation; run_id identifies this single turn. Both
    # optional so non-chat callers (older tests, ad-hoc scripts) still work.
    workspace_id: str # the name of the workspace created by the user using create workspace API this is also the name of the workspace in file system under which there are multiple sub directories with the name of query id prefixed bu query counter
    query_id: UUID | None = None # this will be changed to query_id in the upcoming version
    user_id: UUID
    status: Literal["running", "completed", "failed"]
    query_counter: int = 1

