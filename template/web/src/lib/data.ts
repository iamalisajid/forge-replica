// Read an operation's data and refetch whenever the screen's data version changes (after every write).
import { useEffect, useState } from "react";
import { ApiError, callOperation, fillPath } from "./api";
import type { OperationWire } from "./types";

export interface DataState {
  data: unknown;
  error: Error | null;
  /** True until the first response for the current request arrives. */
  loading: boolean;
}

interface Stored {
  key: string | null;
  data: unknown;
  error: Error | null;
}

/**
 * Fetch ``op`` with ``params`` (null = not ready yet). While refetching after a write the previous data
 * stays on screen, so the page never flashes; the newest response always wins.
 */
export function useOperationData(op: OperationWire | null, params: Record<string, string> | null, version: number): DataState {
  const key = op && params ? `${op.method} ${fillPath(op.path, params)}` : null;
  const [stored, setStored] = useState<Stored>({ key: null, data: undefined, error: null });

  useEffect(() => {
    if (!op || !params || !key) return;
    const controller = new AbortController();
    callOperation(op, params, undefined, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setStored({ key, data, error: null });
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        const error = err instanceof Error ? err : new ApiError(String(err), 0, null);
        setStored((prev) => ({ key, data: prev.key === key ? prev.data : undefined, error }));
      },
    );
    return () => controller.abort();
    // ``key`` captures op and params; ``version`` forces a refetch after writes.
  }, [key, version]);

  const current = stored.key === key && key !== null;
  return {
    data: current ? stored.data : undefined,
    error: current ? stored.error : null,
    loading: key !== null && !current,
  };
}
