"""Install this repo's skills into one machine's Claude Code skills directory.

SELF-CONTAINED BY DESIGN: stdlib only, no omniagentos imports, so the file can
be copied to any teammate's laptop or server and run with a bare ``python3`` —
that is the whole deployment story for dev machines (same shape as
``omniagentos/team/session_collector.py``).

    python3 skills_sync.py --print                 # dry run: touches nothing
    python3 skills_sync.py                         # pull, install, notify
    */30 * * * * python3 $HOME/bin/skills_sync.py >> $HOME/.skills-sync.log 2>&1

Sources (both, in this order): ``<repo>/skills/`` — hand-written operator
skills — and ``<repo>/skills-lib/`` — exported from the skill-library database
by ``scripts/team/skills_publish.py``. Every subdirectory holding a ``SKILL.md``
is one skill, installed as ``<claude-dir>/<slug>/``. A slug present in both
sources is taken from ``skills/``: a human wrote it deliberately, and an
automated export must not overwrite that.

**A dev's personal skills are sacred.** This tool owns exactly the slugs
recorded in its state manifest (``--state``). A directory in the Claude skills
dir that the manifest does not own is never overwritten and never deleted, not
even when the repo grows a skill of the same name — that case is reported and
skipped. The manifest is also what makes removal safe: a skill that vanishes
from the repo is deleted from the Claude dir only because this tool put it
there. The corollary: if the manifest is lost or corrupted, this tool stops
owning what it installed and reports those directories as foreign rather than
adopting them. Recover by deleting the ``<claude-dir>/<slug>`` copies it should
own and re-running — never by hand-editing the manifest to claim a directory.

Exit codes: 0 on success (whether or not anything changed), 2 on a bad
invocation (missing repo, unusable directory). A failed ``git pull`` or a
failed Slack post is a warning on stderr, not a failure — a laptop that is
offline should still install from the tree it already has.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
SKILL_FILENAME = "SKILL.md"
SOURCE_DIRS = ("skills", "skills-lib")
UNKNOWN_VERSION = "-"
WEBHOOK_TIMEOUT_SECONDS = 10
PULL_TIMEOUT_SECONDS = 120

INSTALL = "installed"
UPDATE = "updated"
REMOVE = "removed"
FOREIGN = "skipped-foreign"


@dataclass(frozen=True)
class SkillSource:
    """One skill directory found in the repo."""

    slug: str
    path: Path
    version: str
    source: str
    digest: str


@dataclass(frozen=True)
class Action:
    """One planned change to the Claude skills directory."""

    kind: str
    slug: str
    version: str
    source: SkillSource | None = None
    detail: str = ""

    def line(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.kind:<15} {self.slug}@{self.version}{suffix}"


def _warn(message: str) -> None:
    print(f"skills-sync: {message}", file=sys.stderr)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _safe_slug(slug: str) -> bool:
    """True when *slug* is a single safe path segment.

    Slugs become literal directory names under the Claude skills dir — on
    install and, via the manifest, on REMOVE (an rmtree). A slug with a path
    separator or a leading dot must never reach a filesystem verb, whether it
    came from the repo or from a corrupted manifest.
    """
    return bool(_SLUG_RE.fullmatch(slug))


def _contains_symlink(root: Path) -> bool:
    """True when *root* is, or contains, any symlink."""
    if root.is_symlink():
        return True
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            if (Path(dirpath) / name).is_symlink():
                return True
    return False


def digest_dir(path: Path) -> str:
    """Stable content digest of a skill directory.

    Hashes sorted relative POSIX paths together with each file's bytes, with
    the path length framed in, so that renaming a file or moving content
    between files changes the digest. Ignores mtimes and modes: a plain
    ``git checkout`` must not look like a content change.
    """
    hasher = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file.relative_to(path).as_posix().encode("utf-8")
        body = file.read_bytes()
        hasher.update(str(len(rel)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(rel)
        hasher.update(str(len(body)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(body)
    return hasher.hexdigest()


def _frontmatter_value(skill_md: Path, key: str) -> str:
    """Read one scalar out of a ``SKILL.md`` YAML frontmatter block.

    Deliberately a scanner, not a YAML parser (stdlib only, and PyYAML is not
    installable on every dev machine): it reads top-level ``key: value`` lines
    inside the leading ``---`` fences, skips indented continuation lines, and
    strips one layer of quoting. Anything it cannot read is absent, never
    guessed.
    """
    try:
        with skill_md.open("r", encoding="utf-8", errors="replace") as handle:
            if handle.readline().strip() != "---":
                return ""
            for _ in range(200):
                line = handle.readline()
                if not line or line.strip() == "---":
                    break
                if line[:1] in (" ", "\t", "#") or ":" not in line:
                    continue
                name, _, raw = line.partition(":")
                if name.strip() != key:
                    continue
                value = raw.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    except OSError:
        return ""
    return ""


def discover(repo: Path) -> dict[str, SkillSource]:
    """Every skill directory in the repo, keyed by slug (its directory name).

    ``skills/`` wins over ``skills-lib/`` on a slug collision; the shadowed
    export is reported so a duplicate slug is visible rather than silently
    resolved.
    """
    found: dict[str, SkillSource] = {}
    for source in SOURCE_DIRS:
        root = repo / source
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / SKILL_FILENAME).is_file():
                continue
            slug = child.name
            if not _safe_slug(slug):
                _warn(f"{source}/{slug}: unsafe slug — skipped")
                continue
            if _contains_symlink(child):
                _warn(f"{source}/{slug}: contains a symlink — skipped (skills are plain files)")
                continue
            if slug in found:
                _warn(
                    f"{source}/{slug} is shadowed by {found[slug].source}/{slug} "
                    "(hand-written skills win)"
                )
                continue
            found[slug] = SkillSource(
                slug=slug,
                path=child,
                version=_frontmatter_value(child / SKILL_FILENAME, "version") or UNKNOWN_VERSION,
                source=source,
                digest=digest_dir(child),
            )
    return found


def load_state(path: Path) -> dict[str, Any]:
    """Read the manifest. A missing or unreadable manifest owns NOTHING.

    That is the safe direction: an unreadable manifest makes the tool install
    fresh copies (refusing to overwrite what it does not own) instead of
    deleting a dev's skills on the strength of a file it could not parse.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if path.exists():
            _warn(f"state file {path} is unreadable ({exc}); treating it as owning nothing")
        return {"schema": SCHEMA, "skills": {}}
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        _warn(f"state file {path} has an unexpected shape; treating it as owning nothing")
        return {"schema": SCHEMA, "skills": {}}
    return data


