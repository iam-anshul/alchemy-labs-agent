# Web Search Agent Changelog

This documents the addition of the `web_search` sub-agent — a lightweight,
Linkup-backed internet agent — and the routing changes that make the heavier
`browser` agent the exception rather than the default for web work.

Date: 2026-06-10

## Summary

A new executor type, `web_search`, was added to the roster. It does two things
and nothing else: search the web (returning a synthesized, sourced answer) and
fetch a single page as markdown. Both capabilities are powered by the
[Linkup](https://linkup.so) search engine. The existing `browser` agent — which
carries a full stateful headless browser — is now reserved for genuinely
interactive work (clicking, login, multi-step JS flows, binary downloads).

The intent: stop routing plain "search for X" / "read this page" tasks to the
expensive `browser` agent. Those are now `web_search` tasks; `browser` is the
escalation path only when interactivity or a downloaded binary is required.

## New agent: `web_search`

Implemented in [`web_agent.py`](../web_agent.py).

### Tools

| Tool | Purpose |
| --- | --- |
| `read_file(path)` | Read a dep input file (relative to workspace). |
| `write_file(path, content)` | Persist text outputs (markdown/JSON/CSV/text) under `outputs/`. |
| `submit(produced, notes)` | Finalize once; validates each produced path exists and is non-empty. |
| `web_search_with_linkup(query, depth)` | Search the web via Linkup. Returns a **sourced answer** — a synthesized `answer` plus a list of `sources` (title, URL, snippet) — not a raw link list. |
| `fetch_url(url)` | Fetch one page's full contents as clean markdown, with JavaScript rendered. |

Both web tools are registered with `@theWebAgent.tool_plain` (they take no
`RunContext`); `read_file`/`write_file`/`submit` use `@theWebAgent.tool`
because they need workspace deps.

### Linkup specifics baked into the tool docstrings and system prompt

- **`web_search_with_linkup`** calls `linkup_client.search(..., output_type="sourcedAnswer")`.
  - `query` — guidance steers toward specific, instruction-style natural-language
    queries (Linkup follows query instructions literally), not bare keywords.
  - `depth` — `"standard"` (single-iteration, fast, cheap) vs `"deep"`
    (multi-iteration search-and-scrape, slower and ~10x the cost). Default is
    `"standard"`; `"deep"` is reserved for genuinely hard, multi-step research.
- **`fetch_url`** calls `linkup_client.fetch(url=url, render_js=True)` — returns
  page text as markdown (JS rendered), **not** a saved binary file.

### Limitation (and why `browser` still exists)

`web_search` **cannot** click, log in, fill forms, navigate multi-step flows, or
download binary files (PDF/XLSX). `fetch_url` returns page *text*, not a saved
binary. So when a PDF must land on disk for the `document_answering` agent to
ingest, the upstream fetch task must still be a `browser` task.

### Dependencies

[`requirements.txt`](../requirements.txt) — added `linkup-sdk`.

## Schema change

[`formats_pydantic.py`](../formats_pydantic.py):

- `TaskSpec.agent` literal extended from
  `Literal["browser", "office", "document_answering"]` to include
  `"web_search"`. The planner can now emit `web_search` as a task's agent type.
- `InternalDocAgentDeps` was moved **above** `TaskSpec` so it is defined before
  `TaskSpec` references it as a type annotation (avoids a forward-reference
  `NameError` at class-definition time).

## Dispatch wiring

[`api/routes/chat.py`](../api/routes/chat.py):

- Imported `run_web_executor` from `web_agent`.
- Added a `case "web_search":` branch in `dispatch_executor_agent`, modeled on
  the `office` branch:
  - publishes an `agent_started` event,
  - calls `await run_web_executor(workspace_subdir_path=subdir_path, query=..., expects=..., dep_files=..., sink=task_sink)`
    — note it passes the per-run `subdir_path` (the same sandbox every other
    executor uses), not the bare workspace id,
  - publishes an `agent_ended` event (status derived from `web_result.error`),
  - returns the `ExecutorResult`.

The branch participates in the existing retry loop, file validation, and
human-in-the-loop checkpoints unchanged — it's just another executor type.

## Planner prompt changes

[`system_prompts.py`](../system_prompts.py), `planner_system_prompt`:

- **Roster**: added `web_search` as the **default/preferred** internet agent and
  reframed `browser` as the **heavy interactive** agent. Explicit instruction:
  do NOT route plain search/read tasks to `browser` — those are `web_search`.
- **`document_answering` precondition**: clarified that `web_search` cannot
  download a binary PDF, so a PDF needed for ingestion must come from a
  `browser` task; and that `web_search`/`browser`/`office` (not
  `document_answering`) can read `.md`/`.txt` outputs.
- **Altitude rule**: "for internet tasks, reach for `web_search` first; escalate
  to `browser` only when interactivity or a binary download is required."
- **Worked examples** (HITL section and the search-then-ask example): switched
  the search tasks from `browser` to `web_search`.

## New executor prompt

[`system_prompts.py`](../system_prompts.py), `web_system_prompt`: a full
files-first system prompt for the `web_search` agent, consistent with the
`browser`/`office`/`document_answering` prompts (QUERY / EXPECTED OUTPUT /
INPUT FILES / WORKSPACE contract, single task, submit-once). It documents the
real tools, the Linkup sourcedAnswer behavior, the `standard` vs `deep` depth
trade-off, JS-rendered fetch, source-citation expectations, and the
no-binary-download limitation.

The per-task prompt builder `_build_task_prompt` in
[`web_agent.py`](../web_agent.py) was also corrected — it previously identified
the agent as an "office sub-agent" and embedded the mandatory `officecli`
tool-choice block (tools this agent does not have). It now identifies the agent
as a web-search sub-agent and gives a `HOW TO WORK` section pointing at
`web_search_with_linkup` / `fetch_url` / `write_file`.

## Naming note

The agent type is `web_search` **everywhere** — the `TaskSpec.agent` enum, the
dispatch `case`, the planner roster, and the executor's self-identity all agree.
The underlying module/function names (`web_agent.py`, `run_web_executor`,
`theWebAgent`, `WebDeps`) use the shorter "web" form, but the *agent-type value*
the planner emits is always `web_search`.

## Files touched

| File | Change |
| --- | --- |
| `web_agent.py` | New web_search executor (untracked/new file). |
| `formats_pydantic.py` | `web_search` added to `TaskSpec.agent`; `InternalDocAgentDeps` moved above `TaskSpec`. |
| `api/routes/chat.py` | Import + `case "web_search"` dispatch branch. |
| `system_prompts.py` | Planner roster/routing updates + new `web_system_prompt`. |
| `requirements.txt` | Added `linkup-sdk`. |