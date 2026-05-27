# Resilience & Recovery — Control Loop and Browser Executor

This document explains the failure-handling and resilience changes made to the
planner system. It covers two areas:

1. **Control loop** (`main.py` + `render_todo.py`) — how the system recovers when
   a task fails instead of letting one failure kill the whole run, and how a
   failure's diagnostic reaches the planner without cluttering the user-facing
   `todo.md`.
2. **Browser executor** (`browser_agent.py` + `.env`) — why `browser-use`'s
   agent hung (the DOM-watchdog 30s hang), why it ran far past its step cap, why
   it burned a run trying to solve a CAPTCHA, and the changes that bound, survive,
   and report those failures.

It is written to be read top-to-bottom by someone new to the codebase. Each fix
includes *what changed*, *why*, and *how it behaves now*.

---

## Background: how a failure used to cascade

The original control loop dispatched each ready task once. On any error it
marked the task `failed` and moved on. Because tasks form a dependency DAG, a
single failed leaf took down everything downstream of it. The motivating
example was a three-task plan:

```
t1 (browser)  → find top-5 Indian banks by revenue
t2 (document_answering, deps: t1) → extract financials
t3 (office, deps: t2) → build the PowerPoint
```

`t1`'s browser agent hung on a heavy web page (see Part 2), failed, and the run
ended with all three tasks `[!]` — `t2` and `t3` reported `"upstream task
failed"` and nothing was produced. One stuck page wasted the entire run.

The changes below address this on two fronts: make the browser executor less
likely to hang and able to survive a hang (Part 2), and make the control loop
able to *recover* from a failure rather than abort (Part 1).

---

## Part 1 — Control loop (`main.py` + `render_todo.py`)

A set of related changes turn "first error ends the run" into "a task is retried,
then — if still failing — a planner review can re-route the plan, bounded by a
replan budget." A final change (§1.8) routes failure diagnostics to the planner
on an internal channel, keeping the user-facing `todo.md` clean while ensuring the
planner has the context it needs across multiple replans.

### 1.1 Dependency id resolution (the latent crash)

**Where:** [`main.py` dep-file gathering](../main.py) inside the `for task in ready` loop.

`TaskSpec.deps` is a `list[str]` of task ids (see `formats_pydantic.py`). The
old code treated each `dep` as if it were a task object:

```python
for dep in task.deps:
    dep_files.extend(dep.produced)      # dep is a str → AttributeError
```

A string has no `.produced`, so this raised `AttributeError` the moment any task
with dependencies actually dispatched. It never fired earlier only because the
dependent tasks never ran (their upstream failed first).

**Now:**

```python
for dep in task.deps:
    dep_files.extend(tasks_by_id[dep].produced)
```

`tasks_by_id` is the `{id: TaskSpec}` map built each loop iteration. This
resolves the id to the actual upstream task and gathers its produced file paths,
which are then injected into the executor as its input files. **This fix is a
prerequisite for any multi-task plan to run at all.**

### 1.2 `_merge_plan` — preserve control-loop state, and allow failed-task recovery

**Where:** [`_merge_plan` in `main.py`](../main.py).

The planner emits `TaskSpec` objects with default `status="pending"`,
`produced=[]`, `error=""`, `notes=""`. When we adopt a revised plan, we must not
clobber the control-loop-owned state of surviving tasks. `_merge_plan` carries
that state — `status`, `produced`, **`error`, and `notes`** — forward from the old
plan for any task id that survives.

```python
for t in new.tasks:
    if t.id in old_by_id:
        old_t = old_by_id[t.id]
        rewritten = (t.query, t.expects, t.agent) != (old_t.query, old_t.expects, old_t.agent)
        if old_t.status == "failed" and rewritten:
            # planner changed approach for a failed task -> let it run again.
            # The old error/notes describe the abandoned approach, so clear them.
            t.status = "pending"
            t.produced = []
            t.error = ""
            t.notes = ""
        else:
            # preserve ALL control-loop-owned state, including error and notes
            t.status = old_t.status
            t.produced = old_t.produced
            t.error = old_t.error
            t.notes = old_t.notes
return new
```

Two things this does:

