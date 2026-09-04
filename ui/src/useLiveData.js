import { useCallback, useEffect, useRef, useState } from "react";

// One active request per view. Keep the last good response, without a page cache.
export function useLiveData(url, { active = true, viewKey = "", loadInitially = false, errorMessage = "Unable to refresh data" } = {}) {
  const [result, setResult] = useState({ url, data: null, updatedAt: null, error: "" });
  const [refreshing, setRefreshing] = useState(false);
  const refreshRef = useRef(null);
  const loadedRef = useRef(false);

  useEffect(() => {
    let disposed = false;
    let controller = null;
    let refreshAgain = false;

    async function refresh(force = false) {
      if (disposed || document.visibilityState === "hidden") return;
      if (controller) {
        if (force) refreshAgain = true;
        return;
      }
      refreshAgain = false;
      controller = new AbortController();
      setRefreshing(true);
      try {
        const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error(errorMessage);
        const payload = await response.json();
        if (disposed) return;
        loadedRef.current = true;
        setResult({ url, data: payload?.data || {}, updatedAt: Date.now(), error: "" });
      } catch (error) {
        if (!disposed && error.name !== "AbortError") {
          setResult((previous) => ({
            url,
            data: previous.url === url ? previous.data : null,
            updatedAt: previous.url === url ? previous.updatedAt : null,
            error: error.message || errorMessage,
          }));
        }
      } finally {
        controller = null;
        if (!disposed) {
          setRefreshing(false);
          if (refreshAgain) refresh();
        }
      }
    }

    refreshRef.current = refresh;
    setRefreshing(false);
    if (active || (loadInitially && !loadedRef.current)) refresh();
    const refreshAutomatically = () => refresh();
    const interval = active ? window.setInterval(refreshAutomatically, 30000) : null;
    if (active) {
      window.addEventListener("focus", refreshAutomatically);
      document.addEventListener("visibilitychange", refreshAutomatically);
    }
    return () => {
      disposed = true;
      controller?.abort();
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshAutomatically);
      document.removeEventListener("visibilitychange", refreshAutomatically);
      if (refreshRef.current === refresh) refreshRef.current = null;
    };
  }, [url, active, viewKey, loadInitially, errorMessage]);

  const refresh = useCallback(() => refreshRef.current?.(true), []);
  const current = result.url === url ? result : { data: null, updatedAt: null, error: "" };
  return { ...current, refreshing, loading: current.data === null && !current.error, refresh };
}