def write_state_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".skills-sync-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def plan(sources: dict[str, SkillSource], owned: dict[str, Any], claude_dir: Path) -> list[Action]:
    """Decide what to install, update and remove. Reads only; changes nothing."""
    actions: list[Action] = []
    for slug in sorted(sources):
        source = sources[slug]
        dest = claude_dir / slug
        entry = owned.get(slug)
        if entry is None:
            if dest.exists():
                actions.append(
                    Action(
                        FOREIGN,
                        slug,
                        source.version,
                        source,
                        "already in the skills dir and not owned by this tool",
                    )
                )
                continue
            actions.append(Action(INSTALL, slug, source.version, source))
            continue
        if not dest.exists():
            # We own it but it is gone (a dev cleaned house, a new laptop).
            actions.append(Action(INSTALL, slug, source.version, source, "reinstall"))
            continue
        if str(entry.get("digest") or "") != source.digest:
            actions.append(Action(UPDATE, slug, source.version, source))
    for slug in sorted(owned):
        if slug in sources:
            continue
        if not _safe_slug(slug):
            _warn(f"manifest entry {slug!r}: unsafe slug — ignored, never a removal path")
            continue
        entry = owned[slug]
        version = str(entry.get("version") or UNKNOWN_VERSION)
        if (claude_dir / slug).exists():
            actions.append(Action(REMOVE, slug, version, None, "gone from the repo"))
        else:
            actions.append(Action(REMOVE, slug, version, None, "already absent"))
    return actions


