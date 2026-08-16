"use client";

import { useEffect, useState } from "react";
import { Dialog, ErrorState, Loading } from "@/design";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { StatusBadge } from "./StatusBadge";
import styles from "./health.module.css";
import type { CapabilityDetail } from "./types";

function isCapabilityDetail(value: unknown): value is CapabilityDetail {
  return typeof value === "object" && value !== null && "id" in value && "recent_runs" in value;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

export type CapabilityDetailDialogProps = {
  capabilityId: string | null;
  onClose: () => void;
};

/** Drill-in per capability: fetched lazily on open (not preloaded for every
 * row in the table), rendering recent run history, the exact verification
 * command, the last non-OK run, and the evidence path — never a credential
 * value, only the verification spec's variable/field NAMES the registry
 * already carries. */
export function CapabilityDetailDialog({ capabilityId, onClose }: CapabilityDetailDialogProps) {
  const [detail, setDetail] = useState<CapabilityDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!capabilityId) {
      setDetail(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const response = await fetchWithTimeout(`/api/health?id=${encodeURIComponent(capabilityId)}`, {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        const body: unknown = await response.json();
        if (cancelled) return;
        if (!response.ok || !isCapabilityDetail(body)) {
          setError(typeof body === "object" && body && "error" in body ? String((body as { error: unknown }).error) : "Could not load capability detail.");
          setDetail(null);
        } else {
          setDetail(body);
        }
      } catch {
        if (!cancelled) setError("Could not reach the health API.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [capabilityId]);

  return (
    <Dialog open={capabilityId !== null} onClose={onClose} title={detail ? detail.name : capabilityId ?? ""}>
      {loading ? <Loading variant="skeleton" label="Loading capability detail…" lines={5} /> : null}
      {!loading && error ? <ErrorState message={error} /> : null}
      {!loading && !error && detail ? (
        <div className={styles.detail}>
          <StatusBadge status={detail.status} />
          <div className={styles.detailGrid}>
            <div>
              <strong>Company</strong>
              <div>{detail.company}</div>
            </div>
            <div>
              <strong>Kind</strong>
              <div>{detail.kind}</div>
            </div>
            <div>
              <strong>Owner</strong>
              <div>{detail.owner}</div>
            </div>
            <div>
              <strong>Last checked</strong>
              <div>{formatTimestamp(detail.last_checked)}</div>
            </div>
            <div>
              <strong>Last good</strong>
              <div>{formatTimestamp(detail.last_good)}</div>
            </div>
            <div>
              <strong>Evidence path</strong>
              <div className={styles.mono}>{detail.evidence ?? "none"}</div>
            </div>
          </div>

          <div>
            <strong>What it does</strong>
            <p>{detail.what_it_does || "—"}</p>
          </div>

          <div>
            <strong>Verification command</strong>
            <p className={styles.mono}>{detail.verification_command ?? "no verification configured — this is why it reads UNVERIFIED"}</p>
          </div>

          <div>
            <strong>Last error</strong>
            {detail.last_error ? (
              <p>
                <StatusBadge status={detail.last_error.status} /> at {formatTimestamp(detail.last_error.ts)}
                {detail.last_error.exit_code !== null ? ` — exit code ${detail.last_error.exit_code}` : ""}
                {detail.last_error.evidence ? ` — evidence: ${detail.last_error.evidence}` : ""}
                <br />
                <span className={styles.muted}>
                  The CLI deliberately never captures raw command stdout/stderr here — only exit code, status, and
                  the evidence path above.
                </span>
              </p>
            ) : (
              <p className={styles.muted}>No non-OK run in recent history.</p>
            )}
          </div>

          <div>
            <strong>Recent run history</strong>
            {detail.recent_runs.length === 0 ? (
              <p className={styles.muted}>No runs recorded yet.</p>
            ) : (
              <table className={styles.runsTable}>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Status</th>
                    <th>Exit code</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.recent_runs.map((run) => (
                    <tr key={run.ts}>
                      <td>{formatTimestamp(run.ts)}</td>
                      <td>
                        <StatusBadge status={run.status} />
                      </td>
                      <td>{run.exit_code ?? "—"}</td>
                      <td>{run.latency_ms !== null ? `${run.latency_ms}ms` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
