# A Files-First Planner — Read Me

A walkthrough of how the planner and sub-agents work end to end. Written to be read top to bottom by someone seeing this for the first time. No prior context needed.

---

## The one-paragraph version

A user gives the system a goal. A *planner* (an LLM) writes a `todo.md` file laying out the tasks needed to achieve it. A *control loop* (plain Python) reads that file, dispatches sub-agents to do each task in dependency order, and writes the results back into the file. Each sub-agent runs in its own context, writes its outputs as files in a shared workspace, and reports just the file paths back. The planner is called again between dispatches to look at the updated `todo.md` and decide whether the plan still makes sense. The loop ends when every task is marked done.

That's the whole system. The rest of this doc explains the pieces.

---

## Why files instead of JSON returns

Every sub-agent's "output" is one or more files written into a shared workspace directory. Not a JSON return value, not a string in the planner's context — files on disk. The sub-agent calls `submit(produced=[...paths])` when done.

This matters because it gives you three things for free.

First, the planner's context stays tiny. A task's "result" in `todo.md` is a list of paths, not the actual data. A 50-page report and a one-line answer cost the same.

Second, large outputs work transparently. Excel files, PDFs, charts, scraped HTML — none of these want to live as JSON.

Third, the next task can read whatever it actually needs from the file with its own tools. No upfront schema design.

The cost is you give up strong typing. In practice this matters less than it sounds, because the next consumer is usually another LLM that can handle minor format variation. When you do need strict structure — e.g. piping into deterministic code — you specify a strict file format ("CSV with these columns") in the task description and validate at the boundary.

---

## The four pieces

**Control loop.** Plain Python. Owns the workspace directory, the `todo.md` file, dispatch, concurrency, and the main while-not-done loop. Has no LLM calls of its own; it just calls the planner and executors.

**Planner.** An LLM with no tools. It is given the current `todo.md` as input and asked to return the next version. On the first call it writes the plan from scratch given the user's goal. On every later call it sees the in-progress file (some tasks done, results filled in) and decides whether anything needs to change. If nothing changes, it returns the same file.

**Executor (sub-agent).** An LLM with a small toolkit. Spawned per task. Gets the task description, a `query` string, and a list of input file paths from upstream dependencies. Does its work, writes files into the workspace, calls `submit` with the list of files it wrote. Done. Its context is thrown away after.

**Workspace.** A directory on disk. Holds `todo.md`, `outputs/` (where executors write), and whatever else needs to persist. Lives for one run.

---

## The `todo.md` schema

`todo.md` is a real markdown file with a fixed structure. The planner reads and writes it. The control loop parses it. Both follow the same conventions.

Three sections: a header with goal and metadata, a tasks section, and an optional notes section.

```markdown
# Goal
<one or two sentences stating what the user asked for>

# Workspace
/workspace/run_<id>/

# Status
Started: <ISO timestamp>
Replans used: 0 / 3

# Tasks

## [ ] t1 — <short title>
- agent: <one of: browser | office | document_answering | ...>
- deps: <comma-separated task ids, or "none">
- query: <the specific question or instruction this task should answer>
- expects: <relative path(s) the task should write, plus a prose description
  of what should be in them>
- produced: <filled in by the control loop after the task completes;
  list of relative paths the executor actually wrote>
- notes: <filled in by the control loop from the executor's submit notes;
  free-form text the executor wanted the planner to see>

## [ ] t2 — ...
  ...

# Notes
<optional free-form notes the planner appends when replanning, explaining
what changed and why>
```

Each task block has six fields. Four are written by the planner when the task is created (`agent`, `deps`, `query`, `expects`). Two are filled in by the control loop after the executor finishes (`produced`, `notes`). The checkbox state is also managed by the control loop.

### The checkbox states

- `[ ]` — pending; not yet dispatched
- `[~]` — dispatched, currently running
- `[x]` — completed and accepted
- `[!]` — failed (retries exhausted)

### Why `query` is its own field

You asked about this directly. The reason to separate `query` from `expects` is that they're two different things, and conflating them is one of the easiest ways to confuse the executor.

`query` is *what the executor is supposed to figure out or do.* It's the question, the instruction, the thing the executor is reasoning about. "What were Apple's revenue and operating margin in Q1 2026, and how did they compare to the prior year?"

`expects` is *what file should exist when the executor finishes, and what should be in it.* "Write `outputs/t1_apple.md` with a section per quarter covering revenue, gross margin, operating margin, with citations to page numbers in the source PDF."

The executor needs both. The query tells it what to think about. The expects tells it what artifact to leave behind. Without `query`, you'd have to stuff the question into the expects field and the executor has to disentangle "what am I solving" from "what file should I write." Keeping them separate makes the executor's system prompt clean: *here is your question, here are your inputs, here is the file we expect you to produce.*

