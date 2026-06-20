# Changelog — Planner workspace memory via on-demand look-back tools

Replaces the planner's pushed, capped, synthetic cross-run "message history" with a
**pull-on-demand** model: the planner is told (cheaply) that prior runs exist, and
fetches the specific prior plans and artifacts it needs through dedicated tools —
across **any** prior run in the workspace, not just the most recent. Sub-agent
filesystem isolation is fully preserved.

---

## Background — what existed before

The planner's awareness of earlier work was a **synthetic, pushed** conversation:

- `_build_message_history_from_prior_runs` (`api/routes/chat.py`) read the last **5**
  runs from `workspace_runs` and, for each, emitted a
  `ModelRequest(user_query)` + `ModelResponse(todo_md)` pair, passed to the initial
  planner call via pydantic-ai's `message_history=` parameter.
- This was **push-only** (every initial planner call paid the cost), **capped at 5
  runs**, **most-recent-only in practice**, and gave the planner **no way to reach a
  specific older run** or to use a prior run's *produced files* beyond the single
  eager-restore of the latest run's artifacts.
- Sub-agents are sandboxed to their own run subdir (`_resolve_inside` guard); the
  planner has no filesystem access to any subdir. Prior produced files are persisted
  as base64 in `workspace_runs.produced_artifacts`, and the most recent run's files
  are eagerly restored into each new run's subdir.

### Design decisions taken (from the design discussion)

- **Storage format:** synthetic `(user_query → final todo.md)` content — **no new
  DB table** (the `workspace_runs` row already holds `user_query` + `todo_md`).
- **Delivery:** **hybrid** — always push a cheap one-line "N prior runs exist" hint;
  the planner PULLS detail on demand.
- **Granularity:** both a cheap **list** and a **fetch-by-id** for runs, mirrored for
  artifacts.
- **Scope:** the planner may reach **any** prior run in the workspace.
- **Eager restore:** **kept** (most-recent run's files still auto-restored); the
  tools are additive for older runs.
- **Replanner:** **unchanged** — it gets **no** cross-run history and **none** of
  these tools (replanning stays scoped to the in-flight run, per existing design).
- **Subdir timing:** the run subdir is now created **before** the first planner call
  so the fetch tool has somewhere to write.

---

## What changed

### 1. Cheap awareness hint replaces pushed history

`_build_message_history_from_prior_runs` and the `message_history=` push on the
initial planner call are **removed**. In their place, `_format_prior_runs_hint`
folds a single line into the planner's workspace context (alongside the existing
ready-docs inventory):

> Prior-run history: this workspace has N prior run(s). Most recent: "…" (status=…,
> query_counter=…). If this query continues or builds on earlier work, use the
> look-back tools …

This guarantees the planner can never be *unaware* that history exists (the reason
pull-only-with-no-hint was rejected), while paying near-zero context cost on a fresh,
self-contained query. It degrades to a neutral note if the lookup fails, and says
"this is the first run" when there are no prior runs.

### 2. Four planner-only look-back tools

Registered on `plannerAgent` **only** (not replan, not axis-append) via a new
`_register_history_tools` in `orchestrator.py`:

| Tool | Returns | Purpose |
|---|---|---|
| `list_prior_runs()` | metadata list (query_id, user_query, status, started_at, query_counter, **todo_md_chars only**) | browse the timeline cheaply; no plan content |
| `get_run_todo(query_id)` | one run's full final todo.md | read exactly what a specific past run planned + its produced paths |
| `list_prior_artifacts()` | content-free manifest (query_id, run_started_at, task_id, rel_path, bytes) | see what files prior runs produced |
| `fetch_prior_artifact(query_id, rel_path)` | short confirmation (path + bytes) | copy ONE prior file into THIS run's subdir for an executor to read |

All four exclude the in-flight run.

### 3. Isolation contract (unchanged guarantees, enforced in the new code)

- **Sub-agents** still see only their own run subdir — untouched.
- **The planner** still has **no filesystem read** of any subdir. Its only window
  into prior work is the hint + these tools.
- **`fetch_prior_artifact`** reads bytes **from the database**
  (`workspace_runs.produced_artifacts`), never from another run's directory, and
  writes **only inside the current run's subdir**. The destination is resolved with
  a `relative_to(subdir)` guard that refuses any `rel_path` attempting to escape via
  `..` or an absolute path.

So "any prior run" means the planner may *select* from any run's DB-persisted
artifacts — not that any code reads across sibling subdirs.

### 4. Subdir created before the planner; eager restore kept

