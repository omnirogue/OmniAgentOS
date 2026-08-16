"use client";

import { useMemo, useState } from "react";
import {
  Avatar,
  Badge,
  BarChart,
  Bracket,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Input,
  LineChart,
  Loading,
  PageHeader,
  Pill,
  Select,
  Sparkline,
  Stat,
  StatusDot,
  Table,
  Tabs,
  ThemeToggle,
  ToastStack,
  Tooltip,
  type ToastItem,
} from "../../design";

const sampleSeries = [
  {
    name: "Champion",
    points: [
      { x: "W1", y: 1120, ciLow: 1100, ciHigh: 1140 },
      { x: "W2", y: 1145, ciLow: 1125, ciHigh: 1165 },
      { x: "W3", y: 1160, ciLow: 1135, ciHigh: 1185 },
      { x: "W4", y: 1190, ciLow: 1165, ciHigh: 1215 },
      { x: "W5", y: 1210, ciLow: 1180, ciHigh: 1240 },
    ],
  },
  {
    name: "Challenger",
    points: [
      { x: "W1", y: 1080 },
      { x: "W2", y: 1110 },
      { x: "W3", y: 1135 },
      { x: "W4", y: 1150 },
      { x: "W5", y: 1185 },
    ],
  },
];

const sampleBars = [
  { label: "Claude", value: 42, tone: "champion" },
  { label: "Codex", value: 36, tone: "challenger" },
  { label: "Grok", value: 31, tone: "accent" },
  { label: "Local", value: 18, tone: "draw" },
];

const sampleBracket = [
  [
    { id: "r1m1", a: "Claude-Opus", b: "Codex-High", winner: "Claude-Opus", scoreA: 3, scoreB: 1 },
    { id: "r1m2", a: "Grok-4", b: "Claude-Sonnet", winner: "Grok-4", scoreA: 2, scoreB: 2 },
  ],
  [{ id: "final", a: "Claude-Opus", b: "Grok-4", winner: "Claude-Opus", scoreA: 4, scoreB: 2 }],
];

const tableRows = [
  { id: "run-01", task: "bench-b0", harness: "miniswe", elo: 1210, state: "completed" as const },
  { id: "run-02", task: "sweep-a", harness: "openhands", elo: 1185, state: "running" as const },
  { id: "run-03", task: "ablate-x", harness: "bench", elo: 1090, state: "awaiting_approval" as const },
  { id: "run-04", task: "repair-c", harness: "miniswe", elo: 990, state: "failed" as const },
];

