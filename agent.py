from pydantic_ai import Agent, RunContext
from dotenv import load_dotenv
from formats_pydantic import PlanOutput
import os
from planner_agent_system_prompt import planner_system_prompt
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
#from source.models import agentDeps

load_dotenv()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

model = OpenAIChatModel("qwen3.6-plus", provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_KEY))


planner_agent = Agent(
    model,
    system_prompt=planner_system_prompt,
    retries=3,
    output_type=PlanOutput,
    model_settings=OpenAIChatModelSettings(extra_body={"enable_thinking": False}),
)

# @kubernetesAgent_OPENAI.tool()
# def run_command_openai(ctx: RunContext[agentDeps], command: str) -> str:
#     "Use this function to run all the commands especially kubectl commands"
#     return run_kubectl_command(command, ctx.deps.context_name, ctx.deps.kubeconfig_path, ctx.deps.access_key_id, ctx.deps.access_key)














