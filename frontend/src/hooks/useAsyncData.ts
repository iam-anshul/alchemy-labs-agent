import { useCallback, useEffect, useState } from "react";

export function useAsyncData<T>(
  load: (signal: AbortSignal) => Promise<T>,
  dependencies: readonly unknown[],
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);

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

  return { data, error, isLoading, reload };
}
