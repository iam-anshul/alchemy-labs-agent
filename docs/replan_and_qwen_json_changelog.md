# Replan Decision + Qwen Stringified-JSON Fixes — Changelog

Two related fixes, both rooted in how the Qwen model (served over the
OpenAI-compatible provider) mangles structured output. The first reshaped the
replan flow to stop the planner dropping its task list; that reshape exposed a
deeper, systemic Qwen bug (stringified collections) which the second fix
addresses for every affected agent.

---

# Part 1 — Replan returns a decision, not a regenerated plan

## Background

This builds on the earlier empty-`todo.md` fix
([empty_todo_bug_changelog.md](empty_todo_bug_changelog.md)). Recap of that bug:
on every replan the planner regenerated the **entire** `PlanOutput`, even to say
"nothing changed," and the model occasionally collapsed that regeneration to
`tasks=[]`, which the control loop adopted and rendered as an empty todo.md.

Two fixes were applied there:
1. `PlanOutput.tasks` got `Field(min_length=1)` so an empty plan fails
   validation and pydantic-ai re-prompts.
2. Replans were moved to a **decision** model so the planner doesn't regenerate
   the plan on a no-op.

This changelog documents fix 2 (the `ReplanDecision` work) in full, including a
mid-implementation correction.

## The design: `ReplanDecision`

Instead of a replan returning a `PlanOutput` directly, it returns a
`ReplanDecision`:

- `needs_change: bool` — the planner first decides *whether* the plan needs
  revising. On the common "no change" path it emits just `needs_change=false`
  and the plan is left exactly as-is. It never regenerates the task list, so it
  can never accidentally drop it.
- Plan fields — only filled when `needs_change=true`.

### First attempt (nested) — and why it broke

The initial shape nested a full plan:

```python
class ReplanDecision(BaseModel):
    needs_change: bool
    revised_plan: PlanOutput | None = None   # nested object
```

This **crashed in testing** with:

```
ValidationError: 1 validation error for ReplanDecision
revised_plan
  Input should be an object [input_value='{"goal": "Compare delhi ...", input_type=str]
UnexpectedModelBehavior: Exceeded maximum output retries (3)
```

The Qwen model emitted the nested `revised_plan` object as a **JSON-encoded
string** rather than a real object. pydantic-ai validation rejected the string,
re-prompted, Qwen repeated, retries exhausted, the run crashed. (This is the
same root cause as Part 2 — Qwen stringifies nested/collection fields.)

### Final shape (flat)

`revised_plan` was flattened — the plan fields live at the top level of
`ReplanDecision`, so there is no nested object for the model to stringify:

```python
class ReplanDecision(BaseModel):
    needs_change: bool
    goal: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list)   # NO min_length here
    needs_user_feedback: bool = False
    feedback_question: str | None = None
    notes: str | None = None
```

`tasks` has **no** `min_length` on `ReplanDecision` (unlike `PlanOutput`),
because the no-change case legitimately leaves it empty; the emptiness check is
done in `planner()` instead.

[`formats_pydantic.py`](../formats_pydantic.py)

## Control-flow changes

[`orchestrator.py`](../orchestrator.py):
- Added a second agent, **`replanAgent`** (`output_type=ReplanDecision`), sharing
  the same `planner_system_prompt` as `plannerAgent`. Separate agent because the
  output type differs (initial planning → `PlanOutput`; replanning →
  `ReplanDecision`).
- At the time of this change, both agents registered shared document/report
  lookup tools through `_register_lookup_tools(agent)`. Current planner behavior
  injects ready document ids directly into prompt context and keeps only the
  report lookup tool.

[`api/routes/chat.py`](../api/routes/chat.py):
- `planner()` return type widened to `PlanOutput | None`. Its replan branch now
  calls `replanAgent`, and from the flat decision it returns:
  - `None` when `needs_change` is false **or** `tasks` is empty (a no-op — the
    caller leaves `run.plan` untouched and spends no replan budget), or
  - a freshly **assembled `PlanOutput`** built from the decision's flat fields
    when a genuine revision is present. (Assembling the `PlanOutput` ourselves,
    rather than letting the model emit a nested one, is what avoids the
    stringify crash.)
