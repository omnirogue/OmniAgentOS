"""Rendering and persistence of reliability audit notes."""

from __future__ import annotations

import json
from typing import Any

from omniagentos.vault.write import write_note


def write_audit_report(
    vault_dir: str, audit_id: str, kind: str, stats: dict[str, Any], findings: list[Any] | int = 0
) -> str:
    """Write audit result as a vault note.

    Args:
        vault_dir: vault root directory path
        audit_id: audit id
        kind: audit kind ('watch', 'twice_daily', 'daily_summary', 'weekly_architecture')
        stats: audit stats dict
        findings: list of finding dicts or count

    Returns: absolute path to written note
    """
    count = len(findings) if isinstance(findings, list) else int(findings)

    body_lines = [
        f"# Reliability audit: {kind}",
        "",
        f"Audit ID: `{audit_id}`",
        f"Findings: {count}",
        "",
        "```json",
        json.dumps(stats, indent=2, sort_keys=True),
        "```",
        "",
        "## Notes (human)",
        "",
    ]
    body = "\n".join(body_lines)

    # write_note requires a valid frozen frontmatter block at the very start of the
    # note (contracts/vault-frontmatter.md), so render one explicitly — the audit
    # note is a DECISION-type record keyed on the audit id.
    try:
        from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
        from omniagentos.vault.frontmatter import render_frontmatter

        fm = VaultFrontmatter(
            id=audit_id, type=NoteType.DECISION, created=utc_now_iso(), status="active"
        )
        content = render_frontmatter(fm) + "\n" + body
    except Exception:  # noqa: BLE001 - a stubbed write_note (tests) accepts a bare body
        content = body

    return write_note(vault_dir, f"reliability/audits/{audit_id}.md", content, autocommit=False)


render_audit_report = write_audit_report