- **Recovery branch.** If a task was `failed` **and** the planner meaningfully
  rewrote it (changed its `query`, `expects`, or `agent`), reset it to `pending`
  so it runs again with the new approach, and clear its `error`/`notes` — they
  described the abandoned approach. Gating on `rewritten` matters: resetting
  *every* failed task to pending on every replan would loop a genuinely-impossible
  task forever; requiring an actual change means recovery only happens when
  there's reason to expect a different outcome.
- **Preserve `error`/`notes` otherwise.** Without this, every signature-changing
  replan reset surviving tasks' `error` and `notes` to the planner's `""`
  defaults. That wiped a failed task's diagnostic *before a second replan* — so
  the planner, reviewing again, had forgotten why the task failed and could
  re-propose the same doomed plan. It also silently erased completed tasks'
  executor `notes`. Carrying both forward is what makes the failure diagnostic
  (§1.8) durable across multiple replans.

> **Bug history:** an earlier version had `return new` indented *inside* the
> `for` loop, so it returned after processing only the first task — every other
> task kept the planner's defaults, silently re-running completed tasks and wiping
> their produced lists. `return new` must sit at function-body indentation,
> outside the loop. Fixed.

### 1.3 Replan on failure (removing the early `continue`)

**Where:** the `for task in ready` loop in [`main.py`](../main.py).

The recovery branch in §1.2 is only useful if the planner is actually consulted
*after a failure*. The old failure path did `continue`, which jumped straight to
the next task and skipped the replan call entirely — so the planner never saw a
fresh failure and could never rewrite the failed task. The recovery branch was
effectively dead code.

**Now** both branches fall through to the replan call:

```python
result = ...   # the retry loop from §1.7 produces the final result here
if result.error:
    task.status = "failed"
    task.error = f"after {MAX_DISPATCH_ATTEMPTS} attempts: {result.error}"
else:
    ok, status = validate_files_exist(workspace, result.produced)
    if not ok:
        raise RuntimeError(status)
    task.status = "completed"
    task.produced = result.produced
    task.notes = result.notes

# reached on BOTH success and failure
if thisRun.replans_used < thisRun.replan_budget:
    new_plan = await planner(thisRun)
    if _plan_signature(new_plan) != _plan_signature(thisRun.plan):
        thisRun.plan = _merge_plan(thisRun.plan, new_plan)
        thisRun.replans_used += 1

write_todo_atomic(thisRun)
```

A failure is exactly when the planner most needs to see the state and decide
whether to revise. `_plan_signature` ignores control-loop-owned fields (status,
produced) so a plain status flip doesn't read as "plan changed" and burn the
replan budget on a no-op.

### 1.4 Surgical deadlock branch

**Where:** the `if not ready:` block in [`main.py`](../main.py).

When no task is ready, the old code marked **every** pending task `failed` and
broke the whole run. That's too blunt — it gives up even on branches that a
replan might still rescue, and it conflates "blocked by a failed ancestor" with
"can't run yet."

**Now** it only fails tasks that are actually blocked by a failed ancestor, and
loops again if it made progress:

```python
if not ready:
    failed_ids = {t.id for t in thisRun.plan.tasks if t.status == "failed"}
    progressed = False
    for t in thisRun.plan.tasks:
        if t.status == "pending" and any(dep in failed_ids for dep in t.deps):
            t.status = "failed"
            t.error = f"upstream task failed: {[d for d in t.deps if d in failed_ids]}"
            progressed = True
    write_todo_atomic(thisRun)
    if not progressed:
        break          # genuinely nothing left to do
    continue           # re-evaluate the ready set after marking
```

Each pass marks one more layer of the dependency chain as failed; when a pass
changes nothing (`progressed == False`), the run ends.

### 1.5 How it composes — a worked failure→recovery trace

Using the banks example, with a planner that rewrites `t1` on failure:

1. `t1` dispatches, the browser hangs and fails → `t1.status = "failed"`.
2. Fall-through replan: planner sees `t1` failed (and its `error`), and returns a
   revised plan where `t1`'s `query` says "use a lighter primary source such as
   the bank's investor-relations page." `_plan_signature` differs → adopt it.
