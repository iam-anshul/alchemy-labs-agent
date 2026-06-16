# Document Agent End-to-End Explainer

This is a guided example of how the current `document_answering` agent works, from files on disk to indexed nodes, document Q&A, and report drafting.

## Big Picture

The planner can create a task with:

```json
{
  "id": "t2",
  "agent": "document_answering",
  "query": "Compare Apple's revenue growth and margin trends from the uploaded 10-Qs.",
  "expects": "outputs/apple_trend_answer.md",
  "doc_deps": {
    "doc_answering_mode": "ASK",
    "doc_ids": ["doc_aapl_q1", "doc_aapl_q2"],
    "target_length": null,
    "report_id": null
  }
}
```

The chat control loop dispatches that task in [api/routes/chat.py](/home/tejesh/alchemy-labs-agent/api/routes/chat.py). The document agent branch does not browse the web or edit office files. It calls the document reasoning engine directly:

- ASK mode -> [answer_query](/home/tejesh/alchemy-labs-agent/agent.py)
- REPORT mode -> [draft_report](/home/tejesh/alchemy-labs-agent/report.py)

The document agent is really two things:

- A stored document index: parsed pages, extracted tables, and a summary tree.
- A reasoning pipeline: route to relevant nodes/tables, run pandas on tables if needed, then write a grounded answer or report.

## Example Setup

Imagine the browser agent downloaded these files:

```text
file_system_root/acme_workspace/7_query_uuid/
  outputs/acme_2024_10k.pdf
  outputs/acme_q1_2025_10q.pdf
  outputs/source_notes.md
```

Before `document_answering` can answer over those PDFs, the files must be ingested into the document index. In the current chat path, the planner receives an injected inventory of ready workspace documents and passes those exact `doc_id` values into `TaskSpec.doc_deps` when the user names a ready document. If a file is newly downloaded by a `browser` task, the downstream `document_answering` task must depend on that browser task; the control loop ingests the dependency PDF right before the document task runs.

## Ingestion: File to Database

Document ingestion is the one-time indexing path. It is driven by the document routes and ingest worker:

```text
PDF on disk
  -> parse_document_async(...)
  -> DB docs/pages/tables rows
  -> build_tree(...)
  -> DB nodes rows + docs.doc_summary
```

### Step 1: Parse The PDF

[parsing.py](/home/tejesh/alchemy-labs-agent/parsing.py) uses LlamaParse:

```python
parse_mode="parse_page_with_agent"
high_res_ocr=True
adaptive_long_table=True
outlined_table_extraction=True
```

For each document it produces:

- `ParsedDocument.text`: full markdown text
- `ParsedDocument.pages_text`: one markdown string per page
- `ParsedDocument.tables`: extracted tables
- `ParsedTable.xlsx_bytes`: each table stored as an `.xlsx` byte blob

Example parse result:

```text
doc_id: doc_acme_10k
pages:
  page 1 -> prose markdown
  page 2 -> prose markdown
  page 3 -> prose markdown + table_ids ["tbl_rev"]
tables:
  tbl_rev -> source_page=3, rows=12, xlsx_bytes=<binary workbook>
```

### Step 2: Store Parsed Data

The DB stores the parsed document in these tables:

| Table | What is stored |
| --- | --- |
| `docs` | `doc_id`, `workspace_id`, title, source path, page/table counts, root summary, status |
| `pages` | One row per page: page number, markdown/prose text, table ids, later `node_id` |
| `tables` | One row per extracted table: table id, source page, row count, description, xlsx bytes |

At this stage the document is readable page-by-page, but not yet easy to route through.

### Step 3: Build The Node Tree

[tree.py](/home/tejesh/alchemy-labs-agent/tree.py) turns pages into a hierarchy:

```text
pages
  -> bucket into leaf nodes by token count
  -> summarize each leaf
  -> group leaves into parent nodes
  -> summarize each parent
  -> repeat until one root node remains
```

Each node row stores:

```text
node_id
doc_id
parent_id
depth
title
start_page
end_page
summary
table_ids
child_ids
```

Example tree:

```text
node_root depth=0 pages=1-120
  summary: "ACME annual report covering business overview, financials..."
  child_ids:
    node_business depth=1 pages=1-30
    node_risk depth=1 pages=31-55
    node_financials depth=1 pages=56-90
    node_notes depth=1 pages=91-120

node_financials depth=1 pages=56-90
  child_ids:
    node_income_statement depth=2 pages=56-63 table_ids=[tbl_rev, tbl_margin]
    node_cash_flow depth=2 pages=64-70
```

The important part: the router agent can inspect summaries first instead of reading every page.

## ASK Mode: Focused Document Q&A

