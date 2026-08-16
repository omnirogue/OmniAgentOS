"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Badge, Card, CodeBlock, ErrorState, Loading, Page, PageHeader } from "@/design";
import type { SkillDetailPayload } from "@/features/skills";
import styles from "@/features/skills/skills.module.css";

function isDetail(value: unknown): value is SkillDetailPayload {
  return Boolean(value && typeof value === "object" && "id" in value && "exists" in value);
}

/** /skills/[id] — rewired onto the local skills-extra route's `?id=` detail
 * mode (safe path-allowlist server-side), rendering the raw SKILL.md body.
 * The old route read the frozen-but-unpopulated S-A skills API
 * (GET /api/skills/{id}), which 404s for the ~46 of 55 ~/.claude/skills
 * entries that were never written into that DB table. */
export default function SkillDetailPage() {
  const params = useParams<{ id: string | string[] }>();
  const rawId = typeof params.id === "string" ? params.id : (params.id?.[0] ?? "");
  const id = decodeURIComponent(rawId);

  const [detail, setDetail] = useState<SkillDetailPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/local/skills-extra?id=${encodeURIComponent(id)}`, { cache: "no-store" });
      const json: unknown = await response.json();
      if (!isDetail(json)) throw new Error(`HTTP ${response.status}`);
      if (!json.exists) throw new Error(json.error ?? "skill not found");
      setDetail(json);
      setError(null);
    } catch (reason) {
      setDetail(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (loading && !detail) {
    return (
      <Page>
        <Card>
          <Loading label={`Loading ${id}…`} />
        </Card>
      </Page>
    );
  }

  if (error || !detail) {
    return (
      <Page>
        <ErrorState message={`Could not load "${id}": ${error ?? "unknown error"}`} onRetry={() => void refresh()} />
      </Page>
    );
  }

  return (
    <Page>
      <PageHeader eyebrow="Skill library" title={detail.name} lead={detail.description || undefined} />
      <div className={styles.detailMeta}>
        {detail.isSymlink ? <Badge tone="neutral">domain skill</Badge> : <Badge tone="neutral">repo skill</Badge>}
        {detail.symlinkTarget ? <span className={styles.symlinkTarget}>→ {detail.symlinkTarget}</span> : null}
      </div>
      <Card>
        <CodeBlock maxHeight="60rem">{detail.body}</CodeBlock>
      </Card>
    </Page>
  );
}
