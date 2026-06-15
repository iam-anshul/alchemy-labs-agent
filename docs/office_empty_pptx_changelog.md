# Changelog — Office agent produced empty PPTX/DOCX/XLSX

Fixes a bug where the `office` sub-agent reported success and "produced" an
Office file, but the file opened **completely empty** — e.g. a 14-slide deck the
agent built came out as a blank presentation.

---

## Symptom

- The office agent created a `.pptx`, added slides/content, verified it with
  `officecli view ... stats` / `read` (which reported the correct slide count),
  then submitted `produced=["outputs/Indian_Mutual_Fund_Strategy.pptx"]`.
- The submission passed validation (the file existed and was non-empty in bytes).
- The user opened the file and it had **no slides / no content**.

## Root cause

`officecli` runs a **resident background process** that keeps each document open
in memory ("kept open in background for faster subsequent commands"). Every
mutating command (`create`, `add`, `set`, `batch`, `import`, `merge`, …) applies
its edit to that **in-memory** copy. Those edits are **not flushed to disk until
the resident process is closed** (`officecli close <file>`, or after a ~12-minute
idle timeout).

The office agent never called `officecli close`. So the chain was:

1. `create` writes a blank document shell to disk (a `.pptx` is a zip of XML, so
   even a 0-slide deck is ~8.9 KB on disk).
2. `add` / `batch` add all the real content — but only into the **resident memory
   copy**.
3. `view stats` / `read` report the content because they read from the resident
   memory, so the agent believes the file is complete and submits.
4. `_validate_submission` only checked existence + `st_size > 0`
   (`office_agent.py`), and the blank shell has bytes, so it passed.
5. The file on disk is still the blank `create`-time shell → opens empty.

**Verified directly:** after `officecli add ... --type slide`, officecli reported
`slides: 1` while `python-pptx` read `DISK slides: 0`; after `officecli close`,
the file grew (8949 → 9811 bytes) and the slide appeared on disk.

## Fix

In the `officecli` tool wrapper in `office_agent.py`, after any **mutating**
command succeeds, the wrapper now runs `officecli close <file>` to flush the
resident to disk. The agent cannot forget to flush.

- New constant `_OFFICECLI_MUTATING_COMMANDS = {create, add, set, delete, batch,
  import, merge, move}` identifies commands that change the document.
- New helper `_run_officecli(workspace, cmd_args)` centralizes the subprocess
  call (used for both the main command and the follow-up `close`).
- After a successful mutating command, the wrapper closes the resident for the
  target file (the second positional arg, e.g. `['add', 'outputs/x.pptx', ...]`).
  `close` returns exit 0 and is a harmless no-op when no resident is running, so
  it is safe to call unconditionally. Its output is swallowed — it's bookkeeping
  the agent doesn't need to see.

The submission validator was intentionally **left unchanged** (still checks
existence + non-zero size); the flush fix addresses the actual cause.

## Files changed

- `office_agent.py` — added `_OFFICECLI_MUTATING_COMMANDS`, `_run_officecli()`,
  and the post-mutation resident flush inside the `officecli` tool.

## Verification

- Reproduced the empty-file bug: edits visible to `officecli` but `DISK slides:
  0` via `python-pptx`; confirmed `close` flushes them to disk.
- Confirmed close-after-each-mutation **accumulates** correctly: sequential
  `add`s across multiple slides all persist (reopening from the flushed file
  preserves prior edits).
- Exercised the patched tool's real flush path end-to-end: `create` + two `add`s
  with no manual close → on-disk file contains the slide and its text.

## Trade-off / follow-up

Closing the resident after every mutating command gives up some of officecli's
speed optimization (the next command reopens the doc from disk). For the typical
flow (`create` + a single `batch`/`import` to populate) this is one or two extra
closes — negligible. If the agent later does many one-at-a-time `set` calls and
the open/close overhead becomes noticeable, switch to flushing **once before
submit** (close all open residents in the workspace in `run_office_executor`)
instead of after each command.
