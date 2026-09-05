import { useEffect, useMemo, useState } from "react";

const sourceKey = (row) => row.sourceAddress;

// Live values keep updating, but a background refresh must not move a row while
// an operator is pointing at it, tabbing through it, or preparing a deployment.
// An explicit filter/sort change starts a new ordering immediately.
export function useStableRowOrder(rows, viewKey, locked, { key = sourceKey, pinPage = false } = {}) {
  const [order, setOrder] = useState({ viewKey, rows: [] });

  useEffect(() => {
    if (!locked || order.viewKey !== viewKey) {
      setOrder({ viewKey, rows });
    }
  }, [rows, viewKey, locked, order.viewKey]);

  return useMemo(() => {
    if (!locked || order.viewKey !== viewKey || !order.rows.length) return rows;
    const remaining = new Map(rows.map((row) => [key(row), row]));
    const stable = [];
    for (const previous of order.rows) {
      const id = key(previous);
      if (remaining.has(id)) stable.push(remaining.get(id));
      // Offset-paginated logs can evict the last visible event when new events arrive.
      // Keep that immutable event in place until the interaction ends.
      else if (pinPage) stable.push(previous);
      remaining.delete(id);
    }
    if (pinPage) return stable;
    return [...stable, ...remaining.values()];
  }, [rows, viewKey, locked, order, key, pinPage]);
}
