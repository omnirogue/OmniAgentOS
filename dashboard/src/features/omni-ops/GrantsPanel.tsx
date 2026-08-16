"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Loading,
  Stat,
} from "@/design";
import { grokOpsApi } from "./api";
import styles from "./omni-ops.module.css";
import type { GrokGrant } from "./types";

function formatDate(value: string): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function usagePercent(used: number, max: number): number {
  if (max <= 0) return 0;
  return Math.min(100, Math.round((used / max) * 100));
}

function GrantCard({
  grant,
  onRevoke,
  revoking,
}: {
  grant: GrokGrant;
  onRevoke: (id: string) => void;
  revoking: boolean;
}) {
  const actionPct = usagePercent(grant.actions_used, grant.max_actions);
  const spendPct = usagePercent(grant.spend_used_usd, grant.max_spend_usd);
  const nearCeiling = actionPct >= 75 || spendPct >= 75;

  return (
    <Card>
      <div className={styles.grantCard}>
        <div className={styles.grantHeader}>
          <div>
            <Badge tone={grant.status === "active" ? "ok" : "neutral"}>{grant.status}</Badge>
            <Badge tone="neutral">{grant.capability}</Badge>
            {grant.label ? <span className={styles.toolbarTitle}> — {grant.label}</span> : null}
          </div>
          {grant.status === "active" ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={revoking}
              onClick={() => onRevoke(grant.id)}
            >
              {revoking ? "Revoking…" : "Revoke"}
            </Button>
          ) : null}
        </div>

        <div className={styles.grantMeta}>
          <div>
            <strong>Actions</strong>
            {grant.actions_used} / {grant.max_actions}
            {actionPct > 0 ? (
              <Badge tone={nearCeiling ? "warn" : "neutral"}>{actionPct}%</Badge>
            ) : null}
          </div>
          <div>
            <strong>Spend</strong>
            ${grant.spend_used_usd.toFixed(2)} / ${grant.max_spend_usd.toFixed(2)}
            {spendPct > 0 ? (
              <Badge tone={nearCeiling ? "warn" : "neutral"}>{spendPct}%</Badge>
            ) : null}
          </div>
          <div>
            <strong>Project</strong>
            {grant.project_id ?? "global"}
          </div>
          <div>
            <strong>Expires</strong>
            {formatDate(grant.expires_at)}
          </div>
        </div>

        <p className={styles.toolbarCount}>
          Approval {grant.approval_id} · created {formatDate(grant.created_at)}
        </p>
      </div>
    </Card>
  );
}

export function GrantsPanel() {
  const [grants, setGrants] = useState<GrokGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const seqRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await grokOpsApi.grants();
      if (seq === seqRef.current) {
        setGrants(data);
        setError(null);
      }
    } catch (err) {
      if (seq === seqRef.current) {
        setError(err instanceof Error ? err.message : "Failed to load grants");
      }
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const activeCount = grants.filter((g) => g.status === "active").length;
  const totalSpend = grants.reduce((sum, g) => sum + g.spend_used_usd, 0);
  // D-2 (LiveSim LS-004) audit find: `grants` is never cleared on a failed
  // refresh, so "0 active / 0 total" only means "confirmed empty" when
  // `error` is unset -- otherwise the true grant count is unknown, and this
  // is an authorization-spend surface where a confident zero is the worst
  // possible misread.
  const grantsUnknown = Boolean(error) && grants.length === 0;

  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);

  const confirmRevoke = async () => {
    if (!revokeTarget) return;
    const id = revokeTarget;
    setRevoking(true);
    setNotice(null);
    try {
      await grokOpsApi.revokeGrant(id, "operator_revoke");
      setNotice(`Grant ${id} revoked`);
      setRevokeTarget(null);
      await refresh();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Revoke failed");
    } finally {
      setRevoking(false);
    }
  };

  return (
    <>
      <div className={styles.statGrid}>
        <Stat label="Active grants" value={grantsUnknown ? "—" : activeCount} tone={activeCount > 0 ? "ok" : "default"} />
        <Stat label="Total grants" value={grantsUnknown ? "—" : grants.length} />
        <Stat label="Total spend used" value={grantsUnknown ? "—" : `$${totalSpend.toFixed(2)}`} tone="accent" />
      </div>

      {notice ? <div className={styles.notice}>{notice}</div> : null}

      <div className={styles.toolbar}>
        <Button size="sm" variant="ghost" onClick={() => void refresh()} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading ? <Loading label="Loading grants…" /> : null}
      {error ? <ErrorState title="Grants unavailable" message={error} onRetry={refresh} /> : null}
      {!loading && !error && grants.length === 0 ? (
        <EmptyState
          title="No active grants"
          message="No authorization grants are currently active. Grants are created when an operator approves an agent's capability request with explicit action and spend ceilings."
        />
      ) : null}
      {!loading && grants.length > 0 ? (
        <div className={styles.cardGrid}>
          {grants.map((grant) => (
            <GrantCard key={grant.id} grant={grant} onRevoke={setRevokeTarget} revoking={revoking} />
          ))}
        </div>
      ) : null}

      {/* Revoke confirmation — the design Dialog, not window.confirm. */}
      <Dialog
        open={revokeTarget !== null}
        onClose={() => setRevokeTarget(null)}
        title="Revoke this grant?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRevokeTarget(null)} disabled={revoking}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void confirmRevoke()} disabled={revoking}>
              {revoking ? "Revoking…" : "Revoke"}
            </Button>
          </>
        }
      >
        <p className={styles.toolbarCount}>
          The agent will no longer be authorized for this capability. This is
          recorded as an operator revoke.
        </p>
      </Dialog>
    </>
  );
}
