# Event Streaming Changelog

This is the frontend contract for chat event streaming.

## UI Event Names

The chat stream now uses a small shared event set:

| Event | Meaning |
| --- | --- |
| `run_started` | Chat run began. |
| `agent_started` | Planner or a task agent started. |
| `agent_progress` | User-visible progress update. |
| `artifact_ready` | Renderable/downloadable content is ready. |
| `awaiting_user_input` | Run is waiting for user feedback. |
| `agent_ended` | Planner or a task agent finished or failed. |
| `run_ended` | Whole chat run finished or failed. |

## Event Payload

Every event data payload follows this shape:

```json
{
  "query_id": "...",
  "workspace_id": "...",
  "run_id": "...",
  "task_id": "t1",
  "agent_type": "planner | browser | document_answering | office | system",
  "stage": "planning | browsing | routing | excel | answering | writing_file | validating | done",
  "status": "started | progress | waiting | completed | failed",
  "message": "Short UI-safe text",
  "attempt": 1,
  "timestamp": 1710000000.0,
  "data": {},
  "artifacts": []
}
```

`task_id` is `null` for planner and system-level events.

## Artifact Payload

Artifacts use this shape:

```json
{
  "kind": "file | screenshot | markdown | extracted_content | final_answer",
  "path": "outputs/report.md",
  "filename": "report.md",
  "type": "md",
  "mime_type": "text/markdown",
  "bytes": 1234,
  "content": null,
  "content_base64": null,
  "url": null,
  "metadata": {}
}
```

Use `content` for small markdown/text. Use `content_base64` for screenshots. Use `path`/future artifact URLs for large files.

## Planner

| Event | stage | Notes |
| --- | --- | --- |
| `agent_started` | `planning` / `replanning` | Planner started. |
| `artifact_ready` | `planning` / `replanning` | `todo.md` markdown artifact. |
| `awaiting_user_input` | `planning` | Planner needs clarification. |
| `agent_ended` | `planning` / `replanning` | Includes task count and feedback flag. |

## Browser

| Event | stage | Notes |
| --- | --- | --- |
| `agent_started` | `browsing` | Browser task started. |
| `agent_progress` | `browsing` | Emitted from Browser-Use step hooks. |
| `artifact_ready` | `screenshot` | Screenshot artifact with base64 when available. |
| `artifact_ready` | `download` | New file detected under `outputs/`. |
| `artifact_ready` | `writing_file` | Browser `write_file` tool wrote a file. |
| `agent_ended` | `browsing` | Includes produced files, notes, or error. |

## Document Answering

| Event | stage | Notes |
| --- | --- | --- |
| `agent_started` | `document_query` | Includes candidate doc count. |
| `agent_progress` | `document_hop` | Current hop and question. |
| `agent_progress` | `routing` | Router started/done with compact target counts. |
| `agent_progress` | `excel` | Table analysis started. |
| `artifact_ready` | `excel` | Table findings as extracted content. |
| `agent_progress` | `answering` | Answer drafting started. |
| `artifact_ready` | `answering` | Grounded answer draft. |
| `artifact_ready` | `writing_file` | Saved markdown answer. |
| `agent_ended` | `done` | Confidence, citation count, output path. |

## Report Mode

Report mode uses `agent_type: "document_answering"`.

| Event | stage | Notes |
| --- | --- | --- |
| `agent_started` | `report` | Report drafting started. |
| `agent_progress` | `retrieval` | Evidence retrieval progress. |
| `agent_progress` | `outline` | Outline generation started. |
| `artifact_ready` | `outline` | Outline JSON artifact. |
| `agent_progress` | `section_drafting` | Section started/done. |
| `agent_progress` | `critique` | Critique started/done. |
| `agent_progress` | `summary` | Executive summary progress. |
| `artifact_ready` | `draft` | Draft markdown preview. |
| `artifact_ready` | `writing_file` | Saved report markdown. |
| `agent_ended` | `done` | Report id, sections, words, output path. |

## Office

| Event | stage | Notes |
| --- | --- | --- |
| `agent_started` | `office` | Office task started. |
| `artifact_ready` | `writing_file` | Office `write_file` wrote a file. |
| `agent_ended` | `done` | Produced files, notes, or error. |

## Run Lifecycle

| Event | stage | Notes |
| --- | --- | --- |
| `run_started` | `chat` | Top-level chat run started. |
| `agent_progress` | `validating` | Produced files are being validated. |
| `run_ended` | `done` | Final run status and final `todo.md` artifact. |

