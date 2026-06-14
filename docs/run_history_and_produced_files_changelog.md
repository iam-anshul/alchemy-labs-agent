# Run History, Run Detail, Produced Files & Preview — Changelog

This changelog covers a connected sequence of fixes that made the frontend's
**workspace run history**, **saved run detail**, and **produced-file listing /
download / preview** actually work against the authenticated backend
(`start_server.py`), plus two supporting fixes (a Vite auth-UI proxy collision
and inline previews for text files produced by every sub-agent).

The throughline: the React frontend was already written to call a set of
persisted-workspace endpoints, but several of those endpoints did not exist on
the backend. Each missing endpoint made the UI fall back to a "Backend API
required" / "not available yet" notice (a deliberate graceful-degradation path
in the frontend). We implemented the missing endpoints and matched their
response shapes to the frontend's TypeScript contracts exactly.

All backend endpoints live on `workspace_router` (mounted at prefix
`/workspace` in [`start_server.py`](../start_server.py)) and are
session-protected via SuperTokens `verify_session()`, scoped by the signed-in
`user_id` so nothing leaks across users.

---

## Part 0 — Why nothing showed up

The frontend's `requestOptionalFeature` helper
([`frontend/src/api/runs.ts`](../frontend/src/api/runs.ts)) treats an HTTP
`404` as "this backend doesn't support the feature": it swallows the error,
returns an empty value, and sets `isAvailable: false`. The UI then renders a
`BackendFeatureNotice` ("Backend API required") instead of crashing.

So every missing endpoint manifested as a polite "not available yet" card, not
an error. The four notices we chased, in order:

1. **"Run history is not available yet"** → `GET /workspace/{ws}/runs` missing.
2. **"This backend cannot load saved run details yet"** → `GET /workspace/{ws}/runs/{run_id}` missing.
3. **"Saved output downloads are not available yet"** → `GET /workspace/{ws}/outputs` missing.
4. **"No preview URL is available"** (per-file) → a different bug: produced text
   artifacts emitted over SSE with neither inline `content` nor a fetchable
   `url`.

---

## Part 1 — Workspace run list (`GET /workspace/{workspace_id}/runs`)

### Symptom
The workspace page's "Recent runs" section showed *"Run history is not available
yet — the active backend does not expose the workspace run-list endpoint."*

### Cause
`WorkspacePage` calls `listRuns(workspaceId)` →
`GET /workspace/{workspaceId}/runs` ([`frontend/src/api/runs.ts`](../frontend/src/api/runs.ts)),
expecting `WorkspaceRun[]`. `workspace_router` only had
`create_workspace`, `delete_workspace`, `list_workspace`, and `list_files` — no
run-list route, so the call 404'd.

### Changes

**[`db/utils.py`](../db/utils.py)** — new `list_workspace_runs`:
```python
def list_workspace_runs(db, workspace_id, user_id) -> list[QueryRun]:
    """Every run in a workspace owned by this user, newest first."""
    return list(db.scalars(
        select(QueryRun)
        .where(QueryRun.workspace_id == workspace_id,
               QueryRun.user_id == user_id)
        .order_by(desc(QueryRun.started_at))
    ))
```
Distinct from the existing `list_recent_runs` (which is capped at 5,
chronological oldest-first, and used to reconstruct planner message history):
this returns the **full** history **newest-first**, **user-scoped** so a run
list can't leak across users.

**[`api/response_models.py`](../api/response_models.py)** — new
`WorkspaceRunInfo` Pydantic model, shaped to match the frontend `WorkspaceRun`
type ([`frontend/src/types/api.ts`](../frontend/src/types/api.ts)):
`query_id` (str), `workspace_id`, `user_query`, `status`, `started_at` (ISO-8601
string), `query_counter`, `todo_md` (nullable).

**[`api/routes/workspace.py`](../api/routes/workspace.py)** — new endpoint
`GET /{workspace_id}/runs` plus a `_to_run_info(run)` serializer that converts a
`QueryRun` ORM row to `WorkspaceRunInfo` (UUID → str, datetime → `.isoformat()`).

### Gotcha: shadowed `list` builtin
The file's existing `/list_files` handler is named `async def list(...)`, which
shadows the `list` builtin at module scope. Using `list[WorkspaceRunInfo]` as a
`response_model` therefore raised `TypeError: 'function' object is not
subscriptable`. Fixed by importing `from typing import List` and using
`List[WorkspaceRunInfo]` for the response models in this file.

---

## Part 2 — Saved run detail (`GET /workspace/{workspace_id}/runs/{run_id}`)

### Symptom
Run history now listed, but **clicking a past run** showed *"This backend cannot
load saved run details yet."* and the page hung on "Waiting for run details…"
with a perpetual "connecting" status.

### Cause
`RunPage` calls `getRun(workspaceId, runId)` →
`GET /workspace/{workspaceId}/runs/{runId}`
([`frontend/src/pages/RunPage.tsx`](../frontend/src/pages/RunPage.tsx)), expecting
a single `WorkspaceRun`. For a *finished* run the SSE stream is closed, so the
page relies entirely on this call to reconstruct a historical view (via
`getDisplayEvents`, which builds a synthetic `run_ended` event from the run's
`todo_md`). The endpoint didn't exist → 404 → `historyUnavailable` notice and no
saved view.

### Changes

**[`db/utils.py`](../db/utils.py)** — new `get_workspace_run`:
```python
def get_workspace_run(db, workspace_id, query_id, user_id) -> QueryRun | None:
    """One run by id, scoped to workspace + owning user. None if not found."""
```

**[`api/routes/workspace.py`](../api/routes/workspace.py)** — new endpoint
`GET /{workspace_id}/runs/{run_id}`:
- Parses `run_id` as `UUID` (the PK column type). A malformed id returns **404**
  (not a 500) — caught `ValueError` → `HTTPException(404)`.
- `None` from the db helper → **404** ("run not found"), per the rule that
  unknown/unauthorized resources 404 without leaking existence.
- Reuses the `_to_run_info` serializer from Part 1.

The frontend then loads the saved row, `RunPage` stops trying to stream (the run
status is non-`running`), and `getDisplayEvents` renders the historical run as a
single completed/failed "Saved run" entry carrying the `todo.md` artifact.

---

## Part 3 — Produced files: list, per-run list & download

### Symptom
The workspace "Files" panel had two tabs — **Uploaded** and **Produced**. The
Produced tab was disabled with *"Saved output downloads are not available yet —
this backend does not expose saved output listing and download endpoints."*
(Uploaded already worked via the existing `GET /v1/workspaces/{ws}/documents`;
it was simply empty.)

### Cause
Three frontend calls had no backend:
- `listOutputs(ws)` → `GET /workspace/{ws}/outputs` (Produced tab on `WorkspacePage`)
- `listRunOutputs(ws, runId)` → `GET /workspace/{ws}/runs/{runId}/outputs` (RunPage OutputShelf)
- the per-file `download_url` / `preview_url` each output carries (used directly
  as `<a href>` and `fetch` targets)

Produced files are persisted as base64 in `QueryRun.produced_artifacts` — a list
of `{rel_path, content_b64, bytes, task_id}` written incrementally as each task
finishes (see `append_run_artifacts`). So all three endpoints can be served from
that column; no filesystem reads required.

### Changes

**[`db/utils.py`](../db/utils.py)** — `import base64` hoisted to module top, and
three new helpers:

- `list_workspace_produced_artifacts(db, workspace_id, user_id)` — flattens every
  run's `produced_artifacts` into one list, newest run first. Each entry is the
  stored dict augmented with `run_id` (str) and `run_started_at` (ISO string) so
  the route can build URLs and a `modified_at` without a second query.
- `get_run_produced_artifacts(db, workspace_id, query_id, user_id)` — same
  augmented shape for a single run. Returns `None` if the run doesn't exist for
  this user (→ route 404), or `[]` if it produced nothing.
- `get_run_artifact_bytes(db, workspace_id, query_id, user_id, rel_path)` —
  returns `(raw_bytes, artifact_dict)` decoded from the stored base64 for one
  produced file, or `None` if the run / file isn't found. Backs the download
  route.

**[`api/response_models.py`](../api/response_models.py)** — new `WorkspaceOutput`
model matching the frontend type
([`frontend/src/types/api.ts`](../frontend/src/types/api.ts)): `run_id`,
`task_id`, `filename`, `relative_path`, `bytes`, `mime_type`, `modified_at`,
`preview_url`, `download_url`.

**[`api/routes/workspace.py`](../api/routes/workspace.py)** — a `_to_output`
serializer plus three endpoints:

- `_to_output(workspace_id, art)` builds a `WorkspaceOutput`. It URL-encodes each
  `rel_path` segment (`quote` per segment) so paths with spaces or subdirectories
  work, and constructs:
  - `download_url` = `/workspace/{ws}/runs/{run_id}/outputs/{encoded_rel_path}`
  - `preview_url`  = same `+ ?disposition=inline`
- `GET /{workspace_id}/outputs` → all produced files in the workspace.
- `GET /{workspace_id}/runs/{run_id}/outputs` → one run's produced files
  (404 on unknown run / malformed UUID).
- `GET /{workspace_id}/runs/{run_id}/outputs/{rel_path:path}` → serves the file
  **bytes** decoded from base64. Honors `?disposition=inline` (preview) vs the
  default `attachment` (download) via the `Content-Disposition` header; media
  type guessed from the filename. Imports added: `Response` (FastAPI), `quote`
  (`urllib.parse`), `mimetypes`.

### Route ordering / matching
The `{rel_path:path}` catch-all is greedy, so ordering matters. We verified with
Starlette's `route.matches()` that all five run/output routes resolve to the
intended handler and that none shadows another:

| Request | Resolves to |
| --- | --- |
| `/workspace/ws/runs` | `…/runs` |
| `/workspace/ws/runs/{uuid}` | `…/runs/{run_id}` |
| `/workspace/ws/runs/{uuid}/outputs` | `…/runs/{run_id}/outputs` |
| `/workspace/ws/runs/{uuid}/outputs/outputs/report.pptx` | `…/outputs/{rel_path:path}` |
| `/workspace/ws/outputs` | `…/outputs` |

### Auth on download links
The Produced tab renders downloads as plain same-origin `<a href={download_url}>`
links. Because SuperTokens uses cookie sessions, the browser attaches the session
cookie automatically through the Vite proxy, so `verify_session()` passes on the
download route without extra plumbing.

### Frontend test parity
The existing frontend tests
([`frontend/src/api/runs.test.ts`](../frontend/src/api/runs.test.ts),
[`frontend/src/components/files/FileList.test.tsx`](../frontend/src/components/files/FileList.test.tsx))
already asserted the exact URL scheme
(`/workspace/{ws}/runs/{run_id}/outputs/{rel_path}` and `?disposition=inline`).
Our `_to_output` output matches them; both test files pass unchanged.

---

## Part 4 — Vite auth-UI proxy collision

### Symptom
Navigating to `http://localhost:5173/auth-ui/?redirectToPath=` returned raw JSON
`{"detail":"Not Found"}` instead of the SuperTokens login page.

### Cause
[`frontend/vite.config.ts`](../frontend/vite.config.ts) proxied `"/auth"` to the
backend. Vite proxy keys match by **prefix**, so `/auth-ui` (a *client-side*
route that the React app serves via the SuperTokens prebuilt UI) was also caught
and forwarded to the backend on :8000, which has no such route → 404 JSON.

### Change
Changed the proxy key from the plain prefix `"/auth"` to the regex
`"^/auth(?!-ui)"` (negative lookahead). Now:
- `/auth/*` (SuperTokens **API**, e.g. `/auth/signin`, `/auth/session/refresh`)
  → proxied to the backend.
- `/auth-ui/*` (auth **UI** page) → falls through to Vite's SPA serving, so
  React renders the login form.

Verified the regex routing with a small Node script. **Requires a Vite dev-server
restart** — proxy config is not hot-reloaded.

> Environment note (no code change): during this work the dev server also hit
> two npm-side issues — a missing `@rollup/rollup-darwin-arm64` native binary
> (the well-known npm optional-deps bug) and a later `vite: command not found`
> (devDependencies absent). Both were resolved by removing `node_modules` +
> `package-lock.json` and reinstalling with dev deps included. Not part of the
> source changes, recorded here only for context.

---

## Part 5 — "No preview URL is available" for produced text files

### Symptom
Selecting a produced markdown file (e.g. `t1_weather_data.md`) in a run's focus
panel showed a file card reading *"No preview URL is available"* instead of the
rendered markdown.

### Cause
The frontend artifact preview
([`frontend/src/components/artifacts/ProducedFilePreview.tsx`](../frontend/src/components/artifacts/ProducedFilePreview.tsx))
renders text/markdown inline **if** the artifact carries inline `content`. If
`content` is `null` it falls back to fetching `artifact.url`; if that is also
absent, `fetchArtifactResponse` throws `"No preview URL is available"`. Per the
frontend's design rule, **it never invents a download route from a path** — the
backend must supply either `content` or a `url`.

Some sub-agents emitted produced-file `artifact_ready` SSE events for text files
with **neither** field set.

### Audit of every sub-agent's artifact emission

