"""Central Logfire setup for the whole backend.

Logfire is configured exactly once, on first import of this module, and then
pydantic-ai is instrumented globally. Because pydantic-ai's instrumentation is
process-wide (`logfire.instrument_pydantic_ai()` patches the Agent class), every
agent in the codebase — the planner/replan agents, the router/excel/answer
agents, the report agents, the web-search agent and the office agent — is traced
automatically without touching each agent definition.

Several agents are instantiated at module-import time (e.g. `theWebAgent`,
`plannerAgent`). To make sure their runs are captured, this module must be
imported before those agent modules. Import it at the very top of the app entry
points (`api/app.py`, `start_server.py`, `main.py`).

The Logfire write token lives in `.env` as LOGFIRE_API_KEY. The Logfire SDK
itself looks for LOGFIRE_TOKEN, so we read our key and pass it explicitly.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_configured = False


def setup_logfire(service_name: str = "agentic-rag") -> None:
    """Configure Logfire and instrument pydantic-ai + common libraries.

    Idempotent: safe to call from multiple entry points; only the first call
    does any work. If no LOGFIRE_API_KEY is present, configuration is skipped so
    the app still boots in environments without observability set up.
    """
    global _configured
    if _configured:
        return

    load_dotenv()
    token = os.getenv("LOGFIRE_API_KEY") or os.getenv("LOGFIRE_TOKEN")
    if not token:
        log.warning("LOGFIRE_API_KEY not set; skipping Logfire instrumentation")
        _configured = True
        return

    try:
        import logfire
    except ImportError:
        log.warning("logfire is not installed; skipping instrumentation")
        _configured = True
        return

    # Logfire tokens are region-scoped (`pylf_v2_eu_...` = EU, `..._us_...` = US),
    # but this SDK version doesn't always route the token to the matching region
    # automatically — it defaults to the US endpoint, which rejects an EU token
    # with "401 Invalid token". Derive the region from the token prefix and point
    # the SDK at the correct base URL so validation and ingestion both succeed.
    advanced = None
    region = None
    if token.startswith("pylf_") and len(token.split("_")) >= 3:
        region = token.split("_")[2]  # e.g. "eu" / "us"
    if region in ("eu", "us"):
        from logfire import AdvancedOptions

        advanced = AdvancedOptions(base_url=f"https://logfire-{region}.pydantic.dev")

    logfire.configure(
        token=token,
        service_name=service_name,
        # Don't crash the app if Logfire can't reach its backend.
        send_to_logfire="if-token-present",
        advanced=advanced,
    )

    # Process-wide: traces every pydantic-ai Agent run (model calls, tool calls,
    # retries) across all agents in the codebase. Captures the full message
    # history (prompts + responses) on each agent-run span by default.
    logfire.instrument_pydantic_ai()

    # Capture the outbound HTTP traffic the agents generate (OpenAI/Qwen model
    # calls, Exa/Linkup web search, browser-use). instrument_httpx covers the
    # httpx-based clients pydantic-ai and most SDKs use under the hood.
    try:
        logfire.instrument_httpx(capture_all=True)
    except Exception as e:  # httpx may be absent or already patched
        log.debug("Could not instrument httpx: %s", e)

    log.info("Logfire instrumentation enabled (service=%s)", service_name)
    _configured = True


def instrument_fastapi_app(app) -> None:
    """Trace incoming requests for a FastAPI app, nesting agent runs under them.

    Guarded so a missing `logfire[fastapi]` extra (or no token) never crashes
    app startup — it just skips request tracing.
    """
    try:
        import logfire

        logfire.instrument_fastapi(app, capture_headers=False)
    except Exception as e:
        log.warning("Could not instrument FastAPI app: %s", e)


# Configure on import so that agents instantiated at module-import time are
# already covered by the time their modules load.
setup_logfire()
