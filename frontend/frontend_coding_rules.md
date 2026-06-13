# Frontend Coding Rules

These rules apply to every file under `frontend/`.

Their purpose is to keep the codebase understandable to a small,
backend-focused team. Prefer obvious code over clever code.

## 1. Optimize For Reading

- Write code for the next person reading it, not for the fewest lines.
- Use complete, descriptive names such as `workspaceId` and `runEvents`.
- Keep the main path through a function easy to follow from top to bottom.
- Prefer a small amount of duplication over a premature abstraction.
- Do not introduce patterns that require framework-specific knowledge when
  plain TypeScript or React is sufficient.

## 2. Keep Modules Focused

- One module should have one clear responsibility.
- A page coordinates a route; it does not contain the whole page implementation.
- An API module calls the backend; it does not render or manage React state.
- A hook owns reusable stateful behavior; it does not render UI.
- A component renders one recognizable product concept.
- Split a file when it contains multiple independently understandable concepts,
  not merely because it passed an arbitrary line count.

As a warning sign, reconsider files above roughly 250 lines and functions above
roughly 50 lines. These are prompts to inspect the design, not hard limits.

## 3. Keep Dependencies Pointing One Way

Use this dependency direction:

```text
pages -> components -> hooks/api/types
hooks -> api/types
api -> types
types -> nothing
```

- `api/` must never import React.
- Reusable components must not import page modules.
- Types must not depend on components, hooks, or API functions.
- Avoid circular imports.

## 4. Use TypeScript Honestly

- Enable strict TypeScript settings.
- Do not use `any`.
- Use `unknown` for untrusted data and narrow it before use.
- Model finite backend values with string unions.
- Keep API response and event types in `src/types/`.
- Do not silence errors with broad type assertions.
- Prefer a small parsing function to spreading optional chaining throughout UI
  code.
- Exhaustively handle artifact kinds and event states. Unknown kinds must show
  a safe fallback instead of crashing.

## 5. Keep React Simple

- Use function components and hooks.
- Keep state as close as possible to where it is used.
- Derive values during rendering when they can be calculated from existing
  state.
- Do not mirror props into state.
- Do not use an effect for ordinary calculations or event handling.
- Effects are for synchronizing with external systems such as SSE, timers, and
  browser APIs.
- Always clean up streams, timers, observers, and event listeners.
- Do not add a global state library until state genuinely spans unrelated
  routes and cannot remain in the URL or page state.
- Do not add memoization by default. Use it only after measuring a real problem.

## 6. Separate Server Data From UI State

Server data includes workspaces, documents, runs, events, and artifacts.

UI state includes an open modal, selected tab, focused artifact, input text,
and drag state.

- Do not mix temporary UI fields into API response objects.
- Do not mutate objects received from the API.
- Prefer explicit variables such as `selectedArtifactId` over adding
  `selected: true` to an artifact.
- The URL is the source of truth for the active workspace and run.

## 7. Centralize Backend Access

- Components and pages must call functions from `src/api/`.
- Do not call `fetch` directly from components.
- Build URLs in the API layer only.
- Use one shared request helper for credentials, headers, JSON parsing, and
  consistent errors.
- Use native `FormData` for uploads.
- Use native `EventSource` for SSE when authentication allows it.
- If SSE authentication requires custom headers, use one documented streaming
  helper rather than ad hoc implementations.
- Abort cancellable requests when their owning page unmounts.

## 8. Treat Events As A Contract

The source contract is `docs/event_streaming_changelog.md`.

- Preserve backend event fields even when they are not displayed.
- Normalize incoming events once, near the stream boundary.
- Store events in arrival order.
- Deduplicate only by a stable event identifier supplied by the backend.
- Never infer completion solely from a disconnected stream; use `run_ended` or
  a status endpoint.
- Keep internal-to-display label mapping in one presentation module.
- Never display raw `agent_type`, internal task IDs, stack traces, or internal
  stages directly to users.