ASK mode runs [answer_query](/home/tejesh/alchemy-labs-agent/agent.py).

Example task:

```text
Query:
Compare ACME's revenue growth and gross margin trend between FY2024 and Q1 2025.

doc_ids:
["doc_acme_10k", "doc_acme_q1_10q"]
```

Top-down flow:

```text
answer_query(...)
  -> load candidate docs
  -> Router agent picks page/table targets
  -> Excel agent analyzes tables if needed
  -> Answer agent writes grounded answer
  -> optionally save markdown answer
  -> persist query result
```

### Router Agent

The router agent has these tools in [agent.py](/home/tejesh/alchemy-labs-agent/agent.py):

| Tool | What it does |
| --- | --- |
| `expand_doc(doc_id)` | Returns top-level sections under the document root. |
| `expand_node(node_id)` | Returns child nodes for one node. |
| `peek_node(node_id)` | Returns full node summary when the brief is not enough. |
| `list_doc_tables(doc_id)` | Lists extracted tables for a document. |
| `peek_table(table_id)` | Returns table schema and first rows. |

Example router call sequence:

```text
expand_doc("doc_acme_10k")
  -> sees "Financial Statements", pages 56-90

expand_node("node_financials")
  -> sees "Income Statement", pages 56-63, table_ids=[tbl_rev, tbl_margin]

list_doc_tables("doc_acme_q1_10q")
  -> sees tbl_q1_income, source_page=7

peek_table("tbl_q1_income")
  -> columns: Revenue, Gross Margin, Quarter
```

Router output:

```json
{
  "page_targets": [
    {
      "doc_id": "doc_acme_10k",
      "start_page": 56,
      "end_page": 63,
      "reason": "FY2024 revenue and margin table"
    },
    {
      "doc_id": "doc_acme_q1_10q",
      "start_page": 7,
      "end_page": 9,
      "reason": "Q1 2025 income statement"
    }
  ],
  "table_targets": [
    {
      "doc_id": "doc_acme_10k",
      "table_id": "tbl_rev",
      "reason": "FY2024 revenue rows"
    },
    {
      "doc_id": "doc_acme_q1_10q",
      "table_id": "tbl_q1_income",
      "reason": "Q1 2025 revenue and gross margin"
    }
  ]
}
```

### Excel Agent

If router selected tables, the Excel agent loads each table's `xlsx_bytes` into pandas DataFrames.

Its tools:

| Tool | What it does |
| --- | --- |
| `describe_table(table_id)` | Returns columns, dtypes, shape, and first rows. |
| `run_pandas(table_id, code)` | Runs a safe pandas expression against `df`. |

Example:

```text
describe_table("tbl_rev")
run_pandas("tbl_rev", "df[['Year','Revenue','Gross Margin']].tail(2).to_dict('records')")
run_pandas("tbl_q1_income", "df[['Quarter','Revenue','Gross Margin']].to_dict('records')")
```

Excel output becomes structured `TableFinding`s:

```json
[
  {
    "table_id": "tbl_rev",
    "finding": "FY2024 revenue was $10.2B, up 12% YoY; gross margin was 41.5%."
  },
  {
    "table_id": "tbl_q1_income",
    "finding": "Q1 2025 revenue was $2.8B; gross margin improved to 42.1%."
  }
]
```

### Answer Agent

The answer agent receives:

- rendered page text from selected pages
- accumulated table findings
- citations generated from the page targets

It returns:

```json
{
  "answer": "ACME's revenue growth remained positive...",
  "confidence": "high",
  "needs_more": false,
  "follow_up_questions": [],
  "save_to_file": true,
  "suggested_filename": "acme_revenue_margin_trend.md"
}
```

If `save_to_file` is true, `_maybe_save_answer(...)` writes:

```text
outputs/acme_revenue_margin_trend.md
```

That file includes:

- answer heading
- confidence and hop count
- grounded answer text
- sources with document/page citations

Finally `answer_query` persists the query result to the DB.

## Multi-Hop Behavior

The document answer loop can ask follow-up questions internally.

Example:

```text
Hop 0 question:
"Compare revenue growth and gross margin trend."

Answer agent:
"Need more detail on segment revenue."

Hop 1 question:
"Find ACME segment revenue changes for FY2024 and Q1 2025."
```

Each hop can add more page targets, table targets, and table findings. The final answer is written from cumulative evidence.

## REPORT Mode: Multi-Section Report

REPORT mode runs [draft_report](/home/tejesh/alchemy-labs-agent/report.py).

Example task:

```json
{
  "agent": "document_answering",
  "query": "Draft a standard report on ACME's FY2024 performance, Q1 2025 momentum, risks, and margin outlook.",
  "doc_deps": {
    "doc_answering_mode": "REPORT",
    "doc_ids": ["doc_acme_10k", "doc_acme_q1_10q"],
    "target_length": "standard",
    "report_id": null
  }
}
```

