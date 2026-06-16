# Changelog — Exa API Key Rotation

**Date:** 2026-06-15

## Summary

The web search tool previously used a single Exa API key (`EXA_API_KEY`). The
`.env` file actually holds **six** Exa keys, intended to spread quota and survive
rate limits. This change introduces a rotating Exa client that uses all available
keys in round-robin order and automatically fails over to another key when a call
hits a rate-limit or auth error.

## Files changed

### Added: `exa_rotation.py`

A new module providing `RotatingExaClient`, a drop-in replacement for the plain
`exa_py.Exa` client. Exposes a module-level singleton `exa_client`.

Behavior:

- **Key discovery (`_discover_keys`)** — scans `os.environ` for every variable
  matching `EXA_API_KEY*`. This deliberately handles **both** naming styles found
  in `.env`:
  - `EXA_API_KEY_1`, `EXA_API_KEY_2`, `EXA_API_KEY_3` (with underscore separator)
  - `EXA_API_KEY4`, `EXA_API_KEY5`, `EXA_API_KEY6` (no separator)
  - a bare `EXA_API_KEY` (no suffix) is also picked up if present.

  Keys are de-duplicated and sorted by numeric suffix so rotation order is
  deterministic across restarts.

- **Round-robin rotation (`_next_index`)** — a thread-safe atomic counter hands
  out the next key on every call, so consecutive web searches spread across the
  full key pool (verified: 6 keys cycle `0,1,2,3,4,5,0,1,...`).

- **Failover with backoff (`_call`)** — when a call raises a *key-level* error,
  the client transparently retries the same call against the remaining keys.
  Key-level errors are detected by inspecting the exception text for HTTP status
  codes (`401`, `402`, `403`, `429`) and hints like "rate limit", "quota",
  "unauthorized", "forbidden", "payment required", "invalid api key". The Exa SDK
  surfaces HTTP failures as generic exceptions carrying the status code in their
  message rather than as typed exceptions, so matching on the message is the
  reliable approach. Non-key errors (e.g. bad arguments) are re-raised immediately
  without burning through keys.

  The retry strategy is **multi-pass with backoff**, addressing the case where
  *all* keys are throttled at once:
  - The first walk over the whole key pool happens with **no delay** (every key is
    fresh, so failover should be instant in the common case).
  - If an entire pass fails on rotatable errors, the client **waits, then walks the
    pool again**, up to `_MAX_PASSES` passes (default **3**).
  - The wait honors a server-provided **`Retry-After`** hint when present (read
    from the exception's response headers, or scraped from the message as a
    fallback — the largest hint seen in the pass wins); otherwise it uses
    **exponential backoff** (`base * 2^(pass-1)`, default base **1.0s**, capped at
    **30.0s**).
  - All three knobs are overridable via environment variables without code changes:
    `EXA_ROTATION_MAX_PASSES`, `EXA_ROTATION_BACKOFF_BASE`,
    `EXA_ROTATION_BACKOFF_MAX`.
  - If every key still fails after the final pass, the last error propagates.

- **One client per key** — a reusable `Exa` instance is constructed per key at
  startup, rather than per call.

- **Public surface** — `answer(...)` and `get_contents(...)` (the only methods the
  codebase uses) are explicit pass-throughs; any other Exa method is proxied via
  `__getattr__`, so future SDK methods automatically gain rotation + failover.

- **`load_dotenv()` on import** — the singleton is built at import time, which can
  run before the importing module calls `load_dotenv()` itself, so the module
  loads `.env` first to guarantee keys are present. `load_dotenv` is idempotent and
  does not override already-set real environment variables.

- **No keys → clear error** — if no `EXA_API_KEY*` is found, construction raises a
  `RuntimeError` explaining which env vars to set, instead of failing later with an
  opaque auth error.

### Modified: `web_agent.py`

- Replaced `from exa_py import Exa` + manual single-key construction
  (`EXA_API_KEY = os.getenv("EXA_API_KEY")` / `exa_client = Exa(api_key=EXA_API_KEY)`)
  with `from exa_rotation import exa_client`.
- The two existing call sites are unchanged in form and now transparently use the
  rotating client:
  - `exa_client.answer(query, text=True, model=model)` — the `web_search` tool.
  - `exa_client.get_contents([url], text=True, livecrawl="always")` — the
    `fetch_url` tool.

## Verification

- `exa_rotation` discovers all **6** keys from `.env`.
- Round-robin cursor confirmed to cycle through every key and wrap correctly.
- `exa_client.answer`, `exa_client.get_contents`, and proxied methods are callable.
- `web_agent` imports cleanly with the rotating client in place.

## Notes / follow-ups

- The keys in `.env` are named consistently `EXA_API_KEY_1` … `EXA_API_KEY_6`.
  Discovery also tolerates the no-separator style (`EXA_API_KEY4`) and a bare
  `EXA_API_KEY`, so adding keys in either form will still be picked up.
- Failover retries the *same* call across keys for rate-limit/auth errors only,
  now with multi-pass exponential backoff (and `Retry-After` support) for the case
  where all keys are throttled at once. If every key still fails after
  `EXA_ROTATION_MAX_PASSES` passes, the original error propagates.
- The backoff `time.sleep` is blocking. This is intentional and safe: the web
  tools call the client via `asyncio.to_thread`, so the sleep runs on a worker
  thread and does not stall the event loop.
