"use client";

/**
 * Agent turns are authored in markdown; before this the thread printed the raw
 * source (literal `**bold**` and `- ` bullets). This renders them with the
 * existing dependency-free renderer from features/galaxy — no new npm package,
 * and no `dangerouslySetInnerHTML` anywhere in that module, so there is no
 * raw-HTML injection surface to sanitize.
 *
 * The same component renders the LIVE streaming buffer: the parser tolerates a
 * half-written document (an unterminated ``` fence at EOF is closed for you),
 * so a partial reply degrades to plain paragraphs instead of exploding.
 *
 * Chats have no vault, so `[[wikilinks]]` resolve to inert text.
 */

import { MarkdownBody } from "@/features/galaxy/markdown";

/** Stable identity: a new closure per render would rebuild every inline node. */
const noWikilinks = () => null;

export function ChatMarkdown({ text }: { text: string }) {
  return <MarkdownBody markdown={text} resolveWikilink={noWikilinks} />;
}
