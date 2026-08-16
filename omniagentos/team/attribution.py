"""Deterministic evidence-to-task attribution.

The matchers in this module are deliberately pure: collectors provide ordinary
dicts, and the sweep decides whether and how to persist the result.

**A bare short ref is not a reference.** ``U1``-``U8``/``S1``-``S8`` are the
seeded card refs, and they are also ordinary English: "move uploads bucket to
S3", "security(U-S1) hardening", "S3-creds". Matching ``\\b[A-Z]+-?\\d+\\b``
anywhere in a commit subject attributed 6 of 7 recent real commits on this repo
to a card that had nothing to do with them, which is worse than leaving them
unattributed — an unattributed row sits in an inbox a human empties, while a
wrongly attributed one silently inflates somebody's verified output.

So a short ref only counts when the author SAID it is a reference:

* in prose (commit message, PR title, PR body) it must carry a marker —
  ``refs U3``, ``closes S5``, ``fixes OPS-2``, ``task: U3`` (the convention
  ``docs/operations/team-queue-protocol.md`` publishes);
* in a branch name it must be a whole delimited segment, CASE-SENSITIVELY equal
  to the ref (``fix/U3-resume`` matches ``U3``; ``feat/s3-uploads`` matches
  nothing, because a lowercase slug word is prose, not a ref).

Full ``btk_`` ids stay matchable anywhere: they are 20+ characters of this
system's own namespace and cannot be typed by accident.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

#: A short ref ONLY where an explicit marker word introduces it. The marker is
#: what distinguishes a reference from a noun that happens to look like one.
_MARKED_REF_RE = re.compile(r"(?i)\b(?:refs|closes|fixes|task):?\s+([A-Za-z]+-?\d+)\b")
_TASK_ID_RE = re.compile(r"\bbtk_\w+\b")
#: Branch-name segment delimiters. A ref is matched as a whole segment so
#: ``feature/S5-thing`` names S5 while ``feature/S51-thing`` does not.
_BRANCH_DELIMITERS = "/-_."
_ACTOR_WINDOW = timedelta(hours=12)


def _prose_fields(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Author-written prose: commit message / PR title / PR body."""
    meta = candidate.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    return tuple(
        str(value) for value in (candidate.get("title", ""), meta.get("body", "")) if value
    )


