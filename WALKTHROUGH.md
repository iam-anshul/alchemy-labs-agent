# Doc Reasoner — End-to-End Code Walkthrough

A human-readable guide to every file in the project and exactly what happens when you upload a PDF, ask a question, or draft a report. Written against the current codebase as of May 2026.

---

## What this project is

Doc Reasoner takes PDFs (annual reports, 10-Ks, industry studies), parses them into structured pages and tables, builds a hierarchical summary tree over each document, then answers natural-language questions by routing through that tree with LLM agents — not embeddings.

Two pipelines:

1. **Ingest** — run once per document. Parse → persist → build tree.
2. **Query** — run per question. Route → compute tables → synthesise answer.
3. **Report** — run per brief. Broad retrieval → outline → write sections → critique → revise.

Both pipelines are exposed via a FastAPI HTTP API with SSE streaming, and via CLI scripts.

---

## File map

```
doc-reasoner-manus/            3,979 lines of Python across 27 files
│
├── config.py                  Settings from .env (pydantic-settings)
├── shared.py                  Shared utilities: logging setup, SSE helper
│
├── parsing.py                 LlamaParse wrapper → pages + tables
├── tree.py                    Build hierarchical summary tree over pages
│
├── agent.py                   Query pipeline: Router → Excel → Answer agents
├── agent_schemas.py           Pydantic models for query pipeline I/O
│
├── report.py                  Report pipeline: retrieve → outline → write → critique
├── report_schemas.py          Pydantic models for report pipeline I/O
│
├── api/
│   ├── app.py                 FastAPI app, startup, middleware
│   ├── auth.py                Bearer token auth
│   ├── events.py              In-memory EventBus for SSE streaming
│   ├── ingest.py              Background ingest worker queue
│   ├── schemas.py             API request/response DTOs
│   └── routes/
│       ├── health.py          GET /healthz
│       ├── documents.py       Upload, list, get, SSE stream
│       ├── queries.py         Submit query, poll, SSE stream
│       └── reports.py         Submit report, poll, SSE stream
│
├── db/
│   ├── base.py                SQLAlchemy DeclarativeBase
│   ├── session.py             Engine + SessionLocal factory
│   ├── utils.py               CRUD helpers for all 6 tables
│   └── models/
│       └── models.py          ORM models: Doc, Node, Page, ExtractedTable, Query, Report
│
├── scripts/
│   ├── ask.py                 CLI: ask a question
│   ├── draft.py               CLI: draft a report
│   └── build_nodes.py         CLI: build trees for docs missing nodes
│
├── alembic/                   Database migrations
├── Dockerfile + docker-compose.yml
├── requirements.txt
└── .env                       All configuration
```

---

## Part 1: Configuration

### `config.py` — the single source of truth

Every tunable in the system comes from one place:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    llama_parse_key: str = ""

    tree_max_leaf_tokens: int = 8_000    # max tokens per leaf node
    tree_min_leaf_tokens: int = 4_000    # merge tail leaves below this
    tree_max_children: int = 6           # max children before splitting parent
    tree_concurrency: int = 12           # parallel LLM calls per level

    agent_router_request_limit: int = 60
    agent_excel_request_limit: int = 40
    agent_answer_request_limit: int = 10
    agent_max_hops: int = 3
    agent_max_followups_per_hop: int = 3
    # ... report settings, API settings ...
