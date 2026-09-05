import { useEffect, useMemo, useState } from "react";

// Live values keep updating, but a background refresh must not move a row while
// an operator is pointing at it, tabbing through it, or preparing a deployment.
// An explicit filter/sort change starts a new ordering immediately.
export function useStableRowOrder(rows, viewKey, locked) {
  const [order, setOrder] = useState({ viewKey, addresses: [] });

  useEffect(() => {
    if (!locked || order.viewKey !== viewKey) {
      setOrder({ viewKey, addresses: rows.map((row) => row.sourceAddress) });
    }
  }, [rows, viewKey, locked, order.viewKey]);

  return useMemo(() => {
    if (!locked || order.viewKey !== viewKey || !order.addresses.length) return rows;
    const remaining = new Map(rows.map((row) => [row.sourceAddress, row]));
    const stable = [];
    for (const address of order.addresses) {
      if (remaining.has(address)) stable.push(remaining.get(address));
      remaining.delete(address);
    }
    return [...stable, ...remaining.values()];
  }, [rows, viewKey, locked, order]);
}
