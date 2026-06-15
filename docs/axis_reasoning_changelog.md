# Evidence-Informed Axis Reasoning — Changelog

**Added:** June 15, 2026

This change adds a selective meta-reasoning layer to the planner. For complex
requests, the planner can stop after gathering enough evidence, ask a hidden
axis agent which reasoning dimensions matter, and then append a better-informed
set of tasks.

The user continues to see an ordinary todo list. Axis checkpoints, prompts, and
critiques remain internal.

---

## Why this exists

The original planner had to design the complete task graph before any agent had
read the evidence. That works for predictable requests, but it is weak for
questions where discovery determines the correct analysis.

Examples:

- A company comparison may reveal that refinancing risk matters more than
  headline growth.
- A medical question may reveal an interaction between age, kidney function,
  dose, and another medicine.
- A causal investigation may uncover plausible confounders that require a
  different evidence search.

The planner should not guess all of those dimensions from the user question
alone. The new flow lets it gather evidence first and plan the deeper analysis
afterward.

---

## Runtime flow

```text
User question
    |
    v
Initial planner
    |
    |-- simple request: create the complete plan normally
    |
    `-- complex request: create an evidence-gathering segment
                         ending in an axis checkpoint
                                      |
                                      v
                              Executor completes task
                                      |
                                      v
                             Hidden axis reasoner
                                      |
                          detailed reasoning string
                                      |
                                      v
                           Append-only axis planner
                                      |
                          append the next task segment
                                      |
                                      v
                              Continue execution
```

An axis pass runs once for a selected checkpoint task, not several times per
task. The run allows at most two checkpoints by default.

---

## Planner checkpoint fields

[`formats_pydantic.py`](../formats_pydantic.py) adds two fields to `TaskSpec`:

```python
axis_checkpoint: bool = False
axis_focus: str | None = None
```

`axis_focus` is required whenever `axis_checkpoint` is true. It describes:

1. The downstream decision that evidence may change.
2. Candidate domains worth inspecting.
3. Findings that should cause additional or different work.

Example:

```text
Decision to revisit: which analyses are necessary for a five-year
risk-adjusted company comparison.

Candidate domains: financial quality, valuation, capital structure,
competitive position, and downside risk.

Change signals: weak cash conversion, refinancing exposure, concentration
risk, conflicting forecasts, or conclusions sensitive to assumptions.
```

These fields are available in the planner's structured-output schema but are
excluded from normal Pydantic serialization. They therefore do not appear in:

- `todo.md`
- final `PlanOutput` API responses
- persisted user-facing plan artifacts
- normal UI task descriptions

`render_todo.py` also never renders them.

---

## Planner selection rules

[`system_prompts.py`](../system_prompts.py) now teaches the planner when a
checkpoint is useful.

Use one when:

- the task produces substantive evidence;
- the correct downstream analysis cannot yet be designed confidently;
- evidence may expose material risks, assumptions, contradictions, causal
  alternatives, interactions, or missing domains;
- the result is readable text such as Markdown, JSON, HTML, CSV, or plain text.

Avoid one for:

- simple retrieval or summarization;
- known-field extraction;
- deterministic calculations;
- binary-only downloads;
- formatting and file conversion;
- final artifact creation;
- routine research that cannot change the remaining task graph.

### Segment boundary rule

If the planner selects a checkpoint, the current plan must end at that task.
It must not pre-create speculative synthesis or delivery tasks after the
checkpoint.

This is important because execution is sequential. Ending the segment ensures
that evidence is reviewed and new tasks are appended before any premature final
analysis can run.

The controller validates this rule for:

- initial planning;
- plans regenerated after initial human feedback;
- ordinary operational replans.

---

## Axis reasoner

[`orchestrator.py`](../orchestrator.py) adds `axisAgent`.

Its structured output is intentionally simple:

```python
class AxisReasoningOutput(BaseModel):
    reasoning: str
