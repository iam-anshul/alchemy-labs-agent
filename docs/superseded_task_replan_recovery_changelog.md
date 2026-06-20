# Changelog — `superseded` task status for replan-recovered failures

Fixes two bugs where a run that **recovered** from a failed task (via a replan that
routed around it) was still treated as if the failure mattered: the whole run was
reported as failed, and the dead task could block downstream work.

---

## What was wrong

When an executor task fails, the control loop replans. A common, correct recovery
is for the replanner to **route around** the failed task rather than retry it — e.g.
the original plan was `t1 (browser download) → t2 (document_answering, deps=[t1])`,
`t1`'s download fails, so the replan inserts a recovery sub-chain `t3 (web_search
for direct links) → t4 (browser re-download)` and re-points `t2`'s dependency from
the dead `t1` onto `t4`.

`_merge_plan` preserves task ids and control-loop state across a replan, so the dead
`t1` stays in the merged plan as `status="failed"` even though **nothing depends on
it anymore**. That leftover `failed` task caused two problems:

1. **Reporting bug (deterministic).** The end-of-run status computation in
   `create_chat`'s `finally` was:
   ```python
   if any(t.status == "failed" for t in thisRun.plan.tasks):
       final_status = "failed"
   ```
   So even if the recovery chain (`t3 → t4 → t2`) completed and fully answered the
   user's question, the dead `t1` made the **entire run report as "failed."** This
   also poisoned future planning, because the next run's planner sees this turn's
   persisted status as failed.

2. **Scheduling fragility (latent).** The "no task is ready" sweep force-fails any
   pending task that depends on a failed task:
   ```python
   failed_ids = {t.id for t in thisRun.plan.tasks if t.status == "failed"}
   for t in thisRun.plan.tasks:
       if t.status == "pending" and any(dep in failed_ids for dep in t.deps):
           t.status = "failed"  # "upstream task failed"
   ```
   Because the dead `t1` was in `failed_ids`, any downstream task that still
   referenced it (e.g. if a replan re-pointed deps imperfectly) would be killed even
   though a valid recovery chain existed.

The root cause is that `failed` was overloaded to mean **both** "this turn failed"
**and** "this approach was abandoned but recovered" — and the control loop could not
tell them apart.

This was observed in a real run (HDFC FY2026 report): `t1` download failed, a replan
added `t3`/`t4` and re-pointed `t2` onto `t4` (which is why the answer task `t2`
appeared last with a low id), `Replans used: 1/3`. The recovery was valid, but the
run would still be marked failed.

---

## The fix

Introduce a distinct terminal status, **`"superseded"`**, for a previously-failed
task that a replan has routed around (no surviving task depends on it). It is set
only by `_merge_plan`, never by the planner, and is excluded from both the
upstream-failure propagation and the final run-status determination.

Semantics:

- A `failed` task is downgraded to `superseded` **only if no task in the merged plan
  lists it in `deps`**. A failure that downstream tasks still depend on stays
  `failed` and keeps blocking/propagating as before.
- Only `failed` is ever downgraded — `completed`/`pending`/`dispatched` are
  untouched.
- The existing "rewritten failed task → reset to pending for retry" path in
  `_merge_plan` is unaffected (that runs before the superseded pass and changes the
  status to `pending`, so it is not a candidate for downgrade).

---

## Files changed

### `formats_pydantic.py`

- Added `"superseded"` to the `TaskSpec.status` `Literal`:
  ```python
  status: Literal["pending", "dispatched", "completed", "failed", "superseded"] = "pending"
  ```
  with a comment documenting that it marks a routed-around failure set by
  `_merge_plan` and excluded from failed-run determination and upstream-failure
  propagation.

### `api/routes/chat.py`

- **`_merge_plan`** — after the existing merge loop, before `return new`, added a
  pass that collects every task id referenced in any task's `deps`, then downgrades
  any `failed` task whose id is **not** depended on to `superseded`:
  ```python
  depended_on: set[str] = set()
  for t in new.tasks:
      depended_on.update(t.deps)
  for t in new.tasks:
      if t.status == "failed" and t.id not in depended_on:
          t.status = "superseded"
  ```

- **Task-loop completion check** — `superseded` is now terminal-OK so the loop can
  recognize completion:
  ```python
  if all(task.status in ("completed", "superseded") for task in thisRun.plan.tasks):
      break
  ```

- **Upstream-failure sweep** — unchanged code, but now correct because `superseded`
  tasks are no longer in `failed_ids` (they are status `superseded`, not `failed`),
  so a downstream task is not force-failed by a routed-around ancestor. Added a
  clarifying comment.

- **Final run-status computation** (in `create_chat`'s `finally`) — the
  all-completed branch now accepts `superseded`:
  ```python
  if any(t.status == "failed" for t in thisRun.plan.tasks):
      final_status = "failed"
  elif all(t.status in ("completed", "superseded") for t in thisRun.plan.tasks):
      final_status = "completed"
  ```
  So a run whose only non-completed tasks are superseded is reported `completed`.

### `render_todo.py`

- Added a render symbol for the new status so the user sees it was abandoned (not
  failed):
  ```python
  case "superseded":
      symbol = "[-]"
  ```

---

## What did NOT change

- **No DB migration.** `workspace_runs.status` is a free-form string and `todo_md`
  is rendered text; the change is in-memory plan state + the run-status computation.
  Existing rows are unaffected.
- The planner never emits `superseded` — it is purely control-loop bookkeeping.
- Genuinely-blocking failures (downstream still depends on them) still behave exactly
  as before.

---

## Verification

Reproduced the exact scenario and asserted the fix with direct calls to `_merge_plan`
plus a mirror of the final-status logic:

- **Repro (the bug):** old plan `t1(failed) → t2(deps=t1)`; replan keeps `t1`, adds
  `t3 → t4`, re-points `t2` onto `t4`. After `_merge_plan`: `t1=superseded`,
  `t2=pending`. Completing the recovery chain yields final status **`completed`**
  (previously would have been `failed`).
- **Negative 1:** `t1(failed)` with `t2` still `deps=[t1]` (no recovery) → `t1`
  stays `failed` and keeps blocking. Not downgraded.
- **Negative 2:** a failed task the planner *rewrote* (changed approach) is reset to
  `pending` for retry, not superseded.
- `render_todo` renders a `superseded` task (`[-]`) without error.
- `api/routes/chat.py`, `formats_pydantic.py`, `render_todo.py` compile and import
  cleanly.