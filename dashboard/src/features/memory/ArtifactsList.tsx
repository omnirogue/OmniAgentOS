"use client";

import { useEffect, useState, useCallback } from "react";
import { Card, Table, Loading, ErrorState, EmptyState, Badge, Button, type TableColumn } from "@/design";
import { fetchArtifacts, type ArtifactEnvelope } from "./api";
import { ArtifactPreview } from "@/features/artifacts/ArtifactPreview";

export function ArtifactsList() {
  const [artifacts, setArtifacts] = useState<ArtifactEnvelope[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState<ArtifactEnvelope | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchArtifacts();
      setArtifacts(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load artifacts.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  if (loading) {
    return <Loading label="Retrieving registered artifacts…" />;
  }

  if (error) {
    return <ErrorState message={error} onRetry={loadData} />;
  }

  if (artifacts.length === 0) {
    return (
      <EmptyState
        title="No Artifacts Found"
        message="No artifacts have been registered under the metacog system yet."
      />
    );
  }

  const columns: TableColumn<ArtifactEnvelope>[] = [
    { key: "id", header: "ID", render: (r) => r.id },
    { key: "artifact_type", header: "Type", render: (r) => <Badge tone="neutral">{r.artifact_type.toUpperCase()}</Badge> },
    { key: "task_id", header: "Task ID", render: (r) => r.task_id || "-" },
    { key: "run_id", header: "Run ID", render: (r) => r.run_id || "-" },
    { key: "format", header: "Format", render: (r) => <Badge>{r.format}</Badge> },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setSelectedArtifact(r)}
        >
          Preview
        </Button>
      ),
    },
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.8fr", gap: "var(--space-4)", alignItems: "start" }}>
      <Card style={{ padding: 0 }}>
        <Table columns={columns} rows={artifacts} rowKey={(r) => r.id} />
      </Card>

      <div>
        {selectedArtifact ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3 style={{ margin: 0, fontSize: "var(--font-size-lg)", color: "var(--text-main)" }}>
                Artifact Preview
              </h3>
              <Button size="sm" variant="secondary" onClick={() => setSelectedArtifact(null)}>
                Clear
              </Button>
            </div>
            
            {/* Display artifact metadata */}
            <Card>
              <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)", fontSize: "var(--font-size-sm)" }}>
                <div><strong>ID:</strong> {selectedArtifact.id}</div>
                <div><strong>Type:</strong> {selectedArtifact.artifact_type}</div>
                <div><strong>URI:</strong> {selectedArtifact.content_uri}</div>
                <div><strong>Registered:</strong> {selectedArtifact.created_at}</div>
              </div>
            </Card>

            {/* If content_uri is a local file path, preview it using the existing preview routes */}
            {selectedArtifact.content_uri ? (
              <ArtifactPreview path={selectedArtifact.content_uri} />
            ) : (
              <EmptyState message="No previewable content URI exists for this artifact." />
            )}
          </div>
        ) : (
          <EmptyState message="Select an artifact on the left to preview its content." />
        )}
      </div>
    </div>
  );
}
