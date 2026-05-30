"""Report drafting pipeline: broad retrieval → outline → sections → critic → save."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits
from sqlalchemy import select

from agent import (
    RouterDeps,
    _build_model,
    _build_router,
    _expand_table_targets_with_pages,
    _peek_xlsx_columns,
    _render_page_context,
    _run_excel_on,
    _safe_filename,
)
from agent_schemas import Citation, PageTarget, RouterResult, TableFinding, TableTarget
from api.events import EventSink
from config import get_settings
from db import SessionLocal, utils
from db.models import Doc, ExtractedTable, Node
from report_schemas import (
    CritiqueResult,
    ExecutiveSummary,
    PageRef,
    ReportOutline,
    ReportResult,
    ReportSection,
    SectionDraft,
    TableRef,
)

log = logging.getLogger(__name__)


# ===========================================================================
# Agent prompts
# ===========================================================================

_REPORT_ROUTER_SYSTEM = """\
You are a document routing agent sourcing material for a REPORT, not answering one question.
Your job is to pick SPECIFIC pieces of evidence that should be read to draft a comprehensive report.

You will see:
- The report brief.
- A list of candidate documents with their top-level summaries.

Tools you can use (call as many as needed, in any order):
- `expand_doc(doc_id)`: top-level sections of a document.
- `expand_node(node_id)`: children of a tree node, with brief summaries.
- `peek_node(node_id)`: full summary of a node (use when brief is not enough).
- `list_doc_tables(doc_id)`: every extracted table in a doc.
- `peek_table(table_id)`: detailed schema + first few rows of one table.

Be COMPREHENSIVE. 10–20 page_targets and 5–15 table_targets is expected.
Cast a wide net — under-inclusion means a thin report.

Routing strategy:
1. Read doc summaries. Include every doc plausibly relevant to the brief.
2. For each plausible doc, call `expand_doc` ONCE, then drill into relevant sections.
3. Prefer taking page_targets at reasonable granularity (≤ ~25 pages per target is fine).
4. For numeric / tabular aspects of the brief, include table_targets with clear reasons.
5. NEVER call `expand_node` on a leaf node (has_children=false).
6. Do NOT try to write the report — emit targets only.

Rules:
- Each target must include a one-line `reason`.
- Recall matters more than precision for report sourcing.
"""


_OUTLINE_SYSTEM = """\
You are a report planner. Given a brief, target length, and retrieved page/table refs,
produce a non-overlapping outline.

Rules:
- Each fact should appear in exactly ONE section's must_cover list.
- Target section count by length: brief=3-4, standard=5-7, deep=8-12.
- Assign retrieved page refs (format "doc_id:start-end") and table ids to sections by relevance.
- must_cover lists pin key facts the section must address.
- section_id values must be short kebab-case identifiers (e.g. "exec-summary", "financials").
"""


_SECTION_SYSTEM = """\
You are a report section writer. Write ONE section using ONLY the provided material.

Rules:
- Use only the page content and tabular findings provided — do not invent facts.
- Cite inline as [doc_title, p12-p18].
- Match the tone implied by the overall brief.
- Return markdown for the section body only (no top-level report title).
"""


_CRITIC_SYSTEM = """\
You are a report critic. Compare the draft against the brief and list concrete gaps only.

Rules:
- For each gap, write a precise follow_up_query (specific search target).
- target_section is an existing section_id OR the literal string "new".
- Return empty gaps if the brief is fully covered.
- notes may summarize overall quality but gaps drive revision.
"""


_SUMMARY_SYSTEM = """\
You write an Executive Summary for a finished report.

You will receive:
- The original brief.
- The finished report body (all sections, already drafted).

Write a 4–8 sentence executive summary that:
- Reflects what the body ACTUALLY says — do NOT mention "data not available" if the body has data.
- Surfaces the 3–5 most important findings, with specific numbers from the body.
- Compares or contrasts across entities if the brief is comparative.
- Uses the same units as the body.
- Does NOT introduce facts that are not in the body.
- Does NOT include citations — those live in the body.

