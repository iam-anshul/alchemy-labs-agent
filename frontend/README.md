# Alchemy Labs Frontend

This directory contains the standalone Alchemy Labs web application.

The frontend is intentionally a small client-rendered application. It talks to
the existing FastAPI server for authentication, data, files, agent execution,
and event streaming. No backend changes are required to build the frontend.

Before changing frontend code, read
[`frontend_coding_rules.md`](./frontend_coding_rules.md).

## Stack

- React
- TypeScript
- Vite
- React Router
- SuperTokens React SDK
- Lucide React icons
- Native `fetch`, `FormData`, and `EventSource`
- React Markdown with GitHub-flavored Markdown support
- Lazy DOCX, XLSX, PPTX, CSV, PDF, image, audio, and video previews
- Plain CSS

Do not add another library until the existing stack cannot solve a concrete
problem cleanly.

## Local Development

### Prerequisites

- Python 3.13 with the repository dependencies installed
- Node.js 20 or newer
- Docker with the Compose plugin
- A populated root `.env`

The frontend uses Vite on port `5173`. Vite proxies authentication and API
requests to the authenticated FastAPI server on port `8000`.

### 1. Start PostgreSQL And SuperTokens

From the repository root:

```bash
docker compose -f supertokens.docker-compose.yaml up -d
```

This starts:

- PostgreSQL at `localhost:5432`
- SuperTokens at `http://localhost:3567`

### 2. Configure The Backend

The root `.env` must include values equivalent to:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:root@localhost:5432/postgres
SUPERTOKENS_URI=http://localhost:3567
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o
LLAMA_PARSE_KEY=your-key-if-document-parsing-is-needed
LINKUP_API_KEY=your-key-if-web-search-is-needed
```

Install Python dependencies when the environment is new:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Apply Database Migrations

From the repository root:

```bash
alembic upgrade head
```

### 4. Start The Authenticated Backend

From the repository root:

```bash
python start_server.py
```

Use `start_server.py` for this frontend. The root `docker compose up` command
currently launches `api.app:app`, which does not register the SuperTokens
workspace and chat routes used by the UI.

### 5. Start The Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open these links:

| Service | URL |
| --- | --- |
| Alchemy Labs frontend | [http://localhost:5173](http://localhost:5173) |
| Backend health | [http://localhost:8000/health](http://localhost:8000/health) |
| Protected API docs | [http://localhost:8000/docs](http://localhost:8000/docs) |
| SuperTokens health | [http://localhost:3567/hello](http://localhost:3567/hello) |

The protected API docs use the local Basic Auth credentials currently defined
in `start_server.py`: username `admin`, password `password`.

### Production Build

```bash
cd frontend
npm run typecheck
npm test
npm run build
```

The deployable static bundle is written to `frontend/dist`. The current
backend does not serve this directory, so a production static host must serve
it, route unknown browser paths to `index.html`, and proxy `/auth`,
`/workspace`, `/chat`, and `/v1` to the authenticated backend.

### Troubleshooting

- A blank or redirecting auth page usually means SuperTokens is not running at
  `http://localhost:3567`.
- API `404` responses for workspace or chat calls usually mean
  `api.app:app` was started instead of `start_server.py`.
- Database relation errors mean `alembic upgrade head` has not been run against
  the `DATABASE_URL` used by the backend.
- Persisted run history and produced files are provided by the authenticated
  workspace API. The UI still degrades cleanly when connected to an older
  backend without those routes.

## Product Flow

The first version has three routes:

| Route | Responsibility |
| --- | --- |
| `/workspaces` | List and create workspaces. |
| `/workspaces/:workspaceId` | Upload and list documents, start a run, review saved runs and produced files, and delete the workspace. |
| `/workspaces/:workspaceId/runs/:runId` | Stream compact live activity, answer questions, inspect web pages and artifacts, or reopen a saved run. |

## Project Structure

