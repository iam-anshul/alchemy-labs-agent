from pydantic import BaseModel, Field, model_validator
from typing import Literal
from datetime import datetime
from uuid import UUID
from pydantic_ai.messages import ModelMessage

class InternalDocAgentDeps(BaseModel):
    doc_answering_mode: Literal["ASK", "REPORT"] | None = Field(default=None, description="ASK=focused Q&A, REPORT=multi-section narrative report")
    doc_ids: list[str] | None = Field(default=None, description="RESOLVED doc_ids (NOT filenames). When the user names specific documents, call the fetch_doc_ids tool to resolve each name to its doc_id and put the returned ids here. Leave None for a general question over all documents in the workspace. Never put a raw filename or title here.")
    report_id: str | None = Field(default=None, description="RESOLVED report_id (NOT a report name). Only for doc_answering_mode='REPORT' when the user refers to an existing report by name: call the fetch_report_ids tool to resolve the name to its report_id and put it here. None to draft a fresh report.")
    target_length: Literal["brief", "standard", "deep"] | None = Field(default="standard", description="This is the target length of the report, if the user mentions anything that can be specified to the length of the report, use these field accordingly to it. This shold be filled only when doc_answering mode is 'REPORT' else set this 'None', if the doc_answering mode is 'REPORT' and the user has't specified any length keep the length 'standard'.")

class TaskSpec(BaseModel):
    id: str
    title: str
    agent: Literal["browser", "office", "document_answering", "web_search"]
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
    query_for_human_in_the_loop: str | None = Field(default=None, description="This field is to be populated by planner if human_in_the_loop field is" \
    "'True' then planner needs to populate this field prompting the user to ask the feedback or confirmation or validation query the planner wants to ask.")
    axis_checkpoint: bool = Field(
        default=False,
        exclude=True,
        description=(
            "Internal evidence checkpoint selected by the planner. After this "
            "task completes successfully, the hidden axis reasoner examines its "
            "evidence and an append-only planner adds the next task segment."
        ),
    )
    axis_focus: str | None = Field(
        default=None,
        exclude=True,
        description=(
            "Required when axis_checkpoint=True. Describe the downstream "
            "decision that evidence may change, tentative domains to inspect, "
            "and findings that would require additional analysis."
        ),
    )

    @model_validator(mode="after")
    def validate_axis_checkpoint(self):
        if self.axis_checkpoint and not (self.axis_focus or "").strip():
            raise ValueError("axis_focus is required when axis_checkpoint=True")
        if not self.axis_checkpoint:
            self.axis_focus = None
        return self


class AxisReasoningOutput(BaseModel):
    reasoning: str = Field(
        min_length=1,
        description=(
            "A detailed evidence-grounded planning critique explaining the "
            "material reasoning axes, why they matter, plausible branches, "
            "interactions, contradictions, and information gaps. It must guide "
            "the planner without creating tasks or answering the user."
        ),
    )


class AxisPlanAddition(BaseModel):
    tasks: list[TaskSpec] = Field(
        min_length=1,
        description=(
            "Only NEW tasks to append after the completed checkpoint. Never "
            "repeat, replace, remove, or rewrite an existing task."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Short internal explanation of what the axis critique caused the "
            "planner to add. Do not mention hidden reasoning mechanics."
        ),
    )

class PlanOutput(BaseModel):
    goal: str = Field(default="", description="The distilled intent the planner reasons about. This is what the planner tries to achieve through its tasks.")
    # min_length=1: a plan with zero tasks is never valid. Without this, the
    # model can occasionally emit tasks=[] (especially when regenerating the
    # plan on a replan), which would render an empty todo.md. Requiring at least
    # one task makes pydantic-ai reject an empty plan and re-prompt the model.
    tasks: list[TaskSpec] = Field(min_length=1)
    needs_user_feedback: bool = Field(default=False, description="Populate this field with 'True' when you need to ask the user for clarification or a feedback or just want their confirmation on something. This basically is for human in the loop so 'True' if you want it else 'False'")
    feedback_question: str | None = Field(default=None, description="This is to be used only when 'needs_user_feedback is 'True' and this field is for you to ask the query, clarification, feedback or confirmation to the user. If 'needs_user_feedback' is 'False' this will be 'None'.")

    notes: str | None = Field(
        default=None,
        description="Optional free-form notes the planner appends when replanning, explaining what changed and why." \
        "Notes is the planner's scratchpad. It's free-form text the planner writes to itself across calls, explaining decisions."
    )


class ReplanDecision(BaseModel):
    """Output of a REPLAN call. The planner first decides whether the in-flight
    plan needs revision at all; only when it does does it fill in the plan
    fields. The common 'no change needed' case requires emitting just
    `needs_change=false` — the planner never regenerates (and so can never
    accidentally drop) the task list on a no-op replan.

    The plan fields are FLAT here (tasks/notes/... at the top level), NOT a
    nested PlanOutput object. This is deliberate: the model used here (Qwen via
    tool calling) tends to emit a nested object field as a JSON-encoded *string*,
    which fails validation and burns the agent's output retries until it crashes.
    A flat schema sidesteps that entirely. planner() assembles a real PlanOutput
    from these fields when needs_change is True (and validates tasks then)."""
    needs_change: bool = Field(
        description="True only if the in-flight plan must be revised in light of executor results/notes. False to leave the plan exactly as-is (the common case)."
    )
    # The fields below mirror PlanOutput but are all optional, since they are
    # only filled when needs_change is True. tasks has NO min_length here (unlike
    # PlanOutput) because the no-change case legitimately leaves it empty;
    # planner() rejects an empty task list on a needs_change=True decision.
    goal: str = Field(default="", description="When needs_change=True: the distilled goal. Ignored when needs_change=False.")
    tasks: list[TaskSpec] = Field(default_factory=list, description="When needs_change=True: the COMPLETE revised task list (every task, not a delta; reuse ids of tasks that already ran so their state carries over). Leave empty when needs_change=False.")
    needs_user_feedback: bool = Field(default=False, description="Same meaning as on the initial plan. Only relevant when needs_change=True.")
    feedback_question: str | None = Field(default=None, description="The question to ask the user, only when needs_user_feedback=True.")
    notes: str | None = Field(default=None, description="Optional free-form notes explaining what changed and why.")

class QueryRun(BaseModel): # the name should be changed to QueryRun in the upcoming version
    user_query: str        # the raw user input that kicked off the run
    goal: str | None = None             # the distilled intent the planner reasons about
    workspace: str         # this is the absolute path to the subdir of a workspace
    started_at: datetime
    replans_used: int = 0
    replan_budget: int = 3  # max number of times the planner may revise the plan
    axis_checkpoints_used: int = 0
    axis_checkpoint_budget: int = 2
    plan: PlanOutput | None = None
    # Persistent identity for chat-style state. workspace_id groups multiple
    # runs into one conversation; run_id identifies this single turn. Both
    # optional so non-chat callers (older tests, ad-hoc scripts) still work.
    workspace_id: str # the name of the workspace created by the user using create workspace API this is also the name of the workspace in file system under which there are multiple sub directories with the name of query id prefixed bu query counter
    query_id: UUID | None = None # this will be changed to query_id in the upcoming version
    user_id: UUID
    status: Literal["running", "completed", "failed"]
    query_counter: int = 1
    planner_messages: list[ModelMessage] | None = None

class ChatAcceptedResponse(BaseModel):
    query_id: UUID
    stream_url: str
