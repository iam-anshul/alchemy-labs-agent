import os
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext, ToolOutput
from formats_pydantic import PlanOutput, ReplanDecision
from system_prompts import planner_system_prompt
from pydantic_ai.models.openai import OpenAIChatModelSettings
from qwen_compat import QwenChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from dataclasses import dataclass
from db.utils import get_docID_by_name, get_reportID_by_name
from db import SessionLocal
#from source.models import agentDeps

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

model = QwenChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

@dataclass
class PlannerDeps:
    workspace_name: str

# Initial planning: produce the full plan from the user's goal.
plannerAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=ToolOutput(PlanOutput, name="submit_plan"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)

# Replanning: decide whether the in-flight plan needs revision, and only then
# emit a full revised plan. Separate agent because its output_type differs
# (ReplanDecision, not PlanOutput); it shares the same system prompt so the
# planning rules are identical. Returning needs_change=false on a no-op replan
# means the model never regenerates the task list unnecessarily — which is what
# previously let it occasionally collapse the plan to zero tasks.
replanAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=ToolOutput(ReplanDecision, name="submit_replan_decision"),
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)


def _register_lookup_tools(agent: Agent) -> None:
    """Both planning agents need the same name→id lookup tools."""

    @agent.tool(retries=1)
    def fetch_doc_ids(ctx: RunContext[PlannerDeps], doc_name: str) -> list[str]:
        with SessionLocal() as db:
            return get_docID_by_name(db, ctx.deps.workspace_name, doc_name)

    @agent.tool(retries=1)
    def fetch_report_ids(ctx: RunContext[PlannerDeps], report_name: str) -> list[str]:
        with SessionLocal() as db:
            return get_reportID_by_name(db, ctx.deps.workspace_name, report_name)


_register_lookup_tools(plannerAgent)
_register_lookup_tools(replanAgent)
