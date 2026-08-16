"""Parse a checksum-bound automation pack into closed typed records.

The pack is hostile input. This parser therefore:

* never evaluates, executes or renders anything it reads;
* treats fenced blocks as opaque payload, so a ``### A1.`` line inside a code
  fence cannot forge an item;
* refuses on an unterminated fence, a duplicate item id or an oversized
  document rather than silently truncating;
* stores every span as :class:`UntrustedText`, so prose cannot reach an
  instruction position by accident;
* records prompt-injection markers and self-describing permission claims as
  EVIDENCE for the classifier, not as facts.

Nothing here decides anything. It only turns bytes into records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from omniagentos.packgovernance.checksum import matching_view
from omniagentos.packgovernance.contracts import (
    ArtifactKind,
    BoundArtifact,
    InjectionSignal,
    PackItem,
    PackSection,
    PromptPack,
    ProseClaim,
    UntrustedText,
)
from omniagentos.packgovernance.errors import PackParseError

__all__ = [
    "INJECTION_MARKERS",
    "MAX_PACK_LINES",
    "PERMISSION_PROSE_CLAIMS",
    "UNLABELED_PROSE_FIELD",
    "parse_pack",
    "scan_injection_markers",
]

MAX_PACK_LINES = 200_000
MAX_ITEMS = 2_000

# Prose field that collects bare (unlabeled) paragraph lines inside an item.
# Dropping them would silently discard exactly the prose the capability
# classifier scans, violating "nothing in the pack is silently discarded".
UNLABELED_PROSE_FIELD = "body"

_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*\S)[ \t]*$")
_ITEM_HEADING = re.compile(r"^(?P<id>[A-Z]{1,2}\d{1,3})\.[ \t]+(?P<title>.+)$")
_FENCE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`~]*)$")
_TABLE_ROW = re.compile(r"^\|(?P<cells>.*)\|[ \t]*$")
_PROSE_LABEL = re.compile(r"^\*\*(?P<label>[^*]+?):?\*\*:?[ \t]*(?P<body>.*)$")
_METADATA_KEY = re.compile(r"^\*\*(?P<key>.+?)\*\*$")
_CONSOLIDATED = re.compile(r"consolidated into (?P<target>[A-Z]{1,2}\d{1,3})", re.IGNORECASE)
_SCOPE_LINE = re.compile(r"\*\*Scope:\*\*\s*`(?P<repository>[^`]+)`")

# Markers that indicate an attempt to steer whatever agent reads this document.
# Presence is reported and forces the full pipeline; it is never itself a
# rejection, because a well-written pack legitimately quotes these in
# prohibitions ("NEVER --dangerously-skip-permissions on a production box").
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "disregard your instructions",
    "you are now",
    "new system prompt",
    "system prompt:",
    "override your",
    "approve everything",
    "approve all items",
    "grant all permissions",
    "--dangerously-skip-permissions",
    "bypass the review",
    "skip the review",
    "do not tell the operator",
    "without asking",
    "curl | sh",
    "curl | bash",
    "rm -rf /",
)

# Self-descriptions of permission. The operator ruled these are never load
# bearing: they are captured only so the classifier can contradict them.
PERMISSION_PROSE_CLAIMS: tuple[str, ...] = (
    "read-only",
    "read only",
    "advisory only",
    "propose-only",
    "propose only",
    "read-and-report only",
    "never pushes",
    "never push",
    "no auto-merge",
    "nothing auto-merges",
    "never modifies",
    "never auto-merged",
    "makes no code changes",
    "no llm",
)


@dataclass
class _ItemBuffer:
    item_id: str
    title: str
    line: int
    metadata: dict[str, UntrustedText] = field(default_factory=dict)
    prose: dict[str, UntrustedText] = field(default_factory=dict)
    code_blocks: list[tuple[str, UntrustedText]] = field(default_factory=list)


def scan_injection_markers(text: str, *, first_line: int = 1) -> tuple[InjectionSignal, ...]:
    """Report every known steering marker with its line and a bounded excerpt."""
    signals: list[InjectionSignal] = []
    for offset, line in enumerate(text.split("\n")):
        haystack = matching_view(line).casefold()
        for marker in INJECTION_MARKERS:
            if marker in haystack:
                signals.append(
                    InjectionSignal(
                        marker=marker,
                        line=first_line + offset,
                        excerpt=" ".join(line.split())[:160],
                    )
                )
    return tuple(signals)


def _scan_prose_claims(
    field_name: str, span: UntrustedText
) -> tuple[ProseClaim, ...]:
    haystack = matching_view(span.raw).casefold()
    claims: list[ProseClaim] = []
    for claim in PERMISSION_PROSE_CLAIMS:
        if claim in haystack:
            claims.append(
                ProseClaim(
                    claim=claim,
                    field_name=field_name,
                    line=span.line,
                    excerpt=span.excerpt(),
                )
            )
    return tuple(claims)


def _split_table_row(cells: str) -> list[str]:
    return [cell.strip() for cell in cells.split("|")]


def _flush_prose(buffer: _ItemBuffer | None, label: str | None, body: list[str], line: int) -> None:
    if buffer is None or label is None:
        return
    text = "\n".join(body).strip()
    if not text:
        return
    key = label.strip().casefold()
    existing = buffer.prose.get(key)
    if existing is not None:
        text = existing.raw + "\n" + text
        line = existing.line
    buffer.prose[key] = UntrustedText(raw=text, field_name=key, line=line)


def parse_pack(artifact: BoundArtifact) -> PromptPack:
    """Turn a bound pack artifact into a :class:`PromptPack`.

    Raises :class:`PackParseError` on any structure the parser cannot represent
    faithfully — an unterminated fence, a duplicate item id, or a document over
    the line/item ceilings.
    """
    if artifact.kind is not ArtifactKind.UNTRUSTED_PACK_FIXTURE:
        raise PackParseError(
            f"{artifact.path}: parse_pack requires an untrusted-pack-fixture artifact, "
            f"got {artifact.kind.value}"
        )

    lines = artifact.text.split("\n")
    if len(lines) > MAX_PACK_LINES:
        raise PackParseError(
            f"{artifact.path}: {len(lines)} lines exceeds the {MAX_PACK_LINES} line ceiling"
        )

    title: UntrustedText | None = None
    declared_repository: str | None = None
    sections: list[PackSection] = []
    items: list[_ItemBuffer] = []
    seen_ids: dict[str, int] = {}

    current_item: _ItemBuffer | None = None
    current_section: tuple[str, int, int] | None = None
    section_body: list[str] = []
    prose_label: str | None = None
    prose_body: list[str] = []
    prose_start = 0

    fence_char = ""
    fence_length = 0
    fence_info = ""
    fence_start = 0
    fence_body: list[str] = []

    in_metadata_table = False
    pending_metadata_key: str | None = None

    def close_section(end_line: int) -> None:
        nonlocal current_section, section_body
        if current_section is None:
            return
        heading, level, line_no = current_section
        body = "\n".join(section_body).strip()
        sections.append(
            PackSection(
                heading=heading,
                level=level,
                line=line_no,
                body=UntrustedText(raw=body, field_name="section", line=line_no),
            )
        )
        current_section = None
        section_body = []

    for index, line in enumerate(lines):
        line_no = index + 1

        if fence_char:
            closing = _FENCE.match(line)
            if (
                closing is not None
                and closing.group("fence")[0] == fence_char
                and len(closing.group("fence")) >= fence_length
                and not closing.group("info").strip()
            ):
                payload = "\n".join(fence_body)
                span = UntrustedText(raw=payload, field_name="code", line=fence_start)
                if current_item is not None:
                    current_item.code_blocks.append((fence_info, span))
                else:
                    section_body.append(payload)
                fence_char = ""
                fence_length = 0
                fence_info = ""
                fence_body = []
                continue
            fence_body.append(line)
            continue

        opening = _FENCE.match(line)
        if opening is not None:
            _flush_prose(current_item, prose_label, prose_body, prose_start)
            prose_label = None
            prose_body = []
            fence_char = opening.group("fence")[0]
            fence_length = len(opening.group("fence"))
            fence_info = opening.group("info").strip()
            fence_start = line_no
            fence_body = []
            in_metadata_table = False
            continue

        heading = _HEADING.match(line)
        if heading is not None:
            _flush_prose(current_item, prose_label, prose_body, prose_start)
            prose_label = None
            prose_body = []
            in_metadata_table = False
            pending_metadata_key = None
            level = len(heading.group("hashes"))
            text = heading.group("text").strip()

            if level == 1 and title is None:
                title = UntrustedText(raw=text, field_name="pack_title", line=line_no)
                close_section(line_no)
                continue

            item_match = _ITEM_HEADING.match(text)
            if item_match is not None:
                close_section(line_no)
                item_id = item_match.group("id")
                if item_id in seen_ids:
                    raise PackParseError(
                        f"{artifact.path}:{line_no}: duplicate pack item id {item_id!r} "
                        f"(first seen at line {seen_ids[item_id]})"
                    )
                if len(items) >= MAX_ITEMS:
                    raise PackParseError(
                        f"{artifact.path}:{line_no}: more than {MAX_ITEMS} pack items"
                    )
                seen_ids[item_id] = line_no
                current_item = _ItemBuffer(
                    item_id=item_id,
                    title=item_match.group("title").strip(),
                    line=line_no,
                )
                items.append(current_item)
                continue

            current_item = None
            close_section(line_no)
            current_section = (text, level, line_no)
            continue

        table = _TABLE_ROW.match(line)
        if table is not None and current_item is not None:
            cells = _split_table_row(table.group("cells"))
            if all(set(cell) <= set("-: ") for cell in cells):
                in_metadata_table = True
                continue
            if len(cells) >= 2:
                key_match = _METADATA_KEY.match(cells[0])
                if key_match is not None:
                    key = key_match.group("key").strip().casefold()
                    value = " ".join(cells[1:]).strip()
                    current_item.metadata[key] = UntrustedText(
                        raw=value, field_name=key, line=line_no
                    )
                    pending_metadata_key = key
                    in_metadata_table = True
                    continue
                if in_metadata_table and pending_metadata_key is not None and not cells[0]:
                    # Continuation row of a wrapped metadata cell.
                    previous = current_item.metadata[pending_metadata_key]
                    current_item.metadata[pending_metadata_key] = UntrustedText(
                        raw=previous.raw + " " + " ".join(cells[1:]).strip(),
                        field_name=previous.field_name,
                        line=previous.line,
                    )
                    continue
            in_metadata_table = True
            continue

        prose = _PROSE_LABEL.match(line.strip())
        if prose is not None and current_item is not None:
            _flush_prose(current_item, prose_label, prose_body, prose_start)
            prose_label = prose.group("label")
            prose_body = [prose.group("body").strip()]
            prose_start = line_no
            in_metadata_table = False
            continue

        if prose_label is not None:
            if line.strip():
                prose_body.append(line.rstrip())
            else:
                _flush_prose(current_item, prose_label, prose_body, prose_start)
                prose_label = None
                prose_body = []
            continue

        if current_item is None:
            section_body.append(line)
            if declared_repository is None:
                scope = _SCOPE_LINE.search(line)
                if scope is not None:
                    declared_repository = scope.group("repository").strip()
            continue

        # A bare paragraph line inside an item, outside any labeled prose block:
        # retained under UNLABELED_PROSE_FIELD so it still reaches all_text()
        # and the injection/claim scans instead of being silently discarded.
        if line.strip():
            _flush_prose(current_item, UNLABELED_PROSE_FIELD, [line.rstrip()], line_no)

    if fence_char:
        raise PackParseError(
            f"{artifact.path}:{fence_start}: unterminated {fence_char * fence_length} fence; "
            "refusing to parse a truncated pack"
        )
    _flush_prose(current_item, prose_label, prose_body, prose_start)
    close_section(len(lines))

    if title is None:
        raise PackParseError(f"{artifact.path}: pack has no level-1 title heading")
    if not items:
        raise PackParseError(f"{artifact.path}: pack declares no automation items")

    signals = scan_injection_markers(artifact.text)
    parsed_items = tuple(_finalize(buffer, signals) for buffer in items)

    return PromptPack(
        artifact=artifact,
        title=title,
        items=parsed_items,
        sections=tuple(sections),
        declared_repository=declared_repository,
        injection_signals=signals,
    )


def _finalize(buffer: _ItemBuffer, pack_signals: tuple[InjectionSignal, ...]) -> PackItem:
    title = UntrustedText(raw=buffer.title, field_name="title", line=buffer.line)
    consolidated = _CONSOLIDATED.search(matching_view(buffer.title))
    end_line = buffer.line
    for span in list(buffer.metadata.values()) + list(buffer.prose.values()):
        end_line = max(end_line, span.line)
    for _language, span in buffer.code_blocks:
        end_line = max(end_line, span.line + len(span.raw.split("\n")))

    claims: list[ProseClaim] = list(_scan_prose_claims("title", title))
    for key, span in buffer.metadata.items():
        claims.extend(_scan_prose_claims(key, span))
    for key, span in buffer.prose.items():
        claims.extend(_scan_prose_claims(key, span))

    item_signals = tuple(
        signal for signal in pack_signals if buffer.line <= signal.line <= end_line
    )

    return PackItem(
        item_id=buffer.item_id,
        title=title,
        line=buffer.line,
        metadata=dict(buffer.metadata),
        prose=dict(buffer.prose),
        code_blocks=tuple(buffer.code_blocks),
        consolidated_into=consolidated.group("target") if consolidated else None,
        injection_signals=item_signals,
        prose_claims=tuple(claims),
    )