### Why `produced` is separate from `expects`

`expects` is the planner's intent — "this task should produce a file at this path with this content." `produced` is what actually happened — "the executor wrote these files." They usually match, but not always. An executor might write extra debug files, or split its output across more files than the planner anticipated, or fail to produce something it was supposed to. Having both lets the planner notice mismatches on its next review.

---

## How dependencies share context between agents

The dependency graph does two jobs at once: it orders execution, and it tells the control loop which files to inject into each executor's context.

When a task has `deps: t1, t2`, the control loop waits until t1 and t2 are both `[x]`. Then it gathers their `produced` paths and includes them in the executor's system prompt as the input files. The executor sees: "Files from upstream tasks: outputs/t1_competitors.md, outputs/t2_pricing.md. Read these as needed." It uses its `read_file` tool to pull in whatever it actually needs.

Critically, the executor does *not* see the full workspace listing. It only sees the dep outputs. This is the context isolation working — t3's executor doesn't know t4's screenshots exist, because t4 isn't a dependency.

So your concrete question — "t1 completes, t2 takes that as input files" — works like this:

1. t1 dispatches. Executor writes `outputs/t1_sources.md` and three PDFs to the workspace. Calls `submit(produced=["outputs/t1_sources.md", "outputs/t1_aapl.pdf", "outputs/t1_msft.pdf", "outputs/t1_googl.pdf"])`.
2. Control loop validates these files exist. Writes those four paths into t1's `produced` field in `todo.md`. Flips t1 to `[x]`.
3. Calls the planner with the updated file. Planner reads it, decides nothing needs to change, returns the file unchanged.
4. Control loop looks for ready tasks. t2 has `deps: t1`, and t1 is now `[x]`, so t2 is ready.
5. Control loop dispatches t2. The executor's system prompt is built with t1's `produced` paths injected as "Files from upstream tasks." The executor reads what it needs and gets to work.

The control loop is the thing that walks the dep graph and rendezvous file paths. Neither the planner nor the executors track dependencies themselves — the planner just declares them, the executors just receive the resolved paths.

---

## Tools per agent type

Every executor has a base toolkit: `read_file`, `write_file`, `list_dir`, `submit`. Then each agent type adds specific tools and a tailored system prompt.

**browser** adds `web_search`, `web_fetch`, `browser` (stateful headless browser session). Used when the task needs the live web.

**office** adds `python` (with pandas, openpyxl, python-docx, python-pptx, matplotlib pre-installed) and `shell`. Used when the output is a structured office artifact — Excel, Word, PowerPoint, CSV, charts.

**document_answering** adds `python` only (for text processing, chunking, search if needed). Deliberately does *not* get `web_search` or `browser` — its job is to ground answers in the documents already in the workspace. Removing web tools is a guardrail against hallucination via off-task browsing.

The planner picks the agent type when it creates the task. It cannot pick "all tools" — that would defeat the isolation. Choosing the right type is part of planning.

---

## The end-to-end example

User asks: *"Look up my three biggest competitors' latest earnings reports, summarize the key financials, and build me an Excel model comparing their revenue growth and margins."*

### Step 1 — Control loop initializes

Creates `/workspace/run_xyz/`. Creates an empty `todo.md` with just the goal section filled in. Calls the planner.

### Step 2 — Planner writes the initial plan

The planner returns the full `todo.md` content:

```markdown
# Goal
Compare three biggest competitors' latest earnings: summarize financials
and build an Excel model of revenue growth and margins.

# Workspace
/workspace/run_xyz/

# Status
Started: 2026-05-21T10:14:00Z
Replans used: 0 / 3

# Tasks

## [ ] t1 — Find latest earnings reports for top three competitors
- agent: browser
- deps: none
- query: Identify the three biggest competitors in our space and locate
  each one's most recent quarterly earnings report (10-Q, press release,
  or equivalent). Download the source document.
- expects: outputs/t1_sources.md (markdown list of three competitors,
  each with company name, ticker, fiscal period, and source URL) plus
  outputs/t1_<ticker>.pdf for each downloaded document.
- produced:
- notes:

## [ ] t2 — Extract key financials from each earnings document
- agent: document_answering
- deps: t1
- query: For each competitor, extract revenue (current and prior-year
  comparable quarter), gross margin, operating margin, net income, and
  any forward guidance for the next quarter.
- expects: outputs/t2_financials.md — one section per competitor with the
  five financial metrics listed, each cited to a specific page number in
  the source PDF.
- produced:
- notes:

## [ ] t3 — Build Excel comparison model
- agent: office
- deps: t1, t2
- query: Build a comparison spreadsheet showing revenue growth and
  margin differences across the three competitors. Include charts.
- expects: outputs/comparison.xlsx with three sheets: (1) Raw data —
  table of the financial figures, (2) Growth — YoY revenue growth bar
  chart, (3) Margins — gross and operating margin comparison chart.
- produced:
- notes:

# Notes
```

