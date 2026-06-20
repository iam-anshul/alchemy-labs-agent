# Changelog — Fix `/workspaces` 404 on pasted URL / refresh (dev proxy)

Pasting a workspace URL (e.g. `http://<host>:5173/workspaces`) or refreshing on it
returned `{"detail":"Not Found"}` instead of the app. In-app navigation to the same
route worked fine. This fixes the dev-server case.

---

## What was wrong

The Vite dev server proxies certain path prefixes to the FastAPI backend
(`vite.config.ts` → `server.proxy`). One rule was:

```js
"/workspace": backendTarget,
```

Vite's proxy matches by **prefix**, so `"/workspace"` matched **any** path starting
with `/workspace` — including the SPA's own routes under the **plural** `/workspaces`
(`/workspaces`, `/workspaces/:id`, `/workspaces/:id/runs/:runId`).

The mismatch is structural:

- **Backend API** is mounted at the **singular** prefix `/workspace/...`
  (`start_server.py` → `prefix="/workspace"`; routes like `/workspace/list_workspace`,
  `/workspace/{id}/runs`).
- **Frontend routes** all live under the **plural** `/workspaces` (`App.tsx`).

So when the browser requested `/workspaces` directly (a pasted URL or a hard
refresh), the dev server proxied it to the backend, which has no such route and
replied `{"detail":"Not Found"}` (a FastAPI response — the JSON shape was the tell).

In-app navigation worked because React Router handles `/workspaces` client-side and
never issues that HTTP request; only a fresh request to the server (paste/refresh)
hit the bad proxy rule.

---

## The fix

Anchor the proxy rule so it matches the singular API path only, never the plural SPA
route. In `frontend/vite.config.ts`:

```js
// before
"/workspace": backendTarget,
// after
"^/workspace(/|$)": backendTarget,
```

`^/workspace(/|$)` matches `/workspace` only when followed by `/` (the API, e.g.
`/workspace/list_workspace`) or end-of-string (a bare `/workspace`). `/workspaces`
starts with `/workspace` followed by `s`, so it no longer matches — the dev server
serves the SPA's `index.html` for it and React Router takes over.

Verified against all real paths:

| Path | Proxied to backend? |
|---|---|
| `/workspace/create_workspace` | yes ✅ |
| `/workspace/list_workspace` | yes ✅ |
| `/workspace/test-table/runs` | yes ✅ |
| `/workspace` | yes ✅ |
| `/workspaces` | **no** ✅ (served as SPA) |
| `/workspaces/test-table` | **no** ✅ |
| `/workspaces/test-table/runs/abc` | **no** ✅ |

---

## Files changed

### `frontend/vite.config.ts`

- Changed the `server.proxy` key `"/workspace"` → `"^/workspace(/|$)"`, with a
  comment explaining the singular-API-vs-plural-SPA-route collision. No other proxy
  rules changed.

---

## To take effect

Vite reads `vite.config.ts` at **startup**, so the **dev server must be restarted**
(a hot reload does not pick up proxy-config changes). In the Docker setup, restart
the `frontend` service / container.

---

## ⚠️ Production note — this fixes DEV only

This change fixes the **Vite dev server** path. The current deployment happens to run
the dev server (`Dockerfile.frontend` → `npm run dev --host 0.0.0.0`, exposed on
5173), so this fix covers it.

If/when the frontend is served as a real **production static build** (a built SPA
behind nginx / a static host / CDN), this `vite.config.ts` change does **nothing** in
that path — the dev server isn't involved. There you need a server-side **SPA
catch-all (history-API fallback)** so any unknown route returns `index.html`.

Important: this is a **server rule, not file content** — adding/editing `index.html`
does not fix it. `index.html` already exists (Vite's entry point); the routes
`/workspaces`, `/workspaces/:id` exist only in JavaScript after the app boots, so the
server must be told to return `index.html` for paths that aren't real files.

What's needed for production, depending on how it's served:

- **nginx:** add a fallback in the server block:
  ```nginx
  location / {
      try_files $uri $uri/ /index.html;
  }
  ```
  and proxy the backend paths (`/workspace/`, `/chat`, `/v1`, `/health`,
  `/auth` excluding `/auth-ui`) to the backend — i.e. the same set as the Vite proxy.
- **`vite preview`:** it already serves `index.html` for unknown routes (SPA
  fallback built in), but it does **not** proxy the API — the backend paths must be
  fronted by a reverse proxy.
- **Static host / CDN (Netlify, S3+CloudFront, etc.):** configure a rewrite of all
  paths to `/index.html` (e.g. a `_redirects` / rewrite rule).

A dedicated nginx multi-stage `Dockerfile.frontend` + `nginx.conf` would implement
this cleanly; it was intentionally **not** added in this change (dev-only scope), and
is the recommended follow-up when moving off the dev server in production.

---

## Verification

- Regex behavior verified against the path table above (singular API still proxied;
  plural SPA routes no longer proxied).
- No other files changed; `npm run dev` behavior is otherwise unchanged.