# Empty `todo.md` Bug — Root Cause & Fix

## Symptom

During a run, `todo.md` is written many times (normal). Intermittently it got
overwritten to a degenerate form — header present but the **Tasks section
empty** — and sometimes the run ended in that state:

```
# Goal
Compare delhi and uttarakhand's temperature for today and make a ppt on it

# Workspace
/Users/anshul/agentic-rag/file_system_root/new/1_2e1e57cc-...

# Status
Started: 2026-06-13T20:02:22...
Replans used: 3 / 3

# Tasks

# Notes
(none)
```

Sometimes a later write repaired it; sometimes it stayed broken.

## Root cause

`render_todo` ([render_todo.py](../render_todo.py)) loops `for task in
run.plan.tasks`. With an **empty `tasks` list** it appends nothing, producing
exactly the broken output above — byte for byte.

So the real question was: why was `thisRun.plan.tasks` ever empty?

Two contributing factors:

1. **The schema permitted it.** `PlanOutput.tasks` was a bare `list[TaskSpec]`
   with no minimum length, so `tasks=[]` was a *valid* `PlanOutput`. Nothing
   forced the model to produce at least one task.

2. **The replan design invited it.** On every replan the planner had to
   regenerate the **entire** plan as a `PlanOutput`, even to say "nothing
   changed" (the prompt said "otherwise return the plan unchanged"). A
   structured-output model — especially the Qwen tool-call path used here, which
   is already known to drop/mangle fields — occasionally collapsed that
   regeneration to `tasks=[]`. The control loop then adopted it wholesale:

   ```python
   new_plan = await planner(...)
   if _plan_signature(new_plan) != _plan_signature(thisRun.plan):
       thisRun.plan = _merge_plan(thisRun.plan, new_plan)   # adopts empty plan
   write_todo_atomic(thisRun)                               # renders empty Tasks
   ```

This is why it only manifested mid/late run (during replans, not initial
planning) and why `Replans used: 3 / 3` accompanied the broken file. "Sometimes
self-heals" = a later replan returned a non-empty plan; "stays broken" = the
empty plan was the last one adopted with no budget left to recover.

## Fix

Two changes, complementary — one removes the *reason* the model emits empty on
replans, the other is a validation-layer safety net.

### 1. `tasks` is required (min_length=1)

[`formats_pydantic.py`](../formats_pydantic.py):

```python
tasks: list[TaskSpec] = Field(min_length=1)
```

An empty plan now fails pydantic-ai validation, so the agent (`retries=3`)
re-prompts the model with the error instead of letting `tasks=[]` through. A
zero-task plan is never legitimate, so rejecting it is correct, not a band-aid.

### 2. Replan returns a decision, not a regenerated plan

New schema `ReplanDecision` ([`formats_pydantic.py`](../formats_pydantic.py)):

```python
class ReplanDecision(BaseModel):
    needs_change: bool
    revised_plan: PlanOutput | None = None   # required only when needs_change is True
```

On the common "no change" replan the model emits just `needs_change=false` and
**never regenerates the task list** — so it cannot accidentally drop it. The
full plan is re-emitted only when a revision is genuinely intended (and even
then `min_length=1` guards it).

- [`orchestrator.py`](../orchestrator.py): added `replanAgent`
  (`output_type=ReplanDecision`, same `planner_system_prompt`). At the time,
  both agents shared document/report lookup tools via a `_register_lookup_tools`
  helper. Current planner behavior injects ready document ids directly into
  prompt context and keeps only the report lookup tool.
- [`api/routes/chat.py`](../api/routes/chat.py) `planner()`: the replan branch
  calls `replanAgent`, interprets the decision, and returns `PlanOutput | None`
  — `None` when nothing changed (caller leaves `run.plan` untouched and spends
  no replan budget), the full revised plan otherwise. Return type widened to
  `PlanOutput | None`.
- [`system_prompts.py`](../system_prompts.py): RE-PLANNING section rewritten to
  describe the `ReplanDecision` output (emit `needs_change=false` and do NOT
  re-emit the plan when unchanged; full plan with reused task ids only when
  changing).

### Call-site updates and cleanup

Both replan call sites in `create_chat` changed from signature-diffing to a
null check:

```python
new_plan = await planner(thisRun, sink)
if new_plan is not None:
    thisRun.plan = _merge_plan(thisRun.plan, new_plan)
    thisRun.replans_used += 1
```

- **Removed `_plan_signature`** — the explicit `needs_change` boolean replaces
  it; "did the plan change?" is now answered by the model directly, not by
  diffing two rendered signatures.
- **Kept `_merge_plan`** — still required. When a revised full plan comes back,
  surviving task ids must carry over their control-loop-owned state
  (status/produced/error/notes); otherwise already-completed tasks would reset
  to pending and re-run.

## Why each proposed change was / wasn't used

During design three changes were considered:

- **Make `tasks` required** → used (fix 1). Direct.
- **Send the full current `todo.md` to the planner on replan** → NOT needed: the
  replan prompt already embeds the entire rendered `todo.md` (`current_todo` +
  failure section). The model already had full context; missing context was
  never the cause.
- **Boolean "needs change" + full plan only when changing** → used (fix 2). This
  is the architectural fix that targets the actual trigger (forced
  regeneration on no-op replans).

## Files touched

| File | Change |
| --- | --- |
| `formats_pydantic.py` | `tasks` min_length=1; new `ReplanDecision` |
| `orchestrator.py` | new `replanAgent`; shared `_register_lookup_tools` |
| `api/routes/chat.py` | `planner()` replan path → `ReplanDecision`, returns `PlanOutput \| None`; call sites use null check; removed `_plan_signature`; kept `_merge_plan` |
| `system_prompts.py` | RE-PLANNING section describes `ReplanDecision` |

## Verified

- `PlanOutput(tasks=[])` raises `ValidationError`; both `ReplanDecision` shapes
  (no-change, and change-with-plan) validate.
- All four files parse and import cleanly; no stray `_plan_signature` refs.

## Not yet done / caveats

- **Not live-tested.** Verified at schema/parse/import level only. Real proof:
  run the temperature-PPT query several times and confirm `todo.md` never goes
  empty across replans.
- **Retry exhaustion**: if the model returns an empty/invalid plan 3× in a row,
  pydantic-ai raises `UnexpectedModelBehavior` out of `planner()`. The
  `planner()` calls are not wrapped, so a *persistent* failure would propagate
  rather than degrade gracefully. Rare (three straight invalid generations), and
  separate from this bug, but open.
