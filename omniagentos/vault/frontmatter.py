"""Frontmatter serialization for vault notes (contracts/vault-frontmatter.md).

The frontmatter field set is FROZEN (G1 criterion B7): exactly the 8 fields of
`contracts.VaultFrontmatter`, in the order declared there, YAML via pyyaml, and
`null` for `None`. `parse_frontmatter` enforces the same exact set on the way
back in (no extras, no omissions) so a corrupt/hand-edited note fails loudly
rather than silently losing a field.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import yaml

from omniagentos.contracts import VaultFrontmatter
from omniagentos.vault.errors import VaultError

# The frozen field order (contracts/vault-frontmatter.md). Kept as an explicit
# tuple (rather than relying on VaultFrontmatter.model_fields ordering) because
# THIS order is what is actually frozen by the contract, independent of however
# contracts.py happens to declare the class.
FRONTMATTER_FIELDS: tuple[str, ...] = (
    "id",
    "type",
    "discipline",
    "created",
    "source_run",
    "confidence",
    "status",
    "supersedes",
)

_FRONTMATTER_FIELD_SET = frozenset(FRONTMATTER_FIELDS)

# Matches a leading `---\n...\n---\n` block at the very start of the note.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<yaml>.*?\r?\n)---\r?\n?", re.DOTALL)


def render_frontmatter(fm: VaultFrontmatter) -> str:
    """Render `fm` as the exact 8-field YAML frontmatter block, frozen field
    order, `null` for `None`. Returns the block INCLUDING the `---` fences and
    a trailing newline (ready to prefix directly onto a note body)."""
    data = fm.model_dump(mode="json")
    missing = _FRONTMATTER_FIELD_SET - set(data)
    if missing:  # pragma: no cover - only reachable if contracts.py drifts
        raise VaultError(f"VaultFrontmatter missing expected fields: {sorted(missing)}")
    ordered = {field: data[field] for field in FRONTMATTER_FIELDS}
    yaml_body = yaml.safe_dump(
        ordered,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_body}---\n"


def parse_frontmatter(content: str) -> VaultFrontmatter:
    """Parse the leading frontmatter block of `content` into a VaultFrontmatter.

    Raises VaultError if there is no frontmatter block, or if the parsed YAML
    does not have EXACTLY the 8 frozen fields (extras or omissions both fail).
    Round-trips render_frontmatter output: parse_frontmatter(render_frontmatter(fm)
    + body) == fm.

    Tolerates an UNQUOTED `created` timestamp (e.g. hand-written frontmatter,
    like vault/Home.md's lead-authored skeleton): pyyaml's implicit resolver
    parses a bare `2026-07-11T15:40:00Z` as a `datetime`, not a string, which
    is coerced back to the canonical string form before validation so a human
    editing frontmatter by hand doesn't need to know about YAML's timestamp
    quirk to avoid breaking the note.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise VaultError("no frontmatter block found at start of note content")
    raw = yaml.safe_load(match.group("yaml"))
    if not isinstance(raw, dict):
        raise VaultError("frontmatter block did not parse to a YAML mapping")
    field_set = set(raw)
    extra = field_set - _FRONTMATTER_FIELD_SET
    if extra:
        raise VaultError(f"unexpected frontmatter fields: {sorted(extra)}")
    missing = _FRONTMATTER_FIELD_SET - field_set
    if missing:
        raise VaultError(f"missing frontmatter fields: {sorted(missing)}")
    raw["created"] = _coerce_created(raw.get("created"))
    return VaultFrontmatter(**raw)


def _coerce_created(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%dT00:00:00Z")
    return value


def split_frontmatter(content: str) -> tuple[VaultFrontmatter, str]:
    """Parse the frontmatter block and return (frontmatter, remaining_body) where
    remaining_body is everything after the closing `---` fence."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise VaultError("no frontmatter block found at start of note content")
    fm = parse_frontmatter(content)
    return fm, content[match.end() :]
