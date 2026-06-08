import os
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from formats_pydantic import PlanOutput
from system_prompts import planner_system_prompt
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from dataclasses import dataclass
from db.utils import get_docID_by_name, get_reportID_by_name
from db import SessionLocal
#from source.models import agentDeps

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))

@dataclass
class PlannerDeps:
    workspace_name: str

plannerAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    deps_type=PlannerDeps,
    output_type=PlanOutput,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)

@plannerAgent.tool(retries=1)
def fetch_doc_ids(ctx: RunContext[PlannerDeps], doc_name: str) -> list[str]:
    with SessionLocal() as db:
        doc_ids = get_docID_by_name(db, ctx.deps.workspace_name, doc_name)
    return doc_ids

@plannerAgent.tool(retries=1)
def fetch_report_ids(ctx: RunContext[PlannerDeps], report_name:str) -> list[str]:
    with SessionLocal() as db:
        report_ids = get_reportID_by_name(db, ctx.deps.workspace_name, report_name)
    return report_ids