3. `_merge_plan` sees `t1` was `failed` **and** `rewritten` → resets `t1` to
   `pending`, `produced=[]`. `replans_used` increments to 1.
4. Next while-iteration: `t1` is `pending` with no deps → ready again → dispatches
   with the new approach.
5. If `t1` now succeeds, `t2`/`t3` proceed normally. If it keeps failing, steps
   2–4 repeat until `replan_budget` (3) is exhausted, after which the planner is
   no longer consulted, `t1` stays `failed`, and the deadlock branch (§1.4) fails
   `t2` then `t3` and ends the run.

### 1.6 Termination guarantees

The loop cannot spin forever:

- **Recovery is bounded** by `replan_budget` (default 3, on `Run`). Once
  `replans_used == replan_budget`, no further replans occur, so a perpetually
  failing task can't be reset to pending again.
- **The deadlock branch is monotonic** — every pass that doesn't end the run
  marks at least one more task `failed`; with a finite task list it must reach
  `progressed == False` and `break`.

### 1.7 Dispatch retry — survive transient failures without spending a replan

**Where:** the `for task in ready` loop and the `MAX_DISPATCH_ATTEMPTS` constant
in [`main.py`](../main.py).

Replan-based recovery (§1.2–1.5) re-routes a failed task with a *different*
approach, but it costs a replan and only fires if the planner chooses to rewrite
the task. For *transient* failures — a one-off hang, a flaky page that loads fine
on a second try — the right move is simply to re-attempt the same task. The loop
now does that before declaring failure:

```python
MAX_DISPATCH_ATTEMPTS = 3  # module-level

result = None
for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
    result = await dispatch_executor_agent(task, dep_files, Path(workspace))
    if not result.error:
        break
    print(f"[{task.id}] attempt {attempt}/{MAX_DISPATCH_ATTEMPTS} failed: {result.error}")
    if attempt < MAX_DISPATCH_ATTEMPTS:
        await asyncio.sleep(2 * attempt)   # linear backoff: 2s, 4s

if result.error:
    task.status = "failed"
    task.error = f"after {MAX_DISPATCH_ATTEMPTS} attempts: {result.error}"
else:
    ...  # validate + mark completed
```

Each attempt calls `dispatch_executor_agent`, which **builds a fresh executor**
(and, for browser tasks, a fresh `Browser`/Chrome/CDP session). So a retry
sidesteps a wedged session rather than re-poking the same hung page, and the
browser agent re-plans its own browsing from scratch — it may pick a different
source the second time. A short linear backoff (2s, 4s) gives transient
conditions a moment to clear.

**How retry and replan relate.** These are two layers, applied in order:

1. **Dispatch retry (this section)** — same task, same approach, fresh executor,
   up to `MAX_DISPATCH_ATTEMPTS` times. Cheap; catches transient failures. Does
   *not* consume the replan budget.
2. **Replan recovery (§1.2–1.5)** — only after retries are exhausted and the task
   is marked `failed`, the fall-through replan (§1.3) lets the planner rewrite the
   task with a *different* approach, which `_merge_plan` resets to `pending`.
   Costs a replan.

So a failing browser task is now: tried up to 3 times with fresh browsers → if
still failing, marked `failed` → planner gets a chance to re-route it → bounded
by the replan budget. Independently, the browser executor has its *own* internal
step-failure tolerance via `max_failures` (Part 2), which governs failures
*within* a single attempt.

> **Tuning:** `MAX_DISPATCH_ATTEMPTS` is a module-level constant in `main.py`
> (default 3). Raise it for flakier environments; lower it to fail faster.

### 1.8 Failure diagnostics — internal to the planner, not in `todo.md`

**Where:** `render_task` in [`render_todo.py`](../render_todo.py) and `planner()`
in [`main.py`](../main.py).

`todo.md` is a **user-facing** artifact. Raw executor failure diagnostics — e.g.
`"Agent ended without calling submit. Final result: screener.in search
unresponsive…"` (Part 2, §2.9) — are noise to a human reading the file. But the
**planner** needs exactly that detail to decide whether and how to re-route a
failed task. These two audiences are now served by separate channels.

