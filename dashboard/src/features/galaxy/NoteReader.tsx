"use client";

import Link from "next/link";
import { Badge, EmptyState, ErrorState, Icon, Loading, Pill } from "../../design";
import { Backlinks } from "./Backlinks";
import { backlinksFor, resolveWikilink, type VaultLinkIndex } from "./linkResolver";
import { MarkdownBody, stripLeadingH1, type WikilinkResolver } from "./markdown";
import { confidenceTone, noteTypeColor, noteTypeLabel, statusTone } from "./noteType";
import { vaultHref } from "./paths";
import type { FetchStatus, VaultNoteDetail, VaultTreeNote } from "./types";

function formatDate(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export interface NoteReaderProps {
  /** Vault-relative path of the note being viewed. */
  path: string;
  status: FetchStatus;
  detail: VaultNoteDetail | null;
  errorMessage?: string | null;
  onRetry?: () => void;
  /** Full note list, for backlink computation. */
  notes: VaultTreeNote[];
  linkIndex: VaultLinkIndex;
}

/**
 * Renders a vault note beautifully: frontmatter as a tidy badge header, body via the
 * lightweight safe markdown transform (wikilinks become in-app navigation), and a
 * backlinks section ("who links here").
 */
export function NoteReader({ path, status, detail, errorMessage, onRetry, notes, linkIndex }: NoteReaderProps) {
  if (status === "loading") {
    return <Loading variant="skeleton" lines={9} />;
  }
  if (status === "error") {
    return <ErrorState title="Could not load this note" message={errorMessage ?? "Unknown error."} onRetry={onRetry} />;
  }
  if (status === "empty" || !detail) {
    return <EmptyState icon={<Icon name="helpCircle" size={22} />} title="Note not found" message={`No vault note exists at "${path}".`} />;
  }

  const treeNote = linkIndex.byPath.get(path.toLowerCase()) ?? null;
  const title = treeNote?.title ?? detail.frontmatter.id ?? path;
  const resolveLink: WikilinkResolver = (target) => {
    const resolved = resolveWikilink(target, linkIndex);
    return resolved ? { path: resolved.path, title: resolved.title } : null;
  };
  const body = stripLeadingH1(detail.body, title);
  const backlinks = backlinksFor(path, notes, linkIndex);
  const fm = detail.frontmatter;
  const disciplineNote = fm.discipline ? resolveWikilink(fm.discipline, linkIndex) : null;
  const sourceRunNote = fm.source_run ? resolveWikilink(fm.source_run, linkIndex) : null;
  const supersedesNote = fm.supersedes ? resolveWikilink(fm.supersedes, linkIndex) : null;
  const created = formatDate(fm.created);

  return (
    <article>
      <header style={{ marginBottom: "var(--space-5)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", marginBottom: "var(--space-3)" }}>
          <span
            aria-hidden="true"
            style={{ width: "0.7rem", height: "0.7rem", borderRadius: "50%", background: noteTypeColor(fm.type), flexShrink: 0 }}
          />
          {/* h2, not h1: the page's h1 is the Vault PageHeader (app/vault/layout.tsx) —
              this is a named region (the open note) within that page, kept at the same
              visually-prominent size since it's effectively the document title being read. */}
          <h2 style={{ margin: 0, fontSize: "var(--text-h1)", letterSpacing: "-0.01em", color: "var(--text)" }}>{title}</h2>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)", alignItems: "center" }}>
          <Pill tone="neutral">{noteTypeLabel(fm.type)}</Pill>
          <Badge tone={statusTone(fm.status)}>{fm.status}</Badge>
          {fm.confidence ? <Badge tone={confidenceTone(fm.confidence)}>{fm.confidence} confidence</Badge> : null}
          {fm.discipline ? (
            disciplineNote ? (
              <Link href={vaultHref(disciplineNote.path)}>
                <Pill tone="accent">{fm.discipline}</Pill>
              </Link>
            ) : (
              <Pill tone="neutral">{fm.discipline}</Pill>
            )
          ) : null}
          {fm.source_run ? (
            sourceRunNote ? (
              <Link href={vaultHref(sourceRunNote.path)}>
                <Pill tone="neutral">from {fm.source_run}</Pill>
              </Link>
            ) : (
              <Pill tone="neutral">from {fm.source_run}</Pill>
            )
          ) : null}
          {fm.supersedes ? (
            supersedesNote ? (
              <Link href={vaultHref(supersedesNote.path)}>
                <Pill tone="warn">supersedes {fm.supersedes}</Pill>
              </Link>
            ) : (
              <Pill tone="warn">supersedes {fm.supersedes}</Pill>
            )
          ) : null}
          {created ? (
            <span title={fm.created ?? undefined} style={{ fontSize: "var(--text-micro)", color: "var(--text-faint)" }}>
              {created}
            </span>
          ) : null}
          <span className="mono" style={{ fontSize: "var(--text-micro)", color: "var(--text-faint)", marginLeft: "auto" }}>
            {fm.id}
          </span>
        </div>
      </header>

      <MarkdownBody markdown={body} resolveWikilink={resolveLink} />

      <footer style={{ marginTop: "var(--space-6)", paddingTop: "var(--space-4)", borderTop: "1px solid var(--border)" }}>
        <h2 style={{ fontSize: "var(--text-h3)", margin: "0 0 var(--space-2)", color: "var(--text-muted)", fontWeight: 600 }}>
          Backlinks{backlinks.length > 0 ? ` (${backlinks.length})` : ""}
        </h2>
        <Backlinks backlinks={backlinks} />
      </footer>
    </article>
  );
}