- Support `artifacts` as an array. Do not assume one artifact per event.

## 9. Handle Every Async State

Every network-backed area must intentionally render:

- Loading
- Empty
- Success
- Error

Streaming views must also handle:

- Connecting
- Live
- Waiting for user input
- Reconnecting or disconnected
- Completed
- Failed

Do not leave rejected promises unhandled. User-facing errors should explain
what failed and offer a useful retry when possible.

## 10. Keep Components Predictable

- Props should describe data and actions clearly.
- Use callbacks named for outcomes: `onWorkspaceCreated`, not `handleThing`.
- A reusable component should not navigate unless navigation is its purpose.
- Prefer composition over large configuration objects.
- Avoid generic components with many boolean props.
- If two visual cases behave differently, separate components are often easier
  to understand than one component with many branches.

## 11. Use Plain CSS Deliberately

- Use CSS variables from `tokens.css`; do not scatter raw colors and spacing
  throughout components.
- Use semantic class names such as `.run-event` and `.artifact-preview`.
- Keep selectors shallow.
- Do not use `!important` except when overriding unavoidable third-party CSS,
  with a comment explaining why.
- Do not use inline styles for ordinary styling. Inline styles are acceptable
  for truly dynamic values such as a calculated position or workspace color.
- Build responsive behavior alongside the component, not as a later rewrite.
- Preserve visible keyboard focus and sufficient color contrast.

## 12. Prefer Native HTML

- Use `button` for actions and links for navigation.
- Use real headings in logical order.
- Associate labels with form controls.
- Use lists, tables, dialogs, and progress elements when their semantics fit.
- All interactive controls must work with a keyboard.
- Icon-only buttons require an accessible label.
- Do not build clickable `div` elements.

## 13. Comments Explain Why

- Do not narrate obvious syntax.
- Comment decisions, constraints, browser behavior, and non-obvious mappings.
- Keep comments current when behavior changes.
- Prefer a short named helper over a long explanatory comment.
- Public API helpers and complicated hooks may use short doc comments.

## 14. Errors Stay Useful

- API errors should retain HTTP status and a readable message.
- Log technical detail for developers without exposing sensitive internals to
  users.
- Do not catch an error only to ignore it.
- Error boundaries are for unexpected rendering failures, not normal API
  errors.
- Never include authentication tokens, document contents, or private event
  payloads in browser logs.

## 15. Test Behavior With Risk

Prioritize tests for:

- Event parsing and normalization
- SSE connection cleanup and reconnection behavior
- Activity-label translation
- Artifact-kind selection
- MCQ and free-text answer submission
- File upload behavior
- Route-level loading and error states

Avoid tests that only assert implementation details or duplicate TypeScript.
Use the smallest test that proves user-visible behavior.

## 16. Keep Changes Small

- Implement one vertical product behavior at a time.
- Do not combine feature work with unrelated refactoring.
- Reuse existing patterns before creating a new abstraction.
- Delete dead code when replacing it.
- Do not leave commented-out implementations.
- New dependencies require a concrete reason recorded in the pull request or
  commit description.

## 17. Naming Conventions

- Components and pages: `PascalCase.tsx`
- Hooks: `useName.ts`
- Utilities and API modules: `camelCase.ts`
- CSS files: match the component or use lowercase shared names
- Component names: nouns such as `EventTimeline`
- Event callbacks: `onAction`
- Internal event handlers: `handleAction`
- Boolean values: `is`, `has`, `can`, or `should`
- Avoid abbreviations except established terms such as `id`, `url`, `api`, and
  `sse`

## 18. Before Marking Work Complete

Run:

```bash
npm run typecheck
npm run test
npm run build
```

Then confirm:

- The browser console has no new errors.
- Loading, empty, error, and success states are visible and usable.
- The page works at a narrow viewport.
- Keyboard navigation and focus are usable.
- Requests, streams, and timers are cleaned up.
- No internal agent terminology leaks into the user interface.

If a command cannot be run, state that clearly in the work summary.