```

`get_settings()` is `@lru_cache`d — called once, reused everywhere.

### `.env` — what you actually configure

```
DATABASE_URL=sqlite:///./data/app.db
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen3.6-27b
LLAMA_PARSE_KEY=llx-...
TREE_CONCURRENCY=30
AGENT_MAX_HOPS=3
API_AUTH_TOKENS=token-dev:user_dev
```

---

## Part 2: The data layer

### `db/models/models.py` — six tables

| Table | Primary key | What it stores |
|-------|-------------|----------------|
| `docs` | `doc_id` | One row per uploaded PDF. Has `doc_summary` (root node summary), `status`, `n_pages`, `n_tables`. |
| `nodes` | `node_id` | Hierarchical tree nodes. Each has `parent_id`, `child_ids` (JSON array), `summary`, `start_page`/`end_page`, `table_ids`. |
| `pages` | `(doc_id, page_n)` | One row per page. `prose_text` (markdown), `table_ids` (which tables appear on this page), `node_id` (which leaf node owns it). |
| `tables` | `table_id` | Extracted tables. `xlsx_bytes` (the actual spreadsheet as a binary blob), `source_page`, `row_count`. |
| `queries` | `query_id` | Audit log. Every answered question is stored with its `answer`, `citations_json`, `latency_ms`. |
| `reports` | `report_id` | Draft reports. `brief`, `outline_json`, `draft_md`, `status`. |

Relationships:

```
docs ──< pages      (one doc has many pages)
docs ──< nodes      (one doc has many tree nodes)
docs ──< tables     (one doc has many extracted tables)
nodes ──< nodes     (self-referential: parent → children)
pages >── nodes     (each page points to its leaf node)
```

### `db/utils.py` — CRUD helpers

Every database operation goes through a helper function. Each takes a `Session` as its first argument:

```python
def create_doc(db, **fields) -> Doc         # insert a doc
def get_doc(db, doc_id) -> Doc | None       # fetch by PK
def list_docs(db, workspace_id) -> list     # all docs in workspace
def create_node(db, **fields) -> Node       # insert a tree node
def get_root_nodes(db, doc_id) -> list      # root nodes (parent_id IS NULL)
def bulk_create_pages(db, pages) -> int     # bulk insert pages
def list_pages(db, doc_id) -> list          # all pages, ordered by page_n
def create_table(db, **fields)              # insert extracted table
def list_tables_for_doc(db, doc_id) -> list # all tables for a doc
def create_query(db, **fields)              # audit log entry
def create_report(db, **fields)             # insert report
def update_report(db, report_id, **fields)  # update report fields
```

### `db/session.py` — connection

```python
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
```

Used everywhere as `with SessionLocal() as db:`.

---

## Part 3: Ingestion pipeline

### What happens when you upload a PDF

```
PDF file
  │
  ▼
parsing.py → parse_document_async(path)
  │  Calls LlamaParse with parse_page_with_agent mode
  │  Returns ParsedDocument:
  │    .pages_text = ["page 1 markdown", "page 2 markdown", ...]
  │    .tables = [ParsedTable(table_id, page, rows, xlsx_bytes), ...]
  │    .n_pages = 160
  │
  ▼
api/ingest.py → _persist_parsed(db, doc_id, workspace_id, parsed)
  │  For each table: utils.create_table(db, xlsx_bytes=tbl.xlsx_bytes, ...)
  │  For all pages: utils.bulk_create_pages(db, [...])
  │  Updates doc.n_pages, doc.n_tables, doc.status = "building_tree"
  │
  ▼
tree.py → build_tree(doc_id, workspace_id, db)
  │
  │  Step 1: BUCKET pages into leaf nodes
  │    _bucket_pages(pages, max_leaf=8000, min_leaf=4000)
  │    Greedy: keep adding pages until token count hits max_leaf.
  │    If the last leaf is too small (< min_leaf), merge into previous.
  │    Result: ~12-20 leaf nodes for a 160-page doc.
  │
  │  Step 2: SUMMARISE each leaf (concurrent LLM calls)
  │    _summarise_level(leaves, is_leaf=True, concurrency=30)
  │    Each leaf gets a structured prompt:
  │      "## Overview ... ## Key Facts ... ## Tables ... ## Entities ..."
  │    LLM returns summary + TITLE.
  │
  │  Step 3: GROUP leaves into parents
  │    _group_into_parents(leaves, max_children=6)
  │    Parent content = concatenated child summaries.
  │
  │  Step 4: SUMMARISE parents (concurrent LLM calls)
  │    Same pattern, but uses _PARENT_PROMPT (roll-up summary).
  │
  │  Step 5: REPEAT until single root
  │    while len(current) > 1: group → summarise → repeat
  │
  │  Step 6: PERSIST
  │    BFS to assign depths (root=0, leaves=deepest).
  │    utils.create_node(...) for each node.
  │    Backfill page.node_id (each page points to its leaf).
  │    Set doc.doc_summary = root.summary.
  │
  ▼
