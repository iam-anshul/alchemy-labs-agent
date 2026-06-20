# Changelog — Show in-flight runs in the workspace "Recent runs" list

Makes a chat run visible in the workspace's "Recent runs" list **while it is still
running**, and keeps that list live without a manual page reload. Previously a run
was invisible from the workspace until it finished, even though its own run page
streamed fine.

---

## The problem

When you start a chat run and stay on the run/stream page, you see the live flow —
that worked. But if you navigated **back to the workspace page** while the run was
still going, the run did **not** appear in "Recent runs" until it completed. You had
no way to get back into a still-running run from the workspace.

### Root cause — two independent gaps

There were two separate reasons, and **both** had to be fixed:

1. **Backend: the run's DB row was created too late.**
   `create_chat` (`api/routes/chat.py`) built the run only as an in-memory Pydantic
   object at start. The `workspace_runs` row was first written either:
   - when the **first task produced an artifact** (`append_run_artifacts` creates the
     row with `status="running"`), or
   - at the **end** of the run (`register_query_run`, in the `finally` block).

   So a run that was still **planning** (no artifacts yet) had **no DB row at all**.
   The "Recent runs" list reads from `workspace_runs` (`list_workspace_runs`), so a
   still-planning run simply wasn't in the result. (This is why an older run that got
   far enough to produce artifacts showed as `running`, but a brand-new one didn't.)

2. **Frontend: the list fetched once and never refreshed.**
   `WorkspacePage` loaded documents/runs/outputs **once on mount** via `useAsyncData`
   with no polling. Even after the backend started writing a `running` row, the list
   wouldn't reflect a newly-started run (or a run flipping running → completed)
   without a manual reload.

A related fact that shaped the fix: the run-detail page (`RunPage`) **already**
opens the live SSE stream when a run's `status === "running"`. So once a running run
is listed and clickable, opening it reconnects to the live flow automatically — no
extra work needed for that part.

---

## The fix

### 1. Backend — persist a `running` row at run start

In `create_chat`, immediately after building the in-memory `thisRun`, upsert it into
`workspace_runs` with `status="running"` so the run is listable from the moment it
starts.

- Reuses the existing `db_utils.register_query_run(db, run=thisRun, final_todo=None)`
  — it is an upsert keyed on `query_id`, so the end-of-run call later overwrites the
  same row with the final goal/workspace/todo/status. No new DB helper, no schema
  change, no migration.
- **Safe against the "values not ready yet" concern:** every NOT NULL column on
  `workspace_runs` (`user_query`, `goal`, `workspace`, `started_at`, `workspace_id`,
  `query_id`, `user_id`, `status`, `query_counter`) already has a value on `thisRun`
  at this point. The not-yet-known columns (`todo_md`, `produced_artifacts`) are
  nullable, so they insert as NULL. `started_at` (a `time()` float) is coerced to a
  timezone-aware `datetime` by the Pydantic model before it reaches the DB.
- **Treated as REQUIRED (hard-fail), but cleanly.** If this write fails the run is
  aborted rather than run blind. Because this insert is OUTSIDE the main
  `try/finally`, a bare `raise` would have become a silent "Task exception was never
  retrieved" (the run is a fire-and-forget asyncio task). So before raising, the code
  publishes a `run_ended` / `status="failed"` event and closes the SSE channel, so
  the failure reaches the UI instead of crashing silently.

### 2. Frontend — silent polling of the workspace data

`useAsyncData` gained an optional `pollIntervalMs` argument; `WorkspacePage` passes
`5000` (5s). The list now refreshes itself so a run started elsewhere appears, and a
running run flips to completed/failed, without a reload.

- The poll is a **silent background refresh**: it does NOT toggle `isLoading` (no
  spinner flicker every tick) and does NOT clear data or surface errors on a
  transient poll failure — a failed poll leaves the last good data in place until the
  next tick.
- Combined with backend #1 and the existing `RunPage` stream-on-`running` behavior,
  the full loop works: start a run → go back to the workspace → see it as `running`
  within ~5s → click in → watch the full live flow.

---

## Files changed

### `api/routes/chat.py`

In `create_chat`, after constructing `thisRun` (and before `start_workers`), added a
required pre-insert:

```python
with SessionLocal() as db:
    pre_insert_err = db_utils.register_query_run(db, run=thisRun, final_todo=None)
if pre_insert_err is not None:
    await sink.publish_ui(
        "run_ended", stage="done", status="failed", message="Chat run failed",
        data={"status": "failed", "error": f"could not persist the run: {pre_insert_err}"},
    )
    bus.close(f"query:{query_id}")
    raise HTTPException(
        status_code=500,
        detail=f"Could not write running run row to the database: {pre_insert_err}",
    )
```

with a comment explaining the visibility rationale, the upsert/overwrite-at-end
behavior, the NOT NULL/nullable column safety, and the clean hard-fail.

### `frontend/src/hooks/useAsyncData.ts`

- Added optional third parameter `pollIntervalMs?: number`.
- Imported `useRef`; added a `loadRef` so the polling timer always calls the latest
  `load` closure without restarting the interval on every render.
- Added a second `useEffect` that, when `pollIntervalMs > 0`, runs `load` on a
  `setInterval` as a silent refresh (no `setIsLoading`, ignores transient errors,
  only `setData` on success). It aborts the in-flight request and clears the interval
  on cleanup.

### `frontend/src/pages/WorkspacePage.tsx`

- Passed `5000` as the new `pollIntervalMs` arg to the `useAsyncData(...)` call that
  loads documents/runs/outputs, with a comment explaining the live-refresh intent.

---

## What did NOT change

- **No DB schema change / migration.** `workspace_runs` already had a `status` column
  and nullable `todo_md`/`produced_artifacts`; we just write the row earlier.
- **`RunPage` is unchanged.** It already streams when `status === "running"`, so
  opening an in-flight run from the list shows the full live flow with no edit.
- **End-of-run persistence is unchanged.** `register_query_run` in the `finally`
  still upserts the same row to the final state.

---

## Known limitations / follow-ups

- **Completed-run history still shows only the final snapshot, not the replayed
  flow.** UI events are never persisted (they live in an in-memory bus and are
  discarded at `bus.close()`), so a *completed* run's detail page reconstructs a
  single synthetic `run_ended` event from `todo_md` + outputs. Showing the full flow
  for a completed run would require persisting the event stream (new events table,
  per-event writes incl. screenshots, a history endpoint + frontend replay) — a
  larger change deliberately deferred. The live full-flow works for **in-flight**
  runs via the existing SSE reconnect.
- **Stale `running` rows on crash.** Because the row is written at start and finalized
  in `finally`, a process that dies mid-run leaves the row stuck at `status="running"`
  forever (same as a pre-existing run already observed in this state). A startup
  sweep / reaper that marks orphaned `running` rows as `failed` is the future fix;
  accepted as a known limitation for now.
- **Polling is a fixed 5s interval** regardless of whether any run is active. Cheap
  for these small list endpoints; could later be gated to poll only while a `running`
  run is present.

---

## Verification

- `api/routes/chat.py` compiles and imports cleanly.
- Confirmed via the Pydantic model that `started_at=time()` (a float) coerces to a
  timezone-aware `datetime`, so the first DB write gets a valid `DateTime` value.
- `frontend` `tsc -b` typecheck passes with no errors.