def apply(actions: list[Action], owned: dict[str, Any], claude_dir: Path) -> list[Action]:
    """Perform the plan, mutating *owned*. Returns the actions that took effect.

    A failure on one skill is reported and skipped; the rest still install. The
    manifest is only updated for what actually landed, so a failed copy is
    retried on the next run instead of being recorded as installed.
    """
    applied: list[Action] = []
    for action in actions:
        if action.kind == FOREIGN:
            _warn(
                f"{action.slug}: a directory of that name already exists in {claude_dir} and "
                "is not owned by this tool — left untouched"
            )
            continue
        if not _safe_slug(action.slug):
            _warn(f"{action.slug}: unsafe slug — skipped")
            continue
        dest = claude_dir / action.slug
        # Belt-and-braces against a caller or a TOCTOU swap: the thing we are
        # about to rmtree/copy into must be an immediate child of claude_dir and
        # not reached through a symlinked component.
        if dest.is_symlink() or dest.parent.resolve() != claude_dir.resolve():
            _warn(f"{action.slug}: destination is not a direct child of {claude_dir} — skipped")
            continue
        try:
            if action.kind == REMOVE:
                if dest.exists():
                    shutil.rmtree(dest)
                owned.pop(action.slug, None)
            else:
                assert action.source is not None
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(action.source.path, dest)
                owned[action.slug] = {
                    "digest": action.source.digest,
                    "version": action.source.version,
                    "source": action.source.source,
                    "installed_at": _iso(time.time()),
                }
        except OSError as exc:
            _warn(f"{action.slug}: {action.kind} failed: {exc}")
            continue
        applied.append(action)
    return applied


def git_pull(repo: Path) -> bool:
    """``git pull --rebase`` the clone; a failure is a warning, not an error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "pull", "--rebase", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=PULL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _warn(f"git pull failed ({exc}); continuing on the tree already on disk")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        _warn(
            f"git pull failed ({detail[-1] if detail else 'no output'}); "
            "continuing on the tree already on disk"
        )
        return False
    return True


def summary_message(applied: list[Action], host: str) -> str:
    """The one-line notification: ``skills-sync <host>: updated a@1, b@2``."""
    parts = [
        f"{action.slug}@{action.version}" if action.kind != REMOVE else f"{action.slug} (removed)"
        for action in applied
    ]
    return f"skills-sync {host}: updated {', '.join(parts)}"


def post_webhook(url: str, text: str) -> bool:
    """POST one Slack message. Never raises: notification is not the job."""
    body = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 — a failed notification must not fail the sync
        _warn(f"slack post failed: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", default=str(Path.home() / "OmniAgentOS"), help="OmniAgentOS clone"
    )
    parser.add_argument(
        "--claude-dir",
        default=str(Path.home() / ".claude" / "skills"),
        help="Claude Code skills directory to install into",
    )
    parser.add_argument(
        "--state",
        default=str(Path.home() / ".skills-sync-state.json"),
        help="manifest of the skills this tool owns on this machine",
    )
    parser.add_argument("--no-pull", action="store_true", help="do not git pull the clone first")
    parser.add_argument(
        "--print",
        dest="dry_run",
        action="store_true",
        help="print the plan and exit; touches nothing (implies --no-pull)",
    )
    parser.add_argument("--slack-webhook", help="post a one-line summary here on change")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser()
    claude_dir = Path(args.claude_dir).expanduser()
    state_path = Path(args.state).expanduser()
    if not repo.is_dir():
        _warn(f"repo {repo} is not a directory")
        return 2
    if claude_dir.exists() and not claude_dir.is_dir():
        _warn(f"claude dir {claude_dir} exists and is not a directory")
        return 2
    if not any((repo / source).is_dir() for source in SOURCE_DIRS):
        _warn(f"repo {repo} holds neither {' nor '.join(SOURCE_DIRS)}/ — nothing to sync")
        return 2

    if not args.dry_run and not args.no_pull:
        git_pull(repo)

    sources = discover(repo)
    state = load_state(state_path)
    owned: dict[str, Any] = dict(state.get("skills") or {})
    actions = plan(sources, owned, claude_dir)

    if args.dry_run:
        print(f"skills-sync: {len(sources)} skill(s) in {repo}, target {claude_dir}")
        for action in actions:
            print(f"  would {action.line()}")
        if not actions:
            print("  (no changes)")
        return 0

    if not actions:
        print(f"skills-sync: up to date ({len(sources)} skill(s))")
        return 0

    claude_dir.mkdir(parents=True, exist_ok=True)
    applied = apply(actions, owned, claude_dir)
    for action in applied:
        print(f"skills-sync: {action.line()}")
    if not applied:
        return 0

    write_state_atomic(
        state_path,
        {
            "schema": SCHEMA,
            "claude_dir": str(claude_dir),
            "repo": str(repo),
            "updated_at": _iso(time.time()),
            "skills": owned,
        },
    )
    if args.slack_webhook:
        post_webhook(args.slack_webhook, summary_message(applied, socket.gethostname()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