export default function DesignShowcasePage() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [inputVal, setInputVal] = useState("mission control");
  const [selectVal, setSelectVal] = useState("dark");
  const [pillGone, setPillGone] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([
    { id: "t1", title: "Run completed", message: "bench-b0 finished successfully", tone: "ok", durationMs: 0 },
    { id: "t2", title: "Approval needed", message: "awaiting operator decision", tone: "warn", durationMs: 0 },
  ]);

  const columns = useMemo(
    () => [
      {
        key: "id",
        header: "Run",
        sortable: true,
        sortValue: (r: (typeof tableRows)[0]) => r.id,
        render: (r: (typeof tableRows)[0]) => <span className="mono">{r.id}</span>,
      },
      {
        key: "task",
        header: "Task",
        sortable: true,
        sortValue: (r: (typeof tableRows)[0]) => r.task,
        render: (r: (typeof tableRows)[0]) => r.task,
      },
      {
        key: "harness",
        header: "Harness",
        render: (r: (typeof tableRows)[0]) => <Badge tone="neutral">{r.harness}</Badge>,
      },
      {
        key: "elo",
        header: "ELO",
        sortable: true,
        align: "right" as const,
        sortValue: (r: (typeof tableRows)[0]) => r.elo,
        render: (r: (typeof tableRows)[0]) => <span className="mono">{r.elo}</span>,
      },
      {
        key: "state",
        header: "State",
        render: (r: (typeof tableRows)[0]) => (
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
            <StatusDot state={r.state} />
            <Badge category="runState" tone={r.state}>
              {r.state.replace(/_/g, " ")}
            </Badge>
          </span>
        ),
      },
    ],
    [],
  );

  return (
    <>
      {/* PageHeader renders outside .ds-showcase deliberately: the showcase's own
          `.ds-showcase h1` rule (below, scoped to this reference page's demo sections)
          would otherwise win the cascade over `.ds-page-header__title` and defeat the
          one-title-size guarantee on the one page meant to prove it. */}
      <PageHeader
        eyebrow="Reference"
        title="Design system"
        lead="Living reference for OmniAgentOS primitives and chart kit. Calm, precise mission control — every color and space from frozen tokens."
        actions={<ThemeToggle />}
      />
      <div className="ds-showcase">

      <section className="ds-showcase-section" aria-labelledby="buttons">
        <h2 id="buttons">Buttons</h2>
        <div className="ds-showcase-row">
          <Button variant="primary">Primary</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger">Danger</Button>
          <Button variant="primary" size="sm">
            Small
          </Button>
          <Button variant="primary" size="lg">
            Large
          </Button>
          <Button variant="primary" disabled>
            Disabled
          </Button>
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="badges">
        <h2 id="badges">Badges & status</h2>
        <div className="ds-showcase-row">
          <Badge category="arm" tone="champion">
            champion
          </Badge>
          <Badge category="arm" tone="challenger">
            challenger
          </Badge>
          <Badge category="disposition" tone="promote">
            promote
          </Badge>
          <Badge category="disposition" tone="reject">
            reject
          </Badge>
          <Badge category="result" tone="win">
            win
          </Badge>
          <Badge category="runState" tone="running">
            running
          </Badge>
          <Badge category="runState" tone="paused">
            paused
          </Badge>
          <Badge category="runState" tone="validating">
            validating
          </Badge>
          <StatusDot state="running" />
          <StatusDot state="completed" />
          <StatusDot state="failed" />
          <StatusDot state="awaiting_approval" />
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="stats">
        <h2 id="stats">Stats</h2>
        <div className="ds-showcase-grid">
          <Stat label="Active runs" value="12" delta="+3" tone="accent" hint="last hour" />
          <Stat label="Win rate" value="68%" delta="+4.2pp" tone="promote" />
          <Stat label="Budget used" value="$48" delta="of $100" tone="warn" />
          <Stat label="Failures" value="2" delta="−1" tone="danger" />
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="cards">
        <h2 id="cards">Card, pills, avatars</h2>
        <div className="ds-showcase-grid">
          <Card>
            <strong>Surface card</strong>
            <p style={{ margin: "0.5rem 0 0", color: "var(--text-muted)", fontSize: "var(--fs-small)" }}>
              Token-driven padding, border, and shadow.
            </p>
          </Card>
          <Card raised padding="lg">
            <div className="ds-showcase-row">
              <Avatar name="Ada Lovelace" size="lg" />
              <Avatar name="Grace Hopper" size="md" />
              <Avatar name="Alan Turing" size="sm" />
            </div>
            <div className="ds-showcase-row" style={{ marginTop: "var(--space-3)" }}>
              {!pillGone ? (
                <Pill tone="accent" onDismiss={() => setPillGone(true)}>
                  filter: harness
                </Pill>
              ) : null}
              <Pill tone="ok">stable</Pill>
              <Pill tone="warn">budget</Pill>
              <Pill tone="danger">blocked</Pill>
            </div>
          </Card>
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="forms">
        <h2 id="forms">Inputs & select</h2>
        <div className="ds-showcase-grid">
          <Input
            label="Query"
            value={inputVal}
            onChange={(e) => setInputVal(e.target.value)}
            clearable
            onClear={() => setInputVal("")}
            hint="Supports clear button"
          />
          <Input label="With error" defaultValue="bad" error="Must be a valid run id" />
          <Input label="Disabled" defaultValue="locked" disabled />
          <Select
            label="Theme preference"
            value={selectVal}
            onChange={setSelectVal}
            options={[
              { value: "dark", label: "Dark" },
              { value: "light", label: "Light" },
              { value: "system", label: "System", disabled: true },
            ]}
          />
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="table">
        <h2 id="table">Table</h2>
        <Table columns={columns} rows={tableRows} rowKey={(r) => r.id} caption="Recent runs" />
      </section>

      <section className="ds-showcase-section" aria-labelledby="tabs">
        <h2 id="tabs">Tabs, tooltip, dialog, toast</h2>
        <Card>
          <Tabs
            items={[
              {
                id: "overview",
                label: "Overview",
                content: (
                  <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "var(--fs-small)" }}>
                    Keyboard: ← → to move, Home/End to jump. Active indicator uses{" "}
                    <code className="mono">--accent</code>.
                  </p>
                ),
              },
              {
                id: "tooltip",
                label: "Tooltip",
                content: (
                  <Tooltip content="Hover or focus for details">
                    <Button variant="secondary" size="sm">
                      Hover me
                    </Button>
                  </Tooltip>
                ),
              },
              {
                id: "dialog",
                label: "Dialog",
                content: (
                  <Button variant="primary" size="sm" onClick={() => setDialogOpen(true)}>
                    Open dialog
                  </Button>
                ),
              },
            ]}
          />
        </Card>
        <div style={{ marginTop: "var(--space-4)" }}>
          <ToastStack
            items={toasts}
            onDismiss={(id: string) => setToasts((t) => t.filter((x) => x.id !== id))}
          />
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="states">
        <h2 id="states">Empty / loading / error</h2>
        <div className="ds-showcase-grid">
          <Card padding="none">
            <EmptyState
              message="No experiments match this filter."
              action={<Button size="sm">Clear filters</Button>}
            />
          </Card>
          <Card>
            <Loading label="Fetching runs…" />
            <div style={{ marginTop: "var(--space-4)" }}>
              <Loading variant="skeleton" lines={4} />
            </div>
          </Card>
          <Card padding="none">
            <ErrorState message="API unreachable at :8787" onRetry={() => undefined} />
          </Card>
        </div>
      </section>

      <section className="ds-showcase-section" aria-labelledby="charts">
        <h2 id="charts">Chart kit</h2>
        <div className="ds-showcase-grid" style={{ gridTemplateColumns: "1fr" }}>
          <Card>
            <h3 style={{ margin: "0 0 var(--space-3)", fontSize: "var(--fs-h3)" }}>LineChart + CI band</h3>
            <LineChart series={sampleSeries} />
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 var(--space-3)", fontSize: "var(--fs-h3)" }}>BarChart</h3>
            <BarChart bars={sampleBars} />
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 var(--space-3)", fontSize: "var(--fs-h3)" }}>Bracket</h3>
            <Bracket rounds={sampleBracket} />
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 var(--space-3)", fontSize: "var(--fs-h3)" }}>Sparkline</h3>
            <div className="ds-showcase-row">
              <Sparkline points={[10, 12, 9, 14, 13, 18, 16, 21]} width={160} height={36} />
              <Sparkline points={[21, 18, 19, 15, 14, 12, 11, 9]} tone="danger" width={160} height={36} />
              <Sparkline points={[5, 5, 6, 8, 7, 9, 12, 14]} tone="ok" width={160} height={36} />
            </div>
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 var(--space-3)", fontSize: "var(--fs-h3)" }}>Chart states</h3>
            <div className="ds-showcase-grid">
              <LineChart series={[]} status="empty" />
              <LineChart series={[]} status="loading" />
              <LineChart series={[]} status="error" onRetry={() => undefined} />
            </div>
          </Card>
        </div>
      </section>

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title="Confirm promote"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => setDialogOpen(false)}>
              Promote champion
            </Button>
          </>
        }
      >
        Focus is trapped. Press Escape or the close control to dismiss. Backdrop click also closes.
      </Dialog>
      </div>
    </>
  );
}