- **`render_todo` no longer renders `task.error`.** The `error:` line was removed
  from `render_task`. The `[!]` checkbox still signals failure to the user; the
  verbose reason does not appear in the file. `notes` is still rendered (it's
  short, and useful to a curious reader).
- **`planner()` injects errors on an internal channel.** Before each replan it
  builds a failure section directly from the task objects and appends it to the
  replan prompt — never to `todo.md`:

  ```python
  failures = [f"- {t.id} ({t.title}): {t.error}"
              for t in run.plan.tasks if t.status == "failed" and t.error]
  failure_section = (
      "\n\nExecutor failure details (internal — not shown to the user). Use "
      "these to decide whether a failed task should be re-routed ...\n"
      + "\n".join(failures)
  ) if failures else ""

  replan_prompt = f"Goal: {run.goal}\n\nCurrent plan state:\n\n{current_todo}{failure_section}\n\n..."
  ```

The data still lives on `task.error` (fed from `ExecutorResult.error`); it's just
not written to the file. Crucially, this works together with §1.2: because
`_merge_plan` now preserves `error` across merges, the failure section stays
populated on the **second and third** replans too — so the planner won't forget a
prior failure and re-propose the approach that already failed.

Why `error` and not `notes` for this? The task *failed*, and `notes` is only
populated on the success path (`task.notes = result.notes`). `error` is the
semantically correct field, and keeping it off the file is the whole point.

---

## Part 2 — Browser executor (`browser_agent.py` + `.env`)

`browser-use` (v0.11.13) drives a headless Chrome over the Chrome DevTools
Protocol (CDP). The failures in Part 1's example originated here: the agent hung
for 30s repeatedly and then stopped. This section explains the root cause and the
configuration changes that address it.

### 2.1 Root cause — the DOM-watchdog state-capture hang

Every agent step has two stages: **navigate**, then **capture browser state**
(the DOM snapshot fed to the LLM). They have different timeout behavior.

- **Navigation is already bounded and non-fatal.** In
  `browser/session.py`'s `_navigate_and_wait`, `Page.navigate()` is capped at 20s,
  and the page-readiness wait is short (3s same-domain / 8s cross-domain). On
  timeout it logs `⚠️ Page readiness timeout` and continues with partial content.
  A slow page at navigation does **not** fail the run.

- **State capture is where it hung.** `DOMWatchdog.on_BrowserStateRequestEvent`
  (`browser/watchdogs/dom_watchdog.py`) builds the DOM tree via a task awaited
  with **no inner timeout** (`content = await dom_task`). The fallback to minimal
  state only triggers if the build *raises* — not if it *hangs*.