- Both replan call sites changed from signature-diffing to a null check:
  ```python
  new_plan = await planner(thisRun, sink)
  if new_plan is not None:
      thisRun.plan = _merge_plan(thisRun.plan, new_plan)
      thisRun.replans_used += 1
  ```

### Removed / kept

- **Removed `_plan_signature`** — it diffed two rendered plans to detect whether
  a replan changed anything. The explicit `needs_change` boolean replaces it;
  the model now tells us directly, so signature diffing is dead code.
- **Kept `_merge_plan`** — still required. When a revised full plan comes back,
  surviving task ids must carry over their control-loop-owned state
  (`status`/`produced`/`error`/`notes`); otherwise already-completed tasks would
  reset to `pending` and re-run.

## Prompt

[`system_prompts.py`](../system_prompts.py) RE-PLANNING section rewritten: on a
replan, emit `needs_change=false` and leave the other fields default when
nothing changes (do NOT re-emit the plan); only when a revision is warranted set
`needs_change=true` and fill the flat plan fields with the COMPLETE revised plan
(every task, reuse ids of tasks that already ran, never an empty `tasks` list).

---

# Part 2 — Qwen stringified-collection fix (`QwenChatModel`)

## The issue

After Part 1, a live run still crashed — this time in the **web_search agent**.
The agent searched, wrote its output file correctly, then emitted "produced 1
file(s)" in a tight loop ~8 times and failed every dispatch attempt with:

```
UnexpectedModelBehavior: Exceeded maximum output retries (3)
```

### Root cause

The web agent's finish tool is `submit(produced: list[str], notes: str)`. The
Qwen model emitted the **`produced` list argument as a JSON-encoded string**:

```json
{"produced": "[\"outputs/t1_weather_data.md\"]", "notes": "..."}
```

instead of a real array. pydantic-ai validates tool-call arguments against the
tool schema; a `str` where `list[str]` is expected fails, pydantic-ai re-prompts
the model, Qwen repeats the same stringified output, and after the retry budget
is exhausted the agent run dies. In this initial crash the file still got
written because `write_file`'s args are scalars (`str`); it was specifically the
`list[str]` argument of `submit` that triggered the loop. (The fix's own
over-correction on `write_file.content` — documented below — later showed that
scalar fields are not automatically safe either, which is why the repair ended
up schema-aware.)

This is the **same Qwen behavior** that
[`browser_agent.py`](../browser_agent.py) already documents and shims
(`_unstringify_collections`, `_pad_missing_required` inside
`QwenToolCallChatOpenAI`) — but only the browser agent had protection. The web,
office, and planner agents used a plain `OpenAIChatModel` with none, so any
list/dict tool argument (or, via tool-output mode, any structured output field)
was exposed. It is also exactly why Part 1's nested `revised_plan` crashed.

### Why it affects structured output too (planner/replan)

The model profile reports `default_structured_output_mode = "tool"`. That means
pydantic-ai realizes `output_type=PlanOutput` / `ReplanDecision` as a
**final-result tool call** — the structured output travels as tool-call
arguments. So the same stringify bug that hits `submit`'s `produced` also hits a
planner output field (the crash trace went through `tool_manager.py do_validate`,
the tool-args validation path). One fix therefore covers both tool args and
structured output under this profile.

## The fix: `QwenChatModel` (schema-aware)

New module [`qwen_compat.py`](../qwen_compat.py): a drop-in `OpenAIChatModel`
subclass that decodes Qwen's stringified collection arguments back into real
structures **before pydantic-ai validates them** — but does so **schema-aware**,
decoding only the parameters that are actually declared as collections.

### Why it must be schema-aware (the over-correction lesson)

The first version of this fix was NOT schema-aware: it decoded *any* value that
was a string looking like JSON (`[`/`{`-leading). That blind approach fixed the
`submit.produced` crash but immediately broke a different tool. A live run
revealed it via a diagnostic that printed each tool call's repaired args:

```
tool='write_file' after={'path': 'outputs/slide2_ops.json',
                          'content': [{'command': 'add', ...}, ...]}
```

