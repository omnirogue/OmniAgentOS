"""Parse Obsidian markdown notes into graph nodes and edges.

The parser is deliberately *lenient* about frontmatter (unlike
``omniagentos.vault.frontmatter.parse_frontmatter``, which enforces the frozen
8-field contract): a hand-edited or partial note must never crash a graph build.
We extract only what the graph needs — the note id, its type, its title, its
aliases, and its outbound references (`[[wiki-links]]` + a small set of
frontmatter ref fields).

Robustness rules baked in here:

* Identity is Unicode-normalized (NFC + casefold) so an NFD ``[[Café]]`` link and
  a composed ``Café.md`` file resolve to the same node.
* `[[wiki-links]]` inside fenced/inline code and HTML comments are *not* edges.
* Frontmatter is size- and nesting-bounded so a pathological YAML document can
  never blow the vault build up with a ``RecursionError``.
* Vault iteration never follows a symlink outside the vault root, and generated
  MOC notes are excluded so derived views don't feed back into the graph.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vaultgraph.contracts import MOC_DIR, MOC_MARKER

_log = logging.getLogger(__name__)

# `[[target]]`, `[[target|alias]]`, `[[target#heading]]`, `[[target\|alias]]`
# (the backslash-pipe form appears inside markdown tables in the real vault).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<yaml>.*?\r?\n)---\r?\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)

# Non-rendered regions whose `[[...]]` must never become edges (F9).
_FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*(```+|~~~+).*?(?:^[ \t]*\1[ \t]*$|\Z)")
_HTML_COMMENT_RE = re.compile(r"(?s)<!--.*?(?:-->|\Z)")
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")

# Resource bounds — a single malformed note must never abort the whole build (F5).
MAX_NOTE_BYTES = 5_000_000
MAX_FRONTMATTER_BYTES = 256_000
MAX_YAML_NESTING = 64

# Frontmatter fields whose values name another note (become structured edges).
FRONTMATTER_REF_FIELDS: tuple[str, ...] = ("supersedes", "discipline", "source_run")


def _nfc_casefold(text: str) -> str:
    """Canonicalize text for identity/matching: NFC compose then Unicode
    casefold, so ``Café`` (NFD or NFC) and ``CAFÉ`` collapse to one key (F6)."""
    return unicodedata.normalize("NFC", text).casefold()


def slugify_target(target: str) -> str:
    """Normalize a link/note target to its *basename* graph key.

    Obsidian resolves `[[links]]` by note name, so we key on the basename, drop
    any `#heading` / `^block` anchor, Unicode-normalize (NFC + casefold), and
    collapse internal whitespace. `[[capabilities/web-research#Facts]]` ->
    ``web-research``.
    """
    text = target.strip()
    text = text.split("#", 1)[0].split("^", 1)[0]
    text = text.rsplit("/", 1)[-1]
    return " ".join(_nfc_casefold(text).split()).strip()


def slugify_path(target: str) -> str:
    """Normalize a *path-qualified* target, preserving folder structure.

    ``one/Shared#H`` -> ``one/shared``. Used for Obsidian's path-qualified link
    resolution (``[[folder/Note]]``), which must win over a bare basename match
    when two notes share a filename (F4).
    """
    text = target.strip().split("#", 1)[0].split("^", 1)[0]
    parts = [" ".join(_nfc_casefold(p).split()).strip() for p in text.split("/")]
    return "/".join(p for p in parts if p)


def path_slug_of(relpath: str) -> str:
    """The canonical path-qualified slug of a note file: its relpath sans
    extension, each segment normalized. ``one/Shared.md`` -> ``one/shared``."""
    p = Path(relpath)
    segments = [*p.parts[:-1], p.stem]
    return "/".join(" ".join(_nfc_casefold(s).split()).strip() for s in segments if s)


@dataclass(frozen=True, slots=True)
class LinkRef:
    target: str  # basename key (slugify_target)
    alias: str | None
    kind: str  # EDGE_WIKILINK or "frontmatter:<field>"
    path_hint: str | None = None  # path-qualified slug when the link named a path


@dataclass(slots=True)
class ParsedNote:
    id: str  # preferred key: frontmatter id (slug) if present, else stem slug
    title: str
    ntype: str
    relpath: str
    refs: list[LinkRef] = field(default_factory=list)
    frontmatter_id: str | None = None  # slug of an explicit frontmatter ``id``
    aliases: tuple[str, ...] = ()  # slugified frontmatter aliases
    body: str = ""  # not retained after a vault walk (memory, F7); "" by default

    @property
    def stem_slug(self) -> str:
        return slugify_target(Path(self.relpath).stem)

    @property
    def path_slug(self) -> str:
        return path_slug_of(self.relpath)


def _mask_non_content(text: str) -> str:
    """Blank out fenced code, inline code, and HTML comments (preserving length
    so offsets are unchanged) so their `[[examples]]` are not parsed as edges."""

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    text = _FENCED_CODE_RE.sub(_blank, text)
    text = _HTML_COMMENT_RE.sub(_blank, text)
    text = _INLINE_CODE_RE.sub(_blank, text)
    return text


def _split_wikilink(inner: str) -> tuple[str, str | None]:
    """Split the inside of a `[[...]]` into (target, alias), tolerating the
    escaped ``\\|`` pipe used inside markdown tables."""
    normalized = inner.replace("\\|", "|")
    if "|" in normalized:
        target, alias = normalized.split("|", 1)
        return target.strip(), alias.strip() or None
    return normalized.strip(), None


def extract_wikilinks(text: str) -> list[LinkRef]:
    """Return every `[[wiki-link]]` in ``text`` as a LinkRef (order preserved,
    duplicates kept — the caller de-dupes at edge-insert time).

    Links inside code fences, inline code, and HTML comments are ignored (F9).
    """
    from omniagentos.vaultgraph.contracts import EDGE_WIKILINK

    masked = _mask_non_content(text)
    refs: list[LinkRef] = []
    for match in _WIKILINK_RE.finditer(masked):
        raw_target, alias = _split_wikilink(match.group(1))
        slug = slugify_target(raw_target)
        if not slug:
            continue
        path_hint = slugify_path(raw_target) if "/" in raw_target else None
        if path_hint == slug:
            path_hint = None
        refs.append(LinkRef(target=slug, alias=alias, kind=EDGE_WIKILINK, path_hint=path_hint))
    return refs


def _max_bracket_nesting(text: str) -> int:
    """Cheap upper bound on YAML flow-collection nesting depth — used to reject a
    pathological ``[[[[...`` document *before* ``yaml.safe_load`` recurses on it
    and raises ``RecursionError`` (F5)."""
    depth = max_depth = 0
    for ch in text:
        if ch in "[{":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch in "]}":
            depth -= 1
    return max_depth


def _lenient_frontmatter(content: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    raw_yaml = match.group("yaml")
    # Resource guards: an oversized or deeply nested frontmatter is quarantined
    # (treated as no frontmatter) rather than allowed to exhaust the interpreter.
    if len(raw_yaml.encode("utf-8", "ignore")) > MAX_FRONTMATTER_BYTES:
        _log.warning("vaultgraph: skipping oversized frontmatter (%d bytes)", len(raw_yaml))
        return {}
    if _max_bracket_nesting(raw_yaml) > MAX_YAML_NESTING:
        _log.warning("vaultgraph: skipping over-nested frontmatter")
        return {}
    try:
        parsed = yaml.safe_load(raw_yaml)
    except (yaml.YAMLError, RecursionError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_after_frontmatter(content: str) -> str:
    match = _FRONTMATTER_RE.match(content)
    return content[match.end() :] if match else content


def _title_of(body: str, fallback: str) -> str:
    match = _HEADING_RE.search(body)
    return match.group("title").strip() if match else fallback


def _frontmatter_aliases(fm: dict[str, Any]) -> tuple[str, ...]:
    raw = fm.get("aliases") or fm.get("alias")
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in items:
        slug = slugify_target(str(item))
        if slug:
            out.append(slug)
    return tuple(dict.fromkeys(out))


def parse_note(relpath: str, content: str, *, keep_body: bool = False) -> ParsedNote:
    """Parse one note's raw markdown into a ParsedNote.

    The preferred key is the frontmatter ``id`` when present, else the file stem
    (matching how the vault's own `[[links]]` address notes). Final canonical
    identity and link resolution happen in graph construction, which has the
    cross-note view needed to detect collisions. Frontmatter ref fields
    (supersedes / discipline / source_run) become structured edges.

    ``keep_body`` retains the note body on the result; the vault walk leaves it
    off so a multi-gigabyte vault does not have to hold every note in memory (F7).
    """
    from omniagentos.vaultgraph.contracts import EDGE_FRONTMATTER

    fm = _lenient_frontmatter(content)
    body = _body_after_frontmatter(content)
    stem = Path(relpath).stem

    raw_id = fm.get("id")
    fm_id_slug = slugify_target(str(raw_id)) if raw_id else None
    node_id = fm_id_slug or slugify_target(stem)
    ntype = str(fm.get("type") or "note")
    title = _title_of(body, str(raw_id) if raw_id else stem)

    refs = extract_wikilinks(body)
    for fieldname in FRONTMATTER_REF_FIELDS:
        value = fm.get(fieldname)
        if not value:
            continue
        for item in value if isinstance(value, list) else [value]:
            raw_item = str(item)
            slug = slugify_target(raw_item)
            if slug and slug != node_id:
                path_hint = slugify_path(raw_item) if "/" in raw_item else None
                if path_hint == slug:
                    path_hint = None
                refs.append(
                    LinkRef(
                        target=slug,
                        alias=None,
                        kind=f"{EDGE_FRONTMATTER}:{fieldname}",
                        path_hint=path_hint,
                    )
                )

    return ParsedNote(
        id=node_id,
        title=title,
        ntype=ntype,
        relpath=relpath,
        refs=refs,
        frontmatter_id=fm_id_slug,
        aliases=_frontmatter_aliases(fm),
        body=body if keep_body else "",
    )


def _is_generated_moc(content: str) -> bool:
    """A generated Map-of-Content note carries a marker; those are derived views
    and must not be re-ingested as source nodes on the next rebuild (F3)."""
    return MOC_MARKER in content[:2000]


def _iter_note_paths(root: Path) -> list[Path]:
    """Enumerate ``*.md`` notes under ``root`` deterministically, excluding the
    generated ``moc/`` directory (F3) and never following a symlink whose target
    escapes the vault (F8). Only lightweight path objects are held here — note
    bodies are streamed and discarded during parsing (F7)."""
    root_resolved = root.resolve()
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        # Prune the generated MOC directory outright.
        dirnames[:] = sorted(d for d in dirnames if not (rel_dir == Path(".") and d == MOC_DIR))
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                _log.warning("vaultgraph: skipping symlinked note %s", candidate)
                continue
            try:
                if inode_relative_parts_anchored(candidate.resolve(), root_resolved) is None:
                    _log.warning("vaultgraph: skipping out-of-vault note %s", candidate)
                    continue
            except OSError:
                continue
            paths.append(candidate)
    return sorted(paths)


def walk_vault(vault_dir: str | Path) -> list[ParsedNote]:
    """Parse every ``*.md`` note under ``vault_dir`` (recursively, deterministic).

    Reads are streamed and note bodies discarded after metadata/reference
    extraction (F7). Unreadable, oversized, malformed, symlinked-outside, and
    generated-MOC files are skipped with a diagnostic rather than aborting the
    walk (F3, F5, F8)."""
    root = Path(vault_dir)
    notes: list[ParsedNote] = []
    for path in _iter_note_paths(root):
        try:
            if path.stat().st_size > MAX_NOTE_BYTES:
                _log.warning("vaultgraph: skipping oversized note %s", path)
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _is_generated_moc(content):
            continue
        relpath = str(path.relative_to(root))
        try:
            notes.append(parse_note(relpath, content))
        except (RecursionError, ValueError, yaml.YAMLError) as exc:
            _log.warning("vaultgraph: skipping malformed note %s (%s)", relpath, exc)
            continue
    return notes