- **What actually hangs.** The heavy CDP calls themselves *are* bounded:
  `dom/service.py` runs `DOMSnapshot.captureSnapshot`, `DOM.getDocument`, the
  accessibility tree, and viewport metrics under `asyncio.wait(timeout=10.0)`,
  retries pending ones at `timeout=2.0`, then raises `TimeoutError` — which *is*
  caught and degrades to empty state at ~12s. The genuinely unbounded surfaces
  are:
  - the **per-frame accessibility-tree gather**
    (`_get_ax_tree_for_all_frames`: `Page.getFrameTree` + `asyncio.gather` over
    every iframe's `Accessibility.getFullAXTree`, no timeout), and
  - the **synchronous, CPU-bound serialization** of a huge DOM tree, which blocks
    the event loop. An `asyncio` timeout cannot interrupt synchronous code, so
    this rides all the way to the outer event-bus ceiling.

  Both scale with **DOM size and iframe count** — ad-heavy / infinite-scroll /
  consent-wall pages are the trigger.

- **The 30s ceiling and the halt.** The outer handler has a 30s budget
  (`TIMEOUT_BrowserStateRequestEvent = 30.0`, `browser/events.py`). When the build
  exceeds it, the step fails; `consecutive_failures` increments; at `max_failures`
  (default 5) the agent stops. Control returns to `BrowserExecutor.run`, which
  finds nothing submitted and returns `"Agent exhausted steps without calling
  submit"` — the error Part 1 then has to handle.

> Verified that `browser-use` 0.12.9 (latest at time of writing) has the
> identical code here — `await dom_task` still has no inner timeout. **Upgrading
> does not fix this.** It is a known, recurring class of issue in the project.

### 2.2 Fix A — profile hardening (attacks the root cause)

**Where:** the `Browser(...)` constructor in [`browser_agent.py`](../browser_agent.py).

Shrink the DOM/iframe work so serialization is cheap and the AX-gather is small.
These are plain `Browser` keyword arguments — no patching:

```python
browser = Browser(
    headless=self.headless,
    downloads_path=str(self.workspace / "outputs"),
    cross_origin_iframes=False,   # process only same-origin frames
    max_iframes=10,               # default 100
    max_iframe_depth=3,           # default 5
    paint_order_filtering=False,  # cuts serialization CPU cost
)
```

`cross_origin_iframes=False` is the headline knob — `browser-use`'s own
`BrowserProfile` docstring states that with it False, "only same-origin frames
are processed to avoid complexity and **hanging**." This directly targets the
unbounded per-frame AX gather, the most likely hang surface.

### 2.3 The orphaned-browser bug (prerequisite for Fix A)

**Where:** the `Agent(...)` constructor in [`browser_agent.py`](../browser_agent.py).

Previously the configured `Browser` object was built but **never passed to
`Agent`**, so the agent spun up its own default browser and the configuration
(including `downloads_path`) had no effect. Without this fix, none of Fix A would
apply.

```python
browser_agent = Agent(
    task=task_prompt,
    llm=browser_llm,
    tools=tools,
    use_cloud=self.use_cloud,
    browser=browser,            # ← now actually used
    max_failures=self.max_failures,
)
```

Side benefit: `downloads_path=workspace/outputs` now takes effect, so downloaded
files land in the run's workspace. (Note `max_steps` is intentionally *not* here —
see §2.7.)

### 2.4 `max_failures` — survive transient degrades

**Where:** `BrowserExecutor.__init__` and the `Agent(...)` call in [`browser_agent.py`](../browser_agent.py).

`max_failures` is an **`Agent`** parameter (not a `Browser`/`BrowserProfile`
one — passing it to `Browser` raises `TypeError`). It is the number of
consecutive failed steps the agent tolerates *within a single run* before
stopping. It's threaded through `BrowserExecutor.__init__(..., max_failures: int
= 8)` and passed to the `Agent`.

It is set to **8** (above `browser-use`'s default of 5), so a transient
step-level degrade — e.g. one 10s state-capture timeout from Fix B — doesn't end
the agent prematurely. `main.py`'s `dispatch_executor_agent` also passes
`max_failures=8` explicitly at the dispatch site to make the intent visible.

Note the distinction from the dispatch retry (§1.7): `max_failures` governs
consecutive failed *steps inside one agent run*; `MAX_DISPATCH_ATTEMPTS` governs
how many times the control loop re-runs the *whole* executor. They are
complementary layers.

### 2.5 Fix B — bound the residual hang via env var

**Where:** [`.env`](../.env).

Lower the outer state-capture ceiling so a hang fails-and-degrades faster than
30s:

```bash
TIMEOUT_BrowserStateRequestEvent=10
```

`browser-use` reads this through `_get_timeout()` (`browser/events.py`), which
checks the environment variable and falls back to the 30.0 default. Because the
timeout is evaluated per-event-instance at runtime (via a Pydantic
`default_factory`), and `main.py` calls `load_dotenv()` before any browser event
is created, the value is picked up correctly. Verified: a fresh
`BrowserStateRequestEvent` reports `event_timeout = 10.0` after `load_dotenv()`.

**Caveat:** lowering this ceiling helps the *async* hang path (it can cancel an
awaiting handler sooner). It cannot interrupt the *synchronous* serialization
hang, because the event loop is blocked. That's why Fix A (shrinking the tree so
serialization stays cheap) is primary, and Fix B is defense-in-depth.

### 2.6 Isolation — stop leaking the absolute workspace path

**Where:** `_build_task_prompt` in [`browser_agent.py`](../browser_agent.py).

The task prompt used to interpolate the absolute workspace path
(`All paths are relative to {self.workspace}.`), which broke the system's
context-isolation principle (sub-agents should not know absolute paths). It now
reads:

```
WORKSPACE:
All paths are relative to your workspace root.
Write your outputs under outputs/.
```

The tools resolve relative paths against the workspace internally; the agent
never needs — and no longer sees — the absolute path.

### 2.7 `max_steps` — the cap that wasn't being enforced

**Where:** the `Agent(...)` constructor and `run()` call in [`browser_agent.py`](../browser_agent.py).

`max_steps` was passed to `Agent(...)`, but **`Agent.__init__` does not accept it**
— it's a parameter of `Agent.run()`, with a default of **500**. Passed to the
constructor it was silently ignored, so `run()` used 500. A run that hit a
pathological page (e.g. a CAPTCHA loop, §2.8) would grind through up to 500 steps
before stopping — observed in logs reaching step 56+ with no end in sight despite
`max_steps=30`.

**Now** it's passed where browser-use actually reads it:

```python
browser_agent = Agent(task=..., llm=..., tools=..., browser=browser,
                      use_cloud=..., max_failures=self.max_failures)
