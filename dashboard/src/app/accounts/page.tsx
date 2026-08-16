"use client";

import { useState, type FormEvent } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Input,
  Loading,
  Page,
  PageHeader,
  Pill,
  Section,
  Select,
  StatusDot,
  Table,
  type SelectOption,
  type TableColumn,
} from "@/design";
import { AccountsApiError } from "@/features/accounts/client";
import {
  authTypeLabel,
  formatDateTime,
  relativeTime,
  statusDotState,
  statusLabel,
  statusTone,
  untilTime,
} from "@/features/accounts/format";
import { useAccounts, useAccountUsage } from "@/features/accounts/hooks";
import { ProviderUsageCard, UsageMeters } from "@/features/accounts/UsageMeters";
import type { Account, AccountAuthType, AddAccountInput } from "@/features/accounts/types";
import styles from "@/features/accounts/accounts.module.css";

const AUTH_TYPE_OPTIONS: SelectOption[] = [
  { value: "config_dir", label: "Config directory" },
  { value: "oauth_token", label: "OAuth token" },
  { value: "api_key", label: "API key" },
];

function actionErrorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof AccountsApiError || reason instanceof Error) return reason.message;
  return fallback;
}

/** Inline "Add account" form — type selector plus the one field that type
 * needs (a path for config_dir, a masked secret for oauth_token/api_key).
 * A 400 from the server (duplicate config dir, non-existent path, …) is
 * shown on that same field via Input's own `error` slot. */
function AddAccountForm({ onAdded }: { onAdded: (input: AddAccountInput) => Promise<Account> }) {
  const [authType, setAuthType] = useState<AccountAuthType>("config_dir");
  const [label, setLabel] = useState("");
  const [configDir, setConfigDir] = useState("");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setLabelError(null);
    setFieldError(null);

    let invalid = false;
    if (!label.trim()) {
      setLabelError("A label is required.");
      invalid = true;
    }
    if (authType === "config_dir") {
      if (!configDir.trim()) {
        setFieldError("A config directory path is required.");
        invalid = true;
      }
    } else if (!secret.trim()) {
      setFieldError(`Paste the ${authType === "oauth_token" ? "OAuth token" : "API key"}.`);
      invalid = true;
    }
    if (invalid) return;

    const payload: AddAccountInput = { label: label.trim(), auth_type: authType };
    if (authType === "config_dir") payload.config_dir = configDir.trim();
    else payload.secret = secret.trim();

    setSaving(true);
    try {
      await onAdded(payload);
      setLabel("");
      setConfigDir("");
      setSecret("");
    } catch (reason) {
      setFieldError(actionErrorMessage(reason, "Could not add this account."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={(e) => void onSubmit(e)} className={styles.formGrid}>
      <div className={styles.formRow}>
        <Select
          label="Type"
          options={AUTH_TYPE_OPTIONS}
          value={authType}
          onChange={(value) => {
            setAuthType(value as AccountAuthType);
            setFieldError(null);
          }}
        />
        <Input
          label="Label"
          placeholder="e.g. acct-3"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          error={labelError ?? undefined}
          required
        />
        {authType === "config_dir" ? (
          <Input
            label="Config directory"
            placeholder="~/.claude-account-3"
            value={configDir}
            onChange={(e) => setConfigDir(e.target.value)}
            error={fieldError ?? undefined}
          />
        ) : (
          <Input
            label={authType === "oauth_token" ? "OAuth token" : "API key"}
            type="password"
            placeholder={authType === "oauth_token" ? "Paste the OAuth token…" : "Paste the API key…"}
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            error={fieldError ?? undefined}
          />
        )}
      </div>
      <div>
        <Button type="submit" variant="primary" disabled={saving}>
          {saving ? "Adding…" : "Add account"}
        </Button>
      </div>
    </form>
  );
}

/** Presets rather than a free-text box: a pause is a self-expiring lever, and
 * a mistyped unit is the one way to turn it back into a permanent one. */
const PAUSE_DURATIONS: SelectOption[] = [
  { value: "15", label: "15 minutes" },
  { value: "60", label: "1 hour" },
  { value: "240", label: "4 hours" },
  { value: "720", label: "12 hours" },
  { value: "1440", label: "24 hours" },
];

function RowActions({
  account,
  onToggle,
  onMakeDefault,
  onRemove,
  onPause,
  onResume,
}: {
  account: Account;
  onToggle: (account: Account) => Promise<void>;
  onMakeDefault: (account: Account) => Promise<void>;
  onRemove: (account: Account) => Promise<void>;
  onPause: (account: Account, minutes: number) => Promise<void>;
  onResume: (account: Account) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [minutes, setMinutes] = useState("60");

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  if (pausing) {
    return (
      <div className={styles.pauseForm}>
        <Select label="Pause for" options={PAUSE_DURATIONS} value={minutes} onChange={setMinutes} />
        <Button
          size="sm"
          variant="primary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await onPause(account, Number(minutes));
              setPausing(false);
            })
          }
        >
          Pause
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setPausing(false)}>
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <div className={styles.actionsRow}>
      <Button
        size="sm"
        variant={account.enabled ? "secondary" : "primary"}
        disabled={busy}
        onClick={() => void run(() => onToggle(account))}
      >
        {account.enabled ? "Disable" : "Enable"}
      </Button>
      {account.paused ? (
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void run(() => onResume(account))}>
          Resume
        </Button>
      ) : (
        // Pausing a disabled account would be a no-op dressed up as an action.
        <Button size="sm" variant="secondary" disabled={busy || !account.enabled} onClick={() => setPausing(true)}>
          Pause
        </Button>
      )}
      {!account.is_default ? (
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => void run(() => onMakeDefault(account))}>
          Make default
        </Button>
      ) : null}
      <Button size="sm" variant="danger" disabled={busy} onClick={() => void run(() => onRemove(account))}>
        Remove
      </Button>
    </div>
  );
}

