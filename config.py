from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres only. Format: postgresql+psycopg://<user>:<password>@<host>:<port>/<db>
    # The default points at a local Postgres on the standard port — override via
    # .env for any real deployment.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_rag"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    llama_parse_key: str = ""

    # Tree builder
    tree_max_leaf_tokens: int = 8_000
    tree_min_leaf_tokens: int = 4_000
    tree_max_children: int = 6
    tree_concurrency: int = 12

    # Agent budgets (per-stage request_limit for pydantic-ai)
    agent_router_request_limit: int = 60
    agent_excel_request_limit: int = 40
    agent_answer_request_limit: int = 10
    # Office agent: building a multi-slide deck takes many officecli tool calls
    # (one per batch group + verification), so the pydantic-ai default of 50 is
    # too low and kills large decks mid-build. Give it generous headroom.
    agent_office_request_limit: int = 200
    # Max router → excel → answer iterations per query
    agent_max_hops: int = 3
    # Max follow-up questions the answer agent can queue per hop
    agent_max_followups_per_hop: int = 3

    # Answer file output
    workspace_output_dir: str = "data/workspaces" # No longer in use, need to remove

    # Report drafting
    report_output_dir: str = "data/reports" # No longer in use, need to remove
    report_max_hops: int = 2
    report_section_concurrency: int = 2
    agent_report_retrieval_request_limit: int = 120

    # HTTP API
    api_auth_tokens: str = ""
    api_ingest_workers: int = 2
    api_upload_dir: str = "data/uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()