...
await browser_agent.run(max_steps=self.max_steps)   # the cap is now enforced
```

### 2.8 CAPTCHA / bot-wall handling — stop trying to solve the unsolvable

**Where:** `_build_task_prompt` in [`browser_agent.py`](../browser_agent.py).

A headless browser on Google search gets challenged with reCAPTCHAs aggressively.
The agent would attempt to *solve* them — clicking "I'm not a robot" and bicycle
tiles over and over. These clicks *succeed*, so `max_failures` never trips; only
the step cap (§2.7) eventually stops it. The result was a run burned entirely on
an unwinnable puzzle.

The task prompt now includes an **"IF YOU ARE BLOCKED"** section instructing the
agent to: never attempt CAPTCHAs / checkboxes / bot-checks, leave a blocking page
immediately, prefer primary/official sources over Google search, and — if truly
blocked after a couple of alternatives — stop and submit with whatever partial
findings it has plus a note naming the block.

This is a behavioral guardrail, not a hard stop. It depends on the model
following instructions; pair it with §2.7 (the step cap now actually bounds the
damage) and the dispatch retry / replan recovery in Part 1 (a blocked task fails
and the planner can re-route to a different source). See "Future work" for the
remaining gap: a CAPTCHA block is *semi-permanent*, so retrying it (§1.7) wastes
attempts — distinguishing "blocked" from "transient" failure is not yet done.

### 2.9 Failure-detail capture — tell the planner *why* it failed

**Where:** `run()` and the `_failure_detail` helper in [`browser_agent.py`](../browser_agent.py).

When the agent ends without calling our `submit` tool (it called browser-use's
internal `done`, exhausted steps, or stopped), the old code returned a generic
`"Agent exhausted steps without calling submit"`. The rich account of *what
happened* — which lives in the `AgentHistoryList` returned by `run()` — was
discarded, because the return value wasn't captured.

**Now** `run()` captures the history and a `_failure_detail(history)` helper builds
a planner-readable error from it:

```python
history = await browser_agent.run(max_steps=self.max_steps)
...
if self._submitted is None:
    return ExecutorResult(produced=[], notes="", error=self._failure_detail(history))
