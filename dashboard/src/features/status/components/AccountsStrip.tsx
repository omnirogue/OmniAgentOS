"use client";

import { Card, StatusDot } from "@/design";
import { statusDotState } from "@/features/accounts/format";
import { useAccounts } from "@/features/accounts/hooks";
import styles from "../status.module.css";

/**
 * Thin bottom strip reusing the existing `/api/accounts` client (via
 * `useAccounts`, unmodified — this feature only reads it). Degrades to
 * "unavailable" on a fetch error rather than hiding the strip or showing a
 * stale/fake roster.
 */
export function AccountsStrip() {
  const { accounts, loading, error } = useAccounts();

  if (error) {
    return (
      <Card padding="sm" className={styles.accountsStrip}>
        <span className={styles.accountsLabel}>Accounts:</span>
        <span className={styles.muted}>unavailable ({error})</span>
      </Card>
    );
  }

  if (loading && accounts.length === 0) {
    return (
      <Card padding="sm" className={styles.accountsStrip}>
        <span className={styles.accountsLabel}>Accounts:</span>
        <span className={styles.muted}>loading…</span>
      </Card>
    );
  }

  if (accounts.length === 0) {
    return (
      <Card padding="sm" className={styles.accountsStrip}>
        <span className={styles.accountsLabel}>Accounts:</span>
        <span className={styles.muted}>none configured</span>
      </Card>
    );
  }

  return (
    <Card padding="sm" className={styles.accountsStrip}>
      <span className={styles.accountsLabel}>Accounts:</span>
      {accounts.map((account) => (
        <span key={account.id} className={styles.accountsChip} title={account.status_detail ?? undefined}>
          <StatusDot state={statusDotState(account.status)} />
          {account.label}
          {account.enabled ? "" : " (disabled)"}
          {account.paused ? " (paused)" : ""}
        </span>
      ))}
    </Card>
  );
}