def _branch_fields(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Branch names that belong to THIS candidate — ``meta.head_branch`` only.

    ``branch_hint`` is deliberately excluded: for commit candidates it is the
    repo's *current* checked-out branch at collection time, not the branch the
    commit was authored on, so matching on it bulk-attributes every commit in
    the window to whatever card the parked branch happens to name (measured:
    a checkout sitting on ``fix/U3-resume`` attributed unrelated ``main``
    commits to U3). PR candidates carry their real branch in
    ``meta.head_branch``, which is the only trustworthy branch signal.
    """
    meta = candidate.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    head_branch = meta.get("head_branch", "")
    return (str(head_branch),) if head_branch else ()


def _text_fields(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Every text field, for the ``btk_`` id scan (which is safe everywhere)."""
    return _prose_fields(candidate) + _branch_fields(candidate)


def _branch_names_ref(branches: tuple[str, ...], ref: str) -> bool:
    """True when ``ref`` is a whole delimited segment of one of ``branches``.

    Case-SENSITIVE on purpose: refs are uppercase (the collab store normalizes
    them), branch slugs are lowercase words. ``feat/s3-uploads`` must not read
    as a reference to card ``S3``.
    """
    pattern = re.compile(
        rf"(?:^|[{re.escape(_BRANCH_DELIMITERS)}])"
        rf"{re.escape(ref)}"
        rf"(?:$|[{re.escape(_BRANCH_DELIMITERS)}])"
    )
    return any(pattern.search(branch) for branch in branches)


def _unique_task_id(task_ids: list[str]) -> str | None:
    unique = set(task_ids)
    return next(iter(unique)) if len(unique) == 1 else None


def _normalized_path(value: str) -> str:
    text = str(value).strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _owns_any(owned_paths: Any, files: set[str]) -> bool:
    """True when any changed file is (or is under) one of the card's owned paths.

    An entry is matched as a file OR as a directory prefix, so
    ``omniagentos/team/`` — and the equivalent ``omniagentos/team`` — claim
    every file beneath them. Without this, a card that owns a package only ever
    matched a commit that touched the directory name itself, which no commit
    ever does.
    """
    if not isinstance(owned_paths, list):
        return False
    for entry in owned_paths:
        owned = _normalized_path(str(entry))
        if not owned:
            continue
        prefix = f"{owned}/"
        if any(path == owned or path.startswith(prefix) for path in files):
            return True
    return False


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def match_task_detailed(
    candidate_evidence: dict,
    open_tasks: list[dict],
    *,
    actor_employee_map: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return the uniquely matched task and matcher name, or ``(None, None)``."""

    prose = _prose_fields(candidate_evidence)
    branches_named = _branch_fields(candidate_evidence)
    marked_refs = {
        match.group(1).upper() for text in prose for match in _MARKED_REF_RE.finditer(text)
    }
    task_refs = {
        match.group(0)
        for text in _text_fields(candidate_evidence)
        for match in _TASK_ID_RE.finditer(text)
    }
    if marked_refs or task_refs or branches_named:
        explicit_matches: list[str] = []
        for task in open_tasks:
            if str(task.get("id", "")) in task_refs:
                explicit_matches.append(str(task["id"]))
                continue
            ref = None if task.get("ref") is None else str(task["ref"]).strip()
            if not ref:
                continue
            if ref.upper() in marked_refs or _branch_names_ref(branches_named, ref):
                explicit_matches.append(str(task["id"]))
        explicit = _unique_task_id(explicit_matches)
        if explicit is not None:
            return explicit, "explicit_ref"
        # Multiple real task references are an explicit ambiguity. Do not let
        # a weaker heuristic choose between cards the evidence itself named.
        if len(set(explicit_matches)) > 1:
            return None, None

    meta = candidate_evidence.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    kind = candidate_evidence.get("kind")
    run_link = candidate_evidence.get("ref") if kind == "session" else meta.get("run_id")
    branch_link = meta.get("head_branch") if kind == "pr" else None
    linked_matches: list[str] = []
    for task in open_tasks:
        task_run = task.get("run_id")
        branches = task.get("branches", [])
        if not isinstance(branches, list):
            branches = []
        if (run_link and task_run and str(run_link) == str(task_run)) or (
            branch_link and branch_link in branches
        ):
            linked_matches.append(str(task["id"]))
    linked = _unique_task_id(linked_matches)
    if linked is not None:
        return linked, "existing_link"
    # Two cards claiming the same run/branch is an ambiguity IN THE LINK, not an
    # invitation to guess with a weaker signal: falling through would let the
    # owned-paths or actor-window matcher pick one of the two cards the link
    # already failed to choose between, and record it as if it were certain.
    if len(set(linked_matches)) > 1:
        return None, None

    files = candidate_evidence.get("files", []) if kind == "commit" else []
    file_set = {_normalized_path(str(path)) for path in files} if isinstance(files, list) else set()
    file_set.discard("")
    path_matches: list[str] = []
    if file_set:
        for task in open_tasks:
            if _owns_any(task.get("owned_paths", []), file_set):
                path_matches.append(str(task["id"]))
    path_match = _unique_task_id(path_matches)
    if path_match is not None:
        return path_match, "owned_paths"
    if len(set(path_matches)) > 1:
        return None, None

    if actor_employee_map is None:
        return None, None
    employee_id = actor_employee_map.get(str(candidate_evidence.get("actor", "")))
    occurred_at = _parse_iso(candidate_evidence.get("occurred_at"))
    if employee_id is None or occurred_at is None:
        return None, None

    window_matches: list[str] = []
    for task in open_tasks:
        if task.get("status") != "in_progress" or task.get("owner_employee_id") != employee_id:
            continue
        updated_at = _parse_iso(task.get("updated_at"))
        if updated_at is not None and abs(updated_at - occurred_at) <= _ACTOR_WINDOW:
            window_matches.append(str(task["id"]))
    window_match = _unique_task_id(window_matches)
    if window_match is not None:
        return window_match, "actor_window"
    return None, None
