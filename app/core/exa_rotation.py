"""Round-robin Exa client with automatic key rotation and failover.

The project keeps several Exa API keys in `.env` (to spread quota across keys).
This module discovers all of them, hands out a fresh `Exa` client per call in
round-robin order, and — when a call fails with a rate-limit/auth error —
transparently retries it against the remaining keys before giving up.

Drop-in usage: replace `exa_client.answer(...)` with
`exa_rotation.exa_client.answer(...)`. The public surface mimics the methods the
codebase actually uses (`answer`, `get_contents`); any other attribute access is
proxied to a live `Exa` client using the next key in rotation.
"""

from __future__ import annotations

import os
import re
import threading
import time
from itertools import count

from dotenv import load_dotenv
from exa_py import Exa

# Ensure .env is loaded before we scan os.environ for keys — this module's
# singleton is built at import time, which may run before the importer calls
# load_dotenv() itself. load_dotenv is idempotent and won't override real env.
load_dotenv()


# Errors that indicate the *key* is the problem (exhausted quota, rate limited,
# revoked, unauthorized) and that another key might succeed. The Exa SDK surfaces
# HTTP failures as exceptions carrying the status code in their string form, so
# we match on that rather than on a typed exception hierarchy that doesn't exist.
_ROTATABLE_STATUS = ("401", "402", "403", "429")
_ROTATABLE_HINTS = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "quota",
    "unauthorized",
    "forbidden",
    "payment required",
    "invalid api key",
)

# Backoff defaults for the case where *every* key fails with a rotatable error.
# A single walk over the key pool happens with no delay (each key is fresh); if
# that whole pass fails, we wait and walk the pool again. The delay grows
# exponentially per pass and is capped, so a brief throttle clears without the
# call hanging indefinitely. Overridable via env so it can be tuned without code
# changes.
_MAX_PASSES = int(os.getenv("EXA_ROTATION_MAX_PASSES", "3"))
_BACKOFF_BASE = float(os.getenv("EXA_ROTATION_BACKOFF_BASE", "1.0"))  # seconds
_BACKOFF_MAX = float(os.getenv("EXA_ROTATION_BACKOFF_MAX", "30.0"))  # seconds


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort extraction of a server-provided Retry-After hint, in seconds.

    Rate-limit (429) responses commonly carry a `Retry-After` header. The Exa SDK
    raises generic exceptions, but if the underlying `requests`/`httpx` response is
    reachable on the exception we honor its header; otherwise we fall back to
    scraping a "retry after N seconds" phrase from the message. Returns None when
    no hint is available.
    """
    # 1) A response object attached to the exception (requests/httpx style).
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw:
            try:
                return max(0.0, float(raw))  # delta-seconds form
            except (TypeError, ValueError):
                pass  # HTTP-date form is not worth parsing here

    # 2) Scrape the message text as a last resort.
    m = re.search(r"retry[\s-]?after[:\s]+(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if m:
        try:
            return max(0.0, float(m.group(1)))
        except ValueError:
            pass
    return None


def _discover_keys() -> list[str]:
    """Collect every EXA_API_KEY* value from the environment, in stable order.

    Handles both naming styles present in `.env`:
    `EXA_API_KEY_1` (with separator) and `EXA_API_KEY4` (without). A bare
    `EXA_API_KEY` (no suffix) is included too. Keys are de-duplicated and ordered
    by their numeric suffix so rotation is deterministic across restarts.
    """
    pattern = re.compile(r"^EXA_API_KEY[_]?(\d*)$")
    found: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        m = pattern.match(name)
        if not m or not value or not value.strip():
            continue
        # No suffix sorts first (-1); numbered keys follow in numeric order.
        order = int(m.group(1)) if m.group(1) else -1
        found.append((order, value.strip()))

    # De-duplicate while preserving the sorted order.
    seen: set[str] = set()
    keys: list[str] = []
    for _, value in sorted(found, key=lambda t: t[0]):
        if value not in seen:
            seen.add(value)
            keys.append(value)
    return keys


def _is_rotatable(exc: BaseException) -> bool:
    """True if `exc` looks like a key-level failure worth retrying on another key."""
    text = str(exc).lower()
    if any(code in text for code in _ROTATABLE_STATUS):
        return True
    return any(hint in text for hint in _ROTATABLE_HINTS)


class RotatingExaClient:
    """A thread-safe round-robin pool of `Exa` clients.

    Each call to a proxied method (`answer`, `get_contents`, or anything else on
    the underlying SDK) starts from the next key in rotation and, on a
    key-level error, walks the remaining keys before propagating the failure.
    """

    def __init__(self, keys: list[str] | None = None):
        self._keys = keys if keys is not None else _discover_keys()
        if not self._keys:
            raise RuntimeError(
                "No Exa API keys found. Set EXA_API_KEY or EXA_API_KEY_1, "
                "EXA_API_KEY_2, ... in your environment/.env file."
            )
        # One reusable client per key (the Exa SDK is cheap but holds a session).
        self._clients = [Exa(api_key=k) for k in self._keys]
        self._counter = count()
        self._lock = threading.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _next_index(self) -> int:
        """Atomically advance the round-robin cursor and return a start index."""
        with self._lock:
            return next(self._counter) % len(self._clients)

    def _call(self, method_name: str, *args, **kwargs):
        """Invoke `method_name` on clients in rotation, failing over on key errors.

        First does a no-delay walk over the whole key pool (each key is fresh).
        If *every* key fails with a rotatable error, it waits — honoring a
        server `Retry-After` hint when present, otherwise an exponential backoff
        per pass — and walks the pool again, up to `_MAX_PASSES` times. A
        non-rotatable error (bad arguments, etc.) propagates immediately without
        burning keys or sleeping.
        """
        n = len(self._clients)
        last_exc: BaseException | None = None
        retry_after: float | None = None

        for attempt in range(_MAX_PASSES):
            if attempt > 0:
                # Whole previous pass failed on rotatable errors — back off before
                # trying the pool again. Prefer the server's hint if it gave one.
                backoff = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
                delay = retry_after if retry_after is not None else backoff
                time.sleep(delay)
                retry_after = None

            start = self._next_index()
            for offset in range(n):
                client = self._clients[(start + offset) % n]
                try:
                    return getattr(client, method_name)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - we re-raise below
                    last_exc = exc
                    if not _is_rotatable(exc):
                        # Not a key problem — retrying won't help.
                        raise
                    # Remember the largest Retry-After seen this pass so we wait
                    # at least as long as any key asked us to.
                    hint = _retry_after_seconds(exc)
                    if hint is not None:
                        retry_after = hint if retry_after is None else max(retry_after, hint)
                    # Keep walking the pool; the next key may still be healthy.

        # Exhausted every key across every pass on rotatable errors.
        assert last_exc is not None
        raise last_exc

    # --- Explicit pass-throughs for the methods the codebase uses. Keeping them
    # named (rather than only via __getattr__) makes the supported surface
    # obvious and keeps IDE/type tooling happy. ---

    def answer(self, *args, **kwargs):
        return self._call("answer", *args, **kwargs)

    def get_contents(self, *args, **kwargs):
        return self._call("get_contents", *args, **kwargs)

    def __getattr__(self, name: str):
        """Proxy any other Exa method through the rotation/failover machinery."""
        # __getattr__ only fires for attributes not found normally, so this is
        # safe from recursion against _clients/_keys set in __init__.
        def _proxy(*args, **kwargs):
            return self._call(name, *args, **kwargs)

        return _proxy


# Module-level singleton — import and use exactly like a plain `Exa` client.
exa_client = RotatingExaClient()
