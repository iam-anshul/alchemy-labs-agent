# Structured Output Tool Mode Changelog

Date: 2026-06-14

## Summary

The agents no longer use an ordinary `submit` function tool plus mutable
dependency state to return their final result. Pydantic AI agents now expose
explicit, named terminal output schemas in tool mode and read the validated
result from `AgentRunResult.output`.

This follows Pydantic AI's output contract:

- `ToolOutput(...)` registers a special output tool with a chosen name.
- Because `str` is not an allowed output type, a plain-text response cannot
  successfully end the run.
- Calling the output tool is terminal. Its result is validated and returned to
  the caller rather than passed back to the model like a normal function tool.

Reference:
[Pydantic AI Tool Output](https://pydantic.dev/docs/ai/core-concepts/output/#tool-output).

## Executor Migration

### Web search

[`web_agent.py`](../web_agent.py):

- Added `WebSubmission(produced, notes)`.
- Changed `theWebAgent` to
  `Agent[WebDeps, WebSubmission]` with
  `ToolOutput(WebSubmission, name="submit")`.
- Removed `WebDeps.submitted` and the registered `submit` function tool.
- `run_web_executor` now returns the validated `run_result.output`.
- Added an output validator that checks every produced path is inside the
  workspace, exists, and is non-empty. Invalid output raises `ModelRetry`, so
  the model can correct its terminal payload within the output retry budget.

### Office

[`office_agent.py`](../office_agent.py):

- Added `OfficeSubmission(produced, notes)`.
- Changed `theOfficeAgent` to
  `Agent[OfficeDeps, OfficeSubmission]` with
  `ToolOutput(OfficeSubmission, name="submit")`.
- Removed `OfficeDeps.submitted` and the registered `submit` function tool.
- `run_office_executor` now returns the validated `run_result.output`.
- Added the same workspace, existence, and non-empty output validation with
  `ModelRetry`.

### Browser

[`browser_agent.py`](../browser_agent.py) uses `browser-use`, not Pydantic AI,
so it uses that framework's equivalent structured terminal mechanism:

- Added `BrowserSubmission(produced, notes)`.
- Changed tool construction to `Tools(output_model=BrowserSubmission)`.
- Removed `BrowserExecutor._submitted` and the custom `submit` action.
- The built-in terminal `done` action now carries the structured result.
- `BrowserExecutor.run` parses `history.final_result()`, validates the produced
  files, and returns an `ExecutorResult`.

## Explicit Tool Mode Names

Agents that already returned Pydantic models were also made explicit rather
than relying on Pydantic AI's default tool-output behavior:

| Agent | Output tool name |
| --- | --- |
| Initial planner | `submit_plan` |
| Replanner | `submit_replan_decision` |
| Document router | `submit_document_route` |
| Table analysis | `submit_table_analysis` |
| Document answer | `submit_document_answer` |
| Report outline | `submit_report_outline` |
| Report section | `submit_section_draft` |
| Report critic | `submit_report_critique` |
| Executive summary | `submit_executive_summary` |

Implemented in [`orchestrator.py`](../orchestrator.py),
[`agent.py`](../agent.py), and [`report.py`](../report.py).

## Event Sink Lifecycle

Removing the normal `submit` tool also removed the old place where final
artifact events were emitted. Final sink publication now happens after the
structured output has been validated:

- Output validators are side-effect free. They only normalize paths and raise
  `ModelRetry` for invalid output.
- `run_web_executor` publishes one consolidated `artifact_ready` event after
  `Agent.run()` returns successfully.
- `run_office_executor` now publishes the same consolidated final artifact
  event after successful validation.
- Browser output discovery runs only after its terminal result and produced
  paths validate successfully.
- Per-file `write_file` events still stream during execution.
- The outer dispatcher in [`api/routes/chat.py`](../api/routes/chat.py) remains
  responsible for web and office `agent_started` / `agent_ended` events, which
  avoids duplicate completion events. Browser continues to own its lifecycle
  events internally.

Text artifacts (`.md`, `.txt`, `.csv`, `.json`) include inline content in the
consolidated event. Binary artifacts retain metadata and use the persisted
output route for preview or download.

## Prompt Updates

[`system_prompts.py`](../system_prompts.py) and the executor task prompts now
describe `submit` / `done` as terminal structured output, not ordinary tools
whose results return to the model.

## Why This Fixes The Failure

Previously, a Pydantic AI executor had the default `str` output type and a
normal `submit` tool. The model could therefore finish with plain text without
calling `submit`; the wrapper then reported:

```text
Agent finished without calling submit
```

With `ToolOutput` and no text output type, plain text cannot successfully end
the run. The model must produce the named terminal output schema, and the
caller receives it directly from `run_result.output`.

## Verification

- `python -m py_compile` passes for all changed Python modules.
- `git diff --check` passes.
- A Pydantic AI `TestModel` smoke test confirmed a named `ToolOutput` returns
  the expected Pydantic model.
- Web and office sink smoke tests confirmed one consolidated
  post-validation `artifact_ready` event with produced paths and inline text.
- Existing workspace-output unit tests currently fail on unrelated
  UUID/datetime-to-string validation mismatches.
- The locally installed `browser-use` package is older than the API imported by
  this repository, so a full browser runtime test was not available in this
  environment.

## Files Changed

| File | Change |
| --- | --- |
| `web_agent.py` | Named terminal schema, output validation, post-validation artifact publication. |
| `office_agent.py` | Named terminal schema, output validation, consolidated artifact publication. |
| `browser_agent.py` | Structured browser-use terminal output and post-validation artifact publication. |
| `orchestrator.py` | Explicit named planner and replanner output tools. |
| `agent.py` | Explicit named document pipeline output tools. |
| `report.py` | Explicit named report pipeline output tools. |
| `system_prompts.py` | Terminal structured-output terminology and instructions. |
