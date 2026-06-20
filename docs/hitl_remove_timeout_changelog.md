# Changelog — Remove the human-in-the-loop (HITL) input timeout

Makes both human-in-the-loop waits in the chat control loop block **indefinitely**
until the user answers, instead of timing out after 5 minutes. This also fixes a
latent state-consistency bug on the planner feedback loop.

---

## Background

The chat control loop (`api/routes/chat.py`, inside `create_chat`) pauses for user
input at two points:

1. **Planner feedback (clarification).** When the planner emits a plan with
   `needs_user_feedback=True`, the loop publishes an `awaiting_user_input` event
   and waits for the user's answer, then re-runs the planner with that answer.
   This is a `while thisRun.plan.needs_user_feedback:` loop.
2. **Task-level HITL.** When a task has `human_in_the_loop=True` and a
   `query_for_human_in_the_loop`, the loop waits for the user's response after the
   task runs, then optionally re-runs the task with that feedback appended.

Both waits used:

```python
answer = await asyncio.wait_for(future, timeout=USER_FEEDBACK_TIMEOUT_SECONDS)  # 300s
```

and both had an `except asyncio.TimeoutError:` branch that published a
"User input timed out" progress event.

### The latent bug (planner feedback loop)

The planner-feedback loop's exit condition is re-evaluated against a **freshly
replaced** plan object each iteration (`thisRun.plan = planner_run.output`). On the
normal path this is correct: the new plan's `needs_user_feedback` defaults to
`False` (see `formats_pydantic.PlanOutput`), so the loop exits cleanly with no
manual flag reset needed.

But the **timeout branch** did `break` **without** changing `thisRun.plan`. So on a
feedback timeout the loop exited with `thisRun.plan.needs_user_feedback` still
`True`. Execution then fell through into the task-execution loop and ran a plan that
had explicitly declared it was not runnable without user input — and that stale
`True` was subsequently persisted by `render_todo` / `register_query_run` in the
`finally` block, leaving a stored run permanently marked "awaiting input".

(The task-level HITL timeout did not have this exact fall-through — its downstream
`if human_answer is not None:` guard already handled a missing answer — but it
shared the same "give up after 5 minutes" behavior.)

---

## Change

Remove the timeout from **both** HITL waits. Each now does a plain
`answer = await future`, parking the run until the user responds. The
`except asyncio.TimeoutError:` branches and the now-unused
`USER_FEEDBACK_TIMEOUT_SECONDS` constant are deleted.

Why this also fixes the bug: with no timeout there is no `break` path out of the
planner-feedback loop. The **only** way out is the planner returning a plan whose
`needs_user_feedback` is `False` — so the stale-flag fall-through is structurally
impossible. No explicit `needs_user_feedback = False` reset is needed (and adding
one would be wrong: it would break legitimate multi-turn clarification, where the
planner may need to ask a second question after hearing the first answer).

The `finally: _pending_input.pop(...)` cleanup is preserved in both places.

---

## Files changed

### `api/routes/chat.py`

- **Removed** the module constant `USER_FEEDBACK_TIMEOUT_SECONDS = 300`.
- **Planner feedback wait** (the `while thisRun.plan.needs_user_feedback:` loop):
  replaced `asyncio.wait_for(future, timeout=...)` + its `except asyncio.TimeoutError:`
  block with `answer = await future`. Added a comment documenting the
  wait-indefinitely behavior and why the loop's exit state is now always consistent.
- **Task-level HITL wait** (`if task.human_in_the_loop and task.query_for_human_in_the_loop:`):
  replaced `asyncio.wait_for(future, timeout=...)` + its `except asyncio.TimeoutError:`
  block with `human_answer = await future`. `human_answer` is therefore always set
  by the time the downstream re-run logic reads it.

No other files touched.

---

## Behavior change & accepted trade-off

- **Before:** a HITL prompt unanswered for 5 minutes timed out; the planner loop
  then proceeded with a stale `needs_user_feedback=True` plan (bug), and the
  task-level HITL proceeded with no feedback.
- **After:** the run blocks until the user answers. There is no timeout.

**Accepted limitation (by design decision):** a run parked on an unanswered HITL
question holds its asyncio task, DB sessions, the SSE channel, and the ingest
workers open **indefinitely** if the user abandons it (e.g. closes the tab and
never responds). This is a known resource-leak-on-abandonment trade-off, accepted
for now. A future improvement could cancel the parked run when the SSE client
disconnects.

---

## Verification

- `api/routes/chat.py` imports and compiles cleanly.
- Confirmed no remaining references to `USER_FEEDBACK_TIMEOUT_SECONDS`,
  `asyncio.wait_for`, or `asyncio.TimeoutError` in the file.

---

## Notes / follow-ups

- Abandonment cleanup (cancel-on-SSE-disconnect) is the natural follow-up if parked
  runs become a resource concern.