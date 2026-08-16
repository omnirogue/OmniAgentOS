"""Durable, human-readable continuity workbooks for longhaul tasks."""

from __future__ import annotations

import re
from pathlib import Path

WORKBOOK_ROOT = Path("var") / "longhaul"
_STATUS_RE = re.compile(r"^## Status\s*\n\s*(WORKING|BLOCKED|DONE)\b", re.MULTILINE)


def _path(task_id: str) -> Path:
    if not task_id or Path(task_id).name != task_id:
        raise ValueError("task_id must be a single path component")
    return WORKBOOK_ROOT / task_id / "WORKBOOK.md"


def init_workbook(task_id: str, title: str, brief: str, acceptance: str) -> str:
    path = _path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            f"# {title}\n\n## Goal\n\n{brief.rstrip()}\n\n## Acceptance criteria\n\n{acceptance.rstrip()}\n\n"
            "## Plan\n\n- [ ] Establish and maintain the implementation plan.\n\n## Progress log\n\n## Decisions\n\n"
            "## Next steps\n\n- [ ] Start work.\n\n## Status\nWORKING\n",
            encoding="utf-8",
        )
    return str(path)


def read_workbook(task_id: str) -> str | None:
    try:
        return _path(task_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def workbook_status(task_id: str) -> str | None:
    content = read_workbook(task_id)
    match = _STATUS_RE.search(content) if content is not None else None
    return match.group(1) if match else None


def workbook_summary(task_id: str, max_chars: int = 500) -> str | None:
    content = read_workbook(task_id)
    if content is None:
        return None
    if max_chars < 1:
        return ""
    content = content.strip()
    return content if len(content) <= max_chars else content[:max_chars] + "…"


def append_checkpoint(
    task_id: str, attempt_seq: int, todos_json: str, files_json: str, end_reason: str
) -> None:
    path = _path(task_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Workbook\n\n## Status\nWORKING\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n### Checkpoint (attempt {attempt_seq})\nend_reason: {end_reason}\n"
            f"todos_json: {todos_json}\nfiles_json: {files_json}\n"
        )


__all__ = [
    "WORKBOOK_ROOT",
    "append_checkpoint",
    "init_workbook",
    "read_workbook",
    "workbook_status",
    "workbook_summary",
]
