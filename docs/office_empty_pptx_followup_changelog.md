# Changelog — Office agent empty-PPTX (follow-up, real fix)

This is the **second** changelog for the empty-Office-file bug. The first fix
(`office_empty_pptx_changelog.md`) addressed a real problem but did **not** stop
the empty decks. This change addresses the actual cause and adds a safety net so
an empty artifact can never be shipped silently again.

---

## Why the previous fix did not work

The first fix added an automatic `officecli close <file>` after every mutating
command, on the theory that the deck was empty because officecli's in-memory
"resident" edits were never flushed to disk.

That flush behavior is real, and the fix is correct — **but it was not the cause
of the empty decks in practice.** The user ran the committed flush fix and still
got empty slides. Reproducing the agent's actual workflow revealed the true
cause:

**The content was never successfully added in the first place.** The office
system prompt steered the agent toward officecli's `batch` command (a JSON array
of `add`/`set` ops) to populate the deck. officecli's batch **add-op schema is
strict and unintuitive**, and the model got it wrong:

- Putting `type` / geometry fields (`x`, `y`, `width`, `height`) at the wrong
  level of the op JSON returns `unknown field(s) "x","y",…` or
  `'add' command requires 'type' or 'from' field`.
- `batch` also emits a noisy `stdin is also redirected; stdin will be ignored`
  warning under `subprocess` (harmless, but it muddies the output the agent
  reads).

When the batch ops fail, **every op fails, no content is added**, and `batch`
exits non-zero. But `create` had already written a blank `.pptx` shell to disk.
A blank `.pptx` / `.docx` / `.xlsx` is a valid ZIP of boilerplate XML and is
several KB on disk — so the submission validator's only content check
(`st_size > 0`) passed, and the empty shell shipped.

So the flush fix was flushing a file that correctly had **nothing in it**. The
disk write was never the problem; the failed content insertion was.

A secondary contributor: the agent **never loaded the OfficeCLI skill**
(`load_skill pptx` / `pitch-deck`), despite the system prompt instructing it to.
The skill contains the verified command forms; without it the agent guessed.

---

## The fix (three layers, in `office_agent.py`)

### 1. Harden the submission validator — the safety net (essential)

Byte size cannot tell an empty Office file from a populated one. The validator
now probes real content with `officecli view <file> stats`:

- **`.pptx` / `.docx`** — reject if `words == 0` (and, for pptx, `slides == 0`).
- **`.xlsx`** — reject if `totalCells == 0`.

A rejection returns an error, which the `@theOfficeAgent.output_validator`
turns into a **`ModelRetry`** — so instead of shipping an empty deck, the agent
is told to re-add the content and verify, and gets another attempt.

- New `_OFFICE_CONTENT_EXTS = {".pptx", ".docx", ".xlsx"}`.
- New `_office_content_error(workspace, rel_path)` runs the stats probe and
  returns an actionable error string (or `None` if officecli is unavailable /
  stats unparseable — a probe failure must never block a valid submission).
- `_validate_submission` calls it for Office files after the existing
  existence + non-zero-size checks.

### 2. Fix the prompt's officecli recipe — the cure

The task prompt for a `.pptx` now includes a short, **verified** recipe
(`_PPTX_RECIPE`) that is known to write real content:

- `create`, then for each slide append it and add text shapes **one at a time**
  with explicit geometry (`x/y/width/height`).
- **Do NOT use `batch`** for slides/shapes — its op schema is error-prone and a
  failed batch silently leaves the file empty.
- **Verify before submitting** with `officecli view <file> stats` (confirm
  `slides` and `words` are both > 0).

This recipe is small and always present, so it works even when the agent ignores
the large loaded SKILL.md.

### 3. Force-load the matching OfficeCLI skill — correctness + polish

`run_office_executor` now detects the artifact type from the EXPECTED OUTPUT
text and force-loads the matching skill, injecting its SKILL.md into the task
prompt so the verified design + command conventions are unconditionally in
context (rather than relying on the agent to remember to `load_skill`).

- `_detect_office_ext(expects)` — picks the dominant Office extension named.
- `_SKILL_FOR_EXT` — `.pptx → pitch-deck`, `.xlsx → data-dashboard`,
  `.docx → academic-paper`.
- `_load_skill_text(skill)` — fetches the SKILL.md via `officecli load_skill`.
- `_build_task_prompt(..., skill_text=...)` — injects the recipe (pptx) and the
  loaded skill, and adds a closing reminder to verify each file is non-empty.

---

## Files changed

- `office_agent.py`
  - `_OFFICE_CONTENT_EXTS`, `_office_content_error()`, content probe wired into
    `_validate_submission`.
  - `_PPTX_RECIPE`, `_detect_office_ext()`, `_SKILL_FOR_EXT`, `_load_skill_text()`.
  - `_build_task_prompt()` gained a `skill_text` param and now embeds the recipe +
    skill + verify reminder.
  - `run_office_executor()` force-loads the skill and passes it into the prompt.

The flush fix from the first changelog (`_OFFICECLI_MUTATING_COMMANDS`,
post-mutation `close`) is **kept** — it is correct and prevents a genuine
flush-related failure mode; it simply was not the operative cause here.

---

## Verification

- Validator: empty `.pptx`/`.docx` → rejected (`no text content`); empty `.xlsx`
  → rejected (`0 cells`); populated `.pptx`/`.xlsx` → accepted; non-Office files
  (`.md`) pass through untouched.
- Reproduced the original failure: a `batch` with the schema the prompt
  encouraged → all ops fail → 0 slides on disk; confirmed the verified
  individual-`add` recipe writes real text (`slides`/`words` > 0).
- Prompt assembly: for a `.pptx` task, the prompt now contains the verified
  recipe, the loaded `pitch-deck` skill, and the verify reminder.

## Trade-off / tunable

Force-loading `pitch-deck` adds ~65k characters (~16k tokens) to the office
agent's prompt per pptx run. This is a deliberate correctness-over-tokens choice
and is bounded to one skill per run. If token cost matters, options are: load
the lighter generic `pptx` skill (~40k chars) instead of `pitch-deck`, or rely on
the small `_PPTX_RECIPE` + validator alone and drop the full skill injection.