```

`_failure_detail` pulls `history.final_result()` (the agent's own account, e.g.
"screener.in search unresponsive, no alternatives tried") and the last few real
entries from `history.errors()`, each call wrapped in `try/except` so a
browser-use API quirk can't crash the executor. That string flows into
`task.error` and then — via §1.8 — into the planner's internal failure channel,
giving it the context to make an *informed* re-route instead of re-emitting the
same doomed task.

---

## How Part 1 and Part 2 work together

- **Part 2** makes a browser task *less likely to hang* (Fix A), *fail faster
  when it does* (Fix B), *actually obey its step cap* (§2.7), *not waste a run on
  a CAPTCHA* (§2.8), and — when it does fail — *report why* in a form the planner
  can act on (§2.9).
- **Part 1** ensures that if a task still fails, it's first *retried* with a fresh
  executor (§1.7), then the run *recovers* — the planner is consulted with the
  failure diagnostic on an internal channel (§1.8), can re-route the failed task
  with a different approach (§1.2), and only genuinely-blocked downstream tasks are
  abandoned (§1.4) — all bounded by the replan budget, with the diagnostic
  surviving across replans so the planner doesn't repeat a failed plan.

A single stuck web page, runaway step loop, or CAPTCHA wall should no longer end a
run — and when a task is truly unrecoverable, the planner fails it with the reason
recorded, not silently.

---

## Configuration reference

| Setting | Location | Default | Purpose |
|---|---|---|---|
| `TIMEOUT_BrowserStateRequestEvent` | `.env` | `30` (lib) → `10` (set) | Outer ceiling on DOM state capture before the step fails |
| `cross_origin_iframes` | `Browser(...)` | `True` → `False` | Process only same-origin frames; avoids iframe-driven hangs |
| `max_iframes` | `Browser(...)` | `100` → `10` | Cap iframes processed during DOM build |
| `max_iframe_depth` | `Browser(...)` | `5` → `3` | Cap iframe recursion depth |
| `paint_order_filtering` | `Browser(...)` | `True` → `False` | Reduce serialization CPU cost |
| `max_failures` | `Agent(...)` via `BrowserExecutor` | `8` | Consecutive step failures tolerated *within one agent run* before it stops |
| `max_steps` | `Agent.run(...)` via `BrowserExecutor` | `30` | Max agent steps per run. **Must be passed to `run()`, not the `Agent(...)` constructor** (see §2.7) |
| `MAX_DISPATCH_ATTEMPTS` | `main.py` constant | `3` | How many times the control loop re-runs a whole executor before marking the task failed |
| `replan_budget` | `Run` | `3` | Max planner revisions per run (bounds replan recovery) |

---

## Future work

- **Blocked vs. transient failure signal.** The dispatch retry (§1.7) re-attempts
  *every* error 3×, but a CAPTCHA / bot-wall / login block (§2.8) is
  *semi-permanent* — retrying it just wastes 2 attempts before the planner gets to
  re-route. A structured signal (e.g. `ExecutorResult.blocked=True`) would let the
  loop skip retries for permanent blocks and go straight to replan recovery, while
  still retrying genuinely transient failures.
- **Inner DOM-build timeout (monkeypatch):** wrapping `await dom_task` in
  `asyncio.wait_for` would give true fail-fast for the *async* hang path. It does
  not help the synchronous-serialization path and means patching a dependency, so
  it's held in reserve behind Fix A + Fix B.
- **Domain steering:** `Browser(prohibited_domains=[...])` or prompt-level
  guidance toward primary/structured sources, to avoid the heaviest pages
  entirely.
- **Backoff strategy:** the dispatch retry (§1.7) uses a fixed linear backoff
  (2s, 4s). For rate-limit-style failures, exponential backoff with jitter would
  be gentler; not needed yet.

---

## Source references

Code in this repo:
- `main.py` — control loop, dispatch retry, `_merge_plan`, `_plan_signature`,
  deadlock branch, `planner()` internal failure channel
- `browser_agent.py` — `BrowserExecutor`, `_build_task_prompt` (incl. CAPTCHA
  guardrail), `_failure_detail`, profile config, `max_steps`/`max_failures` wiring
- `render_todo.py` — user-facing rendering (deliberately omits `error`)
- `.env` — `TIMEOUT_BrowserStateRequestEvent`
- `formats_pydantic.py` — `TaskSpec`, `PlanOutput`, `Run`
- `planner_readme_1.md` — the system's overall design

`browser-use` internals referenced (v0.11.13, in `.venv`):
- `browser/watchdogs/dom_watchdog.py` — `on_BrowserStateRequestEvent`, `await dom_task`
- `dom/service.py` — bounded CDP calls, unbounded AX-tree gather, serialization
- `browser/session.py` — `_navigate_and_wait` navigation bounds
- `browser/events.py` — `_get_timeout`, per-event `event_timeout` fields
- `browser/profile.py` — `cross_origin_iframes`, `max_iframes`, etc.
- `agent/service.py` — `max_failures`, the consecutive-failure halt