`write_file(path: str, content: str)` was being called by the office agent to
write an officecli **batch ops `.json` file** — so its `content` is *correctly* a
JSON-array **string**. The blind decoder turned that string into a `list`, which
then failed the `str`-typed `content` field → `Tool 'write_file' exceeded max
retries count of 1` → the office task (t2) blocked entirely.

So the two cases are exact opposites:

| Tool param | Model sends | Schema wants | Correct action |
| --- | --- | --- | --- |
| `submit.produced` (`list[str]`) | JSON-array string | list | **decode** |
| `write_file.content` (`str`) | JSON-array string | str | **leave as string** |

A blind decoder cannot satisfy both. The repair must consult the parameter's
declared type and decode **only** when the schema expects an array/object.

### How it works

`QwenChatModel` overrides `request` (not `_process_response`): `request` receives
`model_request_parameters`, which carries every tool's JSON schema — exactly the
information `_process_response` lacked. For each response it:

1. Builds a map of `tool_name -> {parameter names typed as array/object}` from
   the tool schemas — across both the agent's `function_tools` and the
   `output_tools` (the `final_result` tool used for structured output). Union
   types like `list[str] | None` (`anyOf`/`oneOf`) are handled.
2. For each `ToolCallPart`, parses `args` to a dict if it's a JSON string, then
   unstringifies a value **only if its parameter is in that tool's
   collection-param set**. Scalar fields (`write_file.content`), already-correct
   values, and non-JSON strings are left untouched.

Because these agents use tool-output mode (`default_structured_output_mode =
"tool"`), the same path covers structured output: the planner's `tasks`
list, when stringified, is decoded; a string field inside a task is not.

## Applied to

| File | Change |
| --- | --- |
| `qwen_compat.py` | New: `QwenChatModel` (overrides `request`) + schema-aware `_repair_args` / `_collection_params` / `_decode_if_json_collection`. |
| `web_agent.py` | `OpenAIChatModel` → `QwenChatModel` (fixes the `submit.produced` crash). |
| `office_agent.py` | `OpenAIChatModel` → `QwenChatModel` (same `submit` exposure; and `write_file.content` is now correctly left as a string). |
| `orchestrator.py` | `OpenAIChatModel` → `QwenChatModel` for both planner and replan agents (structured output via tool mode). |

`browser_agent.py` was left as-is: it already has its own
`QwenToolCallChatOpenAI` wrapper covering this for its structured-output path.

## Verified live

The full query — "Compare delhi and uttarakhand's temperature for today and make
a ppt on it" — ran end to end: `t1` (web_search) produced the markdown, `t2`
(office) produced the `.pptx` via officecli, no retry-exhaustion crash, no empty
todo.md. (The temporary `[qwen_compat]` diagnostic print used to find the
`write_file` over-correction was removed in the schema-aware rewrite.)

## Verified

- Unit tests on `_repair_args`: the exact `produced`-as-stringified-list case →
  decoded to a real list; whole-args-as-JSON-string with a nested stringified
  list → fixed; already-good args → unchanged; ordinary/non-JSON strings →
  untouched; `None` args → `None`.
- `ReplanDecision` (flat) validates in both shapes (no-change; change-with-tasks)
  and has no nested `revised_plan` field.
- `PlanOutput(tasks=[])` rejected by `min_length=1`.
- All modified modules parse and import cleanly.

## Not yet verified / caveats

- **Not end-to-end tested live.** Verified at unit/parse/import level. The real
  proof is a full run (e.g. the Delhi/Uttarakhand temperature → PPT query)
  completing without the retry-exhaustion crash and without an empty todo.md.
- **Retry-exhaustion still propagates from `planner()`.** `QwenChatModel` should
  prevent the *cause*, but if the model ever exhausts retries anyway, the
  `planner()` calls in `create_chat` are not wrapped, so the exception would
  still escape uncaught. Hardening that (wrapping `planner()` like the dispatch
  path) remains open.
- `QwenChatModel` only repairs **stringified collections**. It does not pad
  missing required fields (the browser agent's `_pad_missing_required` does);
  if Qwen omits a required field entirely, validation still fails. Not observed
  for these agents yet, but a known gap vs. the browser agent's fuller shim.