```

The result is one detailed planning brief rather than a large nested schema.
This keeps Qwen structured output reliable while still giving the next planner
enough information to create targeted tasks.

Native model thinking is enabled only for this hidden agent:

```python
extra_body={"enable_thinking": True}
```

The reasoning string must explain:

- the real decision frame;
- evidence strengths, weaknesses, applicability, and contradictions;
- material reasoning axes;
- plausible branches or falsification conditions;
- relevant inference operators;
- evidence supporting and challenging each direction;
- cross-axis interactions;
- precise information gaps;
- which dimensions require research versus final synthesis;
- whether one later checkpoint could be justified.

It must not:

- answer the user's original question;
- create tasks;
- make the final recommendation;
- fabricate evidence;
- output private chain-of-thought;
- dump the entire universal axis catalog.

---

## Universal axis catalog

The axis prompt contains a broad cross-domain search space. The agent scans it
internally but returns only dimensions that can materially change the answer or
remaining plan.

The catalog covers:

1. Objective, framing, scope, baselines, and thresholds
2. Assumptions and dependency structure
3. Evidence provenance, authority, independence, and bias
4. Evidence quality, validity, precision, and applicability
5. Logical and inferential validity
6. Competing hypotheses, alternatives, and option structure
7. Causality, mechanisms, confounders, and counterfactuals
8. Quantitative and statistical reasoning
9. Time, sequence, persistence, and regime change
10. Risk, harm, tail events, and failure modes
11. Benefits, costs, opportunity costs, and tradeoffs
12. System dependencies, feedback, emergence, and nonlinear effects
13. Stakeholders, incentives, gaming, and strategic response
14. Technical and operational feasibility
15. Governance, law, ethics, privacy, fairness, and rights
16. Security, misuse, threats, containment, and recovery
17. Communication, confidence calibration, and interpretation
18. Verification, falsification, monitoring, and stopping conditions

The prompt also includes inference operators such as deduction, induction,
abduction, Bayesian updating, causal reasoning, analogy, counterexamples,
sensitivity analysis, scenario analysis, strategic reasoning, systems
reasoning, and defeasible reasoning.

The governing instruction is:

> Scan broadly, select narrowly, branch explicitly, search for
> disconfirmation, and analyze material interactions.

Normally the critique should identify three to seven axes, with a maximum of
ten for genuinely broad multi-domain work.

---

## Evidence supplied to the axis agent

[`api/routes/chat.py`](../api/routes/chat.py) builds an internal evidence bundle
after a checkpoint succeeds.

It includes:

- the original user question;
- the current user-visible plan;
- checkpoint identity and `axis_focus`;
- the completed checkpoint task;
- all completed transitive dependency tasks;
- task queries and executor notes;
- contents of readable produced artifacts.

Evidence loading is bounded:

- maximum `20,000` characters from one file;
- maximum `80,000` characters across the bundle;
- supported text extensions include Markdown, text, JSON, CSV, HTML, XML, and
  YAML.

Unsupported binary artifacts are identified but not injected as text. The
planner prompt therefore requires checkpoint tasks to produce a readable
evidence summary rather than a binary file alone.

---

## Append-only axis planner

`axisAppendPlannerAgent` converts the critique into the next executable plan
segment.

It returns:

```python
class AxisPlanAddition(BaseModel):
    tasks: list[TaskSpec]
    notes: str | None