Top-down report flow:

```text
draft_report(...)
  -> create reports DB row
  -> broad retrieval
  -> make outline
  -> write all sections
  -> assemble draft
  -> critique draft
  -> optionally refine retrieval/outline/sections
  -> write final executive summary
  -> save markdown report
  -> persist report result
```

### 1. Broad Retrieval

Report retrieval uses a report-specific router. It is similar to ASK routing, but broader. It tries to collect enough evidence for the whole report, not just one focused answer.

Example retrieval output:

```text
page_refs:
  doc_acme_10k pages 5-15: business overview
  doc_acme_10k pages 56-63: financial statements
  doc_acme_10k pages 70-80: risks
  doc_acme_q1_10q pages 6-12: Q1 financial update

table_refs:
  tbl_rev
  tbl_margin
  tbl_q1_income
```

If tables are selected, report mode also runs table analysis to create reusable findings.

### 2. Outline

The outline agent receives the report brief plus retrieved page/table summaries. It returns a structured outline.

Example:

```json
{
  "title": "ACME Performance And Margin Outlook",
  "abstract": "A report on ACME's growth, profitability, and risks.",
  "sections": [
    {
      "section_id": "s1",
      "title": "Executive Overview",
      "purpose": "Summarize main conclusions",
      "must_cover": ["FY2024 growth", "Q1 2025 momentum"]
    },
    {
      "section_id": "s2",
      "title": "Revenue And Margin Trends",
      "purpose": "Analyze revenue growth and gross margin",
      "must_cover": ["FY2024 revenue", "Q1 2025 revenue", "margin drivers"]
    },
    {
      "section_id": "s3",
      "title": "Risks And Outlook",
      "purpose": "Explain risks and forward-looking issues",
      "must_cover": ["demand risk", "cost pressure", "guidance"]
    }
  ]
}
```

### 3. Section Drafting

Each section is drafted by a section agent. Sections can run concurrently. For each section, the agent receives:

- overall brief
- section purpose and must-cover points
- relevant rendered page content
- table findings

Example section output:

```markdown
## Revenue And Margin Trends

ACME reported FY2024 revenue of $10.2B, up 12% year over year...
Gross margin improved from 39.8% to 41.5%, primarily due to...
Q1 2025 revenue reached $2.8B...
```

Each section tracks:

- `section_id`
- `title`
- `markdown`
- `citations`
- word count

### 4. Draft Assembly

The report stitches sections together:

```text
title
executive summary placeholder
section 1
section 2
section 3
sources
```

At this point the system emits a draft preview artifact in the event stream.

### 5. Critique And Refine

The critic agent reviews the draft and returns gaps.

Example critique:

```json
{
  "gaps": [
    {
      "section_id": "s3",
      "issue": "Risk section lacks detail on customer concentration."
    }
  ],
  "notes": "Retrieve additional risk disclosure pages."
}
```

If there are gaps and the hop budget allows, report mode refines:

```text
gap -> targeted retrieval -> update outline/sections -> reassemble draft
```

### 6. Final Summary And Save

After sections are stable, report mode writes a final executive summary from the actual drafted body, then saves:

```text
outputs/rep_abc123.md
```

It persists the final report in the `reports` table with:

- `report_id`
- brief
- target length
- outline JSON
- draft markdown
- output path
- section count
- word count
- hop count
- report name

## Event Stream View

For the UI, document answering uses the normalized event stream.

ASK mode looks like:

```text
agent_started      stage=document_query
agent_progress     stage=document_hop
agent_progress     stage=routing
agent_progress     stage=excel
artifact_ready     stage=excel
agent_progress     stage=answering
artifact_ready     stage=answering
artifact_ready     stage=writing_file
agent_ended        stage=done
```

REPORT mode looks like:

```text
agent_started      stage=report
agent_progress     stage=retrieval
agent_progress     stage=outline
artifact_ready     stage=outline
agent_progress     stage=section_drafting
agent_progress     stage=critique
agent_progress     stage=summary
artifact_ready     stage=draft
artifact_ready     stage=writing_file
agent_ended        stage=done
```

## What To Remember

- The document agent does not scan whole PDFs at answer time.
- PDFs are parsed once into pages and tables.
- Pages are summarized into a hierarchical node tree.
- The router navigates the node tree using summaries.
- Tables are stored as xlsx bytes and analyzed through pandas when needed.
- ASK mode produces a focused grounded answer.
- REPORT mode retrieves broad evidence, outlines, drafts sections, critiques, refines, and saves a markdown report.
- Files users see are written under the run workspace, usually `outputs/`.
