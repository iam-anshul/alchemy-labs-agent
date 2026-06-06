from formats_pydantic import TaskSpec, QueryRun

def indent_continuation(text: str) -> str:
    """For multi-line query/expects strings, indent continuation lines so
    they hang under the field name in markdown."""
    lines = text.strip().split("\n")
    if len(lines) == 1:
        return lines[0]
    return lines[0] + "\n" + "\n".join(f"  {line}" for line in lines[1:])

def format_list(items: list[str]) -> str:
    """Render a list field. Empty list -> empty string. Single item -> inline.
    Multiple items -> indented bullets on following lines."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "\n" + "\n".join(f"    - {item}" for item in items)

def render_task(task: TaskSpec) -> str:

    symbol = ""

    match task.status:
        case "pending":
            symbol = "[ ]"
        case "dispatched":
            symbol = "[~]"
        case "completed":
            symbol = "[x]"
        case "failed":
            symbol = "[!]"

    checkbox = symbol
    deps_str = ", ".join(getattr(d, "id", d) for d in task.deps) if task.deps else "none"
    produced_str = format_list(task.produced)
    notes_str = task.notes.strip() or ""
    # task.error is intentionally NOT rendered here. todo.md is user-facing;
    # executor failure diagnostics go to the planner via an internal channel in
    # planner() instead. The [!] checkbox still signals failure to the user.

    return (
        f"## {checkbox} {task.id} — {task.title}\n"
        f"- agent: {task.agent}\n"
        f"- deps: {deps_str}\n"
        f"- query: {indent_continuation(task.query)}\n"
        f"- expects: {indent_continuation(task.expects)}\n"
        f"- produced: {produced_str}\n"
        f"- notes: {notes_str}\n"
    )


def render_todo(run: Run) -> str:
    parts = []

    # Header
    parts.append(f"# Goal\n{run.goal}\n")
    parts.append(f"# Workspace\n{run.workspace}\n")
    parts.append(
        f"# Status\n"
        f"Started: {run.started_at.isoformat()}\n"
        f"Replans used: {run.replans_used} / {run.replan_budget}\n"
    )

    # Tasks — preserve insertion order from the list
    parts.append("# Tasks\n")
    for task in run.plan.tasks:
        parts.append(render_task(task))

    # Plan-level notes — optional, so handle None safely; show "(none)" placeholder
    # to keep the section header present in the rendered file.
    notes_body = (run.plan.notes or "").strip() or "(none)"
    parts.append(f"# Notes\n{notes_body}\n")

    return "\n".join(parts)