doc.status = "ready"
```

### `parsing.py` — LlamaParse wrapper

```python
def _build_parser():
    return LlamaParse(
        api_key=settings.llama_parse_key,
        parse_mode="parse_page_with_agent",   # best quality
        high_res_ocr=True,
        adaptive_long_table=True,
        outlined_table_extraction=True,
    )

def _collect(result) -> ParsedDocument:
    # Walk LlamaParse result page by page.
    # For each page: extract markdown text.
    # For each table item: convert rows to xlsx bytes via openpyxl.
    # Return ParsedDocument with .pages_text and .tables.
```

Tables are stored as actual `.xlsx` binary blobs — not markdown. This is critical because the Excel agent later loads them as pandas DataFrames for precise computation.

### `tree.py` — hierarchical summarisation

The tree exists so that at query time, the Router agent doesn't scan raw text. It walks summaries top-down.

A 160-page CRISIL report becomes:

```
Root (depth 0) — "Indian Auto Sector: Macro, Policy, EV, OEM Analysis"
├── Section A (depth 1, pages 1-55)
│   ├── Leaf 1 (pages 1-12)  "Macro Economic Overview and GDP Analysis"
│   ├── Leaf 2 (pages 13-25) "Auto Policy & PLI Scheme Details"
│   └── Leaf 3 (pages 26-55) "EV Adoption and Battery Technology"
├── Section B (depth 1, pages 56-110)
│   └── ...
└── Section C (depth 1, pages 111-160)
    └── ...
```

Each summary is structured:
- **Overview**: 2-3 sentences
- **Key Facts**: bullet list with actual numbers
- **Tables & Data**: what tables exist, their columns
- **Entities & Terms**: named entities with context
- **Connections**: how this relates to the rest of the doc

---

## Part 4: Query pipeline — tracing one question

Let's trace what happens when you run:

```bash
python scripts/ask.py -v "Which company's green lending pool can fund the PLI auto scheme?"
```

### Step 1: CLI entry (`scripts/ask.py`)

```python
def main():
    args = parse_args()       # query, --workspace, --user, --docs, -v
    setup_logging(args.verbose)
    asyncio.run(run(args))

async def run(args):
    result = await answer_query(
        workspace_id="ws_default",
        user_id="cli_user",
        query="Which company's green lending pool can fund the PLI auto scheme?",
    )
    # print answer, citations, hop trace
```

`setup_logging()` comes from `shared.py` — a shared utility that configures the root logger and quiets noisy HTTP libraries.

### Step 2: `answer_query()` orchestrator (`agent.py:894`)

This is the main entry point. It runs a **multi-hop frontier loop**:

```python
async def answer_query(workspace_id, user_id, query, doc_ids=None, sink=EventSink()):
    # 1. Fetch candidate docs from DB
    with SessionLocal() as db:
        candidates = list(db.scalars(
            select(Doc).where(Doc.workspace_id == workspace_id)
        ))
    # candidates = [berkshire_10k, crisil_report, hdfc_iar]

    # 2. Initialise frontier state
    pending = deque([query])          # questions to investigate
    acc_page_targets = []             # cumulative across hops
    acc_table_targets = []
    acc_findings = []
    hops = []

    # 3. Build agents ONCE (cached model, reused across hops)
    router_agent = _build_router()
    answer_agent = _build_answer_agent()

    # 4. Frontier loop
    while pending and len(hops) < settings.agent_max_hops:
        question = pending.popleft()

        # 4a. ROUTER — pick page ranges + tables
        routed = await router_agent.run(prompt, deps=router_deps)

        # 4b. EXCEL — compute on new tables (only if router picked tables)
        if new_tables:
            findings = await _run_excel_on(new_tables, question=question)

        # 4c. ANSWER — synthesise from cumulative context
        ans = await answer_agent.run(answer_prompt)

        # 4d. TERMINATE or CONTINUE
        if ans.confidence == "high" or not ans.needs_more:
            break
        # Otherwise: queue follow-up questions for next hop
        for fq in ans.follow_up_questions:
            pending.append(fq)

    # 5. Persist to queries table + return
    _save_query(workspace_id, user_id, query, result, ...)
    return result
