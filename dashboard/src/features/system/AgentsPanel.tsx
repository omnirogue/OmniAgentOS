"use client";

import { Fragment, useMemo, useState } from "react";
import { Badge, Button, EmptyState, Icon, Input, Select, TableRoot, useToast } from "@/design";
import { patchSystemAgent } from "./client";
import { EFFORTS, MODEL_FAMILIES, type PatchAgentBody, type SystemAgent } from "./types";
import { EditableNote, PathChip, relTime } from "./shared";
import styles from "./system.module.css";

type SortMode = "recent" | "name";

function AgentDetail({ agent, onUpdated }: { agent: SystemAgent; onUpdated: (a: SystemAgent) => void }) {
  const { push } = useToast();
  const [model, setModel] = useState(agent.model ?? "");
  const [effort, setEffort] = useState(agent.effort ?? "");
  const [category, setCategory] = useState(agent.category === "uncategorized" ? "" : agent.category);
  const [delegated, setDelegated] = useState(agent.delegated_model ?? "");
  const [saving, setSaving] = useState(false);

  const dirty =
    model !== (agent.model ?? "") ||
    effort !== (agent.effort ?? "") ||
    category !== (agent.category === "uncategorized" ? "" : agent.category) ||
    (agent.delegated_model_editable && delegated !== (agent.delegated_model ?? ""));

  const save = async () => {
    const body: PatchAgentBody = {};
    if (model && model !== (agent.model ?? "")) body.model = model;
    if (effort && effort !== (agent.effort ?? "")) body.effort = effort;
    if (category !== (agent.category === "uncategorized" ? "" : agent.category)) body.category = category;
    if (agent.delegated_model_editable && delegated && delegated !== (agent.delegated_model ?? "")) {
      body.delegated_model = delegated;
    }
    if (Object.keys(body).length === 0) return;
    setSaving(true);
    try {
      const result = await patchSystemAgent(agent.name, body);
      onUpdated(result.agent);
      push({
        title: "Agent updated",
        message: result.committed
          ? `${agent.name}: ${result.changed.join(", ")} written and committed.`
          : `${agent.name}: ${result.changed.join(", ")} written (commit skipped).`,
        tone: "success",
      });
    } catch (reason) {
      push({
        title: "Save failed",
        message: reason instanceof Error ? reason.message : "Could not update agent.",
        tone: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.detailPanel}>
      <div className={styles.editRow}>
        <div className={styles.editField}>
          <span className={styles.filterLabel}>Model (relay)</span>
          <Select
            aria-label="Model family"
            value={model}
            onChange={setModel}
            options={MODEL_FAMILIES.map((m) => ({ value: m, label: m }))}
            placeholder="unset"
          />
        </div>
        <div className={styles.editField}>
          <span className={styles.filterLabel}>Effort</span>
          <Select
            aria-label="Effort"
            value={effort}
            onChange={setEffort}
            options={EFFORTS.map((e) => ({ value: e, label: e }))}
            placeholder="unset"
          />
        </div>
        <div className={styles.editField}>
          <span className={styles.filterLabel}>Category</span>
          <Input
            aria-label="Category"
            value={category}
            placeholder="uncategorized"
            onChange={(e) => setCategory(e.target.value)}
          />
        </div>
        <div className={styles.editField}>
          <span className={styles.filterLabel}>Delegated model</span>
          {agent.delegated_model_editable ? (
            <Input
              aria-label="Delegated model"
              value={delegated}
              placeholder="e.g. sol"
              onChange={(e) => setDelegated(e.target.value)}
            />
          ) : (
            <Badge tone="neutral">
              {agent.delegated_model ? `${agent.delegated_model} (read-only)` : "none"}
            </Badge>
          )}
        </div>
        <Button size="sm" variant="primary" disabled={!dirty || saving} onClick={save}>
          {saving ? "Saving…" : "Save config"}
        </Button>
      </div>
      <EditableNote>Writes the file + commits to the home repo — every change is diffable.</EditableNote>

      <div className={styles.metaRowSpaced}>
        <PathChip path={agent.path} label="edit this file" />
      </div>
      <pre className={styles.promptPre} aria-label={`${agent.name} system prompt`}>
        {agent.body_md}
      </pre>
    </div>
  );
}

export function AgentsPanel({
  agents,
  categories,
  agentsDir,
  onAgentUpdated,
}: {
  agents: SystemAgent[];
  categories: string[];
  agentsDir: string;
  onAgentUpdated: (a: SystemAgent) => void;
}) {
  const [category, setCategory] = useState<string>("");
  const [sort, setSort] = useState<SortMode>("recent");
  const [expanded, setExpanded] = useState<string | null>(null);

  const rows = useMemo(() => {
    const filtered = category ? agents.filter((a) => a.category === category) : agents;
    const copy = [...filtered];
    if (sort === "name") {
      copy.sort((a, b) => a.name.localeCompare(b.name));
    } else {
      copy.sort((a, b) => {
        const av = a.last_activated ?? "";
        const bv = b.last_activated ?? "";
        if (av === bv) return a.name.localeCompare(b.name);
        if (!av) return 1; // nulls (never observed) sort last
        if (!bv) return -1;
        return bv.localeCompare(av); // most recent first
      });
    }
    return copy;
  }, [agents, category, sort]);

  if (!agents.length) {
    return (
      <EmptyState
        icon={<Icon name="users" size={22} />}
        title="No agents found"
        message={`No *.md agent definitions in ${agentsDir}.`}
      />
    );
  }

  return (
    <div>
      <div className={styles.filterBar}>
        <div className={styles.filterField}>
          <span className={styles.filterLabel}>Sort</span>
          <Select
            aria-label="Sort agents"
            value={sort}
            onChange={(v) => setSort(v as SortMode)}
            options={[
              { value: "recent", label: "Most recently active" },
              { value: "name", label: "Name" },
            ]}
          />
        </div>
      </div>

      {categories.length > 1 ? (
        <div className={styles.chipRow}>
          <button
            type="button"
            className={category === "" ? `${styles.chip} ${styles.chipActive}` : styles.chip}
            onClick={() => setCategory("")}
          >
            all ({agents.length})
          </button>
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              className={category === c ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              onClick={() => setCategory(c)}
            >
              {c} ({agents.filter((a) => a.category === c).length})
            </button>
          ))}
        </div>
      ) : null}

      <TableRoot>
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col">Model</th>
            <th scope="col">Effort</th>
            <th scope="col">Tools</th>
            <th scope="col">Category</th>
            <th scope="col">Last activated</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((agent) => {
            const open = expanded === agent.name;
            return (
              <Fragment key={agent.name}>
                <tr className={styles.clickableRow} onClick={() => setExpanded(open ? null : agent.name)}>
                  <td>
                    <button
                      type="button"
                      className={styles.expandBtn}
                      aria-expanded={open}
                      onClick={(e) => {
                        e.stopPropagation();
                        setExpanded(open ? null : agent.name);
                      }}
                    >
                      <Icon name={open ? "chevronDown" : "chevronRight"} size={13} />
                      <span className={styles.agentName}>{agent.name}</span>
                    </button>
                    <div className={styles.descCell}>
                      {agent.description}
                    </div>
                  </td>
                  <td className={styles.mutedCell}>{agent.model ?? "-"}</td>
                  <td className={styles.mutedCell}>{agent.effort ?? "-"}</td>
                  <td className={styles.mutedCell}>{agent.tools_count}</td>
                  <td>
                    {agent.category === "uncategorized" ? (
                      <span className={styles.faintCell}>uncategorized</span>
                    ) : (
                      <Badge tone="neutral">{agent.category}</Badge>
                    )}
                  </td>
                  <td>
                    <span className={styles.mutedCell}>{relTime(agent.last_activated)}</span>
                    {agent.last_activated_source ? (
                      <div className={styles.faintCell} title="best-effort signal">
                        {agent.last_activated_source}
                      </div>
                    ) : null}
                  </td>
                </tr>
                {open ? (
                  <tr>
                    <td colSpan={6} className={styles.detailCell}>
                      <AgentDetail agent={agent} onUpdated={onAgentUpdated} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </TableRoot>
    </div>
  );
}
