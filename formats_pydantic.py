from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime
from uuid import UUID

class TaskSpec(BaseModel):
    id: str
    title: str
    agent: Literal["browser", "office", "document_answering"]
    doc_deps: InternalDocAgentDeps | None = Field(default=None, description="only when agent agent type is document_answering else this needs to be None")
    deps: list[str] = Field(default_factory=list) # refrences by id
    query: str
    expects: str
    produced: list[str] = Field(default_factory=list)
    status: Literal["pending", "dispatched", "completed", "failed"] = "pending"
    notes: str = ""
    error: str = ""
    human_in_the_loop: bool = Field(
        default=False,
        description="This specifies whether to ask the user back after the sub agent has done its job or not. This is to be set by the planner agent" \
        "If this is True then after the sub agent run user will be prompted for their feedback or is useful if the user wants to change anything." \
        "This can also be used to ask user for a permission for a specific thing."
        )

class InternalDocAgentDeps(BaseModel):
    doc_answering_mode: Literal["ASK", "REPORT"] | None = Field(default=None, description="ASK=focused Q&A, REPORT=multi-section narrative report")
    doc_ids: list[str] | None = Field(default=None, description="RESOLVED doc_ids (NOT filenames). When the user names specific documents, call the fetch_doc_ids tool to resolve each name to its doc_id and put the returned ids here. Leave None for a general question over all documents in the workspace. Never put a raw filename or title here.")
    report_id: str | None = Field(default=None, description="RESOLVED report_id (NOT a report name). Only for doc_answering_mode='REPORT' when the user refers to an existing report by name: call the fetch_report_ids tool to resolve the name to its report_id and put it here. None to draft a fresh report.")
    target_length: Literal["brief", "standard", "deep"] | None = Field(default="standard", description="This is the target length of the report, if the user mentions anything that can be specified to the length of the report, use these field accordingly to it. This shold be filled only when doc_answering mode is 'REPORT' else set this 'None', if the doc_answering mode is 'REPORT' and the user has't specified any length keep the length 'standard'.")

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

class ChatAcceptedResponse(BaseModel):
    query_id: UUID
    stream_url: str