In `create_chat`, the per-run subdir (`{query_counter}_{query_id}`, already
goal-independent) is now created **before** the first `planner()` call, and the
most-recent-run eager restore runs there first. This gives `fetch_prior_artifact` a
valid `subdir_path` from the very first planner turn. Eager restore is retained, so
the common "continue the most recent run" flow is unchanged; the tools add reach to
**older** runs.

---

## Files changed

### `db/utils.py` — new read helpers (no schema change)

- `list_prior_runs_meta(db, workspace_id, exclude_query_id, limit=50)` — newest-first
  lightweight run metadata; returns `todo_md_chars` (length), **not** content.
- `count_prior_runs(db, workspace_id, exclude_query_id)` — `(count, latest_meta)` for
  the awareness hint; `count` is uncapped.
- `get_run_todo_md(db, workspace_id, query_id)` — one run's full `todo_md` or `None`.
- `get_run_artifacts_by_query_id(db, workspace_id, query_id)` — **raw** stored
  artifact dicts (with `content_b64`) for the fetch tool; workspace-scoped, returns
  `None` if the run is absent.
- `list_prior_artifact_manifest(db, workspace_id, exclude_query_id)` — content-free
  cross-run artifact manifest, newest run first.

### `orchestrator.py`

- `PlannerDeps` extended with optional `subdir_path` and `current_query_id` (defaulted
  so the replan/axis-append agents still construct it with just `workspace_name`).
- New `_register_history_tools(agent)` defining the four tools above; called for
  `plannerAgent` only. Existing `_register_lookup_tools` (report lookup) unchanged.
- Added imports: `base64`, `pathlib.Path`, `uuid.UUID`, and the five db helpers.

### `api/routes/chat.py`

- **Removed** `_build_message_history_from_prior_runs` and the `message_history=`
  push on the initial planner call.
- **Removed** the now-unused `from pydantic_ai.messages import …` import.
- **Added** `_format_prior_runs_hint(workspace_id, current_query_id)`.
- `_with_planner_workspace_context(...)` now takes `current_query_id` and includes the
  hint between the ready-docs inventory and the planner request.
- Both planner invocations (initial planning in `planner()`, and the in-loop HITL
  feedback re-plan) now pass the hint and construct `PlannerDeps` with
  `subdir_path=run.workspace` + `current_query_id`. (The HITL feedback call still
  passes `message_history=thisRun.planner_messages` — that is **within-run**
  continuity, intentionally separate from cross-run history.)
- **Reordered** `create_chat`: subdir creation + eager restore now happen **before**
  `thisRun.plan = await planner(...)`.
- **Kept** the most-recent-run eager restore (`get_latest_prior_run_artifacts` +
  `_restore_artifacts`).

### `system_prompts.py` (planner prompt — "Continuing from a previous run")

- Replaced "their todo.md … appears in your conversation history" with an explicit
  description of the four pull tools and **when** to use them (only for genuine
  continuations; never for self-contained new requests).
- Clarified the critical mechanic: the **most recent** run's files are already in
  `outputs/`; an **older** run's file must be brought in with `fetch_prior_artifact`
  first.
- Updated the continuation bullets and the `document_answering` PDF-ingestion
  EXCEPTION to cover restored **or fetched** files.

---

## What did NOT change

- The replanner (`replanAgent`) gets no history and none of the new tools.
- Sub-agent subdir sandboxing.
- Eager restore of the most recent run.
- The `workspace_runs` schema (no new table; no migration).
- Within-run planner continuity for HITL feedback (`thisRun.planner_messages`).

---

## Verification

- `db.utils`, `orchestrator`, `system_prompts`, and `api.routes.chat` all import and
  `py_compile` cleanly.
- Tool registration confirmed by introspection:
  - `plannerAgent`: `fetch_prior_artifact`, `fetch_report_ids`, `get_run_todo`,
    `list_prior_artifacts`, `list_prior_runs`
  - `replanAgent`: `fetch_report_ids` only
  - `axisAppendPlannerAgent`: `fetch_report_ids` only
- New db helpers smoke-tested against the live DB on a workspace with 3 runs:
  `count_prior_runs` → `(3, latest)`, `list_prior_runs_meta` → 3 entries (including a
  `todo_md_chars=0` entry for a run that never rendered a todo), `get_run_todo_md`
  returned the full 1680-char todo for a completed run.

---

## Notes / follow-ups

- **No budget-trimming logic was needed.** It was designed for the old pushed block;
  with pull, each tool return is naturally bounded (one run, or a metadata list).
  `list_prior_runs_meta` carries a `limit` (default 50) as a soft guard; a future
  refinement could cap a pathologically large single `get_run_todo` return.
- The planner relying on *choosing* to fetch is mitigated by (a) the always-on hint
  and (b) keeping eager restore for the common most-recent case. If continuations
  regress, the fallback is to also push a small recent-history block.