| Agent | Mechanism | Before | Action |
| --- | --- | --- | --- |
| `web_agent.py` | `write_file` tool / `submit` consolidated event | `write_file` inlined content; **`submit` did not** | **Fixed** `submit` |
| `browser_agent.py` | `write_file` tool / `_emit_new_outputs` | `write_file` inlined content; **`_emit_new_outputs` did not** | **Fixed** `_emit_new_outputs` |
| `office_agent.py` | `write_file` tool | inlined content; produces binaries (docx/xlsx/pptx) → download route | No change |
| `agent.py` (document_answering) | saves answer md with `content=out_path.read_text()` | already inlined | No change |
| `report.py` | emits markdown with `content=draft_md` | already inlined | No change |

### Changes

**[`web_agent.py`](../web_agent.py)** — the `submit` tool's consolidated
produced-files `artifact_ready` event now reads each produced text file
(`.md/.txt/.csv/.json`) off disk and sets `content=`, and uses
`kind="markdown"` for `.md`. Binary files stay content-less (they preview /
download via the persisted-outputs route once the run is saved). Read failures
(`UnicodeDecodeError`, `OSError`) degrade to `content=None` rather than raising.

**[`browser_agent.py`](../browser_agent.py)** — `_emit_new_outputs`, which fires
an `artifact_ready` for **every new file the browser saves under `outputs/`**,
previously hard-coded `kind="file"` with no `content`. It now inlines `content`
for text files and uses `kind="markdown"` for `.md`, with the same
`UnicodeDecodeError`/`OSError` guard. This was the most likely source of the
screenshot (a browser/web research output).

### Design rationale
- **Text files** (md/txt/csv/json) → inline `content`. Cheap, previews live with
  no round-trip, consistent with the existing `write_file` tools.
- **Binary files** (docx/xlsx/pptx/png) → no inline content; they preview /
  download via `GET /workspace/{ws}/runs/{run_id}/outputs/{rel_path}` (Part 3)
  once the run is persisted. Mid-run, a binary correctly shows the download
  fallback, matching the README contract that a file artifact shows a download
  action only when the backend provides a `url`.

---

## Net result

- **Recent runs** list populates on the workspace page.
- **Clicking a past run** loads its saved detail and renders the historical
  `todo.md` view instead of hanging.
- **Produced** files tab lists every run's outputs and downloads their original
  bytes; the run-detail OutputShelf lists a single run's outputs.
- **Auth UI** loads at `/auth-ui/` instead of returning JSON 404.
- **Every text file** any sub-agent produces previews inline live; every binary
  file previews/downloads via the persisted-outputs route once saved.

## Files touched

Backend:
- [`db/utils.py`](../db/utils.py) — `list_workspace_runs`, `get_workspace_run`,
  `list_workspace_produced_artifacts`, `get_run_produced_artifacts`,
  `get_run_artifact_bytes`; `import base64`.
- [`api/response_models.py`](../api/response_models.py) — `WorkspaceRunInfo`,
  `WorkspaceOutput`.
- [`api/routes/workspace.py`](../api/routes/workspace.py) — `_to_run_info`,
  `_to_output`, and 5 endpoints (`/{ws}/runs`, `/{ws}/runs/{run_id}`,
  `/{ws}/outputs`, `/{ws}/runs/{run_id}/outputs`,
  `/{ws}/runs/{run_id}/outputs/{rel_path:path}`); imports for `Response`,
  `quote`, `mimetypes`, `List`, `UUID`.
- [`web_agent.py`](../web_agent.py) — `submit` inlines text content.
- [`browser_agent.py`](../browser_agent.py) — `_emit_new_outputs` inlines text content.

Frontend:
- [`frontend/vite.config.ts`](../frontend/vite.config.ts) — `/auth` proxy regex
  excludes `/auth-ui`.

## Verification performed
- `start_server` imports cleanly; all five agent modules import.
- All five run/output routes mount on the live `server` app and resolve to the
  correct handler (Starlette match check), including the greedy `:path` route.
- `tsc --noEmit` clean; frontend tests pass:
  `runs.test.ts`, `FileList.test.tsx`, `runArtifacts.test.ts`,
  `ProducedFilePreview.test.ts`, `MarkdownPreview.test.tsx`,
  `ArtifactPreview.test.tsx`.

## Follow-ups (not done)
- `agent.py` and `report.py` emit artifacts with **absolute** `path`
  (`str(out_path)`) rather than workspace-relative; inline `content` makes
  preview work regardless, but it's inconsistent with the `outputs/...` relative
  paths the other agents use (and the frontend's `isProducedArtifact` keys off
  `outputs/`). Normalizing to relative paths is a possible cleanup.
- `GET /workspace/summaries` (workspace counts / recent activity) is referenced
  by the frontend README's endpoint table but was not implemented in this work.
