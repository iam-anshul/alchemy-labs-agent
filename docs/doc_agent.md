# Doc Agent

The `document_answering` sub-agent in the files-first planner system. Wraps the **Doc Reasoner** RAG engine (see [AGENT_FLOW.md](../AGENT_FLOW.md), [WALKTHROUGH.md](../WALKTHROUGH.md)) and exposes it through a small tool surface, so the planner can hand off "grounded document analysis" as a single task.

Lives in [doc_agent.py](../doc_agent.py). Driven by `doc_system_prompt` in [system_prompts.py](../system_prompts.py).

---

## Where it fits

The planner emits a task with `agent: "document_answering"` whenever a step needs to answer questions or write a report grounded in documents already in the workspace — typically PDFs downloaded by an upstream `browser` task. The control loop in [main.py](../main.py) dispatches one fresh executor per task; the executor has no memory across dispatches.

```
planner          control loop                       doc agent (pydantic-ai)        doc-reasoner
─────────        ─────────────                      ──────────────────────         ────────────
TaskSpec   →     dispatch_executor_agent()    →     run_doc_executor(...)
agent:                                                builds DocDeps + task prompt
"document_                                            runs theDocAgent.run(...)
 answering"                                           ↓
                                                      LLM calls tools as needed:
                                                        read_file
                                                        ingest_documents              →   ingest_local_file
                                                        list_documents / get_document →   list_local_documents / get_local_document
                                                        ask                           →   ask_local_query        →  answer_query (agent.py)
                                                        list_queries / get_query      →   list_local_queries / get_local_query
                                                        draft_report                  →   draft_local_report     →  draft_report (report.py)
                                                        list_reports / get_report     →   list_local_reports / get_local_report
                                                        write_file
                                                        submit
                                                      ↓
                 ←     ExecutorResult           ←     deps.submitted
                       (produced, notes, error?)
```

The local helpers (`*_local_*` functions in [api/routes/documents.py](../api/routes/documents.py), [api/routes/queries.py](../api/routes/queries.py), [api/routes/reports.py](../api/routes/reports.py)) are the same functions the HTTP route handlers use internally — they just skip the HTTP adapter layer (no `UploadFile`, no `Depends`, no SSE channel, no fire-and-forget task spawning).

---

## Tool surface

| Tool | Signature | What it does |
|---|---|---|
| `read_file` | `(path: str) -> str` | Read a text file from the workspace. For PDFs, use `ingest_documents` instead. |
| `write_file` | `(path: str, content: str) -> str` | Write text to a workspace path. Used to assemble output files from `ask` results. |
| `submit` | `(produced: list[str], notes: str) -> str` | Finalize. Validates each path exists and is non-empty before accepting. Call exactly once. |
| `ingest_documents` | `(paths: list[str]) -> list[str]` | Ingest PDFs into the doc-reasoner index. Returns new `doc_id`s. **Not idempotent** — call once per dispatch. |
| `list_documents` | `() -> str` | List indexed docs in this workspace, one JSON object per line. `doc_summary` omitted. |
| `get_document` | `(doc_id: str) -> str` | Full doc metadata + `doc_summary` as pretty-printed JSON. |
| `ask` | `(query: str, doc_ids: list[str] \| None = None) -> str` | **Load-bearing.** Run a query through doc-reasoner. Returns answer + citations + `table_findings` + confidence as JSON. Internal trace fields stripped. |
| `list_queries` | `() -> str` | List prior answered queries in this workspace. Cross-dispatch lookup. |
| `get_query` | `(query_id: str) -> str` | Full stored result of a past query. |
| `draft_report` | `(brief: str, output_relpath: str, target_length: str = "standard", doc_ids: list[str] \| None = None) -> str` | Generate a multi-section markdown report and **write it directly** to `output_relpath`. Returns metadata only. |
| `list_reports` | `() -> str` | List prior drafted reports. Cross-dispatch lookup. |
| `get_report` | `(report_id: str) -> str` | Full stored report (including draft_md) as JSON. |

---

## Lifecycle of one dispatch

