# Changelog — Separate model env var for the document agent

Gives the document agent (tree-building summaries + doc Q&A) its own model
setting, decoupled from the app-wide `openai_model`, and fixes a `build_tree`
failure caused by an invalid model name.

---

## Background

Document ingestion ends in `build_tree` (`tree.py`), which summarizes the parsed
document with an LLM via the OpenAI-compatible endpoint at `OPENAI_BASE_URL`
(a Wafer "pass" endpoint). Both `build_tree` and the doc-answering model
(`agent.py`) read the single `openai_model` setting (env `OPENAI_MODEL`).

`OPENAI_MODEL` was set to `qwen3.6-plus`, which that endpoint does **not** serve.
Ingestion got past LlamaParse (parse completed), then failed at `build_tree`:

```
openai.NotFoundError: 404 - {'error': {'message': "Model 'qwen3.6-plus' is not
available on this endpoint. Available models: GLM-5.1, GLM-5.2, Kimi-K2.6,
Kimi-K2.7-Code, MiniMax-M3, Qwen3.5-397B-A17B, Qwen3.6-35B-A3B, deepseek-v4-flash,
deepseek-v4-pro, qwen3.7-max. ...", 'code': 'model_not_found'}}
```

Two problems surfaced together:
1. The configured model name was invalid for the endpoint.
2. The doc pipeline shared one model setting with the rest of the app, so it
   couldn't run on a different model without coupling the two.

---

## Change

Introduce a dedicated `DOC_AGENT_MODEL` env var for the document agent, falling
back to `OPENAI_MODEL` when unset, and point both doc-side consumers at it.

- New setting `doc_agent_model` (env `DOC_AGENT_MODEL`, default empty) plus a
  `Settings.resolve_doc_agent_model()` helper that returns `doc_agent_model` when
  set, else `openai_model`. Centralizing the fallback keeps every call site
  resolving it the same way.
- `tree.py` (`build_tree`) and `agent.py` (`_build_model`) now use
  `resolve_doc_agent_model()` instead of reading `openai_model` directly.
- `.env` updated to valid endpoint model names:
  - `DOC_AGENT_MODEL="Qwen3.6-35B-A3B"` (valid; closest to the intended
    `qwen3.6-plus`).
  - `OPENAI_MODEL` changed from the invalid `qwen3.6-plus` to
    `Qwen3.5-397B-A17B` (matches the existing `MODEL` var) so the fallback can't
    be broken either.

The planner/orchestrator side (which reads its own `MODEL` env var) is
untouched — only the document agent moved to `DOC_AGENT_MODEL`.

---

## Files changed

### `config.py`

- Added the `doc_agent_model: str = ""` setting (reads `DOC_AGENT_MODEL`).
- Added `Settings.resolve_doc_agent_model()` — returns `doc_agent_model or
  openai_model`, so an unconfigured deployment keeps its previous single-model
  behavior.

### `tree.py`

- `build_tree` resolves its summarization model via `s.resolve_doc_agent_model()`
  instead of `s.openai_model`.

### `agent.py`

- `_build_model` constructs the `OpenAIChatModel` with
  `s.resolve_doc_agent_model()` instead of `s.openai_model`.

### `.env`

- Added `DOC_AGENT_MODEL="Qwen3.6-35B-A3B"`.
- Fixed `OPENAI_MODEL` from `qwen3.6-plus` → `Qwen3.5-397B-A17B`.

---

## Verification

- Settings resolve as expected:
  ```
  openai_model      : Qwen3.5-397B-A17B
  doc_agent_model   : 'Qwen3.6-35B-A3B'
  resolved doc model: Qwen3.6-35B-A3B
  ```
- Live chat-completion call to the Wafer endpoint with the resolved doc model
  succeeded (no more 404):
  ```
  SUCCESS model= Qwen3.6-35B-A3B -> OK
  ```

---

## Notes / follow-ups

- Existing docs left at `failed`/`queued` from the 404 are not auto-retried —
  re-upload to ingest them through `build_tree` to `ready`.
- `DOC_AGENT_MODEL` must name a model the `OPENAI_BASE_URL` endpoint actually
  serves; an invalid name will 404 the same way. The endpoint's error message
  lists the currently available models.
