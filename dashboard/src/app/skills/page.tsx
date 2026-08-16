"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card, ErrorState, Input, Loading, Page, PageHeader, Section, Tooltip } from "@/design";
import type { DomainSkillEntry, SkillsExtraPayload } from "@/features/skills";
import styles from "@/features/skills/skills.module.css";

function isPayload(value: unknown): value is SkillsExtraPayload {
  return Boolean(
    value &&
      typeof value === "object" &&
      "library" in value &&
      "repoSkills" in value &&
      "dormant" in value,
  );
}

function relativizeHome(path: string): string {
  const home = "/Users/youruser";
  return path.startsWith(`${home}/`) ? `~${path.slice(home.length)}` : path;
}

function groupByCategory(skills: DomainSkillEntry[]): Map<string, DomainSkillEntry[]> {
  const groups = new Map<string, DomainSkillEntry[]>();
  for (const skill of skills) {
    const bucket = groups.get(skill.category);
    if (bucket) bucket.push(skill);
    else groups.set(skill.category, [skill]);
  }
  return groups;
}

function matchesQuery(skill: DomainSkillEntry, query: string): boolean {
  if (!query) return true;
  return skill.name.toLowerCase().includes(query) || skill.description.toLowerCase().includes(query);
}

export default function SkillsPage() {
  const [data, setData] = useState<SkillsExtraPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/local/skills-extra", { cache: "no-store" });
      const json: unknown = await response.json();
      if (!response.ok || !isPayload(json)) {
        throw new Error(
          json && typeof json === "object" && "error" in json ? String((json as { error: unknown }).error) : `HTTP ${response.status}`,
        );
      }
      setData(json);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const normalizedQuery = query.trim().toLowerCase();
  const library = data?.library;
  const filteredSkills = useMemo(
    () => (library?.skills ?? []).filter((skill) => matchesQuery(skill, normalizedQuery)),
    [library, normalizedQuery],
  );
  const grouped = useMemo(() => groupByCategory(filteredSkills), [filteredSkills]);
  const categories = useMemo(() => Array.from(grouped.keys()).sort(), [grouped]);

  return (
    <Page>
      <PageHeader
        eyebrow="Skill library"
        title="Skills"
        lead="Every skill available to Claude Code (~/.claude/skills), this repo's own skill files, and — clearly separated — the DB's dormant seed rows that aren't wired into anything live."
        meta={
          library?.scannedAt ? (
            <span>
              Scanned {new Date(library.scannedAt).toLocaleString()}
              {/* OC11 (round 3, 2026-08-14): `stale` was write-only — the
                  API sets it on the deadline-fallback path, but nothing
                  rendered it, so a stale-but-valid snapshot looked identical
                  to a fresh one. */}
              {data?.stale ? " — stale (live read timed out)" : ""}
            </span>
          ) : undefined
        }
      />

      {loading && !data ? (
        <Card>
          <Loading label="Reading the skill library…" />
        </Card>
      ) : null}
      {error ? <ErrorState message={`Could not load skill data: ${error}`} onRetry={() => void refresh()} /> : null}

      {data ? (
        <>
          <Section
            eyebrow="~/.claude/skills"
            title={`Skill library (${library?.skills.length ?? 0})`}
            description="Grouped by source; the badge marks a domain skill symlinked in from elsewhere."
            actions={
              <Input
                aria-label="Filter skills by name or description"
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                clearable
                onClear={() => setQuery("")}
                placeholder="Filter by name or description"
              />
            }
          >
            {library?.source === "error" ? (
              <p className={styles.unavailable}>unavailable: {library.error}</p>
            ) : null}
            {library?.source === "live" && categories.length === 0 ? (
              <p className={styles.empty}>{normalizedQuery ? "No matching skills." : "No skills found."}</p>
            ) : null}
            <div className={styles.categoryGrid}>
              {categories.map((category) => {
                const skills = grouped.get(category) ?? [];
                return (
                  <Card key={category} className={styles.categoryCard}>
                    <div className={styles.categoryHeading}>
                      <h3>{category}</h3>
                      <span>{skills.length}</span>
                    </div>
                    <ul className={styles.skillList}>
                      {skills.map((skill) => (
                        <li key={skill.id}>
                          <div className={styles.skillRow}>
                            <span className={styles.skillName}>
                              <Link href={`/skills/${encodeURIComponent(skill.id)}`}>{skill.name}</Link>
                            </span>
                            {skill.isSymlink ? (
                              <Tooltip content={skill.symlinkTarget ? relativizeHome(skill.symlinkTarget) : "target unresolved"}>
                                <Badge tone="neutral">domain skill</Badge>
                              </Tooltip>
                            ) : null}
                          </div>
                          {skill.description ? <p className={styles.skillDescription}>{skill.description}</p> : null}
                          {skill.isSymlink ? (
                            <p className={styles.symlinkTarget}>→ {skill.symlinkTarget ? relativizeHome(skill.symlinkTarget) : "unresolved target"}</p>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </Card>
                );
              })}
            </div>
          </Section>

          <Section
            eyebrow="This repo"
            title={`Repo skills (${data.repoSkills.skills.length})`}
            description="Structured skill files that live in OmniAgentOS/skills, outside ~/.claude/skills."
          >
            {data.repoSkills.source === "error" ? (
              <p className={styles.unavailable}>unavailable: {data.repoSkills.error}</p>
            ) : null}
            <Card>
              <ul className={styles.skillList}>
                {data.repoSkills.skills.map((skill) => (
                  <li key={skill.id}>
                    <div className={styles.skillRow}>
                      <span className={styles.skillName}>{skill.name}</span>
                      <Badge tone="neutral">{skill.dialect === "frontmatter" ? "structured" : "plain prompt"}</Badge>
                    </div>
                    {skill.summary ? <p className={styles.skillDescription}>{skill.summary}</p> : null}
                    <div className={styles.repoSkillMeta}>
                      {skill.status ? <Badge tone={skill.status === "active" ? "ok" : "neutral"}>{skill.status}</Badge> : null}
                      <span className={styles.symlinkTarget}>{relativizeHome(skill.path)}</span>
                    </div>
                  </li>
                ))}
                {data.repoSkills.skills.length === 0 && data.repoSkills.source === "live" ? (
                  <li className={styles.empty}>No repo skills found.</li>
                ) : null}
              </ul>
            </Card>
          </Section>

          <Section
            eyebrow="Database"
            title={`Dormant DB seed rows (${data.dormant.seeds.length})`}
            description="dormant — no content digest, not injected into any live selection path. Shown for transparency, not as part of the live library."
            className={styles.dormantSection}
          >
            {data.dormant.source === "error" ? (
              <p className={styles.unavailable}>unavailable: {data.dormant.error}</p>
            ) : null}
            <Card>
              <ul className={styles.dormantList}>
                {data.dormant.seeds.map((seed) => (
                  <li key={seed.id}>
                    <span className={styles.skillName}>{seed.id}</span>
                    <Badge tone="neutral">{seed.category}</Badge>
                    <Badge tone="warn">{seed.status}</Badge>
                  </li>
                ))}
                {data.dormant.seeds.length === 0 && data.dormant.source === "live" ? (
                  <li className={styles.empty}>No dormant seed rows found.</li>
                ) : null}
              </ul>
            </Card>
          </Section>
        </>
      ) : null}
    </Page>
  );
}
