"""Export the DB skill library into ``skills-lib/`` and commit what changed.

The hub cron runs this. It is deliberately thin: all of the judgement (which
statuses are servable, digest verification, the content scan, byte-determinism)
lives in :mod:`omniagentos.skills.export`; this file only puts the result into
git.

    .venv/bin/python scripts/team/skills_publish.py
    30 * * * * cd $HOME/OmniAgentOS && .venv/bin/python scripts/team/skills_publish.py

**It does NOT push.** Publishing to the team is a coordinator action; this
script only produces a local commit, so a cron that runs unattended can never
move a shared branch. Someone (or the hub's own push step) pushes later.

**It writes ``skills-lib/``, never ``skills/``.** ``skills/`` holds
hand-written operator skills; automation must not touch them. Both directories
are installed onto dev machines by ``scripts/team/skills_sync.py``.

The commit only ever carries ``skills-lib/`` paths (``git commit -- skills-lib``),
so an unrelated change already staged in the hub checkout cannot be swept into
an automated commit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):  # running the file directly, not as scripts.team.*
    sys.path.insert(0, str(REPO_ROOT))

from omniagentos.skills.export import ExportReport, export_skills  # noqa: E402

OUT_DIRNAME = "skills-lib"


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one publish run did."""

    report: ExportReport
    committed: bool
    changed_paths: tuple[str, ...]
    message: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def publish(
    repo: str | Path = REPO_ROOT,
    *,
    database: str | Path | None = None,
    commit: bool = True,
) -> PublishResult:
    """Export into ``<repo>/skills-lib`` and commit only if something changed.

    Raises ``RuntimeError`` when git itself fails — a publish that could not
    stage its own output must not report success.
    """
    root = Path(repo).resolve()
    report = export_skills(root / OUT_DIRNAME, database=database)

    staged = _git(root, "add", "--all", "--", OUT_DIRNAME)
    if staged.returncode != 0:
        raise RuntimeError(f"git add failed: {staged.stderr.strip()}")

    listed = _git(root, "diff", "--cached", "--name-only", "--", OUT_DIRNAME)
    if listed.returncode != 0:
        raise RuntimeError(f"git diff --cached failed: {listed.stderr.strip()}")
    changed_paths = tuple(line for line in listed.stdout.splitlines() if line.strip())
    if not changed_paths:
        # Nothing to say: a re-export of an unchanged corpus is byte-identical,
        # which is the whole point of the exporter's determinism rules.
        return PublishResult(report, False, (), "")

    # Count SKILLS, not files: the message is read by humans scanning the log.
    # git reports repo-relative paths, i.e. skills-lib/<slug>/SKILL.md.
    slugs = {
        path.split("/")[1]
        for path in changed_paths
        if path.startswith(f"{OUT_DIRNAME}/") and path.count("/") >= 2
    }
    count = len(slugs) or len(changed_paths)
    message = f"{OUT_DIRNAME}: export {count} changed skill(s)"
    if not commit:
        return PublishResult(report, False, changed_paths, message)

    committed = _git(root, "commit", "-m", message, "--", OUT_DIRNAME)
    if committed.returncode != 0:
        raise RuntimeError(f"git commit failed: {committed.stderr.strip() or committed.stdout}")
    return PublishResult(report, True, changed_paths, message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository clone to publish into")
    parser.add_argument("--db", help="database path (default: the configured library DB)")
    parser.add_argument(
        "--no-commit", action="store_true", help="export and stage, but do not commit"
    )
    args = parser.parse_args(argv)

    try:
        result = publish(args.repo, database=args.db, commit=not args.no_commit)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"skills-publish: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for item in result.report.skipped:
        print(
            f"skills-publish: DROPPED {item.slug}@{item.version}: {item.reason} — {item.detail}",
            file=sys.stderr,
        )
    if not result.changed_paths:
        print(f"skills-publish: no change ({len(result.report.exported)} skill(s) exported)")
        return 0
    verb = "committed" if result.committed else "staged"
    print(f"skills-publish: {verb} — {result.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