1. **Control loop** calls `run_doc_executor(workspace, workspace_id, user_id, query, expects, dep_files)`.
2. **`run_doc_executor`** builds `DocDeps(workspace=..., workspace_id=..., user_id=..., docs=[], submitted=None)` and the task prompt (via `_build_task_prompt`), then awaits `theDocAgent.run(task_prompt, deps=deps)`.
3. **The LLM** sees the task prompt + the doc system prompt and the tool schemas. It typically:
    1. `read_file`s any upstream markdown handoffs (e.g. `outputs/t1_sources.md`) to understand what each PDF is.
    2. `ingest_documents([...])` once with all PDFs from INPUT FILES. Records the returned `doc_id`s.
    3. (Optional) `list_documents` / `get_document(doc_id)` to confirm what's indexed.
    4. Decomposes the QUERY into focused `ask(query, doc_ids=[...])` calls.
    5. Assembles outputs:
        - For focused markdowns/CSVs: `write_file(output_path, content)` with hand-stitched content from `ask` results.
        - For multi-section narrative reports: `draft_report(brief, output_relpath, ...)` which writes the file directly.
    6. Calls `submit(produced=[...], notes="...")`. The `submit` tool validates each path exists and is non-empty, then stashes the `ExecutorResult` on `deps.submitted`.
4. **`run_doc_executor`** returns `deps.submitted`. If `submitted` is still `None` (the LLM exited without calling submit), it returns an `ExecutorResult` with `error="Agent loop ended without calling submit"`. If the agent loop raised, it returns `error="Agent loop failed: ..."`.

`DocDeps.docs` is populated by `ingest_documents` as a convenience — the LLM gets the list back as a return value too, but `deps.docs` keeps it accumulated across calls if the LLM calls `ingest_documents` more than once.

---

## Design decisions

### Why local helpers instead of HTTP calls

The doc agent runs in the same Python process as the doc-reasoner. Going through HTTP would mean:

- Bouncing through `UploadFile` / `Depends` / multipart for ingestion.
- Fire-and-forget task spawning for queries/reports (HTTP routes return 202 immediately and stream events via SSE).
- Polling or subscribing to the SSE stream to know when work is done.

That's HTTP-adapter logic — it exists to serve external clients. From inside the process, the natural call is direct: `await answer_query(...)`, `await draft_report(...)`, get the result back. The `ingest_local_file`, `ask_local_query`, `draft_local_report`, `list_local_*`, `get_local_*` helpers live next to the route handlers in `api/routes/*.py` and expose the core logic without the HTTP wrapping.

The HTTP routes themselves are left untouched — they continue to serve external clients. No behavior change for the API.

### Why no `stream_query` / `stream_report` tool

SSE streams expose router/excel/answer events as they fire. They exist for HTTP clients (UIs) that want to show progress. The doc agent is not a UI; it makes one `ask` call, awaits the final answer, uses it, moves on. Intermediate hop events would just pollute its context without changing any decision it makes. And mechanically, `await ask_local_query(...)` blocks until the answer is ready — there's no separate channel to stream from.

If a single `ask` is too slow in practice, the lever to pull is doc-reasoner's internal budget (request limits, `agent_max_hops` in [config.py](../config.py)), not bolted-on streaming.

### Why `draft_report` writes directly instead of returning markdown

Reports run 1k–10k+ words. Returning the full draft through the tool bloats the LLM's context every time — and the LLM almost always uses it for exactly one thing: `write_file` it to the expected output path. Two consequences of the round-trip pattern:

1. The LLM might truncate the draft when echoing it back (especially with smaller models).
2. The draft passes through the LLM twice — once to receive, once to copy to the file — burning tokens for no value.

Coupling the disk write to the call avoids both. The trade-off: the tool now has a side effect that the system prompt has to advertise ("do not also `write_file` to `output_relpath`"). That's worth it.

### `ingest_documents` dedup — what's enforced

The underlying `ingest_local_file` is not idempotent — call it twice on the same PDF and you get two `doc_id`s and two LlamaParse+tree-build runs. To keep the LLM from accidentally paying that cost, the **tool** guards against re-ingestion at the per-dispatch level:

- `DocDeps.ingested_paths: set[str]` tracks which workspace-relative paths have already been ingested in this dispatch.
- `ingest_documents` skips any path already in that set and only ingests the new ones.
- If every path passed is already ingested, the tool returns a string telling the LLM to reuse the existing `doc_ids` rather than retrying.

