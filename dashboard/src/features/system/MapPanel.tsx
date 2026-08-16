"use client";

import { Badge, EmptyState, Icon, Section } from "@/design";
import { MarkdownBody } from "@/features/galaxy";
import type { SystemMap } from "./types";
import { EditableNote, PathChip, fmtDate } from "./shared";
import styles from "./system.module.css";

/**
 * System Map panel. Mermaid is NOT a dashboard dependency (checked package.json),
 * so the .mmd is shown as its styled source with a copy-path affordance and a note
 * that the morning archi job also writes a readable system-map.md — rather than
 * pulling in a heavy renderer. Below it, ARCHI.md renders through the app's own
 * lightweight markdown renderer (features/galaxy MarkdownBody), the same one the
 * vault note reader uses.
 */
export function MapPanel({ map }: { map: SystemMap }) {
  // Strip the archdocs stamp / HTML comments so the rendered prose stays clean.
  const archiClean = map.archi_md ? map.archi_md.replace(/<!--[\s\S]*?-->/g, "").trim() : "";

  return (
    <div>
      <Section
        title="System diagram"
        description="The generated architecture map. Edit the source file and the morning archi job regenerates it."
      >
        {map.diagram_mmd ? (
          <>
            <div className={styles.metaRow}>
              <PathChip path={map.diagram_path} label="source" />
              {map.generated_at ? (
                <Badge tone="neutral">generated {fmtDate(map.generated_at)}</Badge>
              ) : null}
            </div>
            <pre className={styles.mmdSource} aria-label="Mermaid diagram source">
              {map.diagram_mmd}
            </pre>
            <EditableNote>
              Rendered as source (no diagram renderer bundled). The morning archi job also
              writes a readable <code>system-map.md</code> alongside this file.
            </EditableNote>
          </>
        ) : (
          <EmptyState
            icon={<Icon name="grid" size={22} />}
            title="No diagram yet"
            message="docs/architecture/system-map.mmd has not been generated. The morning archi job (python -m omniagentos.archdocs) writes it."
          />
        )}
      </Section>

      <Section
        title="ARCHI.md"
        description="The living architecture map — subsystems, entry points, routes, how to extend."
        actions={map.generated_at ? <Badge tone="neutral">stamp {fmtDate(map.generated_at)}</Badge> : undefined}
      >
        {archiClean ? (
          <>
            <div className={styles.metaRow}>
              <PathChip path={map.archi_path} label="file" />
            </div>
            <div className={styles.archiBody}>
              <MarkdownBody markdown={archiClean} resolveWikilink={() => null} />
            </div>
          </>
        ) : (
          <EmptyState
            icon={<Icon name="database" size={22} />}
            title="ARCHI.md not found"
            message="The repo has no ARCHI.md yet. Run python -m omniagentos.archdocs.generate to create it."
          />
        )}
      </Section>
    </div>
  );
}
