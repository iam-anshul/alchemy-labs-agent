import { useCallback, useEffect, useRef, useState } from "react";

export function useAsyncData<T>(
  load: (signal: AbortSignal) => Promise<T>,
  dependencies: readonly unknown[],
  // Optional silent background refresh. When > 0, `load` is re-run on this
  // interval WITHOUT toggling isLoading (so no spinner flicker) and without
  // clearing existing data on transient errors. Used to keep lists like the
  // workspace "Recent runs" live (e.g. surfacing an in-flight run's status)
  // without the user reloading the page. Pass 0/undefined to disable.
  pollIntervalMs?: number,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);

  // Keep the latest `load` closure reachable from the polling timer without
  // making the timer effect depend on it (inline closures change every render).
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    const controller = new AbortController();
    setIsLoading(true);
    setError(null);

    load(controller.signal)
      .then(setData)
      .catch((loadError: unknown) => {
        if (controller.signal.aborted) return;
        setError(loadError instanceof Error ? loadError.message : "Request failed");
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false);
      });

    return () => controller.abort();
    // Callers provide stable primitives as dependencies. `load` is intentionally
    // excluded because inline closures would otherwise restart every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, reloadToken]);

  useEffect(() => {
    if (!pollIntervalMs || pollIntervalMs <= 0) return;
    let cancelled = false;
    let controller: AbortController | null = null;

    const tick = () => {
      controller = new AbortController();
      // Silent refresh: do NOT setIsLoading (no spinner) and do NOT clear data
      // or surface transient errors — a failed poll just leaves the last good
      // data in place until the next tick succeeds.
      loadRef.current(controller.signal)
        .then((next) => {
          if (!cancelled) setData(next);
        })
        .catch(() => {
          /* ignore transient poll errors; keep showing last good data */
        });
    };

    const id = window.setInterval(tick, pollIntervalMs);
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, pollIntervalMs]);

  return { data, error, isLoading, reload };
}
