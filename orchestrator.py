import os
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from formats_pydantic import PlanOutput
from system_prompts import planner_system_prompt
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
#from source.models import agentDeps

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))


plannerAgent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    output_type=PlanOutput,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)