```text
frontend/
├── public/
├── src/
│   ├── api/
│   │   ├── client.ts
│   │   ├── documents.ts
│   │   ├── runs.ts
│   │   └── workspaces.ts
│   ├── components/
│   │   ├── artifacts/
│   │   ├── files/
│   │   ├── layout/
│   │   ├── runs/
│   │   └── ui/
│   ├── hooks/
│   │   └── useRunStream.ts
│   ├── pages/
│   │   ├── RunPage.tsx
│   │   ├── WorkspacePage.tsx
│   │   └── WorkspacesPage.tsx
│   ├── styles/
│   │   ├── global.css
│   │   └── tokens.css
│   ├── types/
│   │   ├── api.ts
│   │   └── events.ts
│   ├── App.tsx
│   └── main.tsx
├── frontend_coding_rules.md
├── index.html
├── package.json
├── tsconfig.json
└── vite.config.ts
```

Create subdirectories only when they receive real code. Empty architecture is
not useful.

## Module Responsibilities

### `api/`

Contains calls to FastAPI. Components must not construct URLs or call `fetch`
directly.

Each file owns one backend area:

- `client.ts`: base URL, credentials, JSON parsing, and shared errors
- `workspaces.ts`: workspace requests
- `documents.ts`: document listing and upload
- `runs.ts`: starting runs and submitting answers

### `types/`

Contains the frontend representation of backend contracts. Event types must
match `docs/event_streaming_changelog.md`.

Unknown server data enters as `unknown` and is validated or narrowed before
the UI uses it. Do not hide uncertain payloads behind `any`.

### `hooks/`

Contains reusable stateful browser behavior. Initially, only the SSE lifecycle
belongs here.

`useRunStream.ts` will:

1. Open the run stream.
2. Parse named SSE events.
3. Append events in arrival order.
4. Expose connection state and errors.
5. Close the connection on unmount.

It must not decide how events look.

### `pages/`

Pages coordinate API calls and compose components. A page may know route
parameters and loading state, but detailed rendering belongs in components.

### `components/`

Components are grouped by product concept, not by technical pattern.

- `artifacts/`: previews for markdown, screenshots, tables, files, and answers
- `files/`: upload controls and file lists
- `layout/`: top bar and page shell
- `runs/`: timeline, activity rows, and user questions
- `ui/`: small reusable primitives such as Button and EmptyState

### `styles/`

`tokens.css` defines colors, typography, spacing, radii, and borders.
`global.css` defines resets and application-wide defaults.

Component styles should live beside their component when they are introduced.

## Data Flow

Use one-directional data flow:

```text
page loads data
    -> page passes data and callbacks
        -> component renders
            -> user action calls callback
                -> page/API updates state
```

Do not create a global store for the first version.

Use:

- URL parameters for workspace and run identity
- Page state for fetched records
- Component state for temporary UI interaction
- The SSE hook for live event state

## Event Presentation

The backend may expose internal values such as `agent_type`, `stage`, and
`system`. Keep those values in typed event data, but translate them before
display.

Examples:

| Backend value | User-facing label |
| --- | --- |
| `planner` | Planning |
| `browser` | Searching the web |
| `document_answering` | Reading documents |
| `office` | Preparing files |
| `system` + `validating` | Checking results |

The UI describes work being done. It does not explain the internal agent
architecture.

## Current Capabilities

- Sign up, sign in, and sign out through SuperTokens.
- List and create workspaces.
- List and upload workspace documents.
- Start a live agent run.
- Stream and present run activity.
- Submit free-text user feedback while a run is paused.
- Preview styled markdown, todo plans, structured findings, fetched web pages,
  source links, and screenshots.
- Browse every update inside a persistent agent execution using the horizontal
  event scrubber.
- Reopen saved runs, preview produced files, and download their original bytes.
- Delete a workspace after confirmation.

## Backend Integration

### Currently Connected

The frontend uses these required routes from `start_server.py`:

| Method | Route | Frontend use |
| --- | --- | --- |
| `GET` | `/workspace/list_workspace` | List signed-in user workspace names |
| `POST` | `/workspace/create_workspace` | Create a workspace |
| `GET` | `/v1/workspaces/{workspaceId}/documents` | List uploaded documents |
| `POST` | `/v1/workspaces/{workspaceId}/documents` | Upload a document |
| `POST` | `/chat/user_chat` | Start a run or answer a paused run |
| `GET` | `/chat/{runId}/stream` | Stream live run events |
| `DELETE` | `/workspace/delete_workspace` | Delete a workspace after confirmation |
| `GET` | `/workspace/summaries` | Load workspace counts and recent activity |
| `GET` | `/workspace/{workspaceId}/runs` | Load persisted run history |
| `GET` | `/workspace/{workspaceId}/runs/{runId}` | Reopen a saved run |
| `GET` | `/workspace/{workspaceId}/outputs` | List all produced files |
| `GET` | `/workspace/{workspaceId}/runs/{runId}/outputs` | List one run's files |
| `GET` | `/workspace/{workspaceId}/runs/{runId}/outputs/{relativePath}` | Preview or download a produced file |

The run page displays the submitted query from router state and falls back to
the `data.query` field of the `run_started` event. Inline markdown, text, JSON,
and screenshots are rendered from SSE artifact data. Web-search and browser
updates render `data.url`, `data.title`, `data.sources`, and page text supplied
as `data.content`, `data.text`, `data.page_content`, or `data.answer`.

File artifacts only display a download action when the backend provides an
artifact `url`. The frontend does not invent a download route from an artifact
path.

### Persisted API Compatibility

The current backend implements the contracts below. A `404` still marks the
feature as unavailable without breaking live runs, document upload, or
workspace management, which keeps the frontend usable with older deployments.

#### Workspace summaries

`GET /workspace/summaries`

```json
[
  {
    "workspace_id": "Vendor risk",
    "document_count": 3,
    "run_count": 8,
    "running_run_count": 1,
    "created_at": "2026-06-13T12:00:00Z",
    "last_activity_at": "2026-06-13T12:30:00Z"
  }
]
```

#### Workspace run list

`GET /workspace/{workspaceId}/runs`

```json
[
  {
    "query_id": "550e8400-e29b-41d4-a716-446655440000",
    "workspace_id": "Vendor risk",
    "user_query": "Prepare a supplier risk brief",
    "status": "running",
    "started_at": "2026-06-13T12:30:00Z",
    "query_counter": 4,
    "todo_md": null
  }
]
```

#### Run detail

`GET /workspace/{workspaceId}/runs/{runId}`

Returns one object with the same fields as the workspace run-list item.

#### Produced output list

`GET /workspace/{workspaceId}/outputs`

```json
[
  {
    "run_id": "550e8400-e29b-41d4-a716-446655440000",
    "task_id": "t2",
    "filename": "supplier-risk.docx",
    "relative_path": "outputs/supplier-risk.docx",
    "bytes": 48231,
    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "modified_at": "2026-06-13T12:35:00Z",
    "preview_url": "/workspace/Vendor%20risk/runs/550e8400-e29b-41d4-a716-446655440000/outputs/supplier-risk.docx?disposition=inline",
    "download_url": "/workspace/Vendor%20risk/runs/550e8400-e29b-41d4-a716-446655440000/outputs/supplier-risk.docx?disposition=attachment"
  }
]
```

#### Produced output download

`GET /workspace/{workspaceId}/runs/{runId}/outputs/{relativePath}`

Returns the file body with an appropriate content type and download filename.

Every future endpoint must use the existing SuperTokens session and verify that
the signed-in user owns the requested workspace and run. Unknown or
unauthorized resources should return `404` without leaking their existence.

## Definition Of Done

A frontend change is complete when:

- TypeScript passes without errors.
- The production build succeeds.
- Loading, empty, error, and success states are handled.
- Keyboard focus remains visible.
- Network and stream resources are cleaned up.
- New behavior has a focused test when logic is not trivial.
- The code follows `frontend_coding_rules.md`.
