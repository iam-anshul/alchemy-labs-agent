# Doc Reasoner HTTP API

Base URL (local): `http://localhost:8000`

All `/v1/...` endpoints require authentication via a static bearer token configured in `API_AUTH_TOKENS`.

## Authentication

```bash
export TOKEN=token-dev
export AUTH="Authorization: Bearer $TOKEN"
```

Token format in `.env`: `token1:user_alice|token2:user_bob`

---

## Health

### `GET /healthz`

No auth required.

```bash
curl -s http://localhost:8000/healthz
```

Response:

```json
{"status": "ok"}
```

---

## Documents

### `POST /v1/workspaces/{ws_id}/documents`

Upload a document for async ingestion (parse + tree build).

```bash
curl -s -X POST \
  -H "$AUTH" \
  -F "file=@/path/to/report.pdf" \
  http://localhost:8000/v1/workspaces/ws_default/documents
```

Response `202`:

```json
{
  "doc_id": "doc_a1b2c3d4e5f6",
  "status": "queued",
  "stream_url": "/v1/workspaces/ws_default/documents/doc_a1b2c3d4e5f6/stream"
}
```

### `GET /v1/workspaces/{ws_id}/documents`

List documents in a workspace.

```bash
curl -s -H "$AUTH" http://localhost:8000/v1/workspaces/ws_default/documents
```

### `GET /v1/workspaces/{ws_id}/documents/{doc_id}`

Get one document.

```bash
curl -s -H "$AUTH" http://localhost:8000/v1/workspaces/ws_default/documents/doc_a1b2c3d4e5f6
```

### `GET /v1/workspaces/{ws_id}/documents/{doc_id}/stream`

SSE stream of ingest progress. Subscribe immediately after upload.

```bash
curl -N -H "$AUTH" \
  http://localhost:8000/v1/workspaces/ws_default/documents/doc_a1b2c3d4e5f6/stream
```

Example SSE events:

```
event: parse_started
data: {"path": "data/uploads/ws_default/doc_abc.pdf"}

event: parse_done
data: {"n_pages": 42, "n_tables": 7}

event: tree_started
data: {"doc_id": "doc_abc"}

event: tree_leaves_summarised
data: {"n_leaves": 12}

event: tree_level_done
data: {"level": 1, "n_nodes": 3}

event: tree_done
data: {"root_id": "node_xyz"}

event: complete
data: {"doc_id": "doc_abc", "root_id": "node_xyz", "status": "ready"}
```

---

## Queries

### `POST /v1/workspaces/{ws_id}/queries`

Start an async reasoning query.

```bash
curl -s -X POST \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_dev", "query": "What was total revenue?", "doc_ids": null}' \
  http://localhost:8000/v1/workspaces/ws_default/queries
```

Response `202`:

```json
{
  "query_id": "q_a1b2c3d4e5f6",
  "stream_url": "/v1/workspaces/ws_default/queries/q_a1b2c3d4e5f6/stream"
}
```

### `GET /v1/workspaces/{ws_id}/queries`

List recent queries (limit 50, newest first).

```bash
curl -s -H "$AUTH" http://localhost:8000/v1/workspaces/ws_default/queries
```

### `GET /v1/workspaces/{ws_id}/queries/{query_id}`

Poll query status/result.

```bash
curl -s -H "$AUTH" http://localhost:8000/v1/workspaces/ws_default/queries/q_a1b2c3d4e5f6
```

- `200` — completed query (answer present)
- `202` — `{"status": "running"}` while the query task is in progress
- `404` — not found

### `GET /v1/workspaces/{ws_id}/queries/{query_id}/stream`

SSE stream of query progress.

```bash
curl -N -H "$AUTH" \
  http://localhost:8000/v1/workspaces/ws_default/queries/q_a1b2c3d4e5f6/stream
```

Example SSE events:

```
event: query_started
data: {"query_id": "q_abc", "query": "...", "workspace_id": "ws_default", "user_id": "user_dev", "n_candidate_docs": 3}

event: hop_started
data: {"hop": 0, "question": "What was total revenue?"}

event: router_started
data: {"hop": 0}

event: router_done
data: {"page_targets": [...], "table_targets": [...], "reasoning": "..."}

event: excel_started
data: {"n_tables": 2}

event: excel_done
data: {"findings": [...]}

event: answer_started
data: {"hop": 0}

event: answer_done
data: {"answer": "...", "confidence": "high", "needs_more": false, "follow_up_questions": []}

event: complete
data: {"query_id": "q_abc", "query": "...", "answer": "...", "confidence": "high", ...}
```

---

## SSE event types

| Channel | Event | When |
|---------|-------|------|
| ingest | `parse_started` | Before LlamaParse |
| ingest | `parse_done` | After parse; payload `n_pages`, `n_tables` |
| ingest | `tree_started` | Before tree build |
| ingest | `tree_leaves_summarised` | After leaf summaries |
| ingest | `tree_level_done` | After each parent level; `level`, `n_nodes` |
| ingest | `tree_done` | Tree complete; `root_id` |
| ingest | `complete` | Ingest finished |
| ingest | `error` | Failure; `stage`, `error_class`, `message` |
| query | `query_started` | Query begins |
| query | `hop_started` | Each hop; `hop`, `question` |
| query | `router_started` | Before router |
| query | `router_done` | After router; `page_targets`, `table_targets`, `reasoning` |
| query | `excel_started` | Before excel agent (when tables selected) |
| query | `excel_done` | After excel; `findings` |
| query | `answer_started` | Before answer agent |
| query | `answer_done` | After answer; `answer`, `confidence`, `needs_more`, `follow_up_questions` |
| query | `file_saved` | Answer saved to disk; `path` |
| query | `complete` | Full `QueryAnswer` payload |
| query | `error` | Failure; `stage`, `error_class`, `message` |

---

## Reports

### `POST /v1/workspaces/{ws_id}/reports`

Start an async report draft.

```bash
curl -s -X POST \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_dev", "brief": "Summarize FY25 CSR activities", "target_length": "standard"}' \
  http://localhost:8000/v1/workspaces/ws_default/reports
```

Response `202`:

```json
{
  "report_id": "rep_a1b2c3d4e5f6",
  "stream_url": "/v1/workspaces/ws_default/reports/rep_a1b2c3d4e5f6/stream"
}
```

### `GET /v1/workspaces/{ws_id}/reports`

List recent reports (limit 50, newest first).

```bash
curl -s -H "$AUTH" http://localhost:8000/v1/workspaces/ws_default/reports
```

### `GET /v1/workspaces/{ws_id}/reports/{report_id}`

Poll report status/result.

- `200` — completed report (`status=complete`, `draft_md` present)
- `202` — `{"status": "running"}` while the report task is in progress
- `404` — not found

### `GET /v1/workspaces/{ws_id}/reports/{report_id}/stream`

SSE stream of report progress.

Example SSE events:

```
event: report_started
data: {"report_id": "rep_abc", "brief": "...", "workspace_id": "ws_default", "target_length": "standard"}

event: retrieval_started
data: {"hop": 0}

event: retrieval_done
data: {"hop": 0, "n_page_refs": 12, "n_table_refs": 4}

event: outline_started
data: {}

event: outline_done
data: {"outline": {...}}

event: section_started
data: {"section_id": "exec-summary", "title": "Executive Summary"}

event: section_done
data: {"section_id": "exec-summary", "n_words": 420, "n_citations": 3}

event: draft_assembled
data: {"n_words": 2400}

event: critic_started
data: {"hop": 0}

event: critic_done
data: {"hop": 0, "gaps": [], "notes": "..."}

event: saved
data: {"path": "data/reports/ws_default/rep_abc.md"}

event: complete
data: {"report_id": "rep_abc", "brief": "...", "draft_md": "...", ...}
```

Report SSE event types:

| Event | Payload |
|-------|---------|
| `report_started` | `report_id`, `brief`, `workspace_id`, `target_length` |
| `retrieval_started` | `hop`; optional `gap` on targeted retrieval |
| `retrieval_done` | `hop`, `n_page_refs`, `n_table_refs` |
| `outline_started` | `{}` |
| `outline_done` | `outline` (serialized) |
| `section_started` | `section_id`, `title` |
| `section_done` | `section_id`, `n_words`, `n_citations` |
| `draft_assembled` | `n_words` |
| `critic_started` | `hop` |
| `critic_done` | `hop`, `gaps`, `notes` |
| `saved` | `path` |
| `complete` | Full `ReportResult` payload |
| `error` | `error_class`, `message` |

---

## Docker

```bash
docker compose build
docker compose up -d
curl -s http://localhost:8000/healthz
```

Uploads persist under `./data/uploads/` (mounted volume).
