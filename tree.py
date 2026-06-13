"""Build a hierarchical node tree for a parsed document.

Buckets consecutive pages into leaf nodes up to MAX_LEAF_TOKENS, summarises
each leaf, then groups leaves into parents and summarises upward until a
single root remains. Persists all nodes and backfills pages.node_id and
docs.doc_summary.

    await build_tree(doc_id, workspace_id, db)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from api.events import EventSink
from config import get_settings

if TYPE_CHECKING:
    from db.models import Page

# All tunables come from config / .env — see Settings in config.py.

log = logging.getLogger(__name__)

_ENCODING = None


def _count_tokens(text: str) -> int:
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken
            _ENCODING = tiktoken.get_encoding("cl100k_base")
        except Exception:
            pass
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return len(text) // 4


@dataclass
class _Node:
    node_id:   str
    start_page: int
    end_page:  int
    table_ids: list[str]
    content:   str
    summary:   str = ""
    title:     str = ""
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)
    depth:     int = 0


def _new_id() -> str:
    return f"node_{uuid.uuid4().hex[:12]}"


def _bucket_pages(pages: list[Page], max_leaf: int, min_leaf: int) -> list[_Node]:
    leaves: list[_Node] = []
    acc: list[Page] = []
    acc_tok = 0

    def flush() -> None:
        if not acc:
            return
        tids: list[str] = []
        for p in acc:
            tids.extend(p.table_ids or [])
        leaves.append(_Node(
            node_id=_new_id(),
            start_page=acc[0].page_n,
            end_page=acc[-1].page_n,
            table_ids=tids,
            content="\n\n".join(p.prose_text or "" for p in acc),
        ))

    for page in pages:
        tok = _count_tokens(page.prose_text or "")
        if acc_tok + tok > max_leaf and acc_tok >= min_leaf:
            flush()
            acc, acc_tok = [], 0
        acc.append(page)
        acc_tok += tok
    flush()

    # merge a short tail into the previous leaf
    if len(leaves) >= 2 and _count_tokens(leaves[-1].content) < min_leaf:
        prev, tail = leaves[-2], leaves.pop()
        prev.end_page = tail.end_page
        prev.table_ids.extend(tail.table_ids)
        prev.content += "\n\n" + tail.content

    return leaves


def _group_into_parents(children: list[_Node], max_children: int) -> list[_Node]:
    parents: list[_Node] = []
    for child in children:
        if not parents or len(parents[-1].child_ids) >= max_children:
            parents.append(_Node(
                node_id=_new_id(),
                start_page=child.start_page,
                end_page=child.end_page,
                table_ids=[],
                content="",
            ))
        p = parents[-1]
        p.child_ids.append(child.node_id)
        p.end_page = child.end_page
        child.parent_id = p.node_id
        for tid in child.table_ids:
            if tid not in p.table_ids:
                p.table_ids.append(tid)

    child_map = {c.node_id: c for c in children}
    for p in parents:
        p.content = "\n\n---\n\n".join(
            f"Section: {child_map[cid].title}\n{child_map[cid].summary}"
            for cid in p.child_ids
            if cid in child_map
        )
    return parents


_LEAF_PROMPT = """\
You are an expert document analyst. Below is raw text extracted from pages {start}–{end} of a document.

Your task is to write a DETAILED summary of this section that will be used as an index node in a \
document retrieval tree. It must contain enough information for a reasoning system to decide \
whether this section is relevant to an incoming question — WITHOUT needing to read the original text.

Follow this structure exactly:

## Overview
2–3 sentences describing the topic, purpose, and scope of this section.

## Key Facts & Findings
Bullet list of every important fact, figure, percentage, date, name, or conclusion. \
Be specific — include actual numbers and values, not vague references.

## Tables & Data
For each table in this section, write one bullet:
- Table <id or name if known>: what it contains, key columns, notable values or ranges.
If no tables, write "None."

## Entities & Terms
Bullet list of important named entities (organisations, products, people, places, metrics, \
definitions) with a one-line explanation of their role in this section.

## Connections to Broader Document
1–2 sentences on how this section relates to or sets up other parts of the document (if apparent).

---
At the very end, on its own line, write:
TITLE: <a specific 4–8 word title that uniquely describes this section>

---
CONTENT:
{content}
"""

_PARENT_PROMPT = """\
You are an expert document analyst. Below are summaries of {n_children} consecutive sections \
from a document (pages {start}–{end}).

Your task is to write a CONCISE but information-dense roll-up summary of this group of sections. \
This summary will sit at a higher level of a retrieval tree and must be accurate enough for a \
reasoning system to route questions to the right sub-section.

Follow this structure exactly:

## Overview
2–3 sentences on the overall theme and purpose of this group of sections.

## Key Themes & Topics
Bullet list of the main themes, arguments, or topics covered across all child sections.

## Critical Facts & Data Points
Bullet list of the most important facts, numbers, or findings from any child section — \
keep only what a downstream question might target.

## Tables & Structured Data
List any tables or datasets present across child sections, with a brief description of each.
If none, write "None."

## Coverage
Pages {start}–{end}. Sections covered: {titles}

---
At the very end, on its own line, write:
TITLE: <a specific 4–8 word title summarising this group>

---
CHILD SECTION SUMMARIES:
{content}
"""


def build_leaf_summary_prompt(*, start: int, end: int, content: str) -> str:
    return _LEAF_PROMPT.format(
        start=start,
        end=end,
        content=content,
    )


def build_parent_summary_prompt(
    *,
    n_children: int,
    start: int,
    end: int,
    titles: str,
    content: str,
) -> str:
    return _PARENT_PROMPT.format(
        n_children=n_children,
        start=start,
        end=end,
        titles=titles,
        content=content,
    )


def split_summary_title(text: str) -> tuple[str, str]:
    if "TITLE:" in text:
        summary, _, title_line = text.rpartition("TITLE:")
        return summary.strip(), title_line.strip()
    cleaned = text.strip()
    return cleaned, cleaned[:80].split("\n")[0]


async def _summarise(client: AsyncOpenAI, model: str, node: _Node, is_leaf: bool) -> None:
    if is_leaf:
        prompt = build_leaf_summary_prompt(
            start=node.start_page,
            end=node.end_page,
            content=node.content,
        )
    else:
        child_titles = ", ".join(
            line.replace("Section: ", "")
            for line in node.content.splitlines()
            if line.startswith("Section: ")
        ) or "—"
        prompt = build_parent_summary_prompt(
            n_children=len(node.child_ids),
            start=node.start_page,
            end=node.end_page,
            titles=child_titles,
            content=node.content,
        )

    resp = await client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.choices[0].message.content or ""
    node.summary, node.title = split_summary_title(text)


async def _summarise_level(
    nodes: list[_Node], is_leaf: bool, client: AsyncOpenAI, model: str, concurrency: int
) -> None:
    label = "leaf" if is_leaf else "parent"
    log.info("summarising %d %s node(s) (concurrency=%d)", len(nodes), label, concurrency)
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def run(node: _Node) -> None:
        nonlocal done
        t0 = time.monotonic()
        async with sem:
            await _summarise(client, model, node, is_leaf)
        done += 1
        log.debug(
            "[%s] %s p%s–p%s done in %.1fs  title=%r",
            label, node.node_id, node.start_page, node.end_page,
            time.monotonic() - t0, node.title,
        )
        log.info("[%s] %d/%d complete", label, done, len(nodes))

    await asyncio.gather(*[run(n) for n in nodes])


async def build_tree(
    doc_id: str,
    workspace_id: str,
    db: Session,
    sink: EventSink = EventSink(),
) -> str | None:
    """Build and persist the node tree for a doc. Returns root node_id."""
    from db import utils
    from db.models import Node

    await sink.publish("tree_started", {"doc_id": doc_id})
    pages = utils.list_pages(db, doc_id)
    if not pages:
        log.warning("build_tree: no pages found for doc_id=%s", doc_id)
        return None

    t_total = time.monotonic()
    s = get_settings()
    client = AsyncOpenAI(api_key=s.openai_api_key, base_url=s.openai_base_url)
    model  = s.openai_model
    max_leaf = s.tree_max_leaf_tokens
    min_leaf = s.tree_min_leaf_tokens
    max_ch   = s.tree_max_children
    concur   = s.tree_concurrency

    log.info("build_tree start  doc=%s  pages=%d  model=%s", doc_id, len(pages), model)

    leaves = _bucket_pages(pages, max_leaf, min_leaf)
    log.info("bucketed into %d leaf node(s)  (max=%d tok, min=%d tok)", len(leaves), max_leaf, min_leaf)
    await _summarise_level(leaves, is_leaf=True, client=client, model=model, concurrency=concur)
    await sink.publish("tree_leaves_summarised", {"n_leaves": len(leaves)})

    all_nodes: list[_Node] = list(leaves)
    current = leaves
    level = 1
    while len(current) > 1:
        parents = _group_into_parents(current, max_ch)
        log.info("level %d: grouped %d nodes into %d parent(s)", level, len(current), len(parents))
        await _summarise_level(parents, is_leaf=False, client=client, model=model, concurrency=concur)
        await sink.publish("tree_level_done", {"level": level, "n_nodes": len(parents)})
        all_nodes.extend(parents)
        current = parents
        level += 1

    root = current[0]
    log.info("tree depth=%d  total nodes=%d  root=%s", level, len(all_nodes), root.node_id)

    # assign depths from root
    depth_map: dict[str, int] = {root.node_id: 0}
    queue = [root]
    lookup = {n.node_id: n for n in all_nodes}
    while queue:
        p = queue.pop(0)
        for cid in p.child_ids:
            if cid in lookup:
                depth_map[cid] = depth_map[p.node_id] + 1
                queue.append(lookup[cid])
    for n in all_nodes:
        n.depth = depth_map.get(n.node_id, 0)

    # Persist all nodes + the pages.node_id backfill in ONE transaction.
    #
    # nodes.parent_id is a self-referential FK and pages.node_id FKs to
    # nodes.node_id. Both are declared DEFERRABLE INITIALLY DEFERRED (see
    # db/models/models.py + migration 03d439cba9e5), so Postgres validates them
    # at COMMIT over the complete row set rather than per row at insert. That
    # makes this batch insert order-independent: children may be added before
    # their parent and the tree still commits, while a genuinely dangling
    # parent_id is still rejected at commit. One add_all + one commit keeps the
    # whole tree atomic — a mid-write failure rolls everything back (no orphan
    # nodes, no half-built hierarchy). Field values are identical to the old
    # per-node loop.
    log.info("persisting %d nodes to db ...", len(all_nodes))
    db.add_all([
        Node(
            node_id=n.node_id,
            workspace_id=workspace_id,
            doc_id=doc_id,
            parent_id=n.parent_id,
            depth=n.depth,
            title=n.title,
            start_page=n.start_page,
            end_page=n.end_page,
            summary=n.summary,
            table_ids=n.table_ids or [],
            child_ids=n.child_ids or [],
        )
        for n in all_nodes
    ])

    # backfill pages.node_id — same transaction as the node inserts so the FK
    # from pages.node_id -> nodes.node_id is satisfied at the single commit.
    leaf_for_page = {
        pg: leaf.node_id
        for leaf in leaves
        for pg in range(leaf.start_page, leaf.end_page + 1)
    }
    for page in pages:
        page.node_id = leaf_for_page.get(page.page_n)
    db.commit()

    # backfill doc summary
    doc = utils.get_doc(db, doc_id)
    if doc:
        doc.doc_summary = root.summary
        db.commit()

    log.info("build_tree done  doc=%s  root=%s  elapsed=%.1fs", doc_id, root.node_id, time.monotonic() - t_total)
    await sink.publish("tree_done", {"root_id": root.node_id})
    return root.node_id
