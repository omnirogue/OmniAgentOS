"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorState, Icon, Loading, Page, PageHeader, Tabs } from "@/design";
import {
  fetchSystemAgents,
  fetchSystemImprovers,
  fetchSystemMap,
  fetchSystemSkills,
} from "./client";
import type {
  AgentsResponse,
  ImproversResponse,
  SkillsResponse,
  SystemAgent,
  SystemMap,
} from "./types";
import { MapPanel } from "./MapPanel";
import { AgentsPanel } from "./AgentsPanel";
import { SkillsPanel } from "./SkillsPanel";
import { ImproversPanel } from "./ImproversPanel";
import { ActivityPanel } from "./ActivityPanel";
import styles from "./system.module.css";

/**
 * The System transparency page. One system: agents (who) × skills (how) × loops
 * (improvers). Every panel reflects an editable file on disk, and the Agents panel
 * can edit that file (whitelisted frontmatter) in place. Reads are token-gated
 * through the same-origin proxy exactly like the reliability pages.
 */
export function SystemView() {
  const [map, setMap] = useState<SystemMap | null>(null);
  const [agentsData, setAgentsData] = useState<AgentsResponse | null>(null);
  const [skills, setSkills] = useState<SkillsResponse | null>(null);
  const [improvers, setImprovers] = useState<ImproversResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    try {
      const [m, a, s, im] = await Promise.all([
        fetchSystemMap(),
        fetchSystemAgents(),
        fetchSystemSkills(),
        fetchSystemImprovers(),
      ]);
      if (my !== seq.current) return;
      setMap(m);
      setAgentsData(a);
      setSkills(s);
      setImprovers(im);
      setError(null);
    } catch (reason) {
      if (my !== seq.current) return;
      setError(reason instanceof Error ? reason.message : "Unable to load the system surface.");
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onAgentUpdated = useCallback((updated: SystemAgent) => {
    setAgentsData((prev) => {
      if (!prev) return prev;
      const agents = prev.agents.map((a) => (a.name === updated.name ? updated : a));
      const categories = Array.from(new Set(agents.map((a) => a.category))).sort();
      return { ...prev, agents, categories };
    });
  }, []);

  const onImproverPromptSaved = useCallback((label: string, content: string) => {
    setImprovers((prev) => {
      if (!prev) return prev;
      const loops = prev.loops.map((l) => (l.label === label ? { ...l, prompt_md: content } : l));
      return { ...prev, loops };
    });
  }, []);

  const header = (
    <PageHeader
      eyebrow="System"
      title="System transparency"
      lead={
        <span className={styles.visionLine}>
          One system: <strong>agents</strong> (who) × <strong>skills</strong> (how) ×{" "}
          <strong>loops</strong> (improvers) — everything on this page is an editable file.
        </span>
      }
      actions={
        <button type="button" className="ds-btn ds-btn--ghost ds-btn--sm" onClick={() => void refresh()}>
          <Icon name="radio" size={14} /> Refresh
        </button>
      }
    />
  );

  if (loading && !map) {
    return (
      <Page>
        {header}
        <Loading label="Loading the system surface…" />
      </Page>
    );
  }

  if (error && !map) {
    return (
      <Page>
        {header}
        <ErrorState message={`Could not load the system surface: ${error}`} onRetry={() => void refresh()} />
      </Page>
    );
  }

  const agentNames = agentsData?.agents.map((a) => a.name) ?? [];

  const items = [
    {
      id: "map",
      label: "Map",
      content: map ? <MapPanel map={map} /> : null,
    },
    {
      id: "agents",
      label: `Agents${agentsData ? ` (${agentsData.agents.length})` : ""}`,
      content: agentsData ? (
        <AgentsPanel
          agents={agentsData.agents}
          categories={agentsData.categories}
          agentsDir={agentsData.agents_dir}
          onAgentUpdated={onAgentUpdated}
        />
      ) : null,
    },
    {
      id: "skills",
      label: `Skills${skills ? ` (${skills.skills.length})` : ""}`,
      content: skills ? (
        <SkillsPanel skills={skills.skills} categories={skills.categories} skillsDir={skills.skills_dir} />
      ) : null,
    },
    {
      id: "improvers",
      label: `Improvers${improvers ? ` (${improvers.loops.length})` : ""}`,
      content: improvers ? (
        <ImproversPanel data={improvers} onPromptSaved={onImproverPromptSaved} />
      ) : null,
    },
    {
      id: "log",
      label: "Log",
      content: <ActivityPanel agentNames={agentNames} />,
    },
  ];

  return (
    <Page>
      {header}
      <Tabs items={items} aria-label="System panels" />
    </Page>
  );
}
