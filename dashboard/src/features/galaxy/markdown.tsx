"use client";

/**
 * Minimal, SAFE markdown renderer — no heavy markdown npm dependency (per brief). Parses
 * a small block+inline subset (headings, paragraphs, lists, blockquotes, code/fences,
 * tables, hr, bold/italic/inline-code/links) directly into React elements. There is no
 * `dangerouslySetInnerHTML` anywhere in this module — every character of note content
 * flows through JSX text children, so there is no raw-HTML-injection surface to sanitize
 * in the first place. `[[wikilink]]` tokens resolve via a caller-supplied resolver into
 * in-app <Link> navigation (or inert text when unresolved) — see NoteReader.tsx.
 */

import Link from "next/link";
import { Fragment, useMemo, type ReactNode } from "react";
import { vaultHref } from "./paths";

export type WikilinkResolver = (target: string) => { path: string; title: string } | null;

type Block =
  | { kind: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "blockquote"; text: string }
  | { kind: "code"; lang: string; code: string }
  | { kind: "hr" }
  | { kind: "table"; header: string[]; align: Array<"left" | "center" | "right" | null>; rows: string[][] };

function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line) && line.includes("-");
}

function isBlockStart(line: string): boolean {
  return (
    /^(#{1,6})\s+/.test(line) ||
    /^\s*[-*]\s+/.test(line) ||
    /^\s*\d+\.\s+/.test(line) ||
    /^\s*>/.test(line) ||
    /^\s*```/.test(line) ||
    /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
    (line.includes("|") && line.trim().length > 0)
  );
}

function parseBlocks(md: string): Block[] {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() === "") {
      i++;
      continue;
    }

    const fence = /^\s*```\s*([\w.+-]*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] ?? "";
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i] ?? "")) {
        codeLines.push(lines[i] ?? "");
        i++;
      }
      i++; // skip closing fence (tolerates an unterminated fence at EOF)
      blocks.push({ kind: "code", lang, code: codeLines.join("\n") });
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1]!.length as 1 | 2 | 3 | 4 | 5 | 6, text: heading[2]!.trim() });
      i++;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ kind: "hr" });
      i++;
      continue;
    }

    if (line.includes("|") && isTableSeparator(lines[i + 1] ?? "")) {
      const header = splitTableRow(line);
      const align = splitTableRow(lines[i + 1] ?? "").map((cell): "left" | "center" | "right" | null => {
        const l = cell.startsWith(":");
        const r = cell.endsWith(":");
        if (l && r) return "center";
        if (r) return "right";
        if (l) return "left";
        return null;
      });
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && (lines[i] ?? "").includes("|") && (lines[i] ?? "").trim() !== "") {
        rows.push(splitTableRow(lines[i]!));
        i++;
      }
      blocks.push({ kind: "table", header, align, rows });
      continue;
    }

    if (/^\s*>/.test(line)) {
      const qLines: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i] ?? "")) {
        qLines.push((lines[i] ?? "").replace(/^\s*>\s?/, ""));
        i++;
      }
      blocks.push({ kind: "blockquote", text: qLines.join(" ").trim() });
      continue;
    }

    const ulMatch = /^\s*[-*]\s+(.*)$/.exec(line);
    const olMatch = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (ulMatch || olMatch) {
      const ordered = Boolean(olMatch);
      const items: string[] = [(ulMatch ?? olMatch)![1]!];
      i++;
      const itemRe = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
      while (i < lines.length) {
        const m = itemRe.exec(lines[i] ?? "");
        if (!m) break;
        items.push(m[1]!);
        i++;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    const paraLines: string[] = [line.trim()];
    i++;
    while (i < lines.length && (lines[i] ?? "").trim() !== "" && !isBlockStart(lines[i] ?? "")) {
      paraLines.push((lines[i] ?? "").trim());
      i++;
    }
    blocks.push({ kind: "paragraph", text: paraLines.join(" ") });
  }

  return blocks;
}

const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\[\[[^[\]]+\]\])|(\[[^[\]]+\]\([^\s)]+\))|(\*[^*]+\*)|(_[^_]+_)/g;

