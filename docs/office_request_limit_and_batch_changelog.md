# Changelog — Office agent: request-limit crash + correct batch recipe

This is the **third** changelog for the office-agent empty/failed-PPTX saga. The
previous round (`office_empty_pptx_followup_changelog.md`) added a content
validator, a `_PPTX_RECIPE`, and forced skill-loading. A live 15-slide run then
**crashed**, and the crash exposed that part of that recipe was actively wrong.

---

## What happened

A 15-slide deck build died with:

```
pydantic_ai.exceptions.UsageLimitExceeded: The next request would exceed the request_limit of 50
```

Two distinct problems, both traceable to the previous round's recipe.

### Problem 1 — the recipe forbade `batch`, which caused the crash

The previous `_PPTX_RECIPE` told the agent to **avoid `batch`** and add text
shapes "ONE AT A TIME" with individual `officecli add` calls. Every officecli
call is a separate model turn (tool call → response). A 15-slide deck at ~3–5
shapes per slide is 60–100+ tool calls. pydantic-ai's **default `request_limit`
is 50**, and `office_agent.run()` was called without overriding it — so the run
was killed about two-thirds of the way through the build.

### Problem 2 — `batch` was never broken; the earlier diagnosis was wrong

The previous round claimed `batch`'s op schema was "error-prone" and a failed
batch was the empty-deck cause. That was a misdiagnosis caused by testing `batch`
with the **wrong JSON shape** (geometry fields like `x`/`y`/`width`/`height` at
the top level of the op, or `type` misplaced).

The loaded `pitch-deck` / `pptx` SKILL.md uses `batch` heredocs as the *primary*
build method, with this schema (now grep-verified to work):

```json
{"command":"add","parent":"/slide[1]","type":"shape",
 "props":{"text":"Title","x":"2cm","y":"5cm","width":"29cm","height":"3cm",
          "font":"Georgia","size":"44","bold":"true","color":"FFFFFF"}}
```

i.e. `command` / `parent` / `type` at the **top level**, and text + geometry
under **`props`**. With this shape, every op reports `success: true` and the
content lands on disk. So the previous recipe contradicted the very skill it was
also injecting into the prompt — and the contradiction (avoid batch → one add
per shape) is exactly what blew the request budget.

---

## The fix (`office_agent.py`, `config.py`)

### 1. Rewrote `_PPTX_RECIPE` to USE `batch` with the correct schema

- Build slides with `batch` — **one batch call per slide (or per few slides)**,
  not one officecli call per shape. This is both correct and request-efficient.
- Documented the verified op schema (top-level `command`/`parent`/`type`,
  everything else under `props`, explicit geometry on every text shape).
- Listed the mistakes that make ops fail (geometry at top level, missing `type`,
  `prop` vs `props`) and said: if batch returns `success:false`, read the per-op
  error and fix the JSON — do **not** fall back to one-add-per-shape.
- Kept the verify-before-submit step (`officecli view <file> stats`, confirm
  `slides` and `words` > 0).

### 2. Raised the office agent's request limit

- New config setting `agent_office_request_limit: int = 200` (`config.py`) —
  generous headroom over the pydantic-ai default of 50.
- `run_office_executor` now passes `usage_limits=UsageLimits(request_limit=...)`
  to `theOfficeAgent.run(...)` (previously it passed nothing, inheriting the
  default 50). Added imports for `UsageLimits` and `get_settings`.

The content validator, skill force-loading, and the resident-flush fix from the
prior rounds are all **kept** — they remain correct and necessary.

---

## Files changed

- `office_agent.py` — rewrote `_PPTX_RECIPE` (use batch, correct schema); pass
  `usage_limits` into `theOfficeAgent.run`; import `UsageLimits` + `get_settings`.
- `config.py` — added `agent_office_request_limit` (default 200).

---

## Verification

- **Correct batch schema works:** a batch with `command/parent/type` + `props`
  → all ops `success: true`, text lands on disk.
- **End-to-end through the real tool wrapper:** a 5-slide deck built with 1
  `create` + 5 `batch` calls + 1 `view stats` = **7 tool calls total** (a
  15-slide deck ≈ 17 calls, far under both 50 and 200). All 5 slides have title +
  body text on disk; the content validator ACCEPTS it.
- Confirmed pydantic-ai's default `request_limit` is 50 and was the source of the
  crash; the office run now uses 200.

## Lessons / why this took several rounds

1. The empty deck had **multiple independent causes** layered on top of each
   other (resident flush, request limit, batch-schema misuse), so fixing one at
   a time kept revealing the next.
2. A misdiagnosis (testing `batch` with the wrong JSON shape) led to recipe
   guidance that was not just unhelpful but actively harmful — it traded a
   content bug for a crash. The authoritative source was the SKILL.md all along;
   when in doubt, match what the skill actually does and grep-verify against
   `officecli help`.