```

Unlike the ordinary replanner, it does not regenerate `PlanOutput`. It returns
only new tasks.

This enforces the user's intended rule:

> Work completed before the checkpoint remains untouched. The planner starts
> from that checkpoint and appends the next work.

### Controller validation

Before accepting an addition, the controller verifies:

- every task id is new and unique;
- dependencies reference existing or earlier newly-added tasks;
- every new task is downstream of the completed checkpoint;
- at most one new checkpoint exists in a segment;
- a new checkpoint is the final task in that segment;
- checkpoint budget remains.

Invalid additions are returned to the append planner with the specific
validation error. It receives up to three attempts.

When accepted:

- tasks are appended without replacing the existing list;
- new task state is reset to `pending`;
- the updated `todo.md` is published;
- scheduler selection restarts against the expanded dependency graph.

---

## Checkpoint and replan budgets

The two mechanisms have separate purposes and budgets:

| Mechanism | Default budget | Purpose |
| --- | ---: | --- |
| Ordinary replan | 3 | Recover from failures, missing inputs, or executor surprises |
| Axis checkpoint | 2 | Reconsider analytical dimensions after material evidence |

A second checkpoint is allowed only when the first review leads to targeted
evidence gathering whose findings could materially change final synthesis.

Checkpoint metadata is preserved across ordinary full-plan replans. The
ordinary replanner receives an internal checkpoint-state section and cannot
move runnable tasks after a pending checkpoint.

---

## User-visible behavior

Users see only normal tasks such as:

```text
[x] Gather company evidence
[ ] Analyze cash-flow quality and refinancing exposure
[ ] Test valuation under adverse scenarios
[ ] Prepare the final comparison
```

They do not see:

- `axis_checkpoint`
- `axis_focus`
- the universal axis catalog
- the axis critique
- hidden model reasoning
- append-planner control instructions

UI messages use ordinary language such as:

- "Reviewing completed evidence"
- "Plan updated from completed evidence"

---

## Finance example

User request:

> Compare Company A and Company B as five-year investments.

Initial segment:

```text
t1 Gather financial, valuation, debt, competitive, and risk evidence
   axis_checkpoint=true
```

Evidence reveals:

- Company A has faster growth but negative free cash flow.
- Company A has debt maturing before expected cash-flow breakeven.
- Company B grows slowly but has stable cash generation.
- Company B has geographic concentration risk.

The hidden critique may identify:

- growth quality;
- refinancing and dilution risk;
- valuation sensitivity;
- concentration risk;
- downside asymmetry;
- interaction between debt timing and cash-flow breakeven.

The append planner can then add:

```text
t2 Analyze cash conversion and refinancing capacity
t3 Model bear, base, and bull valuation scenarios
t4 Evaluate concentration and resilience
t5 Produce the evidence-grounded comparison
```

The initial planner did not have to guess those tasks before reading evidence.

---

## Files changed

| File | Change |
| --- | --- |
| [`formats_pydantic.py`](../formats_pydantic.py) | Added checkpoint controls, simple axis output, append-only output, validation, and checkpoint budget state. |
| [`orchestrator.py`](../orchestrator.py) | Added the hidden thinking-enabled axis agent and append-only planner agent. |
| [`system_prompts.py`](../system_prompts.py) | Added planner checkpoint rules, extensive universal axis prompt, and append-planner contract. |
| [`api/routes/chat.py`](../api/routes/chat.py) | Added evidence bundling, checkpoint execution, append validation, task appending, budget enforcement, and replan preservation. |
| [`test_axis_planning.py`](../test_axis_planning.py) | Added focused schema, privacy, and validation tests. |

---

## Verification

Verified:

- Python compilation succeeds for all changed modules.
- AST parsing succeeds.
- `git diff --check` reports no whitespace errors.
- Five focused axis-planning tests pass.
- A checkpoint without `axis_focus` is rejected.
- Axis output contains only the detailed `reasoning` string.
- Empty task additions are rejected.
- Internal checkpoint fields remain in the planner schema.
- Internal checkpoint fields are excluded from normal serialization and
  `todo.md`.

Environment limitations observed during verification:

- Importing the complete orchestrator in the current environment is blocked by
  the missing `psycopg` package.
- Two pre-existing workspace-output tests fail under the installed Pydantic
  version because those tests pass UUID and datetime values into string fields.
  Those failures are unrelated to axis reasoning.

