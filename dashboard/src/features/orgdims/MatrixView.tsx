"use client";

import { useEffect, useState } from "react";
import { Badge, ErrorState, Loading } from "@/design";
import { orgdimsApi, type MatrixView as MatrixData } from "./api";

export function OrgMatrixView() {
  const [data, setData] = useState<MatrixData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    setError(null);
    void orgdimsApi
      .matrix()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Failed to load matrix"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  if (loading && !data) return <Loading label="Loading matrix…" />;
  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!data) return null;

  return (
    <div style={{ overflowX: "auto" }}>
      <p style={{ marginBottom: "var(--space-3)", color: "var(--text-muted)" }}>
        Rows = company/product · columns = workstream · {data.card_total} active cards
      </p>
      <table className="ds-table" style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left", padding: 8 }}>Company / product</th>
            {data.columns.map((col) => (
              <th key={col} style={{ textAlign: "center", padding: 8, whiteSpace: "nowrap" }}>
                {col}
              </th>
            ))}
            <th style={{ textAlign: "center", padding: 8 }}>Total</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.row_key}>
              <td style={{ padding: 8, fontWeight: 600 }}>{row.row_key}</td>
              {data.columns.map((col) => (
                <td key={col} style={{ textAlign: "center", padding: 8 }}>
                  {row.counts[col] ? <Badge tone="neutral">{row.counts[col]}</Badge> : "·"}
                </td>
              ))}
              <td style={{ textAlign: "center", padding: 8 }}>
                <Badge tone="ok">{row.total}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.uncategorized.length > 0 ? (
        <p style={{ marginTop: "var(--space-3)" }}>
          <Badge tone="warn">{data.uncategorized.length} uncategorized</Badge> — run bulk reclassify
        </p>
      ) : null}
    </div>
  );
}
