from pydantic_ai import Agent, RunContext
from formats_pydantic import PlanOutput
from system_prompts import planner_system_prompt
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from main import OPENAI_BASE_URL, OPENAI_KEY, MODEL
#from source.models import agentDeps

model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))


planner_agent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    output_type=PlanOutput,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)