function parseInline(text: string, key: string, resolve: WikilinkResolver): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let idx = 0;
  // A per-call regex instance, NOT the shared module-level one: parseInline
  // recurses for **bold**/*em* tokens, and a recursive call exhausting a shared
  // /g regex resets its lastIndex to 0 — the outer loop then re-matches its
  // current token forever (main-thread wedge on any note containing bold).
  const inlineRe = new RegExp(INLINE_RE.source, "g");
  let m: RegExpExecArray | null = inlineRe.exec(text);
  while (m) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const token = m[0];
    const k = `${key}-${idx++}`;
    if (token.startsWith("`")) {
      out.push(
        <code key={k} className="mono" style={{ background: "var(--overlay)", padding: "0.1em 0.35em", borderRadius: "var(--radius-sm)", fontSize: "0.92em" }}>
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("**") || token.startsWith("__")) {
      out.push(<strong key={k}>{parseInline(token.slice(2, -2), k, resolve)}</strong>);
    } else if (token.startsWith("[[")) {
      const inner = token.slice(2, -2);
      const bar = inner.indexOf("|");
      const targetRaw = bar >= 0 ? inner.slice(0, bar) : inner;
      const label = (bar >= 0 ? inner.slice(bar + 1) : inner).trim();
      const target = targetRaw.trim();
      const resolved = resolve(target);
      out.push(
        resolved ? (
          <Link
            key={k}
            href={vaultHref(resolved.path)}
            style={{ color: "var(--accent)", textDecoration: "none", borderBottom: "1px solid var(--accent-ring)" }}
          >
            {label}
          </Link>
        ) : (
          <span
            key={k}
            title={`No vault note resolves "${target}"`}
            style={{ color: "var(--text-faint)", borderBottom: "1px dashed var(--border-strong)", cursor: "help" }}
          >
            {label}
          </span>
        ),
      );
    } else if (token.startsWith("[")) {
      const linkMatch = /^\[([^[\]]+)\]\(([^\s)]+)\)$/.exec(token);
      if (linkMatch) {
        out.push(
          <a key={k} href={linkMatch[2]} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>
            {linkMatch[1]}
          </a>,
        );
      } else {
        out.push(token);
      }
    } else if (token.startsWith("*") || token.startsWith("_")) {
      out.push(<em key={k}>{parseInline(token.slice(1, -1), k, resolve)}</em>);
    }
    last = m.index + token.length;
    m = inlineRe.exec(text);
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderBlock(block: Block, key: number, resolve: WikilinkResolver): ReactNode {
  const k = `b${key}`;
  switch (block.kind) {
    case "heading": {
      const Tag = `h${block.level}` as const;
      const sizeVar =
        block.level === 1 ? "var(--text-h1)" : block.level === 2 ? "var(--text-h2)" : "var(--text-h3)";
      return (
        <Tag
          key={k}
          style={{
            fontSize: sizeVar,
            fontWeight: 600,
            letterSpacing: "-0.01em",
            margin: block.level === 1 ? "0 0 var(--space-3)" : "var(--space-5) 0 var(--space-2)",
            color: "var(--text)",
          }}
        >
          {parseInline(block.text, k, resolve)}
        </Tag>
      );
    }
    case "paragraph":
      return (
        <p key={k} style={{ margin: "0 0 var(--space-3)", lineHeight: 1.7, color: "var(--text)" }}>
          {parseInline(block.text, k, resolve)}
        </p>
      );
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag key={k} style={{ margin: "0 0 var(--space-3)", paddingLeft: "1.4em", lineHeight: 1.7, color: "var(--text)" }}>
          {block.items.map((item, i) => (
            <li key={i}>{parseInline(item, `${k}-${i}`, resolve)}</li>
          ))}
        </Tag>
      );
    }
    case "blockquote":
      return (
        <blockquote
          key={k}
          style={{
            margin: "0 0 var(--space-3)",
            padding: "var(--space-1) var(--space-4)",
            borderLeft: "3px solid var(--border-strong)",
            color: "var(--text-muted)",
          }}
        >
          {parseInline(block.text, k, resolve)}
        </blockquote>
      );
    case "code":
      return (
        <pre
          key={k}
          className="mono"
          style={{
            margin: "0 0 var(--space-3)",
            padding: "var(--space-3)",
            background: "var(--overlay)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-md)",
            overflowX: "auto",
            fontSize: "var(--text-small)",
            lineHeight: 1.6,
          }}
        >
          <code>{block.code}</code>
        </pre>
      );
    case "hr":
      return <hr key={k} style={{ border: "none", borderTop: "1px solid var(--border)", margin: "var(--space-5) 0" }} />;
    case "table":
      return (
        <div key={k} style={{ overflowX: "auto", margin: "0 0 var(--space-3)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--text-small)" }}>
            <thead>
              <tr>
                {block.header.map((cell, i) => (
                  <th
                    key={i}
                    style={{
                      textAlign: block.align[i] ?? "left",
                      borderBottom: "1px solid var(--border-strong)",
                      padding: "var(--space-2) var(--space-3)",
                      color: "var(--text-muted)",
                      fontWeight: 600,
                    }}
                  >
                    {parseInline(cell, `${k}-h${i}`, resolve)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, ri) => (
                <tr key={ri}>
                  {row.map((cell, ci) => (
                    <td
                      key={ci}
                      style={{
                        textAlign: block.align[ci] ?? "left",
                        borderBottom: "1px solid var(--border)",
                        padding: "var(--space-2) var(--space-3)",
                        color: "var(--text)",
                      }}
                    >
                      {parseInline(cell, `${k}-${ri}-${ci}`, resolve)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return null;
  }
}

/** Strip a single leading H1 that duplicates a title already shown in a header. */
export function stripLeadingH1(markdown: string, title: string): string {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length && (lines[i] ?? "").trim() === "") i++;
  const m = /^#\s+(.*)$/.exec((lines[i] ?? "").trim());
  if (m && m[1]!.trim().toLowerCase() === title.trim().toLowerCase()) {
    return lines
      .slice(i + 1)
      .join("\n")
      .replace(/^\n+/, "");
  }
  return markdown;
}

export interface MarkdownBodyProps {
  markdown: string;
  resolveWikilink: WikilinkResolver;
}

export function MarkdownBody({ markdown, resolveWikilink }: MarkdownBodyProps) {
  const blocks = useMemo(() => parseBlocks(markdown), [markdown]);
  if (blocks.length === 0) return null;
  return (
    <div style={{ fontSize: "var(--text-body)" }}>
      {blocks.map((block, i) => (
        <Fragment key={i}>{renderBlock(block, i, resolveWikilink)}</Fragment>
      ))}
    </div>
  );
}
