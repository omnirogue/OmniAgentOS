"""Skill-usage telemetry (migration 131): prove an injected skill was reused.

Before this module nothing recorded which skills a brief actually carried, so
"an extracted skill got replayed" could never be shown from data — only
inferred from prompt text nobody queries. `record_skill_usage` is called by
the two sites that resolve/inject skill content into a brief
(`runner/core.py`'s `_resolved_skill_block` and `swarm/spawn.py`'s
`_resolved_skill_prompt`), right after each has computed the VERIFIED,
actually-injected set — never the pre-verification candidate set a selector
merely ranked.

Fail-soft by construction, mirroring `omniagentos.knowledge.runner_hook`: any
fault here (a bad connection, a missing migration, a closed db) is logged and
swallowed. Telemetry must never cost a run or a spawn. Callers additionally
wrap their own import of this module in a local try/except (2026-08-14 xcrit
F3): a fault in resolving `record_skill_usage` itself — before this function's
own guard is ever reached — must not strip an already-resolved skill block.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

LOG = logging.getLogger(__name__)


def record_skill_usage(
    db_path_or_conn: str | sqlite3.Connection,
    run_id: str,
    skill_ids: Sequence[str],
    brief_kind: str,
    *,
    skill_versions: Sequence[str | None] | None = None,
) -> None:
    """Insert one `skill_usage` row per skill actually injected into a brief.

    ``db_path_or_conn`` is either a filesystem path (a private connection is
    opened and closed) or an existing open connection (reused as-is, and left
    open — the caller owns its lifecycle). ``skill_versions``, when given, is
    the SAME resolved skill's verified version, index-aligned with
    ``skill_ids`` (2026-08-14 xcrit F4: without this a usage row could not
    prove WHICH version was injected). A length mismatch is treated as absent
    rather than mis-paired. Never raises: any failure is logged and swallowed
    so a telemetry fault can never break the caller's run.
    """
    try:
        _write(db_path_or_conn, run_id, skill_ids, brief_kind, skill_versions)
    except Exception:  # noqa: BLE001 -- telemetry must never block a caller
        LOG.warning("skill usage recording failed; continuing", exc_info=True)


def _write(
    db_path_or_conn: str | sqlite3.Connection,
    run_id: str,
    skill_ids: Sequence[str],
    brief_kind: str,
    skill_versions: Sequence[str | None] | None,
) -> None:
    from omniagentos.contracts import utc_now_iso

    raw_ids = list(skill_ids or [])
    raw_versions: list[str | None]
    if skill_versions is not None and len(skill_versions) == len(raw_ids):
        raw_versions = list(skill_versions)
    else:
        raw_versions = [None] * len(raw_ids)

    pairs: list[tuple[str, str | None]] = []
    for raw_id, raw_version in zip(raw_ids, raw_versions, strict=True):
        skill_id = str(raw_id).strip()
        if not skill_id:
            continue
        version = str(raw_version).strip() if raw_version is not None else ""
        pairs.append((skill_id, version or None))

    run = str(run_id or "").strip()
    kind = str(brief_kind or "").strip()
    if not pairs or not run or not kind:
        return

    now = utc_now_iso()
    rows = [(run, skill_id, version, kind, now) for skill_id, version in pairs]
    sql = (
        "INSERT INTO skill_usage (run_id, skill_id, skill_version, brief_kind, injected_at) "
        "VALUES (?, ?, ?, ?, ?)"
    )

    if isinstance(db_path_or_conn, sqlite3.Connection):
        db_path_or_conn.executemany(sql, rows)
        db_path_or_conn.commit()
        return

    from omniagentos.db.store import _connect

    connection = _connect(str(db_path_or_conn))
    try:
        connection.executemany(sql, rows)
        connection.commit()
    finally:
        connection.close()


__all__ = ["record_skill_usage"]
