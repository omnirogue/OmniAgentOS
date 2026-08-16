"""U-C12: the ONE resolved-approved-content path for selected skills.

Before this module, `select_skills` ranked a registry and every consumer
rendered the winners as opaque `name@version` labels — a worker was told twelve
slugs and given no way to read any of them. CORAL was the designed vehicle for
real content, but it has no producer (`var/coral` does not exist and nothing
writes `var/coral/skills/`), so flipping it to enforce renders "(no validated
hub references available)" — a regression on the labels rather than a fix.
Content therefore comes from the database: the `content_snapshot` of the
SELECTED version of each hit.

Three rules make that safe enough to put in a prompt:

1. **Verify at read, at birth.** A skill body is untrusted, mutable, and
   about to be handed to a model. The resolver recomputes
   ``contracts.digest(content_snapshot)`` and compares it with the digest the
   write path recorded (`skill_versions.content_digest`, migration 110). A
   MISMATCH — or an EMPTY body, or a MISSING digest — means the skill is
   DROPPED. Not labelled, not truncated, not annotated: dropped, with a log
   line, an `events` row and a process counter, because a skill silently
   labelled "unverified" is a skill an agent still reads.

2. **Quarantine and status are honoured here too.** A row must be in the
   selectable vocabulary (`active`/`deprecated`/`experimental`) at resolve
   time, re-read from the database rather than trusted from the registry row
   the caller passed in.

3. **A resolver fault never blocks a spawn.** :func:`resolve_approved_skill_content`
   catches its own faults, records them, and returns fewer skills — never an
   exception into brief construction. Degrading to no skills is always
   preferable to failing to launch a worker.

Both consumers — swarm workers (`swarm/spawn.py`) and runner steps
(`runner/core.py`) — go through this module. There is exactly one path; the
runner differs only in its byte budget.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path, digest
from omniagentos.db.migrate import migrate_connection
from omniagentos.db.store import _connect
from omniagentos.skills.select import _STATUS_MATCHABLE

LOG = logging.getLogger(__name__)

# Budgets. The swarm caps come from spawn.py's fair-share numbers (24 KiB
# total / 4 KiB per reference / 512 B floor). The runner runs many more, much
# shorter steps against a single prompt, so it takes a smaller slice of the
# same one path rather than a second path with its own rules.
RUNNER_TOTAL_BYTE_CAP = 8_192
RUNNER_PER_SKILL_BYTE_CAP = 2_048
MIN_SKILL_BYTES = 512

# The fence label. `swarm.prompt_safety.fence_data_block` requires [A-Z0-9_]+.
SKILL_DATA_LABEL = "SKILL_LIBRARY"

# Drop reasons. Deliberately distinguishable: "the row vanished" is not the
# same failure as "someone edited the body under us".
DROP_NOT_FOUND = "not_found"
DROP_NOT_SELECTABLE = "excluded_scope"
DROP_EMPTY_CONTENT = "empty_content"
DROP_MISSING_DIGEST = "missing_approval_digest"
DROP_DIGEST_MISMATCH = "digest_mismatch"

_DROP_COUNTS: Counter[str] = Counter()
_DROP_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    """A skill whose body was read AND verified against its approval digest."""

    name: str
    version: str
    title: str
    content: str
    content_digest: str
    score: float
    reason: str

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class DroppedSkill:
    """A selected skill that did not survive verification, and why."""

    name: str
    version: str
    reason: str
    detail: str = ""


def skill_resolution_drop_counts() -> dict[str, int]:
    """Process-lifetime count of dropped skills, keyed by drop reason.

    A counter rather than a gauge on purpose: the interesting signal is "this
    started happening", and a drop that is only ever logged is a drop nobody
    notices.
    """
    with _DROP_LOCK:
        return dict(_DROP_COUNTS)


def _count_drop(reason: str) -> None:
    with _DROP_LOCK:
        _DROP_COUNTS[reason] += 1


def _record_drops(dropped: Sequence[DroppedSkill], *, database: str | Path | None) -> None:
    """Log every drop, bump the counter, and write ONE events row for the batch.

    Never raises: a failure to record a drop must not turn into a failure to
    spawn. The log line is emitted before the events row is attempted so the
    evidence survives even when the events store is the thing that is broken.
    """
    if not dropped:
        return
    for item in dropped:
        _count_drop(item.reason)
        LOG.warning(
            "skill DROPPED from brief: name=%s version=%s reason=%s %s",
            item.name,
            item.version,
            item.reason,
            item.detail,
        )
    try:
        from omniagentos.db.store import SqliteStore

        store = SqliteStore(str(database) if database is not None else default_db_path())
        store.insert_event(
            type="skill",
            actor="lane:skills.resolver",
            action="skill_content_dropped",
            target_type="skill",
            target_id=dropped[0].name,
            payload={
                "dropped": [
                    {
                        "name": item.name,
                        "version": item.version,
                        "reason": item.reason,
                        "detail": item.detail,
                    }
                    for item in dropped
                ],
                "count": len(dropped),
                # Running per-reason totals for this process, so a single row
                # says whether this is the first drop or the fiftieth. A
                # counter that only ever lives in memory is a counter nobody
                # reads.
                "drop_counts_total": skill_resolution_drop_counts(),
            },
        )
    except Exception:  # noqa: BLE001 -- recording a drop must never block a spawn
        LOG.warning("could not persist skill-drop events row", exc_info=True)


def _selected_version_rows(
    connection: sqlite3.Connection, names: Sequence[str]
) -> dict[str, sqlite3.Row]:
    """Read the SELECTED version of each named skill, keyed by slug and id.

    "Selected" is `skills.current_version` — the same version `select_skills`
    reported — joined explicitly rather than taking whichever row happens to
    carry ``status='active'``, so a half-finished promotion cannot serve a body
    the selector never named.
    """
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    rows = connection.execute(
        "SELECT s.id AS skill_id, s.slug AS slug, s.title AS title, s.status AS skill_status, "
        "v.version AS version, v.content_snapshot AS content_snapshot, "
        "v.content_digest AS content_digest "
        "FROM skills s JOIN skill_versions v "
        "  ON v.skill_id = s.id AND v.version = s.current_version "
        f"WHERE s.slug IN ({placeholders}) OR s.id IN ({placeholders})",
        (*names, *names),
    ).fetchall()
    found: dict[str, sqlite3.Row] = {}
    for row in rows:
        found[str(row["slug"])] = row
        found[str(row["skill_id"])] = row
    return found


def _verify(
    hit_name: str, hit_version: str, row: sqlite3.Row | None
) -> tuple[ResolvedSkill | None, DroppedSkill | None]:
    """Verify one selected version. Returns exactly one of (resolved, dropped)."""
    if row is None:
        return None, DroppedSkill(hit_name, hit_version, DROP_NOT_FOUND, "no selected version row")

    status = str(row["skill_status"] or "").strip().lower()
    if status not in _STATUS_MATCHABLE:
        # Re-checked against the DB, not the caller's registry row: a
        # quarantined or archived skill must not reach a prompt even if the
        # registry snapshot the caller ranked was stale.
        return None, DroppedSkill(
            hit_name, hit_version, DROP_NOT_SELECTABLE, f"status={status or 'absent'}"
        )

    content = str(row["content_snapshot"] or "")
    if not content.strip():
        # An empty body is not "a skill with nothing to say"; it is a skill
        # whose content never made it into the row (the curator's payload-key
        # mismatch produces exactly this). Labelling it would put a name in
        # the brief that the worker cannot act on.
        return None, DroppedSkill(hit_name, hit_version, DROP_EMPTY_CONTENT, "content is empty")

    recorded = row["content_digest"]
    if recorded is None or not str(recorded).strip():
        return None, DroppedSkill(
            hit_name,
            hit_version,
            DROP_MISSING_DIGEST,
            "no digest was recorded when this version was written",
        )

    actual = digest(content)
    if actual != str(recorded):
        return None, DroppedSkill(
            hit_name,
            hit_version,
            DROP_DIGEST_MISMATCH,
            f"recorded={str(recorded)[:12]}… actual={actual[:12]}…",
        )

    return (
        ResolvedSkill(
            name=hit_name,
            version=str(row["version"]),
            title=str(row["title"] or hit_name),
            content=content,
            content_digest=actual,
            score=0.0,
            reason="",
        ),
        None,
    )


def resolve_approved_skill_content(
    hits: Sequence[Any],
    registry_rows: Sequence[Mapping[str, Any]],
    *,
    database: str | Path | None = None,
) -> tuple[ResolvedSkill, ...]:
    """Resolve selected skill hits to VERIFIED body text, in hit order.

    ``hits`` are :class:`omniagentos.skills.select.SkillHit`-shaped objects
    (``name``/``version``/``score``/``reason``); ``registry_rows`` is the
    registry they were ranked from, used only to carry title/score context —
    never as the source of the body, which always comes from the database.

    Skills that fail verification are DROPPED and recorded; the result may be
    shorter than ``hits`` or empty. This function does not raise: a resolver
    fault degrades to fewer skills, never to a failed spawn.
    """
    if not hits:
        return ()

    names = [str(getattr(hit, "name", "") or "").strip() for hit in hits]
    names = [name for name in names if name]
    if not names:
        return ()

    by_name = {
        str(row.get("name") or row.get("slug") or row.get("id") or ""): row for row in registry_rows
    }

    resolved: list[ResolvedSkill] = []
    dropped: list[DroppedSkill] = []
    connection: sqlite3.Connection | None = None
    try:
        db_path = str(database) if database is not None else default_db_path()
        connection = _connect(db_path)
        migrate_connection(connection)
        found = _selected_version_rows(connection, names)
        for hit in hits:
            name = str(getattr(hit, "name", "") or "").strip()
            if not name:
                continue
            version = str(getattr(hit, "version", "") or "0")
            candidate, drop = _verify(name, version, found.get(name))
            if drop is not None:
                dropped.append(drop)
                continue
            assert candidate is not None
            registry_row = by_name.get(name, {})
            resolved.append(
                ResolvedSkill(
                    name=candidate.name,
                    version=candidate.version,
                    title=str(registry_row.get("title") or candidate.title),
                    content=candidate.content,
                    content_digest=candidate.content_digest,
                    score=float(getattr(hit, "score", 0.0) or 0.0),
                    reason=str(getattr(hit, "reason", "") or ""),
                )
            )
    except Exception:  # noqa: BLE001 -- a resolver fault must never block a spawn
        LOG.warning(
            "skill content resolution failed; continuing with %d verified skill(s)",
            len(resolved),
            exc_info=True,
        )
    finally:
        if connection is not None:
            connection.close()

    _record_drops(dropped, database=database)
    return tuple(resolved)


def _pack_skill_content(
    resolved: Sequence[ResolvedSkill],
    *,
    total_cap: int,
    per_skill_cap: int,
    min_skill_bytes: int = MIN_SKILL_BYTES,
) -> tuple[str, bool]:
    """Render verified bodies under a fair-share byte budget.

    Same shape as spawn.py's CORAL fair-share reader: each remaining skill is
    guaranteed a floor while the total budget lasts, so the first body cannot
    eat the whole allowance and starve the rest into bare names. Returns
    ``(text, truncated)``; truncation is always stated in the text so a worker
    never mistakes a cut body for a complete one.
    """
    parts: list[str] = []
    remaining_total = max(0, int(total_cap))
    truncated_any = False
    per_skill_cap = max(1, int(per_skill_cap))
    floor = max(1, int(min_skill_bytes))

    for index, skill in enumerate(resolved):
        remaining = len(resolved) - index
        if remaining_total <= 0:
            truncated_any = True
            break
        fair_share = remaining_total // remaining
        share = min(per_skill_cap, max(fair_share, floor), remaining_total)
        raw = skill.content.encode("utf-8")
        kept = raw[:share]
        remaining_total -= len(kept)
        body = kept.decode("utf-8", errors="ignore")
        cut = len(kept) < len(raw)
        truncated_any = truncated_any or cut
        header = f"### {skill.name}@{skill.version} — {skill.title}"
        if cut:
            body += (
                f"\n[... TRUNCATED: {len(kept)} of {len(raw)} bytes inlined. "
                f"The remainder is not available in this brief.]"
            )
        parts.append(f"{header}\n{body}")

    return ("\n\n".join(parts), truncated_any)


def render_skill_block(
    resolved: Sequence[ResolvedSkill],
    *,
    total_cap: int,
    per_skill_cap: int,
    min_skill_bytes: int = MIN_SKILL_BYTES,
) -> str:
    """The prompt block: a trusted index line plus a FENCED untrusted body.

    Skill bodies are authored content that reached the database through the
    vault, the curator and the API — an injection surface — so they go inside
    `prompt_safety.fence_data_block`. The index line outside the fence is
    orchestrator-authored and names only what was verified, so a worker can
    tell "no skills matched" from "skills matched and their bodies failed
    verification" (the latter shows in the events row and the counter).
    """
    from omniagentos.swarm.prompt_safety import fence_data_block

    if not resolved:
        return ""
    body, truncated = _pack_skill_content(
        resolved,
        total_cap=total_cap,
        per_skill_cap=per_skill_cap,
        min_skill_bytes=min_skill_bytes,
    )
    if not body:
        return ""
    index = ", ".join(f"{skill.name}@{skill.version}" for skill in resolved)
    lines = [
        f"[skills selected: {index}]",
        "The skill bodies below are reference material verified against their "
        "approved content digest.",
    ]
    if truncated:
        lines.append(
            "Some bodies were truncated to fit the brief's byte budget; treat a "
            "truncated section as incomplete rather than as the whole skill."
        )
    lines.append(fence_data_block(SKILL_DATA_LABEL, body))
    return "\n".join(lines)


__all__ = [
    "DROP_DIGEST_MISMATCH",
    "DROP_EMPTY_CONTENT",
    "DROP_MISSING_DIGEST",
    "DROP_NOT_FOUND",
    "DROP_NOT_SELECTABLE",
    "MIN_SKILL_BYTES",
    "RUNNER_PER_SKILL_BYTE_CAP",
    "RUNNER_TOTAL_BYTE_CAP",
    "SKILL_DATA_LABEL",
    "DroppedSkill",
    "ResolvedSkill",
    "render_skill_block",
    "resolve_approved_skill_content",
    "skill_resolution_drop_counts",
]