### Step 3 — Control loop walks the graph

Reads `todo.md`. Parses three tasks. Finds the ready set — tasks where status is `[ ]` and all deps are `[x]`. Only t1 qualifies (no deps).

Flips t1 to `[~]`. Dispatches a browser executor.

### Step 4 — Browser executor runs t1

The control loop builds its system prompt:

```
You are a browser-type sub-agent. Your task is:

QUERY:
Identify the three biggest competitors in our space and locate each one's
most recent quarterly earnings report (10-Q, press release, or equivalent).
Download the source document.

EXPECTED OUTPUT:
outputs/t1_sources.md (markdown list of three competitors, each with
company name, ticker, fiscal period, and source URL) plus
outputs/t1_<ticker>.pdf for each downloaded document.

INPUT FILES FROM UPSTREAM:
(none)

WORKSPACE:
/workspace/run_xyz/

When done, call submit(produced=[...], notes="...").
```

The executor runs its tool loop. Does `web_search`, `web_fetch` a few investor relations pages, downloads three PDFs, writes `outputs/t1_sources.md`. Calls:

```
submit(
  produced=[
    "outputs/t1_sources.md",
    "outputs/t1_aapl.pdf",
    "outputs/t1_msft.pdf",
    "outputs/t1_googl.pdf"
  ],
  notes="Used Q1 2026 earnings — most recent reported. MSFT's report
  segments revenue by cloud/productivity/personal computing; will need
  to decide whether to use consolidated or segment figures downstream."
)
```

### Step 5 — Control loop processes the result

Verifies the four files exist in `/workspace/run_xyz/outputs/` and are non-empty. Writes the paths into t1's `produced` field. Writes the notes string into t1's `notes` field. Flips t1 to `[x]`. Calls the planner.

### Step 6 — Planner reviews

The planner reads the updated `todo.md`. It sees the new `notes` from t1 flagging the segment-vs-consolidated question. It decides: "consolidated is the right call for this comparison" and modifies t2's query to explicitly say so:

```
- query: For each competitor, extract revenue (current and prior-year
  comparable quarter), gross margin, operating margin, net income, and
  any forward guidance. Use consolidated company-wide figures, not
  segment breakdowns.
```

It also appends to the Notes section:

```
# Notes
- t1 flagged that MSFT reports by segment. Specified in t2 query that
  we want consolidated figures for comparability across the three.
```

Returns the updated file. The control loop diffs the new version against the old, detects t2's query changed, and updates its parsed task object.

### Step 7 — t2 dispatches

t1 is `[x]`, t2's only dep is t1, so t2 is ready. Flips to `[~]`. Dispatches a document_answering executor with t1's four produced paths in its system prompt.

The doc-answering executor reads the three PDFs (using `read_file`, which for PDFs returns extracted text), pulls out the requested financial numbers, writes `outputs/t2_financials.md` with citations like `(AAPL Q1 2026, p. 4)`, and calls submit.

### Step 8 — t3 dispatches

After t2 is `[x]`, t3 becomes ready (its deps are t1 and t2, both done). Dispatched to an office executor. Its system prompt includes both `outputs/t1_sources.md` and `outputs/t2_financials.md` as input files.

The office executor doesn't actually need the PDFs — t2 already distilled them. It reads only the two markdown files, uses `python` with pandas and openpyxl to build the spreadsheet with three sheets and embedded charts, calls submit.

### Step 9 — Wrap up

All three tasks `[x]`. Control loop calls the planner one final time. The planner writes a summary section at the bottom of `todo.md`:

```
# Summary
Compared Q1 2026 earnings for Apple, Microsoft, and Google. Revenue
growth and margin comparison delivered in outputs/comparison.xlsx.
Source PDFs and intermediate analysis retained in workspace.
```

The control loop returns the summary string and the path `outputs/comparison.xlsx` to the user.

---

## Writing a dummy planner

The dummy planner is a stub that returns hardcoded plans. Useful for testing the control loop without paying for LLM calls. Here's the contract.

The planner is a function: `planner(current_todo: str, user_goal: str | None) -> str`. It takes the current `todo.md` content and (on first call) the user goal, and returns the next version of the file.

A dummy version for the earnings example:

```python
INITIAL_TODO = """# Goal
{goal}

# Workspace
{workspace}

# Status
Started: {timestamp}
Replans used: 0 / 3

# Tasks

## [ ] t1 — Find latest earnings reports for top three competitors
- agent: browser
- deps: none
- query: Identify the three biggest competitors and locate each one's
  most recent quarterly earnings report. Download the source document.
- expects: outputs/t1_sources.md plus outputs/t1_<ticker>.pdf per
  competitor.
- produced:
- notes:

## [ ] t2 — Extract key financials
- agent: document_answering
- deps: t1
- query: For each competitor, extract revenue, gross margin, operating
  margin, net income, and forward guidance. Cite page numbers.
- expects: outputs/t2_financials.md with one section per competitor.
- produced:
- notes:

## [ ] t3 — Build Excel comparison model
- agent: office
- deps: t1, t2
- query: Build a comparison spreadsheet of revenue growth and margins
  across the three competitors with charts.
- expects: outputs/comparison.xlsx with Raw data, Growth, Margins sheets.
- produced:
- notes:

# Notes
"""

def dummy_planner(current_todo: str, user_goal: str | None) -> str:
    # First call: no todo yet, just a goal. Return the initial plan.
    if not current_todo.strip() or "## [" not in current_todo:
        return INITIAL_TODO.format(
            goal=user_goal,
            workspace="/workspace/run_dummy/",
            timestamp="2026-05-21T10:14:00Z",
        )
    # Every later call: no replanning, just return the file unchanged.
    # (A real planner would inspect the file and decide.)
    if "[x] t1" in current_todo and "[x] t2" in current_todo and "[x] t3" in current_todo:
        # All done — append a summary.
        return current_todo + "\n\n# Summary\nAll three tasks completed.\n"
    return current_todo
```

This is enough to drive the control loop end to end. To swap in a real planner, replace the function body with a call to your LLM of choice, passing the same two arguments in the prompt and asking the model to return the next version of the file as plain text. Same signature, same contract.

For a slightly less dumb version, the planner can also detect "this task's notes flagged something I should think about" and conditionally edit downstream task queries — which is what the example in Step 6 above showed.

---

## The control loop, in pseudocode

```python
def run(user_goal: str):
    workspace = make_workspace()
    todo_path = workspace / "todo.md"
    todo_path.write_text("")

    # Initial planning
    new_todo = planner(current_todo="", user_goal=user_goal)
    todo_path.write_text(new_todo)

    while True:
        tasks = parse_todo(todo_path.read_text())

        if all(t.status == "x" for t in tasks):
            break

        ready = [t for t in tasks if t.status == " "
                 and all(tasks[dep].status == "x" for dep in t.deps)]

        if not ready:
            # Either deadlocked or waiting on running tasks.
            # In a parallel version this is where you'd join on a running task.
            raise RuntimeError("Nothing ready and nothing running")

        for task in ready:
            mark_status(todo_path, task.id, "~")

            dep_files = []
            for dep_id in task.deps:
                dep_files.extend(tasks[dep_id].produced)

            result = dispatch_executor(
                agent_type=task.agent,
                query=task.query,
                expects=task.expects,
                input_files=dep_files,
                workspace=workspace,
            )

            # result is {"produced": [...], "notes": "..."}
            validate_files_exist(workspace, result["produced"])
            write_result(todo_path, task.id, result)
            mark_status(todo_path, task.id, "x")

        # Hand back to planner to review
        new_todo = planner(current_todo=todo_path.read_text(), user_goal=None)
        todo_path.write_text(new_todo)

    # Final pass
    final_todo = planner(current_todo=todo_path.read_text(), user_goal=None)
    todo_path.write_text(final_todo)
    return extract_summary(final_todo), workspace
```

That's the whole loop. Roughly 40 lines of real code once you fill in the parsing and dispatch helpers. The planner is one function. The executors are one function each (parameterized by agent type). The state lives entirely in `todo.md` and the workspace directory — restart-safe by construction.

---

## Things worth keeping in mind as you build

The planner is consulted between dispatches, not during them. Don't try to make it a live participant in execution. Its only job is to look at the file and decide if the plan still makes sense.

Executors are stateless from one task to the next. Their context is built fresh from `(query, expects, dep_files)`. Anything they want to persist must be written to a file.

The `notes` field on each task is the most underrated piece of the design. It's the executor's channel to flag judgment calls to the planner. The planner deciding "actually, given that note, let me tighten the next task's query" is what makes this more than just a static DAG runner.

When you need strict structure (e.g. feeding into deterministic code), specify it in the expects field as a strict format ("CSV with exactly these columns") and validate at the boundary. The files-first design supports strict structure when you want it; it just doesn't force it when you don't.

Keep the agent-type roster small (three to five is usually right). Each new type is a system prompt and a toolkit to maintain. Resist the temptation to add types for narrow specialties — usually the right move is to write a clearer query for an existing type.

The replan budget exists for a reason. Once you wire up a real planner, it will be tempted to revise the plan after every task. Cap it at three replans per run and bias the planner's review prompt against revising unless something genuinely changed.
