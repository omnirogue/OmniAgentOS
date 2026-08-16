"use client";

import { useMemo, useState } from "react";
import { Badge, EmptyState, Icon } from "@/design";
import type { SystemSkill } from "./types";
import { PathChip } from "./shared";
import styles from "./system.module.css";

export function SkillsPanel({
  skills,
  categories,
  skillsDir,
}: {
  skills: SystemSkill[];
  categories: string[];
  skillsDir: string;
}) {
  const [category, setCategory] = useState("");

  const rows = useMemo(
    () => (category ? skills.filter((s) => s.category === category) : skills),
    [skills, category],
  );

  if (!skills.length) {
    return (
      <EmptyState
        icon={<Icon name="hash" size={22} />}
        title="No skills found"
        message={`No SKILL.md files under ${skillsDir}.`}
      />
    );
  }

  return (
    <div>
      {categories.length > 1 ? (
        <div className={styles.chipRow}>
          <button
            type="button"
            className={category === "" ? `${styles.chip} ${styles.chipActive}` : styles.chip}
            onClick={() => setCategory("")}
          >
            all ({skills.length})
          </button>
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              className={category === c ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              onClick={() => setCategory(c)}
            >
              {c} ({skills.filter((s) => s.category === c).length})
            </button>
          ))}
        </div>
      ) : null}

      <div className={styles.timeline}>
        {rows.map((skill) => (
          <div key={skill.name} className={styles.loopCard}>
            <div className={styles.loopHead}>
              <span className={styles.loopLabel}>{skill.name}</span>
              {skill.category !== "uncategorized" ? <Badge tone="neutral">{skill.category}</Badge> : null}
            </div>
            <p className={styles.logSummary}>{skill.description || "No description in SKILL.md frontmatter."}</p>
            <div className={styles.loopMeta}>
              <PathChip path={skill.path} label="edit this file" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