Return ONLY the summary paragraph(s) as markdown prose. No heading, no bullets unless the brief is bullet-shaped.
"""


# ===========================================================================
# Agent builders
# ===========================================================================

def _build_report_router() -> Agent[RouterDeps, RouterResult]:
    return _build_router(system_prompt=_REPORT_ROUTER_SYSTEM)


def _build_outline_agent() -> Agent[None, ReportOutline]:
    return Agent(
        _build_model(),
        output_type=ReportOutline,
        system_prompt=_OUTLINE_SYSTEM,
        retries=2,
    )


def _build_section_agent() -> Agent[None, SectionDraft]:
    return Agent(
        _build_model(),
        output_type=SectionDraft,
        system_prompt=_SECTION_SYSTEM,
        retries=2,
    )


def _build_critic_agent() -> Agent[None, CritiqueResult]:
    return Agent(
        _build_model(),
        output_type=CritiqueResult,
        system_prompt=_CRITIC_SYSTEM,
        retries=2,
    )


def _build_summary_agent() -> Agent[None, ExecutiveSummary]:
    return Agent(
        _build_model(),
        output_type=ExecutiveSummary,
        system_prompt=_SUMMARY_SYSTEM,
        retries=2,
    )


# ===========================================================================
# Ref resolution helpers
# ===========================================================================

def _page_ref_key(doc_id: str, start_page: int, end_page: int) -> str:
    return f"{doc_id}:{start_page}-{end_page}"


def _parse_page_ref_key(key: str) -> tuple[str, int, int]:
    doc_id, pages = key.split(":", 1)
    start_s, end_s = pages.split("-", 1)
    return doc_id, int(start_s), int(end_s)


def _leaf_summary_for_range(db, doc_id: str, start_page: int, end_page: int) -> str:
    nodes = list(db.scalars(select(Node).where(Node.doc_id == doc_id)))
    leaves = [n for n in nodes if not n.child_ids]
    best: Node | None = None
    best_span = 10**9
    for n in leaves:
        if n.start_page is None or n.end_page is None:
            continue
        if n.start_page <= end_page and n.end_page >= start_page:
            span = (n.end_page - n.start_page) + 1
            if span < best_span:
                best = n
                best_span = span
    if best and best.summary:
        return best.summary
    return ""


def _resolve_page_refs(page_targets: list[PageTarget]) -> list[PageRef]:
    refs: list[PageRef] = []
    with SessionLocal() as db:
        for t in page_targets:
            summary = _leaf_summary_for_range(db, t.doc_id, t.start_page, t.end_page)
            refs.append(PageRef(
                doc_id=t.doc_id,
                start_page=t.start_page,
                end_page=t.end_page,
                leaf_summary=summary,
                reason=t.reason,
            ))
    return refs


def _resolve_table_refs(table_targets: list[TableTarget]) -> list[TableRef]:
    refs: list[TableRef] = []
    with SessionLocal() as db:
        for t in table_targets:
            tbl = db.get(ExtractedTable, t.table_id)
            if tbl is None:
                continue
            cols = _peek_xlsx_columns(tbl.xlsx_bytes) if tbl.xlsx_bytes else []
            refs.append(TableRef(
                doc_id=tbl.doc_id,
                table_id=tbl.table_id,
                source_page=tbl.source_page,
                columns=cols,
                description=tbl.description,
                reason=t.reason,
            ))
    return refs


def _render_section_pages(section: ReportSection, page_refs_pool: list[PageRef]) -> str:
    pool_by_key = {
        _page_ref_key(p.doc_id, p.start_page, p.end_page): p for p in page_refs_pool
    }
    targets: list[PageTarget] = []
    for key in section.assigned_page_refs:
        if key not in pool_by_key:
            try:
                doc_id, start, end = _parse_page_ref_key(key)
            except ValueError:
                continue
            targets.append(PageTarget(
                doc_id=doc_id, start_page=start, end_page=end,
                reason=f"assigned to section {section.section_id}",
            ))
        else:
            p = pool_by_key[key]
            targets.append(PageTarget(
                doc_id=p.doc_id, start_page=p.start_page, end_page=p.end_page,
                reason=p.reason or f"assigned to section {section.section_id}",
            ))
    if not targets:
        return "_(no page content assigned)_"
    md, _, _ = _render_page_context(targets)
    return md


def _render_section_findings(
    section: ReportSection,
    table_findings: list[TableFinding],
) -> str:
    if not section.assigned_table_ids:
        return ""
    assigned = {tid for tid in section.assigned_table_ids}
    lines = [
        f"- Table {f.table_id} (doc {f.doc_id}): {f.finding}"
        for f in table_findings
        if f.table_id in assigned
    ]
    if not lines:
        return "_(no tabular findings for assigned tables)_"
    return "## Tabular findings\n" + "\n".join(lines)


def _count_words(md: str) -> int:
    return len(re.findall(r"\b\w+\b", md))


def _strip_leading_title(body: str, title: str) -> str:
    """Section writers sometimes prepend a `## Title` even though told not to.
    Drop a leading H1/H2 line if it matches the section title."""
    stripped = body.lstrip()
    for prefix in (f"## {title}", f"# {title}"):
        if stripped.startswith(prefix):
            after = stripped[len(prefix):].lstrip("\n").lstrip()
            return after
    return body.strip()


def _stitch_draft(
    outline: ReportOutline,
    sections: list[SectionDraft],
    summary: str | None = None,
) -> str:
    abstract = (summary or outline.abstract).strip()
    parts = [f"# {outline.title}\n", f"{abstract}\n"]
    all_citations: list[Citation] = []
    for draft in sections:
        body = _strip_leading_title(draft.markdown, draft.title)
        parts.append(f"\n## {draft.title}\n\n{body}\n")
        all_citations.extend(draft.citations)
    if all_citations:
        seen: set[tuple[str, str]] = set()
        source_lines: list[str] = []
        for c in all_citations:
            key = (c.doc_title, c.pages)
            if key in seen:
                continue
            seen.add(key)
            source_lines.append(f"- {c.doc_title}, pages {c.pages}")
        parts.append("\n## Sources\n" + "\n".join(source_lines) + "\n")
    return "\n".join(parts).strip() + "\n"


async def _write_executive_summary(
    brief: str,
    outline: ReportOutline,
    sections: list[SectionDraft],
    sink: EventSink,
) -> str:
    """Generate the final Executive Summary from the actual drafted body."""
    await sink.publish("summary_started", {})
    body_md = "\n\n".join(
        f"## {d.title}\n\n{_strip_leading_title(d.markdown, d.title)}"
        for d in sections
    )
    prompt = (
        f"Brief: {brief}\n\n"
        f"Outline title: {outline.title}\n\n"
        f"Finished report body:\n\n{body_md[:30000]}"
    )
    out = (await _build_summary_agent().run(prompt)).output
    await sink.publish("summary_done", {"n_words": _count_words(out.summary)})
    return out.summary.strip()


def _save_report_to_disk(report_id: str, workspace_id: str, draft_md: str) -> str:
    s = get_settings()
    out_dir = Path(s.report_output_dir) / workspace_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _safe_filename(f"{report_id}.md")
    out_path.write_text(draft_md, encoding="utf-8")
    return str(out_path.resolve())


# ===========================================================================
# Retrieval
# ===========================================================================

def _retrieval_prompt(brief: str, candidates: list[Doc], *, query: str | None = None) -> str:
    doc_lines = [
        f"- doc_id={d.doc_id}  title={d.title!r}  pages={d.n_pages}  tables={d.n_tables}\n"
        f"  summary: {(d.doc_summary or '')[:800]}"
        for d in candidates
    ]
    focus = query or brief
    return (
        f"Report brief: {brief}\n\n"
        f"Retrieval focus: {focus}\n\n"
        f"Candidate documents ({len(candidates)}):\n" + "\n".join(doc_lines) + "\n\n"
        "Pick comprehensive page_targets and table_targets for this report."
    )


async def _run_retrieval(
    *,
    workspace_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None,
    focus_query: str,
    sink: EventSink,
    hop: int,
    gap: str | None = None,
) -> tuple[list[PageRef], list[TableRef], list[PageTarget], list[TableTarget]]:
    payload: dict = {"hop": hop}
    if gap:
        payload["gap"] = gap
    await sink.publish("retrieval_started", payload)

    with SessionLocal() as db:
        stmt = select(Doc).where(Doc.workspace_id == workspace_id)
        if doc_ids:
            stmt = stmt.where(Doc.doc_id.in_(doc_ids))
        candidates = list(db.scalars(stmt))
    if not candidates:
        raise ValueError(f"No documents found in workspace_id={workspace_id}")

    router = _build_report_router()
    deps = RouterDeps(
        workspace_id=workspace_id,
        user_id=user_id,
        candidate_doc_ids=[d.doc_id for d in candidates],
    )
    s = get_settings()
    limits = UsageLimits(request_limit=s.agent_report_retrieval_request_limit)
    prompt = _retrieval_prompt(brief, candidates, query=focus_query)
    routed = (await router.run(prompt, deps=deps, usage_limits=limits)).output
    _expand_table_targets_with_pages(routed)

    page_refs = _resolve_page_refs(routed.page_targets)
    table_refs = _resolve_table_refs(routed.table_targets)
    await sink.publish("retrieval_done", {
        "hop": hop,
        "n_page_refs": len(page_refs),
        "n_table_refs": len(table_refs),
    })
    return page_refs, table_refs, routed.page_targets, routed.table_targets


async def _broad_retrieval(
    workspace_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None,
    sink: EventSink,
) -> tuple[list[PageRef], list[TableRef], list[TableFinding]]:
    page_refs, table_refs, _, table_targets = await _run_retrieval(
        workspace_id=workspace_id,
        user_id=user_id,
        brief=brief,
        doc_ids=doc_ids,
        focus_query=brief,
        sink=sink,
        hop=0,
    )
    findings = await _run_excel_on(table_targets, question=brief) if table_targets else []
    return page_refs, table_refs, findings


async def _targeted_retrieval(
    *,
    workspace_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None,
    focus_query: str,
    gap_topic: str,
    hop: int,
    sink: EventSink,
) -> tuple[list[PageRef], list[TableRef], list[TableFinding]]:
    new_pages, new_tables, _, table_targets = await _run_retrieval(
        workspace_id=workspace_id,
        user_id=user_id,
        brief=brief,
        doc_ids=doc_ids,
        focus_query=focus_query,
        sink=sink,
        hop=hop,
        gap=gap_topic,
    )
    findings = await _run_excel_on(table_targets, question=focus_query) if table_targets else []
    return new_pages, new_tables, findings


# ===========================================================================
# Phase functions
# ===========================================================================

async def _make_outline(
    brief: str,
    target_length: Literal["brief", "standard", "deep"],
    page_refs: list[PageRef],
    table_refs: list[TableRef],
    sink: EventSink,
) -> ReportOutline:
    await sink.publish("outline_started", {})
    page_lines = [
        f"- {_page_ref_key(p.doc_id, p.start_page, p.end_page)}: {p.leaf_summary[:300]}"
        for p in page_refs
    ]
    table_lines = [
        f"- {t.table_id} (doc {t.doc_id}, page {t.source_page}): cols={t.columns[:8]}"
        for t in table_refs
    ]
    prompt = (
        f"Brief: {brief}\nTarget length: {target_length}\n\n"
        f"Retrieved pages ({len(page_refs)}):\n" + "\n".join(page_lines) + "\n\n"
        f"Retrieved tables ({len(table_refs)}):\n" + "\n".join(table_lines)
    )
    agent = _build_outline_agent()
    outline = (await agent.run(prompt)).output
    await sink.publish("outline_done", {"outline": outline.model_dump()})
    return outline


async def _write_one_section(
    brief: str,
    section: ReportSection,
    page_refs: list[PageRef],
    table_findings: list[TableFinding],
    sink: EventSink,
) -> SectionDraft:
    await sink.publish("section_started", {
        "section_id": section.section_id,
        "title": section.title,
    })
    pages_md = _render_section_pages(section, page_refs)
    findings_md = _render_section_findings(section, table_findings)
    prompt = (
        f"Overall brief: {brief}\n\n"
        f"Section: {section.title}\nPurpose: {section.purpose}\n"
        f"Must cover: {', '.join(section.must_cover) or '(see purpose)'}\n\n"
        f"Page content:\n{pages_md}\n\n{findings_md}"
    )
    agent = _build_section_agent()
    draft = (await agent.run(prompt)).output
    draft.section_id = section.section_id
    draft.title = section.title
    draft.n_words = _count_words(draft.markdown)
    await sink.publish("section_done", {
        "section_id": section.section_id,
        "n_words": draft.n_words,
        "n_citations": len(draft.citations),
    })
    return draft


async def _write_all_sections(
    brief: str,
    outline: ReportOutline,
    page_refs: list[PageRef],
    table_findings: list[TableFinding],
    sink: EventSink,
) -> list[SectionDraft]:
    s = get_settings()
    sem = asyncio.Semaphore(s.report_section_concurrency)

    async def _guarded(section: ReportSection) -> SectionDraft:
        async with sem:
            return await _write_one_section(brief, section, page_refs, table_findings, sink)

    return list(await asyncio.gather(*[_guarded(sec) for sec in outline.sections]))


async def _critique(
    brief: str,
    outline: ReportOutline,
    draft_md: str,
    sink: EventSink,
    hop: int,
) -> CritiqueResult:
    await sink.publish("critic_started", {"hop": hop})
    agent = _build_critic_agent()
    prompt = (
        f"Brief: {brief}\n\nOutline title: {outline.title}\n\n"
        f"Draft markdown:\n{draft_md[:12000]}"
    )
    critique = (await agent.run(prompt)).output
    await sink.publish("critic_done", {
        "hop": hop,
        "gaps": [g.model_dump() for g in critique.gaps],
        "notes": critique.notes,
    })
    return critique


def _merge_page_refs(pool: list[PageRef], new_refs: list[PageRef]) -> list[PageRef]:
    seen = {_page_ref_key(p.doc_id, p.start_page, p.end_page) for p in pool}
    merged = list(pool)
    for p in new_refs:
        key = _page_ref_key(p.doc_id, p.start_page, p.end_page)
        if key not in seen:
            seen.add(key)
            merged.append(p)
    return merged


def _append_table_findings(
    pool: list[TableFinding],
    new_findings: list[TableFinding],
) -> list[TableFinding]:
    seen = {f.table_id for f in pool}
    merged = list(pool)
    for f in new_findings:
        if f.table_id not in seen:
            seen.add(f.table_id)
            merged.append(f)
    return merged


async def _refine(
    *,
    workspace_id: str,
    user_id: str,
    brief: str,
    target_length: Literal["brief", "standard", "deep"],
    outline: ReportOutline,
    section_drafts: list[SectionDraft],
    page_refs: list[PageRef],
    table_findings: list[TableFinding],
    critique: CritiqueResult,
    doc_ids: list[str] | None,
    sink: EventSink,
    hop: int,
) -> tuple[ReportOutline, list[SectionDraft], list[PageRef], list[TableFinding]]:
    drafts_by_id = {d.section_id: d for d in section_drafts}
    sections_by_id = {s.section_id: s for s in outline.sections}

    for gap in critique.gaps:
        new_pages, new_tables, new_findings = await _targeted_retrieval(
            workspace_id=workspace_id,
            user_id=user_id,
            brief=brief,
            doc_ids=doc_ids,
            focus_query=gap.follow_up_query,
            gap_topic=gap.topic,
            hop=hop,
            sink=sink,
        )
        page_refs = _merge_page_refs(page_refs, new_pages)
        table_findings = _append_table_findings(table_findings, new_findings)

        for p in new_pages:
            key = _page_ref_key(p.doc_id, p.start_page, p.end_page)
            if gap.target_section == "new":
                continue
            sec = sections_by_id.get(gap.target_section)
            if sec and key not in sec.assigned_page_refs:
                sec.assigned_page_refs.append(key)
        for t in new_tables:
            if gap.target_section == "new":
                continue
            sec = sections_by_id.get(gap.target_section)
            if sec and t.table_id not in sec.assigned_table_ids:
                sec.assigned_table_ids.append(t.table_id)

        if gap.target_section == "new":
            sec_id = f"gap-{hop}-{len(outline.sections)}"
            new_section = ReportSection(
                section_id=sec_id,
                title=gap.topic,
                purpose=f"Address gap: {gap.topic}",
                assigned_page_refs=[
                    _page_ref_key(p.doc_id, p.start_page, p.end_page) for p in new_pages
                ],
                assigned_table_ids=[t.table_id for t in new_tables],
                must_cover=[gap.topic],
            )
            outline.sections.append(new_section)
            sections_by_id[sec_id] = new_section
            draft = await _write_one_section(brief, new_section, page_refs, table_findings, sink)
            drafts_by_id[sec_id] = draft
        else:
            sec = sections_by_id.get(gap.target_section)
            if sec is None:
                continue
            draft = await _write_one_section(brief, sec, page_refs, table_findings, sink)
            drafts_by_id[sec.section_id] = draft

    ordered = [
        drafts_by_id[s.section_id]
        for s in outline.sections
        if s.section_id in drafts_by_id
    ]
    return outline, ordered, page_refs, table_findings


def _persist_report(
    *,
    workspace_id: str,
    user_id: str,
    target_length: str,
    report_id: str,
    result: ReportResult,
    status: str = "complete",
) -> None:
    with SessionLocal() as db:
        existing = utils.get_report(db, report_id)
        fields = {
            "status": status,
            "outline_json": result.outline.model_dump(),
            "draft_md": result.draft_md,
            "output_path": result.output_path,
            "n_sections": result.n_sections,
            "n_words": result.n_words,
            "n_hops": result.n_hops,
            "latency_ms": result.latency_ms,
        }
        if existing:
            utils.update_report(db, report_id, **fields)
        else:
            utils.create_report(
                db,
                report_id=report_id,
                workspace_id=workspace_id,
                user_id=user_id,
                brief=result.brief,
                target_length=target_length,
                **fields,
            )


# ===========================================================================
# Orchestrator
# ===========================================================================

async def draft_report(
    *,
    workspace_id: str,
    user_id: str,
    brief: str,
    doc_ids: list[str] | None = None,
    target_length: Literal["brief", "standard", "deep"] = "standard",
    report_id: str | None = None,
    sink: EventSink = EventSink(),
) -> ReportResult:
    t0 = time.monotonic()
    s = get_settings()
    report_id = report_id or f"rep_{uuid.uuid4().hex[:12]}"

    with SessionLocal() as db:
        utils.create_report(
            db,
            report_id=report_id,
            workspace_id=workspace_id,
            user_id=user_id,
            brief=brief,
            target_length=target_length,
            status="running",
        )

    await sink.publish("report_started", {
        "report_id": report_id,
        "brief": brief,
        "workspace_id": workspace_id,
        "target_length": target_length,
    })

    try:
        page_refs, table_refs, table_findings = await _broad_retrieval(
            workspace_id, user_id, brief, doc_ids, sink,
        )
        outline = await _make_outline(brief, target_length, page_refs, table_refs, sink)
        section_drafts = await _write_all_sections(
            brief, outline, page_refs, table_findings, sink,
        )
        draft_md = _stitch_draft(outline, section_drafts)
        await sink.publish("draft_assembled", {"n_words": _count_words(draft_md)})

        n_hops = 0
        for hop in range(s.report_max_hops):
            n_hops = hop + 1
            critique = await _critique(brief, outline, draft_md, sink, hop)
            if not critique.gaps:
                break
            outline, section_drafts, page_refs, table_findings = await _refine(
                workspace_id=workspace_id,
                user_id=user_id,
                brief=brief,
                target_length=target_length,
                outline=outline,
                section_drafts=section_drafts,
                page_refs=page_refs,
                table_findings=table_findings,
                critique=critique,
                doc_ids=doc_ids,
                sink=sink,
                hop=hop,
            )
            draft_md = _stitch_draft(outline, section_drafts)
            await sink.publish("draft_assembled", {"n_words": _count_words(draft_md)})

        # Refresh the Executive Summary from the actual drafted body
        # (the outline's `abstract` was written before sections existed).
        final_summary = await _write_executive_summary(brief, outline, section_drafts, sink)
        outline.abstract = final_summary
        draft_md = _stitch_draft(outline, section_drafts, summary=final_summary)

        output_path = _save_report_to_disk(report_id, workspace_id, draft_md)
        await sink.publish("saved", {"path": output_path})

        result = ReportResult(
            report_id=report_id,
            brief=brief,
            outline=outline,
            sections=section_drafts,
            draft_md=draft_md,
            output_path=output_path,
            n_sections=len(section_drafts),
            n_words=_count_words(draft_md),
            n_hops=n_hops,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        _persist_report(
            workspace_id=workspace_id,
            user_id=user_id,
            target_length=target_length,
            report_id=report_id,
            result=result,
        )
        await sink.publish("complete", result.model_dump())
        return result

    except Exception as e:
        with SessionLocal() as db:
            utils.update_report(db, report_id, status="error")
        await sink.publish("error", {
            "error_class": type(e).__name__,
            "message": str(e),
        })
        raise
