"use client";

import { useEffect, useState, useCallback } from "react";
import { Card, Button, Loading, ErrorState, EmptyState, Badge } from "@/design";
import { fetchMemories, promoteMemory, type MemoryRecord } from "./api";

export function MemoryManager() {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [promotingId, setPromotingId] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMemories();
      setMemories(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load memories.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handlePromote = async (id: string) => {
    setPromotingId(id);
    try {
      await promoteMemory(id, true);
      // reload memories
      const updated = await fetchMemories();
      setMemories(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Promotion failed");
    } finally {
      setPromotingId(null);
    }
  };

  if (loading) {
    return <Loading label="Retrieving memory records…" />;
  }

  if (error) {
    return <ErrorState message={error} onRetry={loadData} />;
  }

  if (memories.length === 0) {
    return (
      <EmptyState
        title="No Memories Found"
        message="No virtual memory records or candidates are currently stored in the metacog system."
      />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      {memories.map((mem) => {
        const isPending = mem.promotion_status === "pending" || mem.promotion_status === "shadow";
        return (
          <Card key={mem.id}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "var(--space-4)" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", marginBottom: "var(--space-2)" }}>
                  <Badge tone={mem.promotion_status === "promoted" ? "ok" : "warn"}>
                    {mem.promotion_status.toUpperCase()}
                  </Badge>
                  <Badge tone="neutral">
                    {mem.type.toUpperCase()}
                  </Badge>
                  <span style={{ fontSize: "var(--font-size-sm)", color: "var(--text-muted)" }}>
                    ID: {mem.id}
                  </span>
                </div>
                
                <p style={{ fontSize: "var(--font-size-base)", fontWeight: 500, margin: "0 0 var(--space-3) 0", lineHeight: 1.5, color: "var(--text-main)" }}>
                  {mem.statement}
                </p>

                <div style={{ display: "flex", gap: "var(--space-4)", fontSize: "var(--font-size-sm)", color: "var(--text-muted)" }}>
                  <div>
                    <strong>Confidence:</strong> {(mem.confidence * 100).toFixed(0)}%
                  </div>
                  {mem.evidence && mem.evidence.length > 0 ? (
                    <div>
                      <strong>Evidence:</strong> {mem.evidence.join(", ")}
                    </div>
                  ) : null}
                  {mem.applicability && Object.keys(mem.applicability).length > 0 ? (
                    <div>
                      <strong>Applicability:</strong> {JSON.stringify(mem.applicability)}
                    </div>
                  ) : null}
                </div>
              </div>

              {isPending ? (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void handlePromote(mem.id)}
                  disabled={promotingId === mem.id}
                >
                  {promotingId === mem.id ? "Promoting..." : "Promote"}
                </Button>
              ) : null}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
