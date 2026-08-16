"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, EmptyState, ErrorState, Loading } from "../../design";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";

type PreviewPayload = {
  kind: "text" | "image" | "binary";
  path: string;
  content_type?: string;
  text?: string;
  truncated?: boolean;
  size_bytes?: number;
  note?: string;
};

/**
 * TN.13 — render a safe artifact preview for approval rows.
 * Paths must be under var/artifacts|projects|intake-workspace (server-enforced).
 */
export function ArtifactPreview({ path }: { path: string | null | undefined }) {
  const [data, setData] = useState<PreviewPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!path) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const url = `${API_BASE}/api/artifacts/preview?path=${encodeURIComponent(path)}`;
      const res = await fetchWithTimeout(url, { credentials: "same-origin" });
      if (!res.ok) {
        throw new Error(`preview failed (${res.status})`);
      }
      setData((await res.json()) as PreviewPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "preview failed");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!path) return <EmptyState message="No artifact path on this approval." />;
  if (loading) return <Loading label="Loading preview…" />;
  if (error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return null;

  return (
    <Card>
      <div style={{ fontSize: "var(--font-size-sm)", color: "var(--text-muted)", marginBottom: "var(--space-2)" }}>
        {data.path}
        {data.size_bytes != null ? ` · ${data.size_bytes} bytes` : ""}
        {data.truncated ? " · truncated" : ""}
      </div>
      {data.kind === "text" && data.text != null ? (
        <pre
          style={{
            margin: 0,
            maxHeight: 320,
            overflow: "auto",
            whiteSpace: "pre-wrap",
            fontSize: "var(--font-size-sm)",
          }}
        >
          {data.text}
        </pre>
      ) : null}
      {data.kind === "image" ? (
        <p style={{ margin: 0 }}>{data.note ?? "Image artifact (use download for bytes)."}</p>
      ) : null}
      {data.kind === "binary" ? (
        <p style={{ margin: 0 }}>Binary ({data.content_type ?? "unknown"}) — open via files route.</p>
      ) : null}
    </Card>
  );
}
