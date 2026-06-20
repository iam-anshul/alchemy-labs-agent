# Changelog — Fix duplicate `todo.md` artifact on every plan

Every "Planning complete" entry in the run timeline showed **two identical
`todo.md` chips**. This removes the duplicate.

---

## What was wrong

`publish_todo_artifact` in `api/routes/chat.py` emits two UI events per planning
phase, and it was attaching the **same `todo.md` artifact to both**:

1. `artifact_ready` — message "Plan file is ready", `artifacts=[artifact]`.
2. `agent_ended` — message "Planning complete", **also** `artifacts=[artifact]`.

The frontend timeline groups a phase's events and unions their artifacts with **no
de-duplication** (`EventTimeline.tsx`):

```ts
const artifacts = group.events.flatMap((groupEvent) => groupEvent.artifacts);
```

So the one `todo.md`, attached to two events in the same group, rendered as two chips
on every plan/replan. This is purely a display duplication — only one `todo.md` file
ever existed on disk and in the run state.

---

## The fix

Stop re-attaching the artifact to the `agent_ended` event. `artifact_ready` is the
canonical artifact-delivery event; `agent_ended` is the phase status/completion
signal and does not need to carry the file. The artifact is now delivered exactly
once.

The `agent_ended` event keeps its `data` payload (`phase`, `n_tasks`,
`needs_user_feedback`) — only the redundant `artifacts=[artifact]` is removed.

---

## Files changed

### `api/routes/chat.py`

In `publish_todo_artifact`, removed `artifacts=[artifact]` from the `agent_ended`
`publish_ui(...)` call (kept it on the preceding `artifact_ready` call), with a
comment explaining the UI's no-dedupe artifact union. `publish_ui`'s `artifacts`
parameter defaults to `None`, so omitting it is safe.

Applies to both the `planning` and `replanning` phases, since both go through this
single function.

---

## What did NOT change

- **No frontend changes.** The fix is backend-only; the timeline's flatMap-union
  behavior is left as-is.
- Only the duplicate chip is removed — the `todo.md` is still delivered (once) via
  `artifact_ready`, and the `agent_ended` status data is unchanged.

---

## Verification

- `api/routes/chat.py` compiles and imports cleanly.
- Confirmed in the frontend that artifacts per timeline group come from
  `group.events.flatMap(e => e.artifacts)` with no de-duplication, so removing the
  artifact from one of the two same-group events yields exactly one chip.