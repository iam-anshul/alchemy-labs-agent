# Changelog — Logfire Observability

Adds [Logfire](https://logfire.pydantic.dev) tracing across the entire backend so
every agent run, model call, tool call, HTTP request, and incoming API request is
captured automatically.

---

## Summary

Logfire is now configured **once** at process startup, and pydantic-ai is
instrumented **process-wide**. Because `logfire.instrument_pydantic_ai()` patches
the pydantic-ai `Agent` class itself, **every agent in the codebase is traced
without editing any agent definition** — planner, replan, router, excel, answer,
the report agents (outline/section/critic/summary/router), the web-search agent,
and the office agent. The browser agent (which uses `browser-use`, not
pydantic-ai) is covered by an explicit span plus HTTP instrumentation. FastAPI
requests are traced so each agent run nests under the API call that triggered it.

---

## Files changed

### `observability.py` — **new**

Central, single-source Logfire setup module.

- **`setup_logfire(service_name="agentic-rag")`** — configures Logfire and turns
  on instrumentation. Key properties:
  - **Idempotent.** Guarded by a module-level `_configured` flag so it is safe to
    call from multiple entry points; only the first call does any work.
  - **Reads the token from `.env` as `LOGFIRE_API_KEY`** (falls back to
    `LOGFIRE_TOKEN`). The Logfire SDK natively looks for `LOGFIRE_TOKEN`, so we
    read our key and pass it explicitly to `logfire.configure(token=...)`.
  - **Region-aware.** Logfire tokens are region-scoped — `pylf_v2_eu_...` is EU,
    `..._us_...` is US. This SDK version defaults to the US endpoint and returns
    `401 Invalid token` when an EU token is sent there. `setup_logfire()` derives
    the region from the token prefix and points the SDK at the matching base URL
    (`https://logfire-eu.pydantic.dev` / `https://logfire-us.pydantic.dev`) via
    `AdvancedOptions(base_url=...)`. (See "Region fix" below.)
  - **Fails open.** If `LOGFIRE_API_KEY` is unset, logfire isn't installed, or the
    backend is unreachable, the app still boots — instrumentation is skipped with
    a warning rather than crashing startup (`send_to_logfire="if-token-present"`).
  - **`logfire.instrument_pydantic_ai()`** — process-wide tracing of every
    pydantic-ai agent run (model calls, tool calls, retries) including full
    prompt/response message history on each span.
  - **`logfire.instrument_httpx(capture_all=True)`** — captures outbound HTTP from
    the agents (OpenAI/Qwen model calls, Exa/Linkup web search, browser-use),
    guarded so a missing/already-patched httpx never crashes startup.
- **`instrument_fastapi_app(app)`** — traces incoming requests for a FastAPI app
  so agent runs nest under the triggering request. Guarded in `try/except` so a
  missing `logfire[fastapi]` extra (or absent token) silently skips request
  tracing instead of crashing.
- **Configures on import** (`setup_logfire()` called at module bottom) so that
  agents instantiated at module-import time are already covered by the time their
  modules load.

### `main.py`

- Added `import observability` as the **first** import (before `orchestrator`,
  `agent`, `report`, `office_agent`), so instrumentation is live before any agent
  module — and the global agents they define — is loaded.

### `start_server.py` — primary/dev entry point

- Added `import observability` as the **first** import (before the route modules
  that transitively import the agents).
- Added `observability.instrument_fastapi_app(server)` after app construction to
  trace incoming requests.

### `api/app.py` — alternate entry point

- Added `import observability` as the **first** import.
- Added `observability.instrument_fastapi_app(app)` after app construction.
- _Note: `api/app.py` has a pre-existing, unrelated import bug (it references
  `documents.router` etc., but those modules export `document_router`). It does
  not import cleanly on `main` independent of this change. The working entry point
  is `start_server.py`._

### `browser_agent.py`

- Added `import logfire`.
- Wrapped the `browser_agent.run(...)` call in an explicit
  `logfire.span("browser_agent run", task_id=..., attempt=..., query=...,
  max_steps=...)`. The browser agent is `browser-use`, not pydantic-ai, so it
  gets no automatic pydantic-ai instrumentation; this span makes it visible in
  traces, while its LLM HTTP calls are still captured by `instrument_httpx`.

### `requirements.txt`

- Added `logfire[fastapi,httpx]>=4.0,<5` (pinned to the v4 line, matching the
  installed SDK). The `fastapi` and `httpx` extras provide the request and
  outbound-HTTP instrumenters used above.

---

## Coverage — what gets traced

| Component | Module | How it's traced |
|---|---|---|
| Planner agent | `orchestrator.py` | `instrument_pydantic_ai` (auto) |
| Replan agent | `orchestrator.py` | `instrument_pydantic_ai` (auto) |
| Router agent | `agent.py` | `instrument_pydantic_ai` (auto) |
| Excel agent | `agent.py` | `instrument_pydantic_ai` (auto) |
| Answer agent | `agent.py` | `instrument_pydantic_ai` (auto) |
| Outline / Section / Critic / Summary / Router | `report.py` | `instrument_pydantic_ai` (auto) |
| Web-search agent | `web_agent.py` | `instrument_pydantic_ai` (auto) |
| Office agent | `office_agent.py` | `instrument_pydantic_ai` (auto) |
| Browser agent | `browser_agent.py` | explicit `logfire.span` + `instrument_httpx` |
| Outbound model / search HTTP | all | `instrument_httpx(capture_all=True)` |
| Incoming API requests | `start_server.py`, `api/app.py` | `instrument_fastapi` |

---

## Region fix — why the token first appeared "invalid"

After wiring everything up, `logfire.configure()` returned **`401 Invalid token`**
even with a freshly issued token. Investigation showed it was **not** a bad token
but a **region mismatch**:

- The token is an **EU** token (prefix `pylf_v2_eu_`), project
  `iam-anshul/starter-project`.
- A raw HTTP call to the **EU** endpoint (`logfire-eu.pydantic.dev/v1/info`)
  returned **200** with the token.
- The Logfire SDK was validating/exporting against the **US** endpoint
  (`logfire-us.pydantic.dev`) by default, which rejects an EU token with `401`.

Setting `AdvancedOptions(base_url="https://logfire-eu.pydantic.dev")` resolved it.
`setup_logfire()` derives the region from the token prefix so both EU and US
tokens work without further config.

**Verified end-to-end:**
- `GET /v1/info` (EU) → `200` (validation passes)
- `POST /v1/traces` (EU) → `200` (spans ingested)
- Project: `https://logfire-eu.pydantic.dev/iam-anshul/starter-project`

---

## Configuration

- **`LOGFIRE_API_KEY`** in `.env` — the Logfire write token. If unset, the app
  boots normally with instrumentation disabled.
- Region is detected automatically from the token prefix; no extra config needed
  when rotating tokens within the same region.

## Notes / follow-ups

- If a `401` reappears after a token swap, first confirm the region by testing the
  token against `logfire-eu` vs `logfire-us` `/v1/info` before assuming the token
  is dead.
- `api/app.py`'s pre-existing router-attribute bug is out of scope for this change
  and remains; `start_server.py` is the working entry point.
