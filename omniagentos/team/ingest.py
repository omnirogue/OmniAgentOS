"""Evidence source readers for the Team Work OS sweep."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class IngestUnavailable(RuntimeError):
    """A source could not be reached and must not be retried blindly."""


_GITHUB_MAP_PATH = Path(__file__).resolve().parent.parent.parent / "configs/team_github_map.yaml"


class ActorEmployeeMap(dict[str, str]):
    """Config mapping whose health survives an empty/failing mapping."""

    def __init__(self, *args: Any, loaded: bool = True, error: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.loaded = loaded
        self.error = error


def load_actor_employee_map(path: Path | None = None) -> ActorEmployeeMap:
    """Read the sole GitHub/email identity map; a bad file disables inference safely."""

    target = path or _GITHUB_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return ActorEmployeeMap(loaded=False, error=f"could not load {target}: {exc}")
    if not isinstance(raw, dict) or not all(isinstance(value, str) for value in raw.values()):
        return ActorEmployeeMap(
            loaded=False, error=f"could not load {target}: expected aliases to employee ids"
        )
    return ActorEmployeeMap({str(alias): str(employee) for alias, employee in raw.items()})


# Ordinary sweep attribution and inference deliberately share this one config.
ACTOR_EMPLOYEE_MAP: ActorEmployeeMap = load_actor_employee_map()

_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _commit_records(output: str) -> Iterator[tuple[list[str], list[str]]]:
    header: list[str] | None = None
    files: list[str] = []
    for raw_line in output.splitlines():
        parts = raw_line.split("|", 4)
        if len(parts) == 5 and _COMMIT_SHA_RE.fullmatch(parts[0]):
            if header is not None:
                yield header, files
            header = parts
            files = []
        elif header is not None and raw_line:
            files.append(raw_line)
    if header is not None:
        yield header, files


def iter_commits(
    repo_path: str,
    since_iso: str,
    until_iso: str | None = None,
    *,
    repo_label: str | None = None,
) -> Iterator[dict]:
    """Yield non-merge git commits in the requested author-date window."""

    try:
        branch_result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise IngestUnavailable(f"git branch probe failed for {repo_path}: {exc}") from exc
    branch_hint = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if branch_hint == "HEAD":
        branch_hint = ""
    command = [
        "git",
        "-C",
        repo_path,
        "log",
        "--no-merges",
        f"--since={since_iso}",
    ]
    if until_iso is not None:
        command.append(f"--until={until_iso}")
    command.extend(["--format=%H|%an|%ae|%aI|%s", "--name-only"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IngestUnavailable(f"git log failed for {repo_path}: {exc}") from exc
    label = repo_path if repo_label is None else repo_label
    for header, files in _commit_records(result.stdout):
        sha, author_name, author_email, author_date, subject = header
        yield {
            "kind": "commit",
            "ref": sha,
            "repo": label,
            "actor": author_email,
            "title": subject,
            "occurred_at": author_date,
            "files": files,
            "branch_hint": branch_hint,
            # Git does not reliably expose a GitHub login, but a number of
            # contributors deliberately use that login as their Git author
            # name.  Keep it as metadata for the opt-in inference loop; the
            # ordinary attribution sweep continues to use the email actor.
            "meta": {"author_name": author_name},
        }


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def preflight_github() -> None:
    """Prove the GitHub CLI is callable and authenticated once per caller run."""

    try:
        probe = subprocess.run(
            ["gh", "api", "user", "-q", ".login"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise IngestUnavailable(f"gh preflight failed: {exc}") from exc
    if probe.returncode != 0:
        raise IngestUnavailable(f"gh preflight failed: {probe.stderr.strip()}")


def iter_prs(
    repo_slug: str,
    since_iso: str,
    *,
    preflight: bool = True,
    include_open: bool = False,
) -> Iterator[dict]:
    """Yield recent GitHub PRs, optionally including open work-in-flight PRs.

    The evidence sweep intentionally consumes only completed PRs.  The
    inference loop opts into ``include_open`` to advance a card to review; it
    records that sighting as a non-mechanical note so an open PR can never be
    used to self-verify a card.
    """

    if preflight:
        preflight_github()

    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo_slug,
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title,body,author,createdAt,mergedAt,closedAt,state,headRefName,headRefOid",
            ],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise IngestUnavailable(f"gh pr list failed: {exc}") from exc
    if result.returncode != 0:
        raise IngestUnavailable(f"gh pr list failed: {result.stderr.strip()}")
    try:
        items = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise IngestUnavailable("gh pr list returned invalid JSON") from exc

    since = _parse_iso(since_iso)
    for item in items:
        state = str(item.get("state", "")).upper()
        merged_at = item.get("mergedAt")
        closed_at = item.get("closedAt")
        activity = ""
        if merged_at:
            occurred_at = str(merged_at)
            quality_gate = "pass"
            activity = "merged"
        elif state == "CLOSED" and closed_at:
            occurred_at = str(closed_at)
            quality_gate = "rejected"
            activity = "closed"
        elif include_open and state == "OPEN":
            occurred_at = str(item.get("createdAt") or "")
            if not occurred_at:
                continue
            # The durable row keeps its stable PR identity so merge can regrade
            # this exact (kind, repo, ref) artifact to pass.  Until then it is
            # explicitly non-mechanical and therefore unverifiable.
            quality_gate = "rejected"
            activity = "opened"
        else:
            # An open PR is work in flight, not completed output. Re-sighting it
            # after merge/close will emit the stable (repo, number) artifact.
            continue
        if _parse_iso(occurred_at) < since:
            continue
        author = item.get("author")
        actor = author.get("login", "") if isinstance(author, dict) else ""
        head_branch = str(item.get("headRefName") or "")
        yield {
            "kind": "pr",
            "ref": f"{repo_slug}#{item['number']}",
            "repo": repo_slug,
            "actor": actor,
            "title": str(item.get("title") or ""),
            "occurred_at": occurred_at,
            "quality_gate": quality_gate,
            "branch_hint": head_branch,
            "meta": {
                "state": state,
                "activity": activity,
                "head_branch": head_branch,
                "head_sha": str(item.get("headRefOid") or ""),
                "body": str(item.get("body") or ""),
            },
        }


def iter_sessions(store: Any, since_iso: str) -> Iterator[dict]:
    """Yield task attempts from the board-linked ``task_sessions`` ledger.

    The generic ``sessions`` table has no task id. The long-haul attempt ledger
    does: ``task_sessions.board_task_id`` is the repository's durable bridge
    between a session attempt and a board card. It has no human actor column,
    so actor remains empty rather than being guessed from a provider/model.
    """

    connection = store._connection
    try:
        rows = connection.execute(
            "SELECT id, board_task_id AS task_id, started_at FROM task_sessions "
            "WHERE started_at >= ? ORDER BY started_at ASC, id ASC",
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        yield {
            "kind": "session",
            "ref": str(row["id"]),
            "task_id": str(row["task_id"]),
            "actor": "",
            "title": "",
            "occurred_at": str(row["started_at"]),
            "repo": "",
        }


def load_cursors(path: str) -> dict[str, str]:
    """Load source cursors, returning an empty mapping when absent."""

    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def save_cursors(path: str, cursors: dict[str, str]) -> None:
    """Atomically replace a cursor file using a temporary sibling."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(cursors, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
