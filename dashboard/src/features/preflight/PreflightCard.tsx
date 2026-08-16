"use client";

import { useMemo } from "react";
import { Badge, Card } from "@/design";

export type PreflightData = {
  workstream: string;
  domains: string[];
  risk_class: string;
  priority: string;
  formation_id: string;
  confidence: number;
  skills: Array<{
    name: string;
    version: string;
    score: number;
    reason: string;
  }>;
  owned_paths: string[];
  parallelism: number;
  wide: boolean;
  pinned_formation?: string;
};

export interface PreflightCardProps {
  preflightJson?: string | null;
}

export function PreflightCard({ preflightJson }: PreflightCardProps) {
  const data = useMemo<PreflightData | null>(() => {
    if (!preflightJson) return null;
    try {
      return JSON.parse(preflightJson);
    } catch {
      return null;
    }
  }, [preflightJson]);

  if (!data) return null;

  return (
    <div
      style={{
        marginTop: "12px",
        padding: "10px",
        borderRadius: "6px",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        background: "rgba(255, 255, 255, 0.02)",
        fontSize: "12px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "8px",
          borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
          paddingBottom: "6px",
        }}
      >
        <span style={{ fontWeight: "bold", color: "rgba(255, 255, 255, 0.7)", letterSpacing: "0.05em", textTransform: "uppercase", fontSize: "10px" }}>
          Deploy Preflight
        </span>
        <div style={{ display: "flex", gap: "6px" }}>
          <Badge tone={data.confidence >= 0.8 ? "completed" : "warn"}>
            {Math.round(data.confidence * 100)}% Conf
          </Badge>
          <Badge tone="promote">
            {data.formation_id.toUpperCase()} FORM
          </Badge>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginBottom: "8px" }}>
        <div>
          <span style={{ color: "rgba(255, 255, 255, 0.4)", display: "block", fontSize: "10px" }}>Parallelism</span>
          <span style={{ fontWeight: "600" }}>{data.parallelism} tasks {data.wide ? "(Wide)" : ""}</span>
        </div>
        <div>
          <span style={{ color: "rgba(255, 255, 255, 0.4)", display: "block", fontSize: "10px" }}>Risk Class</span>
          <span style={{ textTransform: "capitalize" }}>{data.risk_class}</span>
        </div>
      </div>

      {data.owned_paths && data.owned_paths.length > 0 && (
        <div style={{ marginBottom: "8px" }}>
          <span style={{ color: "rgba(255, 255, 255, 0.4)", display: "block", fontSize: "10px", marginBottom: "2px" }}>
            Scoped Paths ({data.owned_paths.length})
          </span>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
            {data.owned_paths.map((p) => (
              <code
                key={p}
                style={{
                  background: "rgba(255, 255, 255, 0.06)",
                  padding: "2px 4px",
                  borderRadius: "3px",
                  fontFamily: "monospace",
                  fontSize: "10px",
                  color: "#a5d6ff",
                  maxWidth: "100%",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={p}
              >
                {p}
              </code>
            ))}
          </div>
        </div>
      )}

      {data.skills && data.skills.length > 0 && (
        <div>
          <span style={{ color: "rgba(255, 255, 255, 0.4)", display: "block", fontSize: "10px", marginBottom: "2px" }}>
            Selected Skills
          </span>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
            {data.skills.map((s) => (
              <span
                key={s.name}
                style={{
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  padding: "1px 4px",
                  borderRadius: "4px",
                  fontSize: "10px",
                  color: "rgba(255, 255, 255, 0.8)",
                }}
                title={`${s.name}@${s.version} (score: ${s.score}): ${s.reason}`}
              >
                {s.name}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
