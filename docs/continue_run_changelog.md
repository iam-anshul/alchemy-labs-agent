# Continue / Resume Run — Changelog

Implements the **copy approach** for "continue the work" requests: a new run can
seed its workspace from the most recent prior run's produced files, so the
planner can build on earlier work by path instead of via cross-run task deps
(which the control loop cannot resolve — they caused a `KeyError('t1')` crash).

Storage decisions:
- Produced files are persisted in the DB as a **JSON column on `workspace_runs`**
  (not a separate table).
- Persisted **incrementally, as each task completes**, so the work survives a
  mid-run crash (the "server internet went down" case).
- A continue run seeds from the **most recent prior run** in the same workspace.

---

## Changes

### 1. Schema: `produced_artifacts` JSON column on `workspace_runs`

[`db/models/models.py`](../db/models/models.py) — `QueryRun` model.

Added:
```python
produced_artifacts: Mapped[list | None] = mapped_column(JSONB)
```

Each entry is one produced file:
`{"rel_path": "outputs/t1.pdf", "content_b64": "<base64>", "bytes": N, "task_id": "t1"}`.

Nullable so existing rows remain valid. Stores file bytes as base64 inside JSON
so binaries (PDF/pptx/xlsx) round-trip through the JSON column.

### 2. Migration

[`alembic/versions/b2c3d4e5f6a7_add_produced_artifacts_to_runs.py`](../alembic/versions/b2c3d4e5f6a7_add_produced_artifacts_to_runs.py)
— adds the nullable `produced_artifacts` JSONB column to `workspace_runs`,
chained off head `a1f2c3d4e5b6`. Run with `alembic upgrade head`.

### 3. DB helpers

[`db/utils.py`](../db/utils.py):

- **`append_run_artifacts(...)`** — appends a completed task's produced files to
  the run's `produced_artifacts`, creating the run row if it doesn't exist yet.
  De-dups by `rel_path` (a task re-run overwrites its earlier entry). Reassigns
  the list (not in-place) so SQLAlchemy detects the JSONB change. This is what
  makes mid-run work durable before the end-of-run write.
- **`get_latest_prior_run_artifacts(workspace_id, exclude_query_id)`** — returns
  the most recent prior run's artifacts (for seeding a continuation run).
- **`register_query_run(...)`** — changed from insert-only to **upsert**: since
  the row may already exist (created mid-run by `append_run_artifacts`), it now
  updates the existing row instead of colliding on the `query_id` PK. It does
  not touch `produced_artifacts` (written incrementally).

### 4. Control loop wiring

[`api/routes/chat.py`](../api/routes/chat.py) (`create_chat`), plus two file
helpers (`_read_artifacts`, `_restore_artifacts`) and `import base64`.

- **Seed from prior run** (after the run subdir is created): fetch the latest
  prior run's artifacts via `get_latest_prior_run_artifacts` and write them into
  this run's `outputs/` with `_restore_artifacts` (base64 → bytes, never
  overwrites). Emits a `resuming` progress event. No-op on a workspace's first
  run.
- **Persist incrementally** (when a task is marked `completed`): read its
  produced files with `_read_artifacts` (base64-encoded, binary-safe) and call
  `append_run_artifacts`. Best-effort — a persistence error is logged, not
  fatal. This is what makes mid-run work survive a later crash.
- **Dangling-dep guard** (top of the execution loop): before the readiness scan,
  any pending task whose `deps` reference an id not in this plan is marked
  `failed` with a clear message (instead of `KeyError`-crashing the run at
  `tasks_by_id[dep]`). Triggers a replan so the planner can rewrite the task to
  use the restored files by path. This is the direct fix for the
  `KeyError('t1')` crash from a "continue" request.

## How a "continue the work" run now flows

1. Run A finishes (or crashes) — each completed task's files were already saved
   to `workspace_runs.produced_artifacts` as it finished.
2. User sends "continue the work" → new run B in a fresh subdir.
3. B restores A's artifacts into `B/outputs/` before executing.
4. If the planner still emits a cross-run dep (`deps: [t1]`), the guard fails
   that task cleanly and replans rather than crashing; the planner is told the
   prior files are in `outputs/` and to reference them by path.

## Migration / ops

- Run `alembic upgrade head` to add the `produced_artifacts` column.
- Storage note: artifacts are base64 in a JSONB column, so large binaries
  (PDFs, pptx) inflate the run row. Acceptable for now per the chosen design;
  revisit if rows get large.

### 5. Planner prompt

[`system_prompts.py`](../system_prompts.py), `planner_system_prompt`:

- New section **"Continuing from a previous run"** (after the INITIAL/RE-PLANNING
  modes): tells the planner that (a) it can recognize continuation requests, (b)
  the prior run's produced files are ALREADY restored into this run's `outputs/`,
  (c) it must produce a self-contained plan that references those files **by
  path with empty `deps`**, and (d) it must NEVER put a prior run's task id in
  `deps`. Also: don't re-do work already done.
- Cross-reference added to the **`deps`** field description: deps may only name
  tasks in THIS plan; a restored file is used by path, not via a dep.

## Known limitation: restored PDFs + document_answering

Discovered while writing the prompt. `dep_files` (what the loop ingests via
`ingest_dep_pdfs`) is built ONLY from a task's declared `deps`
(`tasks_by_id[dep].produced`). A **restored** prior-run file lives in `outputs/`
but is not any in-plan task's output, so it is **never ingested**. Therefore:

- ✅ Restored **text/markdown/CSV/pptx** files work for `office`/`web_search`
  continuation tasks — those executors read any workspace path directly.
- ✗ A restored **PDF** that a `document_answering` task must ingest will NOT be
  picked up (empty deps → not in `dep_files` → not ingested → "no documents
  found").

Mitigation (prompt-level): the prompt tells the planner that for a doc task
continuing over a previously-downloaded PDF, it should **re-obtain the PDF with a
`browser` task this run** and depend on that (normal download-then-answer), not
rely on the restored copy. The failing case that motivated this work (HDFC/ICICI
→ `office` PPT reading restored research markdown) is in the ✅ category.

A fuller fix (out of scope here) would let the loop feed restored PDFs into
`ingest_dep_pdfs` even without a declared dep — e.g. ingest restored `.pdf`
files at seed time, or let a task opt into "ingest this restored path".