```

### Step 3: Router agent — walking the tree

The Router is a `pydantic_ai.Agent[RouterDeps, RouterResult]` with 5 tools:

| Tool | What it does | When the LLM calls it |
|------|-------------|----------------------|
| `expand_doc(doc_id)` | Returns depth-1 sections of a doc | Once per relevant doc |
| `expand_node(node_id)` | Returns children of a node | When a section is too broad |
| `peek_node(node_id)` | Returns full summary | When brief is ambiguous |
| `list_doc_tables(doc_id)` | Lists all tables in a doc | When query needs numbers |
| `peek_table(table_id)` | Schema + first rows | To confirm table matches |

**Hard guards** in `RouterDeps` prevent the LLM from wasting tool calls:

```python
@dataclass
class RouterDeps:
    candidate_doc_ids: list[str]
    _expanded_nodes: set[str]     # refuse to re-expand same node
    _known_leaves: set[str]       # refuse to expand leaf nodes
    already_picked_pages: set[str]  # multi-hop dedup
    already_picked_tables: set[str]
```

If the LLM calls `expand_node` on a leaf, it gets back:
```
"REJECTED — this node was already identified as a leaf. Use it as a page_target directly."
```

**For our example query**, the router's tool trace looks like:

```
expand_doc(crisil_report)  → 3 sections: [Macro, EV, OEM]
expand_doc(hdfc_iar)       → 7 sections: [..., ESG Strategy, ...]
expand_node(crisil_macro)  → 6 children: [..., Auto Policy, ...]
expand_node(hdfc_esg)      → 6 children: [..., ESG Finance, ...]
list_doc_tables(crisil)    → 148 tables
peek_table(tbl_pli)        → PLI allocation by sector
list_doc_tables(hdfc)      → 583 tables
peek_table(tbl_green)      → HDFC sustainable finance breakdown
```

Berkshire is skipped — its `doc_summary` mentions insurance/railroads, not green lending.

**Router output** (`RouterResult`):

```python
RouterResult(
    page_targets=[
        PageTarget(doc_id="crisil", start_page=28, end_page=37,
                   reason="PLI scheme details"),
        PageTarget(doc_id="hdfc", start_page=105, end_page=115,
                   reason="HDFC sustainable finance pool"),
    ],
    table_targets=[
        TableTarget(doc_id="crisil", table_id="tbl_pli",
                    reason="PLI budgeted incentives by sector"),
        TableTarget(doc_id="hdfc", table_id="tbl_green",
                    reason="Sustainable finance portfolio split"),
    ],
    reasoning="Cross-doc query. CRISIL has PLI outlay, HDFC has green pool.",
)
```

### Step 4: Excel agent — computing the numbers

Only runs when the router picks table_targets. The Excel agent is `Agent[ExcelDeps, ExcelResult]` with 2 tools:

| Tool | What it does |
|------|-------------|
| `describe_table(table_id)` | Columns, dtypes, shape, first 5 rows |
| `run_pandas(table_id, code)` | `eval(code, {"pd": pd, "df": df, "__builtins__": SAFE_BUILTINS})` |

**Sandbox security**: `run_pandas` uses `eval()` (not `exec()`) with:
- 27 safe builtins (`int`, `float`, `str`, `len`, `sum`, `sorted`, etc.)
- Forbidden tokens blocked: `import `, `open(`, `exec(`, `eval(`, `__`
- Only expressions allowed — assignments are syntax errors in eval mode

**For our example**, the Excel agent runs:

```
describe_table(tbl_pli)
  → shape=[4,3] cols=['Sector', 'Outlay (INR bn)', 'Notes']

run_pandas(tbl_pli, "df[df['Sector'].str.contains('Auto')]['Outlay (INR bn)'].sum()")
  → 751.4

describe_table(tbl_green)
  → shape=[3,2] cols=['Type of Finance', 'Total Outstanding (₹ crore)']

run_pandas(tbl_green, "df['Total Outstanding ...'].apply(lambda x: int(x.replace(',',''))).sum()")
  → 494140
```

**Output**:

```python
ExcelResult(findings=[
    TableFinding(table_id="tbl_pli", doc_id="crisil",
                 finding="PLI auto + ACC = ₹751.4 bn"),
    TableFinding(table_id="tbl_green", doc_id="hdfc",
                 finding="HDFC green-only = ₹67,111 cr. Total sustainable = ₹4,94,140 cr. "
                         "PLI consumes 15.2% of total; 112% of green-only."),
])
```

### Step 5: Context rendering (`_render_page_context`)

Before the Answer agent runs, the orchestrator renders the selected pages as markdown:

```python
context_md, citations, inline_table_ids = _render_page_context(acc_page_targets)
```

This function:
1. Fetches each selected page range from the DB
2. Outputs page-by-page markdown: `### Page 28\n{prose_text}`
3. For each page, finds tables referenced by `page.table_ids`
4. Renders those tables inline as markdown tables (`_xlsx_to_markdown`)
5. Returns `Citation` objects for the answer

The result is a ~25 KB markdown blob that the Answer agent reads.

### Step 6: Answer agent — grounding the response

The Answer agent is `Agent[None, AnswerResult]` — no tools, single shot. It receives:

1. The user query
2. All rendered page content (with inline tables for context)
3. The Excel agent's `TableFinding`s (authoritative computed numbers)

**Output** (`AnswerResult`):

```python
AnswerResult(
    answer="HDFC Bank's total sustainable finance pool is ₹4,94,140 crore. "
           "The PLI scheme totals ₹751.4 bn. It would consume ~15.2% of the pool.",
    citations=[...],
    confidence="high",
    needs_more=False,
    follow_up_questions=[],
)
```

Because `confidence="high"`, the loop stops. If it were `"medium"` with `needs_more=True`, the follow-up questions would be queued and the loop would run another hop.

### Step 7: Multi-hop (when it happens)

On hop > 0, the router prompt gets an extra block:

```
## Previously explored — DO NOT re-pick these
Pages:
  - crisil:28-37
Tables:
  - tbl_pli

## Findings from previous hops
  - Table tbl_pli: PLI auto = ₹751.4 bn

Your job this hop: find DIFFERENT pages/tables addressing the gap.
```

The `RouterDeps.already_picked_pages` set also enforces dedup at the tool level.

**Termination** — any of:
1. `confidence == "high"`
2. `needs_more == False`
3. Router finds zero new targets
4. `hop >= agent_max_hops` (default 3)

### Step 8: Return and persist

```python
result = QueryAnswer(
    query_id="q_abc123",
    query="Which company's green lending pool...",
    page_targets=[...],       # cumulative across all hops
    table_targets=[...],
    table_findings=[...],
    answer="HDFC Bank's total sustainable...",
    confidence="high",
    citations=[...],
    latency_ms=104388,
    n_hops=1,
    hops=[HopTrace(...)],
)

# Persisted to queries table for audit
_save_query(workspace_id, user_id, query, result, doc_ids_used, table_ids_used)
```

---

## Part 5: Report pipeline

The report pipeline (`report.py`) generates multi-section markdown reports. Invoked via:

```bash
python scripts/draft.py "Compare ESG strategies across all companies in the workspace"
```

Or via API:

```bash
curl -X POST -H "Authorization: Bearer token-dev" \
  -d '{"user_id":"dev", "brief":"Compare ESG strategies...", "target_length":"standard"}' \
  http://localhost:8000/v1/workspaces/ws_default/reports
```

### Pipeline stages

```
brief
  │
  ▼
_broad_retrieval()
  │  Router (report variant) picks 10-20 page_targets, 5-15 table_targets
  │  Excel agent computes findings on selected tables
  │
  ▼
_make_outline()
  │  Outline agent produces ReportOutline:
  │    title, abstract, sections (each with assigned_page_refs + table_ids)
  │
  ▼
_write_all_sections()
  │  Section agent writes each section concurrently (semaphore-limited)
  │  Each section gets: its assigned pages + table findings + brief
  │
  ▼
_stitch_draft()
  │  Combines outline title + abstract + section bodies + sources
  │
  ▼
_critique() → CritiqueResult
  │  Critic agent compares draft against brief, finds gaps
  │  Each gap has: topic, follow_up_query, target_section
  │
  ▼ (if gaps found, up to report_max_hops times)
_refine()
  │  For each gap:
  │    1. _targeted_retrieval() to find new material
  │    2. Rewrite the affected section
  │  Restitch draft
  │
  ▼
_write_executive_summary()
  │  Summary agent reads the ACTUAL drafted body (not the outline abstract)
  │  Produces 4-8 sentence executive summary with real numbers
  │
  ▼
_save_report_to_disk()  → data/reports/ws_default/rep_abc.md
_persist_report()       → updates reports table with status="complete"
```

### Report agents (5 total)

| Agent | System prompt | Output type |
|-------|--------------|-------------|
| Report Router | Like query router but more aggressive: "10-20 page_targets expected" | `RouterResult` |
| Outline | "Produce non-overlapping outline. brief=3-4, standard=5-7, deep=8-12 sections" | `ReportOutline` |
| Section Writer | "Write ONE section using ONLY provided material. Cite as [doc_title, p12-p18]" | `SectionDraft` |
| Critic | "Compare draft against brief. List concrete gaps with follow_up_query" | `CritiqueResult` |
| Summary | "Write 4-8 sentence exec summary from actual body. Surface top findings with numbers" | `ExecutiveSummary` |

---

## Part 6: HTTP API

### Startup (`api/app.py`)

```python
@asynccontextmanager
async def lifespan(app):
    _run_migrations()              # Alembic upgrade head
    ingest.start_workers(n=2)      # Background ingest workers
    yield

app = FastAPI(title="Doc Reasoner API", lifespan=lifespan)
app.include_router(health.router)      # /healthz
app.include_router(documents.router)   # /v1/workspaces/{ws_id}/documents
app.include_router(queries.router)     # /v1/workspaces/{ws_id}/queries
app.include_router(reports.router)     # /v1/workspaces/{ws_id}/reports
```

### Authentication (`api/auth.py`)

Static bearer tokens parsed from `API_AUTH_TOKENS` in `.env`:

```
API_AUTH_TOKENS=token-dev:user_dev|token-prod:user_alice
```

Every `/v1/...` endpoint requires `Authorization: Bearer <token>`. The `get_current_user` dependency extracts and validates the token.

### Document upload flow

```
POST /v1/workspaces/ws_default/documents
  │ (multipart file upload)
  │
  ▼
documents.py → upload_document()
  │ 1. Save file to data/uploads/ws_default/doc_abc.pdf
  │ 2. Insert doc row with status="queued"
  │ 3. enqueue_ingest(doc_id) → puts on asyncio.Queue
  │ 4. Return 202: { doc_id, status: "queued", stream_url }
  │
  ▼
ingest.py → ingest_worker() picks up from queue
  │ 1. parse_document_async(path) → LlamaParse
  │ 2. _persist_parsed(db, ...) → pages + tables to DB
  │ 3. build_tree(doc_id, ...) → node tree
  │ 4. doc.status = "ready"
  │ 5. SSE events published throughout via EventSink
  │
  ▼
GET /v1/workspaces/ws_default/documents/doc_abc/stream
  │ (SSE: parse_started → parse_done → tree_started → tree_done → complete)
```

### Query flow

```
POST /v1/workspaces/ws_default/queries
  { "user_id": "dev", "query": "What was total revenue?" }
  │
  ▼
queries.py → create_query()
  │ Spawns asyncio.create_task(_run_query(...))
  │ Returns 202: { query_id, stream_url }
  │
  ▼
_run_query() → answer_query(ws_id, user_id, query, sink=EventSink)
  │ Full multi-hop pipeline runs
  │ SSE events: query_started → hop_started → router_started →
  │   router_done → excel_started → excel_done →
  │   answer_started → answer_done → complete
```

### SSE streaming (`shared.py`)

All three resource types (documents, queries, reports) share the same SSE pattern via `shared.sse_stream()`:

```python
def sse_stream(bus, channel_id, request):
    async def _generate():
        channel = bus.get_or_create(channel_id)
        async for event in channel.subscribe():
            if await request.is_disconnected():
                break
            yield {"event": event.type, "data": json.dumps(event.payload)}
    return EventSourceResponse(_generate())
```

### Event system (`api/events.py`)

```
EventBus (singleton: api.events.bus)
  └── EventChannel (one per job: "ingest:doc_abc", "query:q_123", "report:rep_456")
       ├── ._log: list[Event]        # replay buffer for late subscribers
       ├── ._subscribers: set[Queue]  # active SSE connections
       └── .closed: bool             # set when job completes

EventSink (passed to pipeline functions)
  └── .publish("event_type", {payload})  # no-op if no bus attached
```

CLI scripts use `EventSink()` (no bus) — events are silently dropped.
API routes attach a real bus: `EventSink(bus=bus, channel_id="query:q_abc")`.

---

## Part 7: Schemas

### Query schemas (`agent_schemas.py`)

```python
PageTarget      # doc_id, start_page, end_page, reason
TableTarget     # doc_id, table_id, reason
RouterResult    # page_targets, table_targets, reasoning

Citation        # doc_id, doc_title, pages
TableFinding    # table_id, doc_id, finding (computed text)
ExcelResult     # findings[], notes

AnswerResult    # answer, citations, confidence, needs_more, follow_up_questions,
                # save_to_file, suggested_filename
HopTrace        # hop, question, targets, findings, confidence, needs_more

QueryAnswer     # query_id, query, all targets/findings, answer, confidence,
                # citations, latency_ms, n_hops, hops[], output_path
```

### Report schemas (`report_schemas.py`)

```python
PageRef          # doc_id, start/end_page, leaf_summary, reason
TableRef         # doc_id, table_id, source_page, columns, description
ReportSection    # section_id, title, purpose, assigned_page_refs, assigned_table_ids
ReportOutline    # title, abstract, sections[]
ReportGap        # topic, follow_up_query, target_section
CritiqueResult   # gaps[], notes
SectionDraft     # section_id, title, markdown, citations, n_words
ReportResult     # report_id, brief, outline, sections, draft_md, output_path, stats
ExecutiveSummary # summary
```

---

## Part 8: How to run it

### Local development

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set OPENAI_API_KEY and LLAMA_PARSE_KEY

# Start API server
uvicorn api.app:app --reload --port 8000

# Upload a document
curl -X POST -H "Authorization: Bearer token-dev" \
  -F "file=@raw/my_report.pdf" \
  http://localhost:8000/v1/workspaces/ws_default/documents

# Ask a question (CLI)
python scripts/ask.py "What was the total revenue?"

# Ask via API
curl -X POST -H "Authorization: Bearer token-dev" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"dev", "query":"What was total revenue?"}' \
  http://localhost:8000/v1/workspaces/ws_default/queries

# Draft a report
python scripts/draft.py "Summarize key financial metrics across all documents"
```

### Docker

```bash
docker compose build
docker compose up -d
curl http://localhost:8000/healthz
```

---

## Part 9: Why this design vs naive RAG

| Naive RAG | This system |
|-----------|-------------|
| Chunk all pages, embed, top-k retrieve | Pre-built tree of summaries — LLM-routed |
| Tables get flattened into prose chunks | Tables stay structured; pandas-evaluated |
| One-shot retrieve → answer | Multi-hop with `needs_more` signal |
| Recall depends on embedding quality | Recall depends on summary quality |
| Numeric answers estimated from text | Numeric answers **computed** via pandas |
| ~all pages touched at query time | Typically 5-25 pages per query |
| No cross-doc reasoning | Router sees all docs, picks across them |

The key insight: **summaries are written at ingest time for routing, not for reading**. Each node summary contains enough specifics (numbers, entity names, table descriptions) that the Router can make informed skip/take/drill decisions without scanning raw text.