export default function AccountsPage() {
  const { accounts, loading, error, refresh, create, setEnabled, makeDefault, remove, pause, resume } =
    useAccounts();
  const usage = useAccountUsage();
  const [actionError, setActionError] = useState<string | null>(null);

  const pause_ = async (account: Account, minutes: number) => {
    setActionError(null);
    try {
      await pause(account.id, minutes, "paused from the dashboard");
    } catch (reason) {
      setActionError(actionErrorMessage(reason, "Could not pause this account."));
    }
  };

  const resume_ = async (account: Account) => {
    setActionError(null);
    try {
      await resume(account.id);
    } catch (reason) {
      setActionError(actionErrorMessage(reason, "Could not resume this account."));
    }
  };

  const toggle = async (account: Account) => {
    setActionError(null);
    try {
      await setEnabled(account.id, !account.enabled);
    } catch (reason) {
      setActionError(actionErrorMessage(reason, "Could not update this account."));
    }
  };

  const makeDefault_ = async (account: Account) => {
    setActionError(null);
    try {
      await makeDefault(account.id);
    } catch (reason) {
      setActionError(actionErrorMessage(reason, "Could not set this account as default."));
    }
  };

  const remove_ = async (account: Account) => {
    if (!window.confirm(`Remove account "${account.label}"? Sessions will stop rotating through it.`)) return;
    setActionError(null);
    try {
      await remove(account.id);
    } catch (reason) {
      setActionError(actionErrorMessage(reason, "Could not remove this account."));
    }
  };

  const columns: TableColumn<Account>[] = [
    {
      key: "account",
      header: "Account",
      sortable: true,
      sortValue: (a) => a.email ?? a.label,
      render: (a) => (
        <div className={styles.identityCell}>
          <div className={styles.identityName}>
            <span>{a.email ?? a.label}</span>
            {a.is_default ? <Pill tone="accent">Default</Pill> : null}
            <Badge tone={a.enabled ? "ok" : "neutral"}>{a.enabled ? "Enabled" : "Disabled"}</Badge>
            {/* A pause is temporary and self-lifting, so say WHEN it lifts —
                otherwise it reads like a second kind of "disabled". */}
            {a.paused ? <Pill tone="warn">Paused, back {untilTime(a.paused_until)}</Pill> : null}
          </div>
          {a.paused && a.pause_reason ? <div className={styles.faint}>{a.pause_reason}</div> : null}
          {a.email ? <div className={styles.faint}>Label: {a.label}</div> : null}
          {a.config_dir ? <div className={`${styles.faint} ${styles.mono}`}>{a.config_dir}</div> : null}
        </div>
      ),
    },
    {
      key: "type",
      header: "Type",
      render: (a) => (
        <div className={styles.row}>
          <span>{authTypeLabel(a.auth_type)}</span>
          {a.has_secret ? (
            <Pill tone="neutral">Secret on file</Pill>
          ) : a.auth_type !== "config_dir" ? (
            <Pill tone="warn">No secret stored</Pill>
          ) : null}
        </div>
      ),
    },
    {
      key: "usage",
      header: "Usage",
      sortable: true,
      // Sort by whichever window is closest to exhaustion, so "who is nearly
      // out" is one click away. Unknown sorts last, not as 0%.
      sortValue: (a) => {
        const snapshot = a.config_dir ? usage.byConfigDir.get(a.config_dir) : undefined;
        if (!snapshot?.available || !snapshot.windows.length) return -1;
        return Math.max(...snapshot.windows.map((w) => w.percent));
      },
      render: (a) =>
        usage.loading ? (
          <span className={styles.faint}>Loading…</span>
        ) : (
          <UsageMeters snapshot={a.config_dir ? usage.byConfigDir.get(a.config_dir) : undefined} />
        ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      sortValue: (a) => a.status,
      render: (a) => (
        <div className={styles.identityCell}>
          <Badge
            tone={statusTone(a.status)}
            style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", width: "fit-content" }}
          >
            <StatusDot state={statusDotState(a.status)} label={statusLabel(a.status)} />
            {statusLabel(a.status)}
          </Badge>
          {a.status_detail ? <div className={styles.faint}>{a.status_detail}</div> : null}
        </div>
      ),
    },
    {
      key: "lastUsed",
      header: "Last used",
      sortable: true,
      sortValue: (a) => (a.last_used_at ? Date.parse(a.last_used_at) : -1),
      render: (a) => <span title={formatDateTime(a.last_used_at)}>{relativeTime(a.last_used_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      render: (a) => (
        <RowActions
          account={a}
          onToggle={toggle}
          onMakeDefault={makeDefault_}
          onRemove={remove_}
          onPause={pause_}
          onResume={resume_}
        />
      ),
    },
  ];

  return (
    <Page>
      <PageHeader
        eyebrow="Workspace"
        title="Accounts"
        lead="Enabled accounts are rotated across Claude Code sessions to increase concurrent capacity past any single account's rate limit."
      />

      <Section title="Add account" description="Register another Claude login the scheduler can rotate sessions through.">
        <Card raised>
          <AddAccountForm onAdded={create} />
        </Card>
      </Section>

      <Section
        title="Registered accounts"
        actions={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              void refresh();
              void usage.refresh();
            }}
          >
            Refresh
          </Button>
        }
      >
        {actionError ? <ErrorState message={actionError} /> : null}
        {loading ? (
          <Card>
            <Loading variant="skeleton" label="Loading accounts…" lines={4} />
          </Card>
        ) : null}
        {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
        {!loading && !error && accounts.length === 0 ? (
          <Card>
            <EmptyState
              title="No accounts registered"
              message="Add a Claude login above to start rotating sessions across it."
            />
          </Card>
        ) : null}
        {!loading && !error && accounts.length > 0 ? (
          <Card padding="none">
            <Table columns={columns} rows={accounts} rowKey={(a) => a.id} caption="Accounts" />
          </Card>
        ) : null}
      </Section>

      <Section
        title="Other providers"
        description="Fleet-wide quota for the non-Claude CLIs. These have no per-account registry entry, so their limits are reported once per provider."
      >
        <Card>
          {usage.error ? (
            <ErrorState message={usage.error} onRetry={usage.refresh} />
          ) : usage.loading ? (
            <Loading variant="skeleton" label="Loading usage…" lines={2} />
          ) : (
            <div className={styles.providerUsageGrid}>
              {usage.otherProviders.map((snapshot) => (
                <ProviderUsageCard key={snapshot.provider} snapshot={snapshot} />
              ))}
            </div>
          )}
        </Card>
      </Section>
    </Page>
  );
}
