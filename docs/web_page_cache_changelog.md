# Web Page Cache Changelog

Date: 2026-06-14

## Summary

Web pages fetched by the `web_search` agent are now stored in memory for the
whole planner run instead of being returned directly to the model. This keeps
full page contents out of the LLM context while allowing later web tasks and
dispatch retries to reuse pages already fetched during the run.

## Changes

### Run-scoped page cache

[`web_agent.py`](../web_agent.py) defines `CachedPage` and stores a shared
`page_cache` on `WebDeps`.

[`api/routes/chat.py`](../api/routes/chat.py) creates one cache per
`create_chat` run and passes it through every web-agent dispatch. The cache is
discarded when the planner run ends and is not shared across user runs.

### `fetch_url`

`fetch_url(url)` now:

- returns a stable page ID derived from the URL;
- stores the complete fetched page in the run cache;
- returns only the page ID, URL, character count, and cache status;
- reuses an existing cached page when the same URL is requested again.

The complete page text no longer enters the model context.

### `search_page`

Added `search_page(page_id, pattern, max_matches=20)`.

- Scans the entire cached page with a case-insensitive regular expression.
- Supports multiple terms with regex alternation (`term1|term2`).
- Returns matching excerpts with line numbers.
- Caps results at 50 matches and bounds each excerpt to control context size.
- Reports the total match count and whether returned results were truncated.

### Prompt context

Each web task receives a compact list of pages visited during the planner run:
page ID, URL, and character count. It can search those pages without fetching
them again.

[`system_prompts.py`](../system_prompts.py) now documents the Exa-backed
`web_search`, cached `fetch_url`, and full-page `search_page` workflow.

## Verification

- `python -m py_compile web_agent.py api/routes/chat.py system_prompts.py`
- `git diff --check`

Both checks pass. Full application import remains blocked by the existing local
`browser_use` version mismatch where `browser_agent.py` cannot import `Tools`.
