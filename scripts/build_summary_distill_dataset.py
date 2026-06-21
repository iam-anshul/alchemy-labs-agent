"""Build a message-format distillation dataset for document-tree summaries.

Examples:
    PYTHONPATH=. python scripts/build_summary_distill_dataset.py \
        --hf-dataset my-org/my-corpus \
        --hf-split train \
        --text-field text \
        --output data/summary_distill/summary_messages.jsonl

    PYTHONPATH=. python scripts/build_summary_distill_dataset.py \
        --input docs.jsonl \
        --text-field text \
        --output data/summary_distill/summary_messages.jsonl


Finance — US public (target ~65K)

eloukas/edgar-corpus — ~6.7M filings, 1993-2020, section-split (Item 1, 1A, 7/MD&A pre-extracted). Sample 25K section chunks, stratified across items and years. Your single best finance source.
JanosAudran/financial-reports-sec — alternative EDGAR cut with sentence-level structure, use to backfill if edgar-corpus sections are too long. 5K.
mrSoul7766/ECTSum — 2,425 earnings call transcripts with telegraphic bullet summaries. Use all ~2.4K; keep the native bullets as one style variant AND regenerate prose targets, so the model learns both formats.
ashraq/financial-news-articles — ~300K finance news. Sample 12K.
Trade-the-event (EDT) — ~300K financial news with event labels. Sample 8K, biased toward event-dense articles.
czyssrs/FinQA — ~8K table+text contexts from earnings reports. Use 5K contexts as table-node sources.
ConvFinQA — ~4K. Use 3K contexts.
next-tat/TAT-QA — ~2.7K hybrid table+paragraph contexts. Use all ~2.7K.
PatronusAI/financebench (150 docs) — too small to train on, reserve for eval only.

Legal (target ~40K)

allenai/multi_lexsum — ~9K civil rights cases with summaries at three granularities (long/short/tiny). Use 8K cases × 3 lengths = 24K examples, keeping native targets. This is your length-control training data for free; map tiny/short/long onto your <len:64/128/256> buckets.
theatticusproject/cuad (or cuad) — 510 contracts. Generate clause-level + section-level + doc-level summaries: ~3K examples.
billsum — ~23K US bills. Sample 8K, regenerate (native summaries are decent but inconsistent).
kiddothe2b/contract-nli (ContractNLI) — 607 NDAs. 600 doc-level.
pile-of-law/pile-of-law — 256GB of contracts, court docs, regulatory material. Sample 3K docs from the contracts and regulatory subsets as extra source diversity.
Exploration-lab/IL-TUR (Indian legal benchmark, includes summarization task) + ILDC judgments — sample 4K Indian judgments, chunk to rhetorical-role sections.

Scientific / government / technical (target ~55K)

ccdv/govreport-summarization — 19.5K GAO/CRS reports. Use 15K. Highest-value long-structured-document source on the Hub; closest public analog to enterprise reports.
ccdv/arxiv-summarization — 215K. Sample 15K, split into section-level chunks rather than full papers, plus 3K full-paper-level for tree roots.
ccdv/pubmed-summarization — 133K. Sample 8K.
allenai/scitldr — 5.4K extreme-compression TLDRs. Use all, keep native targets; teaches aggressive compression for <len:64>.
README/API docs: pull from bigcode/the-stack markdown subset (or codeparrot/github-code filtered to .md). Sample 10K doc files.

Books / long-form (target ~12K + tree substrate)

kmfoda/booksum — ~12K chapter-level pairs. Use 10K chapter-level; also the primary substrate for recursive trees below.
pszemraj/booksum-short — cleaned variant if kmfoda's alignment noise bites.

General web / news (target ~75K)

HuggingFaceFW/fineweb-edu — sample 35K docs (1K-8K tokens, dedup against eval). Pure teacher-generated targets. This is your generalization backbone for "from web."
vblagoje/cc_news (or cc_news) — 708K articles. Sample 12K.
multi_news — 56K multi-doc clusters. Use 10K. Multi-doc → single summary is structurally identical to your merged-node case; high value.
EdinburghNLP/xsum + abisee/cnn_dailymail — source articles only, 8K + 8K, all targets regenerated (their references are lead-biased garbage for your purpose).

Forums / email / chat (target ~30K)

webis/tldr-17 — 3.8M Reddit post+TLDR. Sample 10K, filter length >300 tokens, regenerate targets (author TLDRs are noisy but the informal register is the point).
HuggingFaceH4/stack-exchange-preferences or a StackExchange dump — sample 8K threads (question + answers → thread summary).
aeslc — 18K Enron email bodies. Use 6K, regenerate full summaries (native target is just subject lines).
knkarthick/dialogsum — 13K. Use 4K.
samsum — 16K. Use 3K (it's short and synthetic-ish, low weight).

Meetings / transcripts (target ~10K)

lytang/MeetingBank — 1,366 council meetings, ~6.9K segment-level summaries. Use 6K segments.
QMSum (pszemraj/qmsum or original) — 1.8K query-focused pairs over 232 meetings. Use all; the query-focused format generalizes to "summarize this node with respect to X" if your agent ever needs it.
AMI/ICSI — covered by the above, skip unless you want more.

Tables (target ~14K)

ToTTo (GEM/totto) — 120K table→text. Sample 8K.
LogicNLG — 37K. Sample 3K.
wikitablequestions — tables as source, teacher writes table-node summaries. 3K.

Code (target ~5K)

code_search_net — sample 5K function/file-level pairs, regenerate docstring-style summaries. Keep small unless the doc agent will actually index repos.

Indic / multilingual (target ~25K)

csebuetnlp/xlsum — Hindi subset (~70K BBC articles). Sample 12K Hindi, plus 3K spread over 2-3 other languages you might plausibly see (Tamil, Bengali) for robustness.
GEM/wiki_lingua Hindi — ~9K. Use 5K.
ai4bharat/sangraha — massive raw Indic corpus. Sample 5K finance/news-adjacent docs as source, teacher generates (decide now whether targets are Hindi or English — for a doc agent I'd summarize into English regardless of source language, and train that mapping explicitly).


"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import AsyncOpenAI  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.ingestion.tree import (  # noqa: E402
    _Node,
    _bucket_pages,
    _count_tokens,
    _group_into_parents,
    build_leaf_summary_prompt,
    build_parent_summary_prompt,
    split_summary_title,
)


DEFAULT_SYSTEM_MESSAGE = "Follow the user's document-summary instructions exactly."
DEFAULT_TEACHER_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_TEACHER_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_TEACHER_API_KEY_ENV = "NVIDIA_API_KEY"
DEFAULT_TEACHER_API_KEYS_ENV = "NVIDIA_API_KEYS"
DEFAULT_TEACHER_API_KEY_FILE = "data/summary_distill/nvidia_api_keys.txt"
DEFAULT_TEACHER_CONCURRENCY = 350
DEFAULT_DOC_CONCURRENCY = 24
DEFAULT_ROW_BATCH_SIZE = 400
SCRIPT_DATASET_ERROR_HINT = (
    "This dataset uses an old Hugging Face dataset script. Install a script-compatible "
    "datasets release, for example: pip install 'datasets>=2.20,<4'."
)
_SPLIT_SLICE_RE = re.compile(r"^(?P<split>[^\[]+)\[(?P<start>\d*)?:(?P<stop>\d*)?\]$")


def _split_window(split: str) -> tuple[str, int, int | None]:
    match = _SPLIT_SLICE_RE.match(split)
    if not match:
        return split, 0, None
    base_split = match.group("split")
    start = int(match.group("start") or 0)
    stop_raw = match.group("stop")
    stop = int(stop_raw) if stop_raw else None
    limit = None if stop is None else max(0, stop - start)
    return base_split, start, limit


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _eta(*, done: int, total: int | None, elapsed: float) -> str:
    if not total or done <= 0:
        return "eta unknown"
    remaining = max(0, total - done)
    rate = done / elapsed if elapsed > 0 else 0
    if rate <= 0:
        return "eta unknown"
    return f"eta {_format_duration(remaining / rate)}"


def _rate(count: int, elapsed: float, unit: str) -> str:
    if elapsed <= 0:
        return f"0 {unit}/s"
    return f"{count / elapsed:.2f} {unit}/s"


def _parse_api_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    keys: list[str] = []
    for part in re.split(r"[\s,]+", raw):
        key = part.strip().strip("`'\"")
        if key:
            keys.append(key)
    return keys


def _load_api_keys(args: argparse.Namespace, settings: Any) -> list[str]:
    keys: list[str] = []
    keys.extend(_parse_api_keys(os.environ.get(args.api_keys_env)))
    keys.extend(_parse_api_keys(os.environ.get(args.api_key_env)))
    if args.api_key_file:
        path = Path(args.api_key_file)
        if path.exists():
            keys.extend(_parse_api_keys(path.read_text(encoding="utf-8")))
    if not keys and settings.openai_api_key:
        keys.append(settings.openai_api_key)

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


@dataclass(frozen=True)
class SourceSpec:
    key: str
    hf_dataset: str
    hf_split: str = "train"
    limit: int | None = None
    skip_rows: int = 0
    hf_config: str | None = None
    text_fields: tuple[str, ...] = ()
    pages_fields: tuple[str, ...] = ()
    title_fields: tuple[str, ...] = ("title",)
    id_fields: tuple[str, ...] = ("id",)
    join_fields: tuple[str, ...] = ()
    loader: str = "datasets"
    group_fields: tuple[str, ...] = ()
    group_text_field: str | None = None
    group_max_rows: int = 80
    skip: bool = False
    notes: str = ""


SOURCE_SPECS: dict[str, SourceSpec] = {
    # Finance - US public
    "edgar_corpus": SourceSpec(
        key="edgar_corpus",
        hf_dataset="eloukas/edgar-corpus",
        hf_split="train",
        text_fields=("section_text", "text", "content", "item_text"),
        title_fields=("section", "item", "company", "title"),
        id_fields=("accession_number", "filing_id", "id"),
        skip=True,
        notes=(
            "Old script dataset and viewer is disabled. Replaced in all-plan by "
            "financial_reports_sec viewer chunks."
        ),
    ),
    "financial_reports_sec": SourceSpec(
        key="financial_reports_sec",
        hf_dataset="JanosAudran/financial-reports-sec",
        hf_config="small_lite",
        hf_split="train",
        limit=7500,
        text_fields=("text", "sentence", "content", "section_text"),
        title_fields=("docID", "name", "section"),
        id_fields=("sentenceID", "docID"),
        loader="viewer",
        group_fields=("docID", "section"),
        group_text_field="sentence",
        group_max_rows=80,
        notes=(
            "Backfill finance reports via HF dataset-viewer API because this repo "
            "uses an old local dataset script. Groups consecutive sentences by "
            "docID+section into section chunks."
        ),
    ),
    "ectsum": SourceSpec(
        key="ectsum",
        hf_dataset="mrSoul7766/ECTSum",
        hf_split="train",
        text_fields=("transcript", "text", "article", "content"),
        title_fields=("symbol", "company", "title"),
        notes="Use all train examples.",
    ),
    "financial_news_articles": SourceSpec(
        key="financial_news_articles",
        hf_dataset="ashraq/financial-news-articles",
        hf_split="train",
        limit=12000,
        text_fields=("text", "article", "content", "description"),
        title_fields=("title", "headline"),
    ),
    "trade_the_event": SourceSpec(
        key="trade_the_event",
        hf_dataset="ashraq/financial-news-articles",
        hf_split="train",
        skip_rows=12000,
        limit=8000,
        text_fields=("text", "article", "content", "news"),
        title_fields=("title", "headline"),
        notes="EDT dataset id was unavailable; using a second financial-news slice as substitute.",
    ),
    "finqa": SourceSpec(
        key="finqa",
        hf_dataset="next-tat/TAT-QA",
        hf_split="train",
        limit=2500,
        loader="viewer",
        text_fields=("context", "paragraph", "text", "pre_text", "post_text"),
        join_fields=("paragraphs", "table"),
        title_fields=("filename", "id"),
        skip=True,
        notes=(
            "Original FinQA HF id unavailable; TAT-QA viewer substitute returns 500 "
            "from HF dataset-viewer in this environment."
        ),
    ),
    "convfinqa": SourceSpec(
        key="convfinqa",
        hf_dataset="next-tat/TAT-QA",
        hf_split="train",
        skip_rows=2500,
        limit=1500,
        loader="viewer",
        text_fields=("context", "paragraph", "text", "pre_text", "post_text"),
        join_fields=("paragraphs", "table"),
        skip=True,
        notes=(
            "Original ConvFinQA HF id unavailable; TAT-QA viewer substitute returns "
            "500 from HF dataset-viewer in this environment."
        ),
    ),
    "tatqa": SourceSpec(
        key="tatqa",
        hf_dataset="next-tat/TAT-QA",
        hf_split="train",
        loader="viewer",
        text_fields=("context", "paragraphs", "text"),
        join_fields=("paragraphs", "table"),
        skip=True,
        notes="HF dataset-viewer returns 500 and datasets loader has Arrow conversion errors.",
    ),
    "financebench_eval": SourceSpec(
        key="financebench_eval",
        hf_dataset="PatronusAI/financebench",
        hf_split="train",
        skip=True,
        notes="Reserved for eval only; not included in --source-plan all.",
    ),
    # Legal
    "multi_lexsum": SourceSpec(
        key="multi_lexsum",
        hf_dataset="allenai/multi_lexsum",
        hf_split="train",
        limit=8000,
        loader="viewer",
        text_fields=("sources", "document", "text", "article"),
        title_fields=("case_name", "title", "id"),
        skip=True,
        notes="Old script dataset; HF dataset-viewer returns 404 in this environment.",
    ),
    "cuad": SourceSpec(
        key="cuad",
        hf_dataset="theatticusproject/cuad",
        hf_split="train",
        text_fields=("context", "contract", "text"),
        title_fields=("title", "document_name", "id"),
        skip=True,
        notes="Skipped by default because the HF dataset resolves PDF files and requires pdfplumber.",
    ),
    "billsum": SourceSpec(
        key="billsum",
        hf_dataset="billsum",
        hf_split="train",
        limit=8000,
        text_fields=("text", "bill_text", "article"),
        title_fields=("title", "bill_id"),
    ),
    "contract_nli": SourceSpec(
        key="contract_nli",
        hf_dataset="kiddothe2b/contract-nli",
        hf_split="train",
        limit=600,
        loader="viewer",
        text_fields=("premise", "document", "contract", "text"),
        title_fields=("document_name", "id"),
        skip=True,
        notes="Old script dataset; HF dataset-viewer returns 404 in this environment.",
    ),
    "pile_of_law_contracts": SourceSpec(
        key="pile_of_law_contracts",
        hf_dataset="pile-of-law/pile-of-law",
        hf_config="contracts",
        hf_split="train",
        text_fields=("text", "content"),
        title_fields=("meta", "title", "id"),
        skip=True,
        notes="Old script dataset; skipped by default. Use a local mirror via --input if needed.",
    ),
    "il_tur": SourceSpec(
        key="il_tur",
        hf_dataset="Exploration-lab/IL-TUR",
        hf_split="train",
        text_fields=("judgement", "judgment", "text", "document"),
        title_fields=("title", "case_name", "id"),
        skip=True,
        notes="Gated on HF; skipped by default unless you run it explicitly with HF_TOKEN.",
    ),
    # Scientific / government / technical
    "govreport": SourceSpec(
        key="govreport",
        hf_dataset="ccdv/govreport-summarization",
        hf_split="train",
        limit=15000,
        text_fields=("report", "document", "text"),
        title_fields=("title", "id"),
    ),
    "arxiv_sections": SourceSpec(
        key="arxiv_sections",
        hf_dataset="ccdv/arxiv-summarization",
        hf_split="train",
        limit=15000,
        text_fields=("article", "text", "document"),
        title_fields=("article_id", "id", "title"),
    ),
    "arxiv_roots": SourceSpec(
        key="arxiv_roots",
        hf_dataset="ccdv/arxiv-summarization",
        hf_split="train",
        skip_rows=15000,
        limit=3000,
        text_fields=("article", "text", "document"),
        title_fields=("article_id", "id", "title"),
        notes="Separate 3K slice for full-paper/root-style examples.",
    ),
    "pubmed": SourceSpec(
        key="pubmed",
        hf_dataset="ccdv/pubmed-summarization",
        hf_split="train",
        limit=8000,
        text_fields=("article", "text", "document"),
        title_fields=("article_id", "id", "title"),
    ),
    "scitldr": SourceSpec(
        key="scitldr",
        hf_dataset="allenai/scitldr",
        hf_split="train",
        loader="viewer",
        text_fields=("source", "source_labels", "text", "article"),
        title_fields=("paper_id", "id", "title"),
        skip=True,
        notes="Old script dataset; HF dataset-viewer returns 404 in this environment.",
    ),
    "the_stack_markdown": SourceSpec(
        key="the_stack_markdown",
        hf_dataset="bigcode/the-stack",
        hf_config="Markdown",
        hf_split="train",
        text_fields=("content", "text"),
        title_fields=("max_stars_repo_path", "path", "repo_name"),
        skip=True,
        notes="Gated on HF; skipped by default. Use HF_TOKEN or a local markdown corpus via --input.",
    ),
    # Books / long-form
    "booksum": SourceSpec(
        key="booksum",
        hf_dataset="kmfoda/booksum",
        hf_split="train",
        limit=10000,
        text_fields=("chapter", "text", "document"),
        title_fields=("chapter_title", "book_title", "title"),
    ),
    "booksum_short": SourceSpec(
        key="booksum_short",
        hf_dataset="pszemraj/booksum-short",
        hf_split="train",
        text_fields=("chapter", "text", "document"),
        title_fields=("chapter_title", "book_title", "title"),
    ),
    # General web / news
    "fineweb_edu": SourceSpec(
        key="fineweb_edu",
        hf_dataset="HuggingFaceFW/fineweb-edu",
        hf_config="sample-10BT",
        hf_split="train",
        limit=35000,
        text_fields=("text", "content"),
        title_fields=("id", "url"),
    ),
    "cc_news": SourceSpec(
        key="cc_news",
        hf_dataset="vblagoje/cc_news",
        hf_split="train",
        limit=12000,
        text_fields=("text", "article", "content"),
        title_fields=("title", "headline"),
    ),
    "multi_news": SourceSpec(
        key="multi_news",
        hf_dataset="multi_news",
        hf_split="train",
        limit=10000,
        loader="viewer",
        text_fields=("document", "text", "article"),
        title_fields=("id", "title"),
    ),
    "xsum": SourceSpec(
        key="xsum",
        hf_dataset="EdinburghNLP/xsum",
        hf_split="train",
        limit=8000,
        text_fields=("document", "text", "article"),
        title_fields=("id", "title"),
    ),
    "cnn_dailymail": SourceSpec(
        key="cnn_dailymail",
        hf_dataset="abisee/cnn_dailymail",
        hf_config="3.0.0",
        hf_split="train",
        limit=8000,
        text_fields=("article", "document", "text"),
        title_fields=("id", "title"),
    ),
    # Forums / email / chat
    "tldr17": SourceSpec(
        key="tldr17",
        hf_dataset="webis/tldr-17",
        hf_split="train",
        limit=10000,
        loader="viewer",
        text_fields=("content", "body", "selftext", "text"),
        title_fields=("title", "subreddit", "id"),
    ),
    "stack_exchange": SourceSpec(
        key="stack_exchange",
        hf_dataset="HuggingFaceH4/stack-exchange-preferences",
        hf_split="train",
        limit=8000,
        text_fields=("question", "text", "prompt"),
        join_fields=("question", "answers", "response_j", "response_k"),
        title_fields=("qid", "id", "title"),
    ),
    "aeslc": SourceSpec(
        key="aeslc",
        hf_dataset="aeslc",
        hf_split="train",
        limit=6000,
        text_fields=("email_body", "body", "text", "email"),
        title_fields=("subject", "id"),
    ),
    "dialogsum": SourceSpec(
        key="dialogsum",
        hf_dataset="knkarthick/dialogsum",
        hf_split="train",
        limit=4000,
        text_fields=("dialogue", "dialog", "text"),
        title_fields=("id", "topic"),
    ),
    "samsum": SourceSpec(
        key="samsum",
        hf_dataset="Samsung/samsum",
        hf_split="train",
        limit=3000,
        text_fields=("dialogue", "dialog", "text"),
        title_fields=("id",),
    ),
    # Meetings / transcripts
    "meetingbank": SourceSpec(
        key="meetingbank",
        hf_dataset="lytang/MeetingBank",
        hf_split="train",
        skip=True,
        text_fields=("source", "transcript", "meeting", "text"),
        title_fields=("meeting_id", "id", "title"),
        notes="Dataset id unavailable in this environment; skipped by default.",
    ),
    "qmsum": SourceSpec(
        key="qmsum",
        hf_dataset="pszemraj/qmsum",
        hf_split="train",
        skip=True,
        text_fields=("meeting_transcripts", "transcript", "text"),
        join_fields=("query", "meeting_transcripts", "transcript"),
        title_fields=("id", "meeting_id"),
        notes="Dataset id unavailable in this environment; skipped by default.",
    ),
    # Tables
    "totto": SourceSpec(
        key="totto",
        hf_dataset="GEM/totto",
        hf_split="train",
        limit=8000,
        loader="viewer",
        text_fields=("table", "table_page_title", "sentence_annotations"),
        join_fields=("table_page_title", "table_section_title", "table", "highlighted_cells"),
        title_fields=("table_page_title", "example_id", "id"),
    ),
    "logicnlg": SourceSpec(
        key="logicnlg",
        hf_dataset="logicnlg",
        hf_split="train",
        skip=True,
        text_fields=("table", "text", "content"),
        join_fields=("table", "topic"),
        title_fields=("topic", "id"),
        notes="Dataset id unavailable in this environment; skipped by default.",
    ),
    "wikitablequestions": SourceSpec(
        key="wikitablequestions",
        hf_dataset="wikitablequestions",
        hf_split="train",
        limit=3000,
        loader="viewer",
        text_fields=("table", "question", "text"),
        join_fields=("table", "question"),
        title_fields=("id",),
    ),
    # Code
    "code_search_net_python": SourceSpec(
        key="code_search_net_python",
        hf_dataset="code_search_net",
        hf_config="python",
        hf_split="train",
        limit=5000,
        text_fields=("whole_func_string", "func_code_string", "code", "text"),
        title_fields=("func_name", "repo", "path"),
    ),
    # Indic / multilingual
    "xlsum_hindi": SourceSpec(
        key="xlsum_hindi",
        hf_dataset="csebuetnlp/xlsum",
        hf_config="hindi",
        hf_split="train",
        limit=12000,
        loader="viewer",
        text_fields=("text", "article"),
        title_fields=("title", "id"),
    ),
    "xlsum_tamil": SourceSpec(
        key="xlsum_tamil",
        hf_dataset="csebuetnlp/xlsum",
        hf_config="tamil",
        hf_split="train",
        limit=1500,
        loader="viewer",
        text_fields=("text", "article"),
        title_fields=("title", "id"),
    ),
    "xlsum_bengali": SourceSpec(
        key="xlsum_bengali",
        hf_dataset="csebuetnlp/xlsum",
        hf_config="bengali",
        hf_split="train",
        limit=1500,
        loader="viewer",
        text_fields=("text", "article"),
        title_fields=("title", "id"),
    ),
    "wiki_lingua_hi": SourceSpec(
        key="wiki_lingua_hi",
        hf_dataset="GEM/wiki_lingua",
        hf_config="hi",
        hf_split="train",
        limit=5000,
        loader="viewer",
        text_fields=("source", "text", "article"),
        title_fields=("gem_id", "id", "title"),
    ),
    "sangraha": SourceSpec(
        key="sangraha",
        hf_dataset="ai4bharat/sangraha",
        hf_config="verified",
        hf_split="train",
        limit=5000,
        text_fields=("text", "content"),
        title_fields=("doc_id", "id", "url"),
        notes="May require config/auth depending on which Sangraha shard you use.",
    ),
}


@dataclass
class CorpusPage:
    page_n: int
    prose_text: str
    table_ids: list[str]


@dataclass
class CorpusDoc:
    doc_id: str
    title: str
    pages: list[CorpusPage]


class DatasetWriter:
    def __init__(self, path: Path, fmt: str) -> None:
        self.path = path
        self.fmt = fmt
        self._records: list[dict[str, Any]] = []
        self._fh = None

    def __enter__(self) -> "DatasetWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.fmt == "jsonl":
            self._fh = self.path.open("w", encoding="utf-8")
        return self

    def write(self, record: dict[str, Any]) -> None:
        if self.fmt == "jsonl":
            assert self._fh is not None
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        else:
            self._records.append(record)

    def __exit__(self, *_exc: object) -> None:
        if self._fh is not None:
            self._fh.close()
        if self.fmt == "json":
            self.path.write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_stringify(item) for item in value]
        return "\n\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        scalar_parts: list[str] = []
        for key, item in value.items():
            item_text = _stringify(item).strip()
            if item_text:
                scalar_parts.append(f"{key}: {item_text}")
        return "\n".join(scalar_parts)
    return str(value)


def _get_path(row: dict[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _first_text(row: dict[str, Any], fields: Iterable[str]) -> str:
    for field_name in fields:
        text = _stringify(_get_path(row, field_name)).strip()
        if text:
            return text
    return ""


def _joined_text(row: dict[str, Any], fields: Iterable[str]) -> str:
    parts: list[str] = []
    for field_name in fields:
        text = _stringify(_get_path(row, field_name)).strip()
        if text:
            parts.append(f"{field_name}:\n{text}")
    return "\n\n".join(parts)


def _group_records(records: Iterable[dict[str, Any]], spec: SourceSpec) -> Iterator[dict[str, Any]]:
    if not spec.group_fields or not spec.group_text_field:
        yield from records
        return

    current_key: tuple[str, ...] | None = None
    current_rows: list[dict[str, Any]] = []
    group_index = 0

    def flush() -> dict[str, Any] | None:
        nonlocal group_index, current_rows
        if not current_rows:
            return None
        group_index += 1
        first = current_rows[0]
        text_parts = [
            _stringify(_get_path(row, spec.group_text_field or "")).strip()
            for row in current_rows
        ]
        text = " ".join(part for part in text_parts if part)
        key_label = "_".join(current_key or ("group",))
        row = dict(first)
        row["id"] = f"{key_label}_{group_index}"
        row["title"] = " / ".join(current_key or (key_label,))
        row["text"] = text
        row["source_row_count"] = len(current_rows)
        current_rows = []
        return row

    for row in records:
        key = tuple(_stringify(_get_path(row, field_name)).strip() for field_name in spec.group_fields)
        if current_key is None:
            current_key = key
        if key != current_key or len(current_rows) >= spec.group_max_rows:
            grouped = flush()
            if grouped is not None:
                yield grouped
            current_key = key
        current_rows.append(row)

    grouped = flush()
    if grouped is not None:
        yield grouped


def _fallback_row_text(row: dict[str, Any]) -> str:
    excluded = {
        "summary", "summaries", "highlights", "abstract", "target", "targets",
        "label", "labels", "answer", "answers", "output", "outputs",
    }
    parts: list[str] = []
    for key, value in row.items():
        if key.lower() in excluded:
            continue
        text = _stringify(value).strip()
        if len(text) >= 80:
            parts.append(f"{key}:\n{text}")
    return "\n\n".join(parts)


def _load_json_records(path: Path) -> Iterator[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                yield row
    elif isinstance(data, dict):
        yield data
    else:
        raise ValueError(f"{path} must contain a JSON object or array of objects")


def _load_jsonl_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no} is not a JSON object")
            yield row


def _iter_local_records(paths: Iterable[Path], text_field: str) -> Iterator[dict[str, Any]]:
    for path in paths:
        if path.is_dir():
            yield from _iter_local_records(sorted(path.iterdir()), text_field)
            continue
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            yield from _load_jsonl_records(path)
        elif suffix == ".json":
            yield from _load_json_records(path)
        elif suffix in {".txt", ".md"}:
            yield {
                "id": path.stem,
                "title": path.name,
                text_field: path.read_text(encoding="utf-8"),
            }


def _iter_hf_viewer_records(args: argparse.Namespace, spec: SourceSpec) -> Iterator[dict[str, Any]]:
    base_split, split_start, split_limit = _split_window(spec.hf_split)
    limit = None if spec.group_fields else (spec.limit if spec.limit is not None else split_limit)
    offset = spec.skip_rows + split_start
    yielded = 0
    length = 100
    token = os.environ.get("HF_TOKEN")
    while True:
        if limit is not None and yielded >= limit:
            break
        request_length = length
        if limit is not None:
            request_length = min(request_length, limit - yielded)
        params = {
            "dataset": spec.hf_dataset,
            "config": spec.hf_config or "default",
            "split": base_split,
            "offset": offset,
            "length": request_length,
        }
        url = "https://datasets-server.huggingface.co/rows?" + urlencode(params)
        headers = {"User-Agent": "alchemy-labs-summary-distill/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"{spec.key}: HF dataset-viewer request failed at offset {offset}: {exc}") from exc

        rows = payload.get("rows") or []
        if not rows:
            break
        for item in rows:
            row = item.get("row") if isinstance(item, dict) else None
            if isinstance(row, dict):
                yielded += 1
                yield row
        offset += len(rows)


def _iter_hf_records(args: argparse.Namespace, spec: SourceSpec | None = None) -> Iterator[dict[str, Any]]:
    if spec and spec.loader == "viewer":
        yield from _group_records(_iter_hf_viewer_records(args, spec), spec)
        return

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "The Hugging Face `datasets` package is required for --hf-dataset. "
            "Install it in your environment, then rerun this script."
        ) from exc

    dataset_name = spec.hf_dataset if spec else args.hf_dataset
    raw_split = spec.hf_split if spec else (args.hf_split or "train")
    dataset_split, split_start, split_limit = _split_window(raw_split)
    dataset_config = spec.hf_config if spec else args.hf_config
    limit = None if spec and spec.group_fields else (
        spec.limit if spec and spec.limit is not None else split_limit
    )
    skip_rows = (spec.skip_rows if spec else 0) + split_start
    load_kwargs: dict[str, Any] = {
        "path": dataset_name,
        "split": dataset_split,
        "streaming": args.hf_streaming,
    }
    if dataset_config:
        load_kwargs["name"] = dataset_config
    try:
        dataset = load_dataset(**load_kwargs)
    except RuntimeError as exc:
        message = str(exc)
        if "Dataset scripts are no longer supported" in message:
            label = spec.key if spec else dataset_name
            raise RuntimeError(f"{label}: {message}\n{SCRIPT_DATASET_ERROR_HINT}") from exc
        raise
    if skip_rows:
        dataset = dataset.skip(skip_rows)
    if limit is not None:
        dataset = dataset.take(limit)

    records = (row for row in dataset if isinstance(row, dict))
    if spec:
        records = _group_records(records, spec)
    for row in records:
        if isinstance(row, dict):
            yield row


def _split_text_to_pages(text: str, target_tokens: int) -> list[str]:
    form_pages = [p.strip() for p in text.split("\f") if p.strip()]
    if len(form_pages) > 1:
        return form_pages

    chunks: list[str] = []
    acc: list[str] = []
    acc_tokens = 0
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for para in paragraphs or [text]:
        para_tokens = _count_tokens(para)
        if acc and acc_tokens + para_tokens > target_tokens:
            chunks.append("\n\n".join(acc))
            acc = []
            acc_tokens = 0
        acc.append(para)
        acc_tokens += para_tokens
    if acc:
        chunks.append("\n\n".join(acc))
    return chunks or [text]


def _coerce_pages(
    row: dict[str, Any],
    args: argparse.Namespace,
    spec: SourceSpec | None = None,
) -> list[str]:
    page_fields: tuple[str, ...] = ()
    if args.pages_field:
        page_fields += (args.pages_field,)
    if spec:
        page_fields += spec.pages_fields
    for page_field in page_fields:
        raw_pages = _get_path(row, page_field)
        if isinstance(raw_pages, list):
            pages = [_stringify(page).strip() for page in raw_pages]
            pages = [page for page in pages if page]
            if pages:
                return pages
        if isinstance(raw_pages, str):
            return _split_text_to_pages(raw_pages, args.synthetic_page_tokens)

    join_text = _joined_text(row, spec.join_fields if spec else ())
    if join_text:
        return _split_text_to_pages(join_text, args.synthetic_page_tokens)

    text_fields: tuple[str, ...] = (args.text_field,)
    if spec:
        text_fields += spec.text_fields
    text = _first_text(row, text_fields) or _fallback_row_text(row)
    if not text:
        return []
    return _split_text_to_pages(str(text), args.synthetic_page_tokens)


def _coerce_doc(
    row: dict[str, Any],
    index: int,
    args: argparse.Namespace,
    spec: SourceSpec | None = None,
) -> CorpusDoc | None:
    page_texts = _coerce_pages(row, args, spec)
    if not page_texts:
        return None
    id_fields = (args.id_field,)
    title_fields = (args.title_field,)
    if spec:
        id_fields += spec.id_fields
        title_fields += spec.title_fields
    doc_id = _first_text(row, id_fields) or f"doc_{index}"
    title = _first_text(row, title_fields) or doc_id
    pages = [
        CorpusPage(page_n=i, prose_text=text, table_ids=[])
        for i, text in enumerate(page_texts, start=1)
    ]
    return CorpusDoc(doc_id=doc_id, title=title, pages=pages)


def _child_titles(node: _Node) -> str:
    return ", ".join(
        line.replace("Section: ", "")
        for line in node.content.splitlines()
        if line.startswith("Section: ")
    ) or "—"


def _example(
    *,
    system_message: str,
    user_prompt: str,
    assistant_text: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_text.strip()},
        ],
        "metadata": metadata,
    }


class TeacherClientPool:
    def __init__(
        self,
        *,
        api_keys: list[str],
        base_url: str,
        model: str,
        log_every: int,
    ) -> None:
        if not api_keys:
            raise RuntimeError("At least one teacher API key is required.")
        self.clients = [AsyncOpenAI(api_key=key, base_url=base_url) for key in api_keys]
        self.model = model
        self.log_every = log_every
        self._lock = asyncio.Lock()
        self._next_index = 0
        self._ok = 0
        self._failed = 0

    async def _next_client(self) -> tuple[int, AsyncOpenAI]:
        async with self._lock:
            index = self._next_index
            self._next_index = (self._next_index + 1) % len(self.clients)
            return index, self.clients[index]

    async def complete(self, prompt: str, semaphore: asyncio.Semaphore) -> str:
        async with semaphore:
            key_index, client = await self._next_client()
            started = time.monotonic()
            try:
                resp = await client.chat.completions.create(
                    model=self.model,
                    temperature=0.1,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:
                async with self._lock:
                    self._failed += 1
                    failed = self._failed
                print(f"[llm] failed #{failed} key={key_index + 1}/{len(self.clients)} error={exc}")
                raise

            elapsed = time.monotonic() - started
            text = (resp.choices[0].message.content or "").strip()
            async with self._lock:
                self._ok += 1
                ok = self._ok
                failed = self._failed
            if self.log_every > 0 and (ok == 1 or ok % self.log_every == 0):
                print(
                    f"[llm] ok #{ok} failed={failed} "
                    f"key={key_index + 1}/{len(self.clients)} "
                    f"latency={elapsed:.1f}s prompt_tokens_est={_count_tokens(prompt)}"
                )
            return text

    async def check_model(self) -> None:
        _, client = await self._next_client()
        resp = await client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with OK."}],
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"model check passed: {self.model} -> {text}")


async def _complete(
    teacher: TeacherClientPool,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> str:
    return await teacher.complete(prompt, semaphore)


async def _summarize_one(
    *,
    teacher: TeacherClientPool,
    node: _Node,
    is_leaf: bool,
    doc: CorpusDoc,
    writer: DatasetWriter,
    system_message: str,
    source_spec: SourceSpec | None,
    teacher_semaphore: asyncio.Semaphore,
) -> None:
    if is_leaf:
        prompt = build_leaf_summary_prompt(
            start=node.start_page,
            end=node.end_page,
            content=node.content,
        )
        task = "leaf_summary"
        child_count = 0
    else:
        prompt = build_parent_summary_prompt(
            n_children=len(node.child_ids),
            start=node.start_page,
            end=node.end_page,
            titles=_child_titles(node),
            content=node.content,
        )
        task = "parent_summary"
        child_count = len(node.child_ids)

    assistant_text = await _complete(teacher, prompt, teacher_semaphore)
    node.summary, node.title = split_summary_title(assistant_text)
    writer.write(
        _example(
            system_message=system_message,
            user_prompt=prompt,
            assistant_text=assistant_text,
            metadata={
                "source": "summary_tree_distill",
                "source_key": source_spec.key if source_spec else None,
                "hf_dataset": source_spec.hf_dataset if source_spec else None,
                "hf_config": source_spec.hf_config if source_spec else None,
                "hf_split": source_spec.hf_split if source_spec else None,
                "task": task,
                "doc_id": doc.doc_id,
                "doc_title": doc.title,
                "node_id": node.node_id,
                "start_page": node.start_page,
                "end_page": node.end_page,
                "n_input_tokens_est": _count_tokens(prompt),
                "n_child_nodes": child_count,
                "teacher_model": teacher.model,
            },
        )
    )


async def _process_doc(
    *,
    doc: CorpusDoc,
    writer: DatasetWriter,
    teacher: TeacherClientPool,
    args: argparse.Namespace,
    source_spec: SourceSpec | None = None,
    teacher_semaphore: asyncio.Semaphore | None = None,
) -> int:
    if teacher_semaphore is None:
        teacher_semaphore = asyncio.Semaphore(args.teacher_concurrency)
    leaves = _bucket_pages(doc.pages, args.max_leaf_tokens, args.min_leaf_tokens)
    if not leaves:
        return 0

    n_examples = 0
    await asyncio.gather(*[
        _summarize_one(
            teacher=teacher,
            node=leaf,
            is_leaf=True,
            doc=doc,
            writer=writer,
            system_message=args.system_message,
            source_spec=source_spec,
            teacher_semaphore=teacher_semaphore,
        )
        for leaf in leaves
    ])
    n_examples += len(leaves)

    if args.leaves_only:
        return n_examples

    current = leaves
    while len(current) > 1:
        parents = _group_into_parents(current, args.max_children)
        await asyncio.gather(*[
            _summarize_one(
                teacher=teacher,
                node=parent,
                is_leaf=False,
                doc=doc,
                writer=writer,
                system_message=args.system_message,
                source_spec=source_spec,
                teacher_semaphore=teacher_semaphore,
            )
            for parent in parents
        ])
        n_examples += len(parents)
        current = parents

    return n_examples


async def _process_doc_batch(
    *,
    batch: list[tuple[int, CorpusDoc, SourceSpec | None]],
    writer: DatasetWriter,
    teacher: TeacherClientPool,
    args: argparse.Namespace,
    teacher_semaphore: asyncio.Semaphore,
) -> list[tuple[int, CorpusDoc, int]]:
    results = await asyncio.gather(*[
        _process_doc(
            doc=doc,
            writer=writer,
            teacher=teacher,
            args=args,
            source_spec=source_spec,
            teacher_semaphore=teacher_semaphore,
        )
        for _, doc, source_spec in batch
    ])
    return [
        (source_index, doc, n_examples)
        for (source_index, doc, _), n_examples in zip(batch, results)
    ]


async def _drain_doc_buffer(
    *,
    batch: list[tuple[int, CorpusDoc, SourceSpec | None]],
    writer: DatasetWriter,
    teacher: TeacherClientPool,
    args: argparse.Namespace,
    teacher_semaphore: asyncio.Semaphore,
) -> list[tuple[int, CorpusDoc, int]]:
    drained: list[tuple[int, CorpusDoc, int]] = []
    for start in range(0, len(batch), args.doc_concurrency):
        chunk = batch[start:start + args.doc_concurrency]
        drained.extend(await _process_doc_batch(
            batch=chunk,
            writer=writer,
            teacher=teacher,
            args=args,
            teacher_semaphore=teacher_semaphore,
        ))
    return drained


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate system/user/assistant summary-tree distillation data."
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--hf-dataset", help="Hugging Face dataset name or path.")
    src.add_argument(
        "--source",
        action="append",
        choices=sorted(SOURCE_SPECS),
        help="Run one curated source from SOURCE_SPECS. Repeat for multiple sources.",
    )
    src.add_argument(
        "--source-plan",
        choices=["all"],
        help="Run the curated corpus plan. `all` excludes specs marked eval-only/skip.",
    )
    src.add_argument(
        "--input",
        nargs="+",
        type=Path,
        help="Local .jsonl, .json, .txt, .md, or directories containing them.",
    )
    parser.add_argument("--hf-config", default=None)
    parser.add_argument("--hf-split", default="train")
    parser.add_argument(
        "--hf-streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream HF rows instead of materializing datasets locally. Enabled by default.",
    )
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument(
        "--inspect-schemas",
        action="store_true",
        help="Load one row per selected HF source, print detected keys/coercion, and exit.",
    )
    parser.add_argument("--text-field", default="text")
    parser.add_argument("--pages-field", default=None)
    parser.add_argument("--id-field", default="id")
    parser.add_argument("--title-field", default="title")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=Path("data/summary_distill/summary_messages.jsonl"))
    parser.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    parser.add_argument("--model", default=DEFAULT_TEACHER_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_TEACHER_BASE_URL)
    parser.add_argument("--api-key-env", default=DEFAULT_TEACHER_API_KEY_ENV)
    parser.add_argument("--api-keys-env", default=DEFAULT_TEACHER_API_KEYS_ENV)
    parser.add_argument("--api-key-file", default=DEFAULT_TEACHER_API_KEY_FILE)
    parser.add_argument("--check-model", action="store_true", help="Send one tiny request to verify the model id works, then exit.")
    parser.add_argument("--llm-log-every", type=int, default=50)
    parser.add_argument("--teacher-concurrency", type=int, default=DEFAULT_TEACHER_CONCURRENCY)
    parser.add_argument("--doc-concurrency", type=int, default=DEFAULT_DOC_CONCURRENCY)
    parser.add_argument(
        "--row-batch-size",
        type=int,
        default=DEFAULT_ROW_BATCH_SIZE,
        help="Maximum coerced source documents held in memory before processing and flushing.",
    )
    parser.add_argument("--synthetic-page-tokens", type=int, default=1200)
    parser.add_argument("--max-leaf-tokens", type=int, default=None)
    parser.add_argument("--min-leaf-tokens", type=int, default=None)
    parser.add_argument("--max-children", type=int, default=None)
    parser.add_argument("--leaves-only", action="store_true")
    parser.add_argument("--system-message", default=DEFAULT_SYSTEM_MESSAGE)
    return parser


def _selected_specs(args: argparse.Namespace) -> list[SourceSpec]:
    if args.source_plan == "all":
        return [spec for spec in SOURCE_SPECS.values() if not spec.skip]
    if args.source:
        return [SOURCE_SPECS[key] for key in args.source]
    return []


def _print_sources() -> None:
    for key in sorted(SOURCE_SPECS):
        spec = SOURCE_SPECS[key]
        status = "skip/eval" if spec.skip else "train"
        config = f" config={spec.hf_config}" if spec.hf_config else ""
        limit = f" limit={spec.limit}" if spec.limit is not None else ""
        print(
            f"{key}: {status} loader={spec.loader} dataset={spec.hf_dataset}{config} "
            f"split={spec.hf_split}{limit}"
        )
        if spec.notes:
            print(f"  notes: {spec.notes}")


def _print_schema_probe(
    *,
    row: dict[str, Any],
    doc: CorpusDoc | None,
    spec: SourceSpec | None,
) -> None:
    label = spec.key if spec else "custom_hf_dataset"
    print(f"\n== schema probe: {label} ==")
    if spec:
        print(f"dataset={spec.hf_dataset} config={spec.hf_config} split={spec.hf_split}")
        print(f"text_fields={spec.text_fields}")
        print(f"join_fields={spec.join_fields}")
        print(f"pages_fields={spec.pages_fields}")
    print("row keys:", ", ".join(sorted(row.keys())))
    if doc is None:
        print("coercion: no usable text found")
        return
    print(f"coercion: doc_id={doc.doc_id!r} title={doc.title!r} pages={len(doc.pages)}")
    first_page = doc.pages[0].prose_text.replace("\n", " ")
    print(f"first_page_preview={first_page[:500]}")


def _inspect_schemas(args: argparse.Namespace) -> None:
    specs = _selected_specs(args)
    if specs:
        for spec in specs:
            try:
                rows = _iter_hf_records(args, spec)
                row = next(rows)
            except StopIteration:
                print(f"\n== schema probe: {spec.key} ==\nno rows")
                continue
            except Exception as exc:
                print(f"\n== schema probe: {spec.key} ==\nload failed: {exc}")
                continue
            doc = _coerce_doc(row, 1, args, spec)
            _print_schema_probe(row=row, doc=doc, spec=spec)
        return

    if not args.hf_dataset:
        raise RuntimeError("--inspect-schemas requires --hf-dataset, --source, or --source-plan")
    try:
        rows = _iter_hf_records(args)
        row = next(rows)
    except Exception as exc:
        raise RuntimeError(f"schema inspection failed: {exc}") from exc
    doc = _coerce_doc(row, 1, args)
    _print_schema_probe(row=row, doc=doc, spec=None)


async def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    if args.list_sources:
        _print_sources()
        return
    if not args.check_model and not args.hf_dataset and not args.source and not args.source_plan and not args.input:
        parser.error("one of --hf-dataset, --source, --source-plan, or --input is required")
    if args.inspect_schemas:
        _inspect_schemas(args)
        return

    settings = get_settings()
    args.max_leaf_tokens = args.max_leaf_tokens or settings.tree_max_leaf_tokens
    args.min_leaf_tokens = args.min_leaf_tokens or settings.tree_min_leaf_tokens
    args.max_children = args.max_children or settings.tree_max_children
    if args.teacher_concurrency < 1:
        parser.error("--teacher-concurrency must be >= 1")
    if args.doc_concurrency < 1:
        parser.error("--doc-concurrency must be >= 1")
    if args.row_batch_size < 1:
        parser.error("--row-batch-size must be >= 1")

    api_keys = _load_api_keys(args, settings)
    if not api_keys:
        raise RuntimeError(
            f"Teacher API key not found. Set {args.api_keys_env}, {args.api_key_env}, "
            f"or put keys in {args.api_key_file}."
        )
    teacher = TeacherClientPool(
        api_keys=api_keys,
        base_url=args.base_url,
        model=args.model,
        log_every=args.llm_log_every,
    )
    print(
        f"teacher model={args.model} base_url={args.base_url} "
        f"keys={len(api_keys)} concurrency={args.teacher_concurrency}"
    )
    if args.check_model:
        await teacher.check_model()
        return
    teacher_semaphore = asyncio.Semaphore(args.teacher_concurrency)
    run_started = time.monotonic()
    total_docs = 0
    total_examples = 0
    with DatasetWriter(args.output, args.format) as writer:
        specs = _selected_specs(args)
        if specs:
            for spec in specs:
                print(f"\n== {spec.key}: {spec.hf_dataset} [{spec.hf_split}] ==")
                source_started = time.monotonic()
                source_docs = 0
                source_examples = 0
                source_target = args.limit if args.limit is not None else spec.limit
                batch: list[tuple[int, CorpusDoc, SourceSpec | None]] = []
                try:
                    rows = _iter_hf_records(args, spec)
                    for index, row in enumerate(rows, start=1):
                        if args.limit is not None and source_docs >= args.limit:
                            break
                        if spec.limit is not None and source_docs >= spec.limit:
                            break
                        doc = _coerce_doc(row, index, args, spec)
                        if doc is None:
                            continue
                        source_docs += 1
                        total_docs += 1
                        batch.append((source_docs, doc, spec))
                        if len(batch) >= args.row_batch_size:
                            drain_started = time.monotonic()
                            print(
                                f"{spec.key}: draining {len(batch)} buffered docs "
                                f"({source_docs}/{source_target or '?'} read, "
                                f"{_eta(done=source_docs, total=source_target, elapsed=drain_started - source_started)})"
                            )
                            drained = await _drain_doc_buffer(
                                batch=batch,
                                writer=writer,
                                teacher=teacher,
                                args=args,
                                teacher_semaphore=teacher_semaphore,
                            )
                            drain_elapsed = time.monotonic() - drain_started
                            for source_index, done_doc, n_examples in drained:
                                total_examples += n_examples
                                source_examples += n_examples
                                print(f"{spec.key}:{source_index}: {done_doc.doc_id} -> {n_examples} examples")
                            print(
                                f"{spec.key}: drain done in {_format_duration(drain_elapsed)}; "
                                f"source {_rate(source_docs, time.monotonic() - source_started, 'docs')}, "
                                f"examples={source_examples}, "
                                f"{_eta(done=source_docs, total=source_target, elapsed=time.monotonic() - source_started)}"
                            )
                            batch = []
                except Exception as exc:
                    print(f"SKIP {spec.key}: load/generation failed: {exc}")
                    batch = []
                if batch:
                    drain_started = time.monotonic()
                    print(
                        f"{spec.key}: draining {len(batch)} buffered docs "
                        f"({source_docs}/{source_target or '?'} read, "
                        f"{_eta(done=source_docs, total=source_target, elapsed=drain_started - source_started)})"
                    )
                    drained = await _drain_doc_buffer(
                        batch=batch,
                        writer=writer,
                        teacher=teacher,
                        args=args,
                        teacher_semaphore=teacher_semaphore,
                    )
                    drain_elapsed = time.monotonic() - drain_started
                    for source_index, done_doc, n_examples in drained:
                        total_examples += n_examples
                        source_examples += n_examples
                        print(f"{spec.key}:{source_index}: {done_doc.doc_id} -> {n_examples} examples")
                    print(
                        f"{spec.key}: drain done in {_format_duration(drain_elapsed)}; "
                        f"source {_rate(source_docs, time.monotonic() - source_started, 'docs')}, "
                        f"examples={source_examples}, "
                        f"{_eta(done=source_docs, total=source_target, elapsed=time.monotonic() - source_started)}"
                    )
                print(
                    f"{spec.key}: source done docs={source_docs}, examples={source_examples}, "
                    f"elapsed={_format_duration(time.monotonic() - source_started)}"
                )
        else:
            batch = []
            source_started = time.monotonic()
            try:
                if args.hf_dataset:
                    rows = _iter_hf_records(args)
                else:
                    rows = _iter_local_records(args.input, args.text_field)
                for index, row in enumerate(rows, start=1):
                    if args.limit is not None and total_docs >= args.limit:
                        break
                    doc = _coerce_doc(row, index, args)
                    if doc is None:
                        continue
                    total_docs += 1
                    batch.append((total_docs, doc, None))
                    if len(batch) >= args.row_batch_size:
                        drain_started = time.monotonic()
                        print(
                            f"draining {len(batch)} buffered docs "
                            f"({total_docs}/{args.limit or '?'} read, "
                            f"{_eta(done=total_docs, total=args.limit, elapsed=drain_started - source_started)})"
                        )
                        drained = await _drain_doc_buffer(
                            batch=batch,
                            writer=writer,
                            teacher=teacher,
                            args=args,
                            teacher_semaphore=teacher_semaphore,
                        )
                        drain_elapsed = time.monotonic() - drain_started
                        for doc_index, done_doc, n_examples in drained:
                            total_examples += n_examples
                            print(f"{doc_index}: {done_doc.doc_id} -> {n_examples} examples")
                        print(
                            f"drain done in {_format_duration(drain_elapsed)}; "
                            f"{_rate(total_docs, time.monotonic() - source_started, 'docs')}, "
                            f"examples={total_examples}, "
                            f"{_eta(done=total_docs, total=args.limit, elapsed=time.monotonic() - source_started)}"
                        )
                        batch = []
            except Exception as exc:
                raise RuntimeError(f"load/generation failed: {exc}") from exc
            if batch:
                drain_started = time.monotonic()
                print(
                    f"draining {len(batch)} buffered docs "
                    f"({total_docs}/{args.limit or '?'} read, "
                    f"{_eta(done=total_docs, total=args.limit, elapsed=drain_started - source_started)})"
                )
                drained = await _drain_doc_buffer(
                    batch=batch,
                    writer=writer,
                    teacher=teacher,
                    args=args,
                    teacher_semaphore=teacher_semaphore,
                )
                drain_elapsed = time.monotonic() - drain_started
                for doc_index, done_doc, n_examples in drained:
                    total_examples += n_examples
                    print(f"{doc_index}: {done_doc.doc_id} -> {n_examples} examples")
                print(
                    f"drain done in {_format_duration(drain_elapsed)}; "
                    f"{_rate(total_docs, time.monotonic() - source_started, 'docs')}, "
                    f"examples={total_examples}, "
                    f"{_eta(done=total_docs, total=args.limit, elapsed=time.monotonic() - source_started)}"
                )

    print(
        f"Wrote {total_examples} examples from {total_docs} docs to {args.output} "
        f"in {_format_duration(time.monotonic() - run_started)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
