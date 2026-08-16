"""update_home (contracts/interfaces.md §p05).

Home.md is a lead-owned skeleton (contracts/interfaces.md: "Home.md — dashboard
note (lead-owned skeleton; p05 updater refreshes stats block)"). update_home
rewrites ONLY the text between the `<!-- omniagentos:status:begin -->` and
`<!-- omniagentos:status:end -->` markers — everything else in the file
(frontmatter, Map section, any human-authored "## Notes (human)" section) is
left byte-for-byte untouched, by construction. It never creates Home.md and
never touches git (no `autocommit` parameter in the frozen signature).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.vault.errors import VaultError

STATUS_BEGIN = "<!-- omniagentos:status:begin -->"
STATUS_END = "<!-- omniagentos:status:end -->"


def update_home(vault_dir: str, stats: dict[str, Any]) -> None:
    """Rewrite the status block of `<vault_dir>/Home.md` from `stats`.

    Raises VaultError if Home.md does not exist, or does not contain both
    status markers.
    """
    home_path = Path(vault_dir) / "Home.md"
    if not home_path.is_file():
        raise VaultError(f"{home_path} does not exist (Home.md is a lead-owned skeleton)")

    content = home_path.read_text(encoding="utf-8")
    start = content.find(STATUS_BEGIN)
    end = content.find(STATUS_END)
    if start == -1 or end == -1 or end < start:
        raise VaultError(f"{home_path} is missing the {STATUS_BEGIN}/{STATUS_END} markers")

    block = _render_status_block(stats)
    new_content = content[: start + len(STATUS_BEGIN)] + "\n" + block + "\n" + content[end:]
    home_path.write_text(new_content, encoding="utf-8")


def _render_status_block(stats: dict[str, Any]) -> str:
    if not stats:
        return "_No runs recorded yet._"
    lines = [f"_Last updated: {utc_now_iso()}_", ""]
    for key, value in stats.items():
        lines.append(f"- **{_label(key)}:** {_format_value(value)}")
    return "\n".join(lines)


def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _format_value(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items()) if value else "_none_"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value) if value else "_none_"
    if value is None:
        return "_none_"
    return str(value)