This is a per-dispatch guard, not a global one. Across dispatches (or across the HTTP API), the same PDF can still produce multiple `doc_id`s — fixing that requires hash-dedup inside `ingest_local_file`, which is a DB-schema change and out of scope for this pass.

### Why `list_queries` / `get_query` / `list_reports` / `get_report` exist

Cross-dispatch lookup. Within one dispatch the LLM remembers its own `ask` / `draft_report` calls already. These tools earn their keep only when a *prior* sub-agent in the same doc-reasoner workspace answered something relevant — which is rare, because the planner is the one with the cross-task view. They're cheap to keep and the system prompt explicitly tells the LLM "you will rarely need these," so the cost is mostly the few hundred tokens of tool-schema overhead per dispatch.

### Path safety

All workspace-relative paths the LLM passes go through `_resolve_inside(workspace, path)`, which resolves and confirms the result is under the workspace root. Path-traversal attempts (`../../etc/passwd`) become `ERROR: path ... is outside the workspace` strings the LLM sees as tool output. The `submit` tool runs the same check on every `produced` path before accepting.

---

## Integration: wiring [main.py](../main.py)

The `case "document_answering":` branch in `dispatch_executor_agent` (around [main.py:158](../main.py#L158)) is currently a stub. To wire it:

```python
case "document_answering":
    return await run_doc_executor(
        workspace=workspace,
        workspace_id=workspace.name,  # the run's folder name is a stable id per run
        user_id="doc_agent",          # static; or thread through from Run.user_query
        query=task_spec.query,
        expects=task_spec.expects,
        dep_files=dep_files,
    )
```

`dispatch_executor_agent` doesn't currently have `workspace_id` or `user_id` in its parameters — the doc agent is the only executor that needs them, so the simplest move is to derive them inside the `case` block (as above) rather than thread two extra parameters through `dispatch_executor_agent` for every agent type.

Mapping choices to be aware of:

- **`workspace_id`** is what doc-reasoner uses to scope its SQLite tables. Setting it to `workspace.name` (the run's workspace folder name, e.g. `91Make the b`) means each planner run's documents are isolated from other runs. That's almost always what you want — it keeps `ask` from accidentally surfacing documents from a different run.
- **`user_id`** is just an audit tag on the docs/queries/reports tables. Set to `"doc_agent"` or thread through from `Run` if you want real attribution.

---

## Known limitations

- **`target_length` on `draft_report` is typed as `str`, not `Literal`.** Pydantic-AI's JSON schema for `Literal` is enforced patchily on non-OpenAI providers (Qwen et al.). To keep the tool reliable on the actual model in use, the parameter is `str` with a hand-rolled validator that returns an `ERROR:` string for invalid values. If you confirm `Literal` is enforced on your provider, swap it back for tighter schema-level validation.
- **No path-collision check in `draft_report`.** If `output_relpath` already exists, it's silently overwritten. The `submit`-time non-empty check catches "wrote nothing", but doesn't catch "overwrote a different task's output". In practice the planner emits unique paths per task, so this hasn't bitten — but it's not a safety property the code enforces.
- **Cross-dispatch ingest dedup.** Within one dispatch, `ingest_documents` deduplicates by workspace-relative path via `DocDeps.ingested_paths`. Across dispatches (or across the HTTP API), the same PDF still produces multiple `doc_id`s — proper dedup requires a content-hash check inside `ingest_local_file`, which is a DB-schema change and not in this pass.
- **HTTP `list_queries` and `list_reports` route handlers were not refactored** to use the new local helpers, only the local helpers were added alongside. This keeps the API surface untouched at the cost of some duplicated DB logic. Same call pattern in both places; trivial to consolidate later if it becomes a maintenance burden.

---

## See also

- [system_prompts.py](../system_prompts.py) — `doc_system_prompt`, which the LLM actually reads.
- [planner_readme_1.md](../planner_readme_1.md) — the planner architecture this agent type plugs into.
- [AGENT_FLOW.md](../AGENT_FLOW.md), [WALKTHROUGH.md](../WALKTHROUGH.md) — the doc-reasoner engine being wrapped.
- [api/routes/documents.py](../api/routes/documents.py), [api/routes/queries.py](../api/routes/queries.py), [api/routes/reports.py](../api/routes/reports.py) — the `*_local_*` helpers the tools call.
