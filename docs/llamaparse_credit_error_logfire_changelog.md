# Changelog — Surface LlamaParse credit exhaustion in Logfire

Makes an exhausted LlamaParse (LlamaCloud) plan visible at a glance in Logfire,
instead of letting it hide inside an undifferentiated parse error.

---

## Background

Document ingestion parses every uploaded file through LlamaParse
(`parse_document_async` in `parsing.py`). When the LlamaParse account runs out of
credits, the SDK does **not** raise a typed billing error — it raises a generic
`Exception` whose message embeds the API's JSON body:

```
Exception: Failed to parse the file:
{"detail":"You've exceeded the maximum number of credits for your plan."}
```

The ingest worker (`api/ingest.py`) catches this, flips the doc to
`status='failed'`, and logs it — but as an ordinary `parse`-stage failure. In
Logfire it was indistinguishable from a per-document parse bug, so an
account-wide credit outage (which blocks **every** upload) looked like a string
of unrelated parse failures. Uploads, meanwhile, succeed and sit at `queued` /
`failed` with no obvious signal as to why.

---

## Change

Wrap the `parser.aparse(path)` call in `parse_document_async` so the credit
error is detected and logged to Logfire as a **distinct, searchable** error
before being re-raised.

- On the credit-exhaustion message (matches `"credits for your plan"` /
  `"exceeded the maximum number of credits"`), emit a dedicated `logfire.error`:

  > LlamaParse out of credits: parsing is blocked until the plan is topped up or
  > LLAMA_PARSE_KEY points at an account with credits

- On any other parse failure, emit a generic `logfire.error`
  (`LlamaParse failed to parse document`) so those stay visible too rather than
  being swallowed.
- Both branches attach structured attributes — `path`, `llama_parse_error`
  (the raw message), and `error_class` — so Logfire can be filtered on the
  attribute as well as the message text.
- The exception is **re-raised unchanged** in both branches, so the ingest
  worker still flips the doc to `status='failed'` exactly as before. No
  behavior change to the ingest pipeline.

---

## Files changed

### `parsing.py`

- Added `import logfire`.
- Wrapped `result = await parser.aparse(path)` in `parse_document_async` in a
  `try/except Exception`. The handler classifies the error (credit-exhaustion vs
  other), logs the appropriate `logfire.error` with structured attributes, and
  re-raises.

No other files touched.

---

## Verification

Ran `parse_document_async` against the stuck PDF
(`data/uploads/second-fix/doc_c85d07e6edf5.pdf`) with Logfire configured. The
account was still out of credits, so the credit path fired:

```
Logfire project URL: https://logfire-eu.pydantic.dev/iam-anshul/starter-project
LlamaParse out of credits: parsing is blocked until the plan is topped up or
LLAMA_PARSE_KEY points at an account with credits
re-raised as expected: Exception
```

- The dedicated error reached the EU Logfire project.
- The exception still propagated (`Exception`), preserving the `status='failed'`
  ingest flow.

---

## Notes / follow-ups

- This only adds **visibility**. Nothing parses until LlamaParse credits are
  restored (top up the plan, or point `LLAMA_PARSE_KEY` at an account with
  credits), after which the affected docs must be re-uploaded — existing
  `queued`/`failed` rows are not auto-retried.
- Not yet surfaced to the **UI**: an upload blocked by credit exhaustion still
  shows as a generic failure / spinner to the user. Surfacing "failed: out of
  credits" in the frontend is a possible follow-up.
