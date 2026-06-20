# Changelog — Surface browser & office executor failures in Logfire

Logs the full diagnostic for browser and office sub-agent failures to Logfire, so
the real reason a task failed is captured server-side instead of being reduced to a
short, user-safe UI message.

---

## Background

Per the event-streaming contract (`docs/event_streaming_changelog.md`), the `message`
field on every UI event is intentionally **"Short UI-safe text"**, and the frontend
timeline renders only `message` — it does not surface `data.error`. That is by
design: the UI is user-focused.

The consequence is that when a sub-agent failed, the *detailed* error was either
buried in the SSE `data.error` payload (which the UI discards) or — in one case — not
captured anywhere server-side. A run would show a generic line like "Browser agent
ended without submitting" or "Office agent failed" with no searchable record of the
actual cause (model connection error, officecli crash, request-limit exhaustion,
step errors, validation failure, etc.).

This was noticed from a UI timeline showing "Browser agent ended without submitting"
with no further detail; the underlying error text existed in `data.error` but was not
visible to the user and not logged.

### Design decision

Do **not** show the detailed error in the UI (the UI stays user-focused, consistent
with the streaming contract). Instead, send the full diagnostic to **Logfire**, where
it is searchable and tied to the run via structured attributes.

---

## What was wrong, per agent

### `browser_agent.py`

Two failure exits, both publishing only a short UI `message` with the real error in
`data.error` (UI-discarded), and **neither logging to Logfire**:

1. **Run-loop exception** (`run()` `try/except` around `browser_agent.run(...)`) —
   message "Browser agent failed".
2. **No structured submission** (final_result missing/invalid, or submission-validation
   failure) — message "Browser agent ended without submitting", with the rich
   `_failure_detail(history)` output (final_result + last step errors) only in
   `data.error`.

### `office_agent.py`

One failure exit, and **worse than the browser case**: the agent-loop `except` in
`run_office_executor` caught the exception into `result.error` and returned it — with
**no `logfire` call and no `publish_ui` at all** at that point. The full exception was
invisible server-side; only the control loop's generic "Office agent failed" message
reached the UI. (The office agent's empty/invalid-output rejection is handled
separately inside the agent loop via the `@output_validator` → `ModelRetry`, so there
is no separate "ended without submitting" branch to instrument.)

---

## Change

Add a `logfire.error(...)` at each failure exit, carrying the full diagnostic plus
run context as structured attributes. The UI events are left exactly as they were.

---

## Files changed

### `browser_agent.py`

- **Run-loop exception branch** — added before the existing `publish_ui`:
  ```python
  logfire.error(
      "Browser agent run loop failed",
      task_id=task_id, attempt=attempt, query=query,
      error_class=type(e).__name__, error=error,
  )
  ```
- **No-submission branch** — added before the existing `publish_ui`:
  ```python
  logfire.error(
      "Browser agent ended without submitting",
      task_id=task_id, attempt=attempt, query=query,
      error_class=type(e).__name__, failure_detail=detail,
  )
  ```
  (`detail` is the existing `_failure_detail(history)` output plus any validation
  message — final_result and the last few step errors.)

`logfire` was already imported in this module.

### `office_agent.py`

- Added `import logfire`.
- **Agent-loop `except`** in `run_office_executor` — captured the error string and
  logged it before returning:
  ```python
  error = f"Agent loop failed: {type(e).__name__}: {e}"
  logfire.error(
      "Office agent loop failed",
      query=query, expects=expects,
      error_class=type(e).__name__, error=error,
  )
  ```

---

## What did NOT change

- **No frontend changes.** The UI continues to render only the short `message`, by
  design. `data.error` is still sent on the events as before.
- The `ExecutorResult.error` returned to the control loop is unchanged, so the
  planner's failure-aware replanning still receives the same error text.
- Office agent failure context is limited to `query`/`expects`/`error_class`/`error`
  because `run_office_executor` does not currently receive `task_id`/`attempt` (the
  browser executor does). Threading those through for parity is a possible follow-up
  (it would touch `dispatch_executor_agent` in `api/routes/chat.py`).

---

## Verification

- `browser_agent.py` and `office_agent.py` compile and import cleanly.

---

## Notes / follow-ups

- For full attribute parity, thread `task_id`/`attempt` into `run_office_executor` so
  the office log can be tied to a specific task/attempt the way the browser log is.