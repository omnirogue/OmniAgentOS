"""Vault-backed skills library data-access layer.

The functions in this module are the frozen S-A interface. SQLite is the
structured index/version store; ``vault/playbook/skill-*.md`` remains the
human-editable source of truth for instruction bodies.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import (
    _repo_root,
    default_db_path,
    default_vault_dir,
    digest,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate_connection
from omniagentos.db.store import _connect
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.path_containment import inode_paths_equal
from omniagentos.skills.scan import scan_content

LOG = logging.getLogger(__name__)

_VALID_RISKS = frozenset({"low", "model", "major"})
_VALID_STATES = frozenset({"pending", "approved", "rejected"})

# ONE status vocabulary, defined by the `skills.status` CHECK (migration 109)
# and mirrored by omniagentos.skills.select. Kept here so the DAL refuses an
# out-of-vocabulary status with a readable error instead of a raw IntegrityError.
VALID_SKILL_STATUSES = frozenset(
    {"active", "archived", "quarantined", "deprecated", "experimental"}
)
QUARANTINED_STATUS = "quarantined"

# The two tells of a curator auto-capture that is not evidence of a reusable
# pattern. Both are literal strings this repo's own curator emits:
#   * selfimprove/curator.py substitutes the no-steps sentence when a manifest
#     yields ZERO step names — the note records no procedure at all.
#   * `harness=mock` in the gate evidence means the run never touched a real
#     agent, so it cannot be evidence of a reusable workflow.
# 34 of the 41 live skills matched these; they are quarantined, never deleted.
_NO_STEP_EVIDENCE_MARKER = "No step-level detail was recorded in the ledger manifest"
_MOCK_HARNESS_MARKER = "harness=mock"
AUTO_CAPTURE_QUARANTINE_REASON = "auto-capture:no-step-evidence-or-mock-harness"
_SELECTION_LIST_FIELDS = (
    ("tools", "tools_json"),
    ("artifacts", "artifacts_json"),
    ("risk_classes", "risk_classes_json"),
)
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---\r?\n?", re.DOTALL)
_LIBRARY_METADATA_RE = re.compile(
    r"<!--\s*skill-library\s*(?P<yaml>.*?)-->", re.DOTALL | re.IGNORECASE
)


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = _connect(default_db_path())
    try:
        migrate_connection(connection)
        yield connection
    finally:
        connection.close()


def _json_load(value: Any, default: Any) -> Any:
    """Parse JSON, or return *default* only for genuine absence (None).

    Unparseable strings must not look like a successful empty object/list —
    that would present corrupted evidence as a favourable empty result.
    """
    if not isinstance(value, str):
        return default if value is None else value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable JSON: {exc.msg}") from exc


def _string_list(value: Any) -> list[str]:
    """Coerce a declared metadata value into a list of non-empty strings.

    Accepts a JSON/YAML list, a single scalar, or a comma-separated string.
    Never invents entries: an absent or unusable value yields ``[]``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: list[Any] = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        parts = [value]
    out: list[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in out:
            out.append(text)
    return out


def _is_evidence_free_autocapture(content: str) -> bool:
    """True when a skill body carries no evidence of a reusable pattern.

    This is the quarantine predicate, kept in one place so the migration's SQL,
    the vault indexer and the tests all mean the same thing by it.
    """
    return _NO_STEP_EVIDENCE_MARKER in content or _MOCK_HARNESS_MARKER in content


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_diff(original: str, diff_text: str) -> str:
    """Apply a unified diff to *original* text, strictly.

    This is the fix for the diff-only favourable-absence defect: an UPDATE
    proposal carrying only ``proposed_diff`` must actually mutate the content,
    not copy the current snapshot through unchanged. Strictness is the safety
    property — ANY context or removal line that does not match the current
    text byte-for-byte raises :class:`ValueError` rather than fuzzy-matching,
    because a fuzzy applier could silently land the wrong hunk at the wrong
    offset and reintroduce the same "recorded an improvement that never
    happened" bug in a new form.

    No prior-art patch applier exists in this repo for *string* content —
    ``omniagentos/agentless/patch.py`` shells out to ``git apply`` against a
    real (scratch) git checkout on disk, which is the wrong shape for a diff
    stored as a column value with no filesystem representation. Stdlib-only:
    no new dependency.
    """
    diff_lines = diff_text.splitlines()
    orig_lines = original.splitlines()
    orig_trailing_newline = original.endswith("\n") or original == ""

    result: list[str] = []
    pointer = 0
    found_hunk = False
    index = 0
    while index < len(diff_lines):
        line = diff_lines[index]
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            index += 1
            continue
        found_hunk = True
        old_start = int(match.group(1))
        old_start_idx = max(old_start - 1, 0)
        if old_start_idx < pointer or old_start_idx > len(orig_lines):
            raise ValueError(
                f"diff hunk out of order or out of range at original line {old_start}"
            )
        result.extend(orig_lines[pointer:old_start_idx])
        pointer = old_start_idx
        index += 1
        while index < len(diff_lines) and _HUNK_HEADER_RE.match(diff_lines[index]) is None:
            hunk_line = diff_lines[index]
            if hunk_line.startswith("\\"):
                # "\ No newline at end of file" — not a content line.
                index += 1
                continue
            marker = hunk_line[:1] or " "
            content = hunk_line[1:]
            if marker == " ":
                if pointer >= len(orig_lines) or orig_lines[pointer] != content:
                    raise ValueError(f"diff context mismatch at original line {pointer + 1}")
                result.append(orig_lines[pointer])
                pointer += 1
            elif marker == "-":
                if pointer >= len(orig_lines) or orig_lines[pointer] != content:
                    raise ValueError(f"diff removal mismatch at original line {pointer + 1}")
                pointer += 1
            elif marker == "+":
                result.append(content)
            else:
                raise ValueError(f"malformed diff hunk line: {hunk_line!r}")
            index += 1
    if not found_hunk:
        raise ValueError("proposed_diff contains no applicable hunks")
    result.extend(orig_lines[pointer:])
    patched = "\n".join(result)
    if orig_trailing_newline and patched:
        patched += "\n"
    return patched


def _version_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["evidence"] = _json_load(value.pop("evidence_json", "{}"), {})
    return value


def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["evidence_files"] = _json_load(value.pop("evidence_files_json", "[]"), [])
    return value


def _skill_from_connection(connection: sqlite3.Connection, skill_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM skills WHERE id = ? OR slug = ?", (skill_id, skill_id)
    ).fetchone()
    if row is None:
        raise KeyError(f"skill {skill_id!r} not found")
    value = dict(row)
    resolved_id = str(value["id"])
    versions = [
        _version_dict(version)
        for version in connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version DESC",
            (resolved_id,),
        ).fetchall()
    ]
    current = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
        (resolved_id, value["current_version"]),
    ).fetchone()
    linked_files: list[str] = []
    if value.get("vault_note_path"):
        linked_files.append(str(value["vault_note_path"]))
    for proposal in connection.execute(
        "SELECT evidence_files_json FROM update_proposals WHERE skill_id = ?",
        (resolved_id,),
    ).fetchall():
        for linked in _json_load(proposal["evidence_files_json"], []):
            if isinstance(linked, str) and linked not in linked_files:
                linked_files.append(linked)
    value["version"] = _version_dict(current)
    value["versions"] = [version for version in versions if version is not None]
    value["linked_files"] = linked_files
    return value


def list_tree() -> list[dict]:
    """Return active and archived skills grouped by category and subcategory."""
    with _database() as connection:
        rows = connection.execute(
            "SELECT id, slug, category, subcategory, title, status, preferred_method "
            "FROM skills ORDER BY category COLLATE NOCASE, subcategory COLLATE NOCASE, "
            "title COLLATE NOCASE, id"
        ).fetchall()
    categories: list[dict[str, Any]] = []
    category_map: dict[str, dict[str, Any]] = {}
    subcategory_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        category_name = str(row["category"])
        subcategory_name = str(row["subcategory"])
        category = category_map.get(category_name)
        if category is None:
            category = {"category": category_name, "subcategories": []}
            category_map[category_name] = category
            categories.append(category)
        key = (category_name, subcategory_name)
        subcategory = subcategory_map.get(key)
        if subcategory is None:
            subcategory = {"name": subcategory_name, "skills": []}
            subcategory_map[key] = subcategory
            category["subcategories"].append(subcategory)
        subcategory["skills"].append(
            {
                "id": row["id"],
                "slug": row["slug"],
                "title": row["title"],
                "status": row["status"],
                "preferred_method": row["preferred_method"],
            }
        )
    return categories


def list_skills(
    *,
    database: str | Path | None = None,
    include_archived: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Flat skill registry for spawn-time selection (M-02).

    Returns rows shaped for :func:`omniagentos.skills.select.select_skills`:

    ``name``, ``version``, ``domains``, ``risk_classes``, ``tools``,
    ``artifacts``, ``status``, plus ``id`` / ``slug`` / ``title`` for callers.

    ``domains`` is derived from the skill's category/subcategory so a task
    discipline like ``coding`` matches category ``Coding`` without requiring
    a separate domain column. ``tools`` / ``artifacts`` / ``risk_classes`` are
    read verbatim from what the skill DECLARES (migration 109's JSON columns,
    populated from a vault note's ``<!-- skill-library -->`` block). They were
    previously hardcoded ``[]`` — with ``preferred_method``'s prose sentence
    stuffed into ``tools`` — which left three of ``select_skills``'s four
    scoring signals permanently unable to fire and the fourth comparing a tool
    name against an English sentence.

    Quarantined skills are NEVER returned, not even with
    ``include_archived=True``: quarantine is the machine's judgement that a row
    is retained evidence rather than a usable skill, and inlining one into a
    prompt is the exact failure this flag exists to prevent. Use
    :func:`list_tree` or :func:`get_skill` to inspect them.
    """
    cap = max(1, int(limit))
    db_path = Path(database) if database is not None else default_db_path()
    connection = _connect(str(db_path))
    columns = (
        "SELECT id, slug, category, subcategory, title, summary, "
        "preferred_method, status, current_version, "
        "tools_json, artifacts_json, risk_classes_json FROM skills "
    )
    try:
        migrate_connection(connection)
        if include_archived:
            rows = connection.execute(
                columns + "WHERE status <> 'quarantined' "
                "ORDER BY category COLLATE NOCASE, title COLLATE NOCASE, id "
                "LIMIT ?",
                (cap,),
            ).fetchall()
        else:
            rows = connection.execute(
                columns + "WHERE status = 'active' "
                "ORDER BY category COLLATE NOCASE, title COLLATE NOCASE, id "
                "LIMIT ?",
                (cap,),
            ).fetchall()
    finally:
        connection.close()

    registry: list[dict[str, Any]] = []
    for row in rows:
        category = str(row["category"] or "").strip()
        subcategory = str(row["subcategory"] or "").strip()
        slug = str(row["slug"] or row["id"] or "").strip()
        domains: list[str] = []
        for part in (category, subcategory):
            if part and part.lower() not in domains:
                domains.append(part.lower())
        # Also expose hyphen/space variants so "web-dev" matches "Web Dev".
        for part in list(domains):
            alt = part.replace(" ", "-").replace("_", "-")
            if alt and alt not in domains:
                domains.append(alt)
        declared: dict[str, list[str]] = {}
        for field, column in _SELECTION_LIST_FIELDS:
            try:
                declared[field] = _string_list(_json_load(row[column], []))
            except ValueError:
                # A corrupted JSON column must not read as "declares nothing"
                # (that is the favourable-empty-default this repo keeps
                # finding); drop the whole row rather than mis-score it.
                declared = {}
                break
        if not declared:
            continue
        registry.append(
            {
                "id": str(row["id"]),
                "slug": slug,
                "name": slug or str(row["title"] or ""),
                "title": str(row["title"] or ""),
                "version": str(row["current_version"] or "1"),
                "domains": domains,
                "risk_classes": declared["risk_classes"],
                "tools": declared["tools"],
                "artifacts": declared["artifacts"],
                "status": str(row["status"] or "active"),
                "summary": str(row["summary"] or ""),
                "preferred_method": str(row["preferred_method"] or ""),
            }
        )
    return registry


def get_skill(skill_id: str) -> dict:
    """Return a skill, its selected version, all versions, and linked files."""
    with _database() as connection:
        return _skill_from_connection(connection, skill_id)


def upsert_skill(data: dict) -> str:
    """Insert or update one structured skill record and return its stable id.

    Two hygiene invariants live here because this is the single write path
    every authoring route (vault index, curator, API) funnels through:

    * **Born quarantined.** A NEW skill whose body carries no step-level
      evidence, or whose provenance says the run used the mock harness, is
      inserted with ``status='quarantined'``. The row and its body are kept
      verbatim as evidence; it is simply never selected or inlined. Without
      this a fresh database would re-import the 34 auto-captures as ``active``
      and undo migration 109 on the first API start.
    * **Non-active status is sticky against automated writes.** An UPDATE never
      promotes a non-active status (quarantined, archived, deprecated, experimental)
      back to ``active``. Only :func:`release_quarantine` or explicit operator
      actions clear it — so the startup vault re-index, which rewrites every note
      in place with the default ``status='active'``, cannot silently resurrect one.
      Conversely an EXISTING active row may still be explicitly changed to any
      other status, and operator releases survive the next index.
    """
    values = dict(data)
    slug = str(values.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    # Curator-captured skills carry `discipline` but no explicit Category>Subcategory
    # hierarchy; derive a sensible bucket so daily-loop capture never trips the
    # required-field gate. Explicit hierarchy (UI/seed) is left untouched.
    if not str(values.get("category") or "").strip():
        values["category"] = str(values.get("discipline") or "").strip() or "Uncategorized"
    if not str(values.get("subcategory") or "").strip():
        values["subcategory"] = "General"
    if "status" in values:
        status_in = str(values["status"] or "").strip().lower()
        if status_in not in VALID_SKILL_STATUSES:
            raise ValueError(
                f"invalid skill status {values['status']!r}; "
                f"expected one of {sorted(VALID_SKILL_STATUSES)}"
            )
        values["status"] = status_in
    declared_json = {
        column: json.dumps(_string_list(values.get(field)))
        for field, column in _SELECTION_LIST_FIELDS
        if field in values
    }
    now = utc_now_iso()
    with _database() as connection:
        existing = connection.execute(
            "SELECT * FROM skills WHERE id = ? OR slug = ?",
            (values.get("id", slug), slug),
        ).fetchone()
        skill_id = str(existing["id"]) if existing is not None else str(values.get("id") or slug)
        if existing is None:
            required = ("category", "subcategory", "title")
            missing = [name for name in required if not str(values.get(name) or "").strip()]
            if missing:
                raise ValueError(f"missing required fields: {', '.join(missing)}")
            status = str(values.get("status", "active"))
            quarantine_reason: str | None = None
            quarantined_at: str | None = None
            # Scan content for security issues (supply-chain scanner integration)
            content_to_scan = str(values.get("content_snapshot", ""))
            if content_to_scan:
                scan_result = scan_content(content_to_scan)
                # If scan fails (high/critical findings), quarantine the new skill
                if not scan_result.passed and status != QUARANTINED_STATUS:
                    status = QUARANTINED_STATUS
                    quarantine_reason = "scan:high-or-critical-findings"
                    quarantined_at = now
            if status != QUARANTINED_STATUS and _is_evidence_free_autocapture(
                str(values.get("content_snapshot", ""))
            ):
                status = QUARANTINED_STATUS
                quarantine_reason = AUTO_CAPTURE_QUARANTINE_REASON
                quarantined_at = now
            if status == QUARANTINED_STATUS and quarantine_reason is None:
                # Only set explicit reason if not already set by scanner
                quarantine_reason = str(
                    values.get("quarantine_reason") or "explicit:caller-requested"
                )
                quarantined_at = now
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO skills "
                    "(id, slug, category, subcategory, title, summary, preferred_method, "
                    "fallback_method, vault_note_path, status, current_version, created_at, "
                    "updated_at, tools_json, artifacts_json, risk_classes_json, "
                    "quarantine_reason, quarantined_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_id,
                        slug,
                        values["category"],
                        values["subcategory"],
                        values["title"],
                        values.get("summary", ""),
                        values.get("preferred_method"),
                        values.get("fallback_method"),
                        values.get("vault_note_path"),
                        status,
                        now,
                        now,
                        declared_json.get("tools_json", "[]"),
                        declared_json.get("artifacts_json", "[]"),
                        declared_json.get("risk_classes_json", "[]"),
                        quarantine_reason,
                        quarantined_at,
                    ),
                )
                first_content = str(values.get("content_snapshot", ""))
                connection.execute(
                    "INSERT INTO skill_versions "
                    "(id, skill_id, version, content_snapshot, preferred_method, fallback_method, "
                    "change_reason, evidence_json, author, status, created_at, content_digest) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        new_id("skv"),
                        skill_id,
                        first_content,
                        values.get("preferred_method"),
                        values.get("fallback_method"),
                        values.get("change_reason", "Initial version"),
                        json.dumps(values.get("evidence") or {}, sort_keys=True),
                        values.get("author", "system:index"),
                        now,
                        digest(first_content),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return skill_id

        mutable = (
            "category",
            "subcategory",
            "title",
            "summary",
            "preferred_method",
            "fallback_method",
            "vault_note_path",
            "status",
        )
        updates: dict[str, Any] = {name: values[name] for name in mutable if name in values}
        existing_status = str(existing["status"])
        incoming_status = str(values.get("status", ""))
        if existing_status != "active" and incoming_status == "active":
            # Sticky: an automated re-index must not promote a non-active
            # row back to active by replaying the default status.
            updates.pop("status", None)
        updates.update(declared_json)
        updates["slug"] = slug
        assignments = ", ".join(f"{name} = ?" for name in updates)
        connection.execute(
            f"UPDATE skills SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now, skill_id),
        )
        if "content_snapshot" in values:
            # The digest is written in the SAME statement as the body: a write
            # path that could update one without the other would make
            # verify-at-read a coin flip instead of a check.
            content = str(values["content_snapshot"])
            connection.execute(
                "UPDATE skill_versions SET content_snapshot = ?, content_digest = ? "
                "WHERE skill_id = ? AND version = ?",
                (content, digest(content), skill_id, existing["current_version"]),
            )
        return skill_id


def _insert_version(
    connection: sqlite3.Connection,
    skill_id: str,
    *,
    content: str,
    preferred_method: str | None,
    fallback_method: str | None,
    change_reason: str,
    evidence: Any,
    author: str,
) -> tuple[int, str]:
    skill = connection.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if skill is None:
        raise KeyError(f"skill {skill_id!r} not found")
    latest = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM skill_versions WHERE skill_id = ?",
        (skill_id,),
    ).fetchone()
    version = int(latest["version"]) + 1
    version_id = new_id("skv")
    connection.execute(
        "INSERT INTO skill_versions "
        "(id, skill_id, version, content_snapshot, preferred_method, fallback_method, "
        "change_reason, evidence_json, author, status, created_at, content_digest) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'superseded', ?, ?)",
        (
            version_id,
            skill_id,
            version,
            content,
            preferred_method if preferred_method is not None else skill["preferred_method"],
            fallback_method if fallback_method is not None else skill["fallback_method"],
            change_reason,
            json.dumps(evidence or {}, sort_keys=True),
            author,
            utc_now_iso(),
            digest(content),
        ),
    )
    return version, version_id


def new_version(
    skill_id: str,
    *,
    content: str,
    preferred_method: str | None = None,
    fallback_method: str | None = None,
    change_reason: str,
    evidence: Any = None,
    author: str,
) -> int:
    """Create an unselected version; the current version is unchanged."""
    with _database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version, _ = _insert_version(
                connection,
                str(skill_id),
                content=str(content),
                preferred_method=preferred_method,
                fallback_method=fallback_method,
                change_reason=str(change_reason),
                evidence=evidence,
                author=str(author),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return version


def _notify(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    title: str,
    body: str,
    severity: str,
    payload: dict[str, Any],
) -> None:
    NotificationsDal(connection).create(
        {
            "kind": "approval",
            "title": title,
            "body": body,
            "severity": severity,
            "ref_type": "update_proposal",
            "ref_id": proposal_id,
            "payload": payload,
        }
    )


def propose_update(
    skill_id: str,
    *,
    proposed_content: str | None = None,
    proposed_diff: str | None = None,
    evidence_files: list[str] | None = None,
    linked_execution: str | None = None,
    risk: str = "low",
    created_by: str,
) -> str:
    """Create a proposal, notify the approval feed, and auto-approve low risk."""
    if risk not in _VALID_RISKS:
        raise ValueError(f"invalid risk {risk!r}")
    if proposed_content is None and not str(proposed_diff or "").strip():
        # A blank diff with no content body is not a proposal — it proposes
        # nothing. Accepting it used to produce an approval-stamped no-op
        # version, and it is the SHAPE THE CURATOR EMITS in production:
        # difflib.unified_diff returns "" when the learning content already
        # equals the skill's content (selfimprove/curator.py::_proposed_diff),
        # which then routed to risk=low and auto-approved. Refusing at
        # submission keeps that junk out of the pending queue entirely.
        raise ValueError("proposed_content or a non-empty proposed_diff is required")
    proposal_id = new_id("upd")
    now = utc_now_iso()
    with _database() as connection:
        if connection.execute("SELECT 1 FROM skills WHERE id = ?", (skill_id,)).fetchone() is None:
            raise KeyError(f"skill {skill_id!r} not found")
        connection.execute(
            "INSERT INTO update_proposals "
            "(id, skill_id, proposed_diff, proposed_content, evidence_files_json, "
            "linked_execution_id, risk, state, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                proposal_id,
                skill_id,
                proposed_diff,
                proposed_content,
                json.dumps(evidence_files or [], sort_keys=True),
                linked_execution,
                risk,
                created_by,
                now,
            ),
        )
        _notify(
            connection,
            proposal_id=proposal_id,
            title="Skill update proposal",
            body=f"{risk} risk update proposed for {skill_id}",
            severity="info" if risk == "low" else "warning",
            payload={"skill_id": skill_id, "risk": risk, "state": "pending"},
        )
    if risk == "low":
        try:
            decide_proposal(
                proposal_id,
                approve=True,
                note="Automatically approved low-risk update",
                decided_by="system:auto-approve",
            )
        except ValueError as exc:
            # The decision path REFUSED (the diff no longer applies, or the
            # result would be identical to the current version). Fail closed
            # AND stay visible: the proposal is already committed, so raising
            # here would abandon a pending row whose id the caller never
            # learns. It stays pending for a human, and the refusal lands in
            # the same approval feed the proposal did.
            LOG.warning(
                "proposal %s: automatic low-risk approval refused, left pending: %s",
                proposal_id,
                exc,
            )
            with _database() as connection:
                _notify(
                    connection,
                    proposal_id=proposal_id,
                    title="Automatic approval refused",
                    body=f"Proposal {proposal_id} stays pending: {exc}",
                    severity="warning",
                    payload={
                        "skill_id": skill_id,
                        "risk": risk,
                        "state": "pending",
                        "auto_approve_error": str(exc),
                    },
                )
    return proposal_id


def decide_proposal(
    proposal_id: str, *, approve: bool, note: str | None = None, decided_by: str
) -> dict:
    """Resolve a pending proposal and atomically promote an approved version."""
    target_state = "approved" if approve else "rejected"
    version_number: int | None = None
    version_id: str | None = None
    with _database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            proposal = connection.execute(
                "SELECT * FROM update_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise KeyError(f"proposal {proposal_id!r} not found")
            if proposal["state"] != "pending":
                connection.rollback()
                result = _proposal_dict(proposal)
                result["new_version"] = None
                result["version_info"] = None
                return result
            if approve:
                current = connection.execute(
                    "SELECT v.* FROM skills s JOIN skill_versions v "
                    "ON v.skill_id = s.id AND v.version = s.current_version WHERE s.id = ?",
                    (proposal["skill_id"],),
                ).fetchone()
                content = proposal["proposed_content"]
                proposed_diff = proposal["proposed_diff"]
                applied_diff_sha256: str | None = None
                diff_mismatch = False
                if content is None and not str(proposed_diff or "").strip():
                    # Legacy rows predate the submission-time check in
                    # propose_update, so the refusal is repeated at the decision
                    # seam. Copying the current snapshot through at this point
                    # was the original defect.
                    raise ValueError(
                        f"proposal {proposal_id!r}: carries no proposed_content and an "
                        "empty proposed_diff — there is nothing to apply. Reject it, or "
                        "file a proposal that carries content or a non-empty diff."
                    )
                if content is not None and proposed_diff:
                    # Precedence unchanged: proposed_content wins when both are
                    # present. But a diff that was ALSO submitted and does not
                    # reconcile with the winning content is worth knowing about
                    # (it means the two fields describe different edits), so
                    # this is logged rather than silently dropped.
                    try:
                        reconciled = _apply_unified_diff(
                            current["content_snapshot"] if current is not None else "",
                            str(proposed_diff),
                        )
                    except ValueError:
                        diff_mismatch = True
                    else:
                        diff_mismatch = reconciled != content
                    if diff_mismatch:
                        LOG.warning(
                            "proposal %s: proposed_diff does not reconcile with "
                            "proposed_content; proposed_content wins per precedence",
                            proposal_id,
                        )
                elif content is None and proposed_diff:
                    # UPDATE proposals carrying ONLY a diff must actually mutate
                    # the content at approval time — copying the current
                    # snapshot through unchanged mints a versioned, approved
                    # no-op (the favourable-absence defect this fix closes).
                    if current is None or current["content_snapshot"] is None:
                        raise ValueError(
                            f"proposal {proposal_id!r}: no current content_snapshot to "
                            "apply proposed_diff against"
                        )
                    try:
                        content = _apply_unified_diff(
                            str(current["content_snapshot"]), str(proposed_diff)
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"proposal {proposal_id!r}: proposed_diff does not apply "
                            f"cleanly against the current content_snapshot: {exc}"
                        ) from exc
                    applied_diff_sha256 = digest(str(proposed_diff))
                if content is None:
                    # Unreachable through the branches above, and it stays that
                    # way: the only historical route to this line was the
                    # `content = current["content_snapshot"]` fall-through this
                    # fix deleted. Refusing here means a future edit that
                    # reopens it fails loudly instead of minting a version from
                    # an absent body.
                    raise ValueError(
                        f"proposal {proposal_id!r}: no content resolved for approval"
                    )
                current_snapshot = None if current is None else current["content_snapshot"]
                if current_snapshot is not None and str(content) == str(current_snapshot):
                    # THE invariant, enforced at the one seam that promotes a
                    # version to ACTIVE, so it holds for every content source:
                    # an identical body from proposed_content, a diff that
                    # applies to the same text, or a diff that changes nothing.
                    # Minting version N+1 with the current content records an
                    # improvement that never happened.
                    raise ValueError(
                        f"proposal {proposal_id!r}: approving it would mint a version "
                        "whose content is identical to the current version — an "
                        "approval-stamped no-op. Reject the proposal, or file one that "
                        "changes the content."
                    )
                evidence = {
                    "proposal_id": proposal_id,
                    "files": _json_load(proposal["evidence_files_json"], []),
                    "linked_execution": proposal["linked_execution_id"],
                    "proposed_diff": proposal["proposed_diff"],
                    "applied_diff_sha256": applied_diff_sha256,
                    "diff_mismatch": diff_mismatch,
                }
                version_number, version_id = _insert_version(
                    connection,
                    str(proposal["skill_id"]),
                    content=str(content),
                    preferred_method=None,
                    fallback_method=None,
                    change_reason=f"Approved update proposal {proposal_id}",
                    evidence=evidence,
                    author=str(decided_by),
                )
                connection.execute(
                    "UPDATE skill_versions SET status = 'superseded' WHERE skill_id = ?",
                    (proposal["skill_id"],),
                )
                connection.execute(
                    "UPDATE skill_versions SET status = 'active' WHERE id = ?", (version_id,)
                )
                connection.execute(
                    "UPDATE skills SET current_version = ?, updated_at = ? WHERE id = ?",
                    (version_number, utc_now_iso(), proposal["skill_id"]),
                )
            connection.execute(
                "UPDATE update_proposals SET state = ?, decided_by = ?, decided_at = ?, "
                "decision_note = ? WHERE id = ?",
                (target_state, decided_by, utc_now_iso(), note, proposal_id),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        updated = connection.execute(
            "SELECT * FROM update_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        assert updated is not None
        result = _proposal_dict(updated)
        result["new_version"] = version_number
        result["version_info"] = _version_dict(
            connection.execute(
                "SELECT * FROM skill_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if version_id is not None
            else None
        )
        _notify(
            connection,
            proposal_id=str(proposal_id),
            title=f"Skill update {target_state}",
            body=f"Proposal {proposal_id} was {target_state} by {decided_by}",
            severity="info",
            payload={
                "skill_id": updated["skill_id"],
                "state": target_state,
                "new_version": version_number,
            },
        )
        return result


def restore_version(skill_id: str, version: int) -> dict:
    """Select an existing historical version without copying its content."""
    with _database() as connection:
        selected = connection.execute(
            "SELECT 1 FROM skill_versions WHERE skill_id = ? AND version = ?",
            (skill_id, version),
        ).fetchone()
        if selected is None:
            raise KeyError(f"version {version} for skill {skill_id!r} not found")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE skill_versions SET status = 'superseded' WHERE skill_id = ?", (skill_id,)
            )
            connection.execute(
                "UPDATE skill_versions SET status = 'active' WHERE skill_id = ? AND version = ?",
                (skill_id, version),
            )
            connection.execute(
                "UPDATE skills SET current_version = ?, updated_at = ? WHERE id = ?",
                (version, utc_now_iso(), skill_id),
            )
            if connection.total_changes == 0:
                raise KeyError(f"skill {skill_id!r} not found")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return _skill_from_connection(connection, str(skill_id))


def release_quarantine(skill_id: str, *, released_by: str, note: str | None = None) -> dict:
    """Lift a machine quarantine, restoring the skill to ``active``.

    This is the reversal side of the U-16 hygiene pass: nothing was deleted, so
    an operator who disagrees with a quarantine can put the row back into
    selection. The release survives the next vault re-index because
    :func:`upsert_skill` only ever auto-quarantines a NEWLY inserted row.

    Raises ``KeyError`` for an unknown skill and ``ValueError`` when the skill
    is not quarantined — releasing something that was never held would be a
    silent no-op reported as a success.
    """
    with _database() as connection:
        row = connection.execute(
            "SELECT id, status FROM skills WHERE id = ? OR slug = ?",
            (skill_id, skill_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"skill {skill_id!r} not found")
        if str(row["status"]) != QUARANTINED_STATUS:
            raise ValueError(f"skill {skill_id!r} is not quarantined (status={row['status']!r})")
        resolved_id = str(row["id"])
        connection.execute(
            "UPDATE skills SET status = 'active', quarantine_reason = ?, "
            "quarantined_at = NULL, updated_at = ? WHERE id = ?",
            (
                f"released_by:{released_by}" + (f"; {note}" if note else ""),
                utc_now_iso(),
                resolved_id,
            ),
        )
        return _skill_from_connection(connection, resolved_id)


def mark_preferred(skill_id: str, method: str) -> dict:
    """Set the skill-level preferred method used by tree/list consumers."""
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE skills SET preferred_method = ?, updated_at = ? WHERE id = ?",
            (method, utc_now_iso(), skill_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"skill {skill_id!r} not found")
        return _skill_from_connection(connection, str(skill_id))


def search(q: str, *, limit: int = 20) -> list[dict]:
    """Search the structured index using deterministic SQLite LIKE scoring."""
    query = q.strip()
    if not query or limit < 1:
        return []
    pattern = f"%{query}%"
    with _database() as connection:
        rows = connection.execute(
            "SELECT id, slug, title, summary, preferred_method, fallback_method FROM skills "
            "WHERE slug LIKE ? COLLATE NOCASE OR title LIKE ? COLLATE NOCASE "
            "OR summary LIKE ? COLLATE NOCASE OR preferred_method LIKE ? COLLATE NOCASE "
            "OR fallback_method LIKE ? COLLATE NOCASE LIMIT ?",
            (pattern, pattern, pattern, pattern, pattern, int(limit)),
        ).fetchall()
    lowered = query.casefold()
    results: list[dict[str, Any]] = []
    for row in rows:
        title = str(row["title"] or "").casefold()
        slug = str(row["slug"] or "").casefold()
        summary = str(row["summary"] or "").casefold()
        score = 1.0 if lowered == title or lowered == slug else 0.8 if lowered in title else 0.6
        if lowered in summary and score < 0.7:
            score = 0.7
        results.append(
            {"id": row["id"], "slug": row["slug"], "title": row["title"], "score": score}
        )
    results.sort(key=lambda item: (-float(item["score"]), str(item["title"]).casefold()))
    return results[: int(limit)]


def _metadata_for_note(path: Path, content: str) -> tuple[dict[str, Any], str]:
    metadata: dict[str, Any] = {}
    body = content
    frontmatter = _FRONTMATTER_RE.match(content)
    if frontmatter is not None:
        parsed = yaml.safe_load(frontmatter.group("yaml"))
        if isinstance(parsed, dict):
            metadata.update(parsed)
        body = content[frontmatter.end() :]
    library = _LIBRARY_METADATA_RE.search(body)
    if library is not None:
        parsed = yaml.safe_load(library.group("yaml"))
        if isinstance(parsed, dict):
            metadata.update(parsed)
        body = body[: library.start()] + body[library.end() :]
    filename_id = path.stem.removeprefix("skill-")
    metadata.setdefault("id", filename_id)
    metadata.setdefault("slug", metadata["id"])
    title_match = re.search(r"^#\s+(?:Skill:\s*)?(.+?)\s*$", body, re.MULTILINE)
    metadata.setdefault(
        "title", title_match.group(1) if title_match else filename_id.replace("_", " ").title()
    )
    purpose = re.search(r"^##\s+Purpose\s*$\s*(.+?)(?=\n##\s|\Z)", body, re.MULTILINE | re.DOTALL)
    if purpose is not None:
        purpose_text = " ".join(
            line.strip() for line in purpose.group(1).splitlines() if line.strip()
        )
        metadata.setdefault("summary", purpose_text)
    metadata.setdefault("summary", "")
    discipline = str(metadata.get("discipline") or "Uncategorized")
    metadata.setdefault("category", discipline.replace("-", " ").title())
    metadata.setdefault("subcategory", "General")
    metadata.setdefault("status", "active")
    metadata.setdefault("vault_note_path", f"vault/playbook/{path.name}")
    # Declared selection metadata. A note that says nothing declares nothing —
    # these stay absent rather than defaulting to a favourable empty list, so
    # upsert_skill leaves any previously declared values alone.
    for field, _column in _SELECTION_LIST_FIELDS:
        if field in metadata:
            metadata[field] = _string_list(metadata[field])
    return metadata, body.strip() + "\n"


def sync_playbook_from_repo(source: Path | None = None) -> int:
    """Mirror <repo>/vault/playbook/*.md into default_vault_dir()/playbook.

    Copy-only (never deletes); skips when source and destination resolve to
    the same directory. The runtime copy is a read-only mirror — edit the
    repo vault, not the runtime vault.

    Returns 0 for a genuine empty source directory. Raises FileNotFoundError
    when the source path is missing so callers cannot confuse "not measured"
    with a successful zero-copy run.
    """
    source = source or Path(_repo_root()) / "vault" / "playbook"
    if not source.exists():
        raise FileNotFoundError(f"playbook source not found: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"playbook source is not a directory: {source}")
    dest = Path(default_vault_dir()) / "playbook"
    # Safety (`is not False`): unknown identity must not risk a self-copy.
    if inode_paths_equal(source.resolve(), dest.resolve()) is not False:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for note in sorted(source.glob("*.md")):
        target = dest / note.name
        if target.exists():
            src_stat, dst_stat = note.stat(), target.stat()
            if (src_stat.st_size, src_stat.st_mtime) == (dst_stat.st_size, dst_stat.st_mtime):
                continue
        shutil.copy2(note, target)
        copied += 1
    return copied


def index_vault_playbook() -> int:
    """Import or refresh every ``vault/playbook/skill-*.md`` note idempotently.

    Existing skills are UPDATED in place: structured fields and the current
    version's ``content_snapshot`` are overwritten from the vault note. The
    vault is the source of truth — manual content edits to the current
    version are superseded on the next index run (edits saved as new,
    unselected versions survive).

    Returns 0 for a genuine empty playbook directory. Raises FileNotFoundError
    when the playbook path is missing so a failed measure is not logged as a
    successful zero-import count.

    One malformed note no longer costs the rest of the corpus: every note is
    attempted, failures are collected, and a ``RuntimeError`` naming them is
    raised AFTER the good notes have landed. The returned count is the number
    actually indexed — never a claim about the ones that failed.
    """
    playbook = Path(default_vault_dir()) / "playbook"
    if not playbook.exists():
        raise FileNotFoundError(f"playbook directory not found: {playbook}")
    if not playbook.is_dir():
        raise NotADirectoryError(f"playbook path is not a directory: {playbook}")
    count = 0
    failures: list[str] = []
    for path in sorted(playbook.glob("skill-*.md")):
        try:
            content = path.read_text(encoding="utf-8")
            metadata, body = _metadata_for_note(path, content)
            payload: dict[str, Any] = {
                "id": str(metadata["id"]),
                "slug": str(metadata.get("slug") or metadata["id"]),
                "category": str(metadata["category"]),
                "subcategory": str(metadata["subcategory"]),
                "title": str(metadata["title"]),
                "summary": str(metadata.get("summary") or ""),
                "preferred_method": metadata.get("preferred_method"),
                "fallback_method": metadata.get("fallback_method"),
                "vault_note_path": str(metadata["vault_note_path"]),
                "status": str(metadata["status"]),
                "content_snapshot": body,
                "change_reason": "Imported from vault playbook",
                "author": "system:vault-index",
            }
            for field, _column in _SELECTION_LIST_FIELDS:
                if field in metadata:
                    payload[field] = metadata[field]
            upsert_skill(payload)
        except Exception as exc:  # noqa: BLE001 -- one bad note must not hide the rest
            LOG.exception("vault index failed for %s", path.name)
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        count += 1
    if failures:
        raise RuntimeError(
            f"indexed {count} playbook note(s); {len(failures)} failed: " + "; ".join(failures)
        )
    return count


def list_proposals(*, state: str | None = None) -> list[dict[str, Any]]:
    """List update proposals for the API queue."""
    if state is not None and state not in _VALID_STATES:
        raise ValueError(f"invalid proposal state {state!r}")
    with _database() as connection:
        if state is None:
            rows = connection.execute(
                "SELECT * FROM update_proposals ORDER BY created_at DESC, id DESC"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM update_proposals WHERE state = ? ORDER BY created_at DESC, id DESC",
                (state,),
            ).fetchall()
    return [_proposal_dict(row) for row in rows]


__all__ = [
    "AUTO_CAPTURE_QUARANTINE_REASON",
    "QUARANTINED_STATUS",
    "VALID_SKILL_STATUSES",
    "decide_proposal",
    "get_skill",
    "index_vault_playbook",
    "list_proposals",
    "list_skills",
    "list_tree",
    "mark_preferred",
    "new_version",
    "propose_update",
    "release_quarantine",
    "restore_version",
    "search",
    "sync_playbook_from_repo",
    "upsert_skill",
]
