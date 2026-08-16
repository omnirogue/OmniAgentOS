"""History-sweep refusals for ``merge-gate.sh`` (WP-P0 "merge-gate secret hole").

``scripts/merge-gate.sh`` merges candidates with ``git merge --no-ff``, so EVERY
commit in ``merge-base..candidate`` lands in main's history permanently. Every
path-shaped refusal in that file used to grade
``git diff --name-only HEAD...<candidate>`` — the NET tree delta — which is a
different set. A branch that adds ``secrets/leak.env`` in commit 1 and deletes it
in commit 2 has a perfectly clean net diff, and the secret blob is in main
forever, never scanned.

Every fixture branch here therefore has TWO OR MORE commits and a net diff that
looks innocent, plus one real, innocuous file change so that ``no-change`` can
never be the check that refuses — the refusal each case asserts stays the
OPERATIVE one.

Modelled directly on :mod:`tests.scripts.test_merge_gate_m8_refusals`; the fake
interpreter, the receipt minting, the un-armed ``MERGE_GATE_PINNED=0`` default
and the exact-snippet mutation pattern are that module's, reused deliberately so
the two files stay legible side by side.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceStore,
    binding_digest,
    workspace_digest_for,
)
from tests.scripts.test_merge_gate_m8_refusals import fake_python_for, run_contained

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE = REPO_ROOT / "scripts" / "merge-gate.sh"
# The counterfeit harness materialises the tree WITHOUT .venv, so a corpus entry
# that names these tests would otherwise exec an interpreter that is not there.
# The running interpreter is the same venv; falling back to it keeps this file
# usable as a counterfeit must_fail target.
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
REAL_PYTHON = _VENV_PYTHON if _VENV_PYTHON.is_file() else Path(sys.executable)

BASE_MIGRATION = "omniagentos/db/migrations/0001_base.sql"
BASE_MIGRATION_SQL = "-- 0001 base, already on main\nCREATE TABLE base (id INTEGER);\n"
MIGRATIONS_README = "omniagentos/db/migrations/README.md"
MIGRATIONS_README_TEXT = "# migrations\n\npointer doc; the runner never reads this file\n"
SPACED_MIGRATION = "omniagentos/db/migrations/0003_base with space.sql"
SPACED_MIGRATION_SQL = "-- 0003 spaced, already on main\nCREATE TABLE spaced (id INTEGER);\n"


@dataclass(frozen=True)
class FixtureBranch:
    name: str
    candidate_sha: str
    merge_base_sha: str
    refusal: str | None
    reason: str | None


@dataclass(frozen=True)
class SweepRepo:
    path: Path
    evidence_root: Path
    branches: dict[str, FixtureBranch]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _install_fake_python(repo: Path) -> None:
    """Use the real receipt verifier and make the unrelated gate ladder instant.

    Byte-for-byte the helper in ``test_merge_gate_m8_refusals``: the receipt
    verification these fixtures depend on is REAL, and everything the sweep does
    not grade (ladder, ruff, counterfeit corpus) answers instantly.
    """
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    source_root = shlex.quote(str(REPO_ROOT))
    real_python = shlex.quote(str(REAL_PYTHON))
    python.write_text(
        f"""#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ]; then
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf 'CAUGHT    cf-fixture\\n'
  printf -- '------------------------------------------------------------\\n'
  printf '%s\\n' "${{MERGE_GATE_TEST_CF_REPORT:-total=1  caught=1  survived=0  skipped_platform=0  other=0}}"
  exit "${{MERGE_GATE_TEST_CF_RC:-0}}"
fi
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
exec {real_python} "$@"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)


# --- fixture-branch construction ---------------------------------------------
#
# A one-commit fixture cannot express this defect at all: the whole hole lives in
# the gap between "what the tip tree looks like" and "what the merge lands". So
# every helper below builds at least two commits, and each branch carries an
# innocuous real change so its net diff is non-empty.


def _write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, *relatives: str, force: bool = False) -> None:
    for relative in relatives:
        _git(repo, *(("add", "-f", relative) if force else ("add", relative)))
    _git(repo, "commit", "-m", message)


def _add_then_delete_branch(
    repo: Path,
    branch: str,
    *,
    hidden_path: str,
    hidden_content: str,
    innocuous_path: str,
    force: bool = False,
) -> str:
    """Add ``hidden_path`` in commit 1, delete it in commit 2.

    The resulting net diff names ONLY ``innocuous_path``. The blob at
    ``hidden_path`` is still introduced by the merge.
    """
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, hidden_path, hidden_content)
    _commit(repo, f"fixture: {branch} introduces {hidden_path}", hidden_path, force=force)

    _git(repo, "rm", "-q", "-f", hidden_path)
    _write(repo, innocuous_path, f"innocuous lane work for {branch}\n")
    _commit(repo, f"fixture: {branch} deletes {hidden_path}", innocuous_path)

    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _modify_then_revert_branch(
    repo: Path,
    branch: str,
    *,
    target_path: str,
    original_content: str,
    innocuous_path: str,
) -> str:
    """Modify ``target_path`` in commit 1, restore it byte-identically in commit 2."""
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, target_path, original_content + "ALTER TABLE base ADD COLUMN sneaked TEXT;\n")
    _commit(repo, f"fixture: {branch} edits {target_path}", target_path)

    _write(repo, target_path, original_content)
    _write(repo, innocuous_path, f"innocuous lane work for {branch}\n")
    _commit(repo, f"fixture: {branch} reverts {target_path}", target_path, innocuous_path)
    assert (repo / target_path).read_text(encoding="utf-8") == original_content

    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _add_then_modify_branch(repo: Path, branch: str, *, new_path: str) -> str:
    """Add a NEW migration, then iterate on it — the legal shape this must allow."""
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, new_path, "-- 0002 new, not yet on main\nCREATE TABLE fresh (id INTEGER);\n")
    _commit(repo, f"fixture: {branch} adds {new_path}", new_path)

    _write(
        repo, new_path, "-- 0002 new, not yet on main\nCREATE TABLE fresh (id INTEGER, n TEXT);\n"
    )
    _write(repo, "notes/migration-iteration.txt", "reviewed the new migration\n")
    _commit(repo, f"fixture: {branch} fixes {new_path}", new_path, "notes/migration-iteration.txt")

    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _delete_branch(repo: Path, branch: str, *, target_path: str) -> str:
    """Delete a migration main already has — a `D` the M-only filter once dropped."""
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _git(repo, "rm", "-q", target_path)
    _write(repo, "notes/delete-lane.txt", f"lane work around deleting {target_path}\n")
    _commit(repo, f"fixture: {branch} deletes {target_path}", "notes/delete-lane.txt")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _rename_branch(repo: Path, branch: str, *, old_path: str, new_path: str) -> str:
    """Rename a migration main already has — R100 must split into D+A and refuse."""
    _git(repo, "checkout", "-q", "-b", branch, "main")
    # `git mv` stages the rename itself; re-adding old_path would exit 128
    # (the path no longer exists on disk) — commit the staged rename directly.
    _git(repo, "mv", old_path, new_path)
    _git(repo, "commit", "-q", "-m", f"fixture: {branch} renames {old_path}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _merge_only_modify_restore_branch(
    repo: Path,
    branch: str,
    *,
    target_path: str,
    original_content: str,
) -> str:
    """Mutate and restore a migration ONLY inside merge commits (evil merges).

    No non-merge commit touches the file, so a `--no-merges` history walk is
    structurally blind to it while `--no-ff` still lands the mutated blob in
    main's history; only a `-m` per-parent walk can see it.
    """
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, "notes/evil-lane.txt", "lane work before the merges\n")
    _commit(repo, f"fixture: {branch} first", "notes/evil-lane.txt")

    _git(repo, "checkout", "-q", "-b", f"{branch}-side1")
    _write(repo, "notes/evil-side1.txt", "side branchlet one\n")
    _commit(repo, f"fixture: {branch}-side1 work", "notes/evil-side1.txt")
    _git(repo, "checkout", "-q", branch)
    _git(repo, "merge", "--no-ff", "--no-commit", "-q", f"{branch}-side1")
    _write(repo, target_path, original_content + "ALTER TABLE base ADD COLUMN evil TEXT;\n")
    _git(repo, "add", target_path)
    _git(repo, "commit", "-q", "-m", f"fixture: merge {branch}-side1 (mutates in-merge)")

    _git(repo, "checkout", "-q", "-b", f"{branch}-side2")
    _write(repo, "notes/evil-side2.txt", "side branchlet two\n")
    _commit(repo, f"fixture: {branch}-side2 work", "notes/evil-side2.txt")
    _git(repo, "checkout", "-q", branch)
    _git(repo, "merge", "--no-ff", "--no-commit", "-q", f"{branch}-side2")
    _write(repo, target_path, original_content)
    _git(repo, "add", target_path)
    _git(repo, "commit", "-q", "-m", f"fixture: merge {branch}-side2 (restores in-merge)")
    assert (repo / target_path).read_text(encoding="utf-8") == original_content

    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _clean_two_commit_branch(repo: Path, branch: str) -> str:
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, "notes/clean-one.txt", "first honest commit\n")
    _commit(repo, f"fixture: {branch} first", "notes/clean-one.txt")
    _write(repo, "notes/clean-two.txt", "second honest commit\n")
    _commit(repo, f"fixture: {branch} second", "notes/clean-two.txt")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _merge_smuggle_branch(repo: Path, branch: str, *, side: str, hidden_path: str) -> str:
    """Hide the add-then-delete on a SIDE BRANCHLET merged into the candidate.

    The two offending commits are reachable only through the merge commit's
    SECOND parent. ``git rev-list --objects`` walks every parent, so the blob is
    still in the swept set; anything that follows one line of descent — or that
    reads the tip tree — sees nothing at all.
    """
    _git(repo, "checkout", "-q", "-b", branch, "main")
    _write(repo, "notes/smuggle-main.txt", "lane work on the candidate branch\n")
    _commit(repo, f"fixture: {branch} first", "notes/smuggle-main.txt")

    _git(repo, "checkout", "-q", "-b", side)
    _write(repo, hidden_path, "SMUGGLED_TOKEN=not-a-real-secret\n")
    _commit(repo, f"fixture: {side} introduces {hidden_path}", hidden_path)
    _git(repo, "rm", "-q", "-f", hidden_path)
    _write(repo, "notes/smuggle-side.txt", "side branchlet work\n")
    _commit(repo, f"fixture: {side} deletes {hidden_path}", "notes/smuggle-side.txt")

    _git(repo, "checkout", "-q", branch)
    _git(repo, "merge", "--no-ff", "-q", "-m", f"fixture: merge {side}", side)
    _write(repo, "notes/smuggle-after-merge.txt", "more lane work after the merge\n")
    _commit(repo, f"fixture: {branch} after merge", "notes/smuggle-after-merge.txt")

    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _receipt(case: FixtureBranch, repo: Path) -> GateEvidence:
    command = "anthropic-review candidate"
    targets = ("candidate",)
    workspace_digest = workspace_digest_for(repo)
    finished = datetime.now(UTC)
    candidate = case.candidate_sha
    return GateEvidence(
        schema=SCHEMA,
        routine_id="merge-gate",
        run_id=candidate,
        iteration=1,
        gate_type="merge_candidate",
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id="merge-gate",
            run_id=candidate,
            iteration=1,
            gate_type="merge_candidate",
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate,
            merge_base_sha=case.merge_base_sha,
        ),
        # Merge receipts are pytest evidence by contract — see
        # gate_evidence.MERGE_GATE_TOOL. Kept valid so the refusal each case
        # asserts stays the OPERATIVE one.
        tool="pytest",
        tool_version="8.3.2",
        exit_code=0,
        checks_collected=1,
        checks_passed=1,
        checks_skipped=0,
        checks_failed=0,
        started_at=(finished - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        nonce=hashlib.sha256(case.name.encode()).hexdigest()[:32],
        workspace_sha=candidate,
        workspace_tree_clean=True,
        interpreter=str(REAL_PYTHON),
        interpreter_version="3.12",
        node_inventory_digest="0" * 64,
        deselected_count=0,
        candidate_sha=candidate,
        merge_base_sha=case.merge_base_sha,
    )


@pytest.fixture
def sweep_repo(tmp_path: Path) -> SweepRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "History Sweep Test")
    _git(repo, "config", "user.email", "history-sweep@example.com")
    (repo / ".gitignore").write_text(".venv\nnode_modules\nvar\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("main architecture\n", encoding="utf-8")
    (repo / "WORKBOOK.md").write_text("shared workbook\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    # A migration that ALREADY EXISTS AT THE MERGE-BASE. Only such a file can be
    # modified non-append-only; case (c) below covers the other kind.
    _write(repo, BASE_MIGRATION, BASE_MIGRATION_SQL)
    # The pointer doc that shares the directory. It exists at the merge-base so
    # case (b2) exercises the exact PR #380 false-refusal shape: history `M` on
    # a non-.sql file the runner never executes.
    _write(repo, MIGRATIONS_README, MIGRATIONS_README_TEXT)
    # A migration whose valid filename contains a space — whitespace-splitting
    # awk once truncated it out of the -Fx intersection (case b5).
    _write(repo, SPACED_MIGRATION, SPACED_MIGRATION_SQL)
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    # The gate runs its counterfeit step only when this directory exists in the
    # trial-merge worktree; the harness itself is intercepted by the fake python.
    corpus = repo / "tests" / "counterfeits" / "harness.py"
    corpus.parent.mkdir(parents=True, exist_ok=True)
    corpus.write_text("# fixture stand-in; the fake python answers -m\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    candidates = {
        # (a) the required counterfeit shape: a secret path that is not in the
        #     tip tree and is in the history the merge lands.
        "secret": (
            "fixture/secret-add-then-delete",
            _add_then_delete_branch(
                repo,
                "fixture/secret-add-then-delete",
                hidden_path="secrets/counterfeit.env",
                hidden_content="COUNTERFEIT_TOKEN=not-a-real-secret\n",
                innocuous_path="notes/secret-lane.txt",
            ),
            "secrets",
            "secrets/counterfeit.env",
        ),
        # (a2) THE OBJECT-DEDUP CASE (2026-08-05 review). Byte-identical to
        #      base.txt, which exists at the merge-base. `rev-list --objects`
        #      marks that blob UNINTERESTING and prints neither it nor the new
        #      path it was committed at, so the first implementation of this
        #      sweep passed the branch. An EMPTY file is the universal form of
        #      this (main carries 119 zero-length tracked files), which makes it
        #      the likely real-world shape, not an exotic one.
        "secret-duplicate-content": (
            "fixture/secret-duplicate-content",
            _add_then_delete_branch(
                repo,
                "fixture/secret-duplicate-content",
                hidden_path="secrets/dup.env",
                hidden_content="base\n",
                innocuous_path="notes/dup-lane.txt",
            ),
            "secrets",
            "secrets/dup.env",
        ),
        # (b) a migration that exists on main, edited then put back.
        "migration-revert": (
            "fixture/migration-modify-then-revert",
            _modify_then_revert_branch(
                repo,
                "fixture/migration-modify-then-revert",
                target_path=BASE_MIGRATION,
                original_content=BASE_MIGRATION_SQL,
                innocuous_path="notes/migration-lane.txt",
            ),
            "migrations-append-only",
            f"modified {BASE_MIGRATION}",
        ),
        # (b3) a migration main has, DELETED — `D` must refuse (F002).
        "migration-delete": (
            "fixture/migration-delete",
            _delete_branch(
                repo, "fixture/migration-delete", target_path=BASE_MIGRATION
            ),
            "migrations-append-only",
            BASE_MIGRATION,
        ),
        # (b4) a migration main has, RENAMED — --no-renames splits to D+A (F002).
        "migration-rename": (
            "fixture/migration-rename",
            _rename_branch(
                repo,
                "fixture/migration-rename",
                old_path=BASE_MIGRATION,
                new_path="omniagentos/db/migrations/0009_renamed.sql",
            ),
            "migrations-append-only",
            BASE_MIGRATION,
        ),
        # (b5) space-bearing filename edited then restored — tab parsing (F003).
        "migration-space": (
            "fixture/migration-space-touch",
            _modify_then_revert_branch(
                repo,
                "fixture/migration-space-touch",
                target_path=SPACED_MIGRATION,
                original_content=SPACED_MIGRATION_SQL,
                innocuous_path="notes/space-lane.txt",
            ),
            "migrations-append-only",
            SPACED_MIGRATION,
        ),
        # (b6) mutate+restore ONLY inside merge commits — `-m` walk (F004).
        "migration-evil-merge": (
            "fixture/migration-evil-merge",
            _merge_only_modify_restore_branch(
                repo,
                "fixture/migration-evil-merge",
                target_path=BASE_MIGRATION,
                original_content=BASE_MIGRATION_SQL,
            ),
            "migrations-append-only",
            BASE_MIGRATION,
        ),
        # (b2) FALSE-POSITIVE GUARD (PR #380, 2026-08-13): the directory's
        #      README touched in history, restored byte-identically — a docs
        #      file, not a migration; the *.sql scoping must not refuse it.
        "migration-readme-touch": (
            "fixture/migration-readme-touch",
            _modify_then_revert_branch(
                repo,
                "fixture/migration-readme-touch",
                target_path=MIGRATIONS_README,
                original_content=MIGRATIONS_README_TEXT,
                innocuous_path="notes/readme-lane.txt",
            ),
            None,
            None,
        ),
        # (c) FALSE-POSITIVE GUARD: iterating on your own new migration is legal.
        "migration-iterate": (
            "fixture/migration-add-then-modify",
            _add_then_modify_branch(
                repo,
                "fixture/migration-add-then-modify",
                new_path="omniagentos/db/migrations/0002_new.sql",
            ),
            None,
            None,
        ),
        # (d) a tracked interpreter tree, added and withdrawn.
        "tracked-env": (
            "fixture/venv-add-then-delete",
            _add_then_delete_branch(
                repo,
                "fixture/venv-add-then-delete",
                hidden_path=".venv/blob.txt",
                hidden_content="a whole dependency tree, briefly\n",
                innocuous_path="notes/venv-lane.txt",
                force=True,
            ),
            "tracked-env",
            ".venv/blob.txt",
        ),
        # (e) the sweep must not refuse ordinary multi-commit branches.
        "control": (
            "fixture/clean-two-commits",
            _clean_two_commit_branch(repo, "fixture/clean-two-commits"),
            None,
            None,
        ),
        # (f) the same crime, hidden behind a real merge commit.
        "merge-smuggle": (
            "fixture/merge-smuggle",
            _merge_smuggle_branch(
                repo,
                "fixture/merge-smuggle",
                side="fixture/smuggle-side",
                hidden_path="secrets/smuggled.env",
            ),
            "secrets",
            "secrets/smuggled.env",
        ),
    }
    _install_fake_python(repo)

    branches = {
        key: FixtureBranch(
            name=name,
            candidate_sha=candidate_sha,
            merge_base_sha=base_sha,
            refusal=refusal,
            reason=reason,
        )
        for key, (name, candidate_sha, refusal, reason) in candidates.items()
    }

    evidence_root = tmp_path / "gate-evidence"
    store = GateEvidenceStore(evidence_root)
    for case in branches.values():
        signed = store.sign(_receipt(case, repo))
        receipt_path = evidence_root / "records" / "merge-gate" / f"{case.candidate_sha}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    return SweepRepo(path=repo, evidence_root=evidence_root, branches=branches)


def _run_gate(
    fixture: SweepRepo,
    case: FixtureBranch,
    *,
    gate_script: Path = MERGE_GATE,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "REPO": str(fixture.path),
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(fixture.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(fixture.evidence_root),
        # STATE THE MODE, never inherit it. An operator who armed determinism
        # with `MERGE_GATE_PINNED=1 bash scripts/merge-gate.sh ...` EXPORTS the
        # value, and it rides into every fixture gate this module starts — where
        # GATE_WS then derives from the script's location and the gate refuses
        # `gate-workspace-missing` before scoring a single step. These fixtures
        # exercise the DEFAULT, un-armed gate; `env_extra` still overrides.
        "MERGE_GATE_PINNED": "0",
        **(env_extra or {}),
    }
    # CONTAINED: a gate spawns pytest which spawns more gates; on timeout
    # subprocess.run kills only the direct child and the rest orphan to PPID 1.
    return run_contained(
        ["bash", str(gate_script), case.name],
        cwd=fixture.path,
        env=env,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


# --- the required counterfeit: net diff vs. what --no-ff lands ----------------
#
# The gate merges with `git merge --no-ff`, so the secret scan has to grade the
# swept set. The revert-check below restores EXACTLY the pre-fix three-dot net
# diff at that one call site and shows the same branch — a real secret, really
# committed — earning MERGE GATE: PASS. That is the defect, executed end to end.

_SWEPT_SECRET_CHECK = """LEAKED=$(printf '%s\\n' "$SWEPT_PATHS" | grep -E "$SECRET_RE"); rc=$?
classifier_rc "$rc" "secrets"
[ -n "$LEAKED" ] && fail "secrets" "branch touches $(echo "$LEAKED" | tr '\\n' ' ')" || pass "secrets"
"""

_NET_DIFF_SECRET_CHECK = """LEAKED=$(git diff --name-only "HEAD...$CANDIDATE_SHA" 2>/dev/null | grep -E "$SECRET_RE"); rc=$?
classifier_rc "$rc" "secrets"
[ -n "$LEAKED" ] && fail "secrets" "branch touches $(echo "$LEAKED" | tr '\\n' ' ')" || pass "secrets"
"""


def _gate_with_net_diff_secret_scan(tmp_path: Path) -> Path:
    """merge-gate.sh with the pre-fix, net-diff-only secret scan restored."""
    source = MERGE_GATE.read_text(encoding="utf-8")
    assert source.count(_SWEPT_SECRET_CHECK) == 1, (
        "swept secret-scan mutation did not bind exactly once — the check moved; "
        "re-anchor this revert-check instead of deleting it"
    )
    mutated = source.replace(_SWEPT_SECRET_CHECK, _NET_DIFF_SECRET_CHECK)
    path = tmp_path / "merge-gate-net-diff-secrets.sh"
    path.write_text(mutated, encoding="utf-8")
    marker = _NET_DIFF_SECRET_CHECK.splitlines()[0]
    applied = [line for line in mutated.splitlines() if line == marker]
    assert applied == [marker], "net-diff secret scan was not restored exactly once"
    print(f"MUTATION APPLIED [secrets]: {applied[0]}")
    return path


def test_secret_added_then_deleted_across_commits_is_refused(
    sweep_repo: SweepRepo, tmp_path: Path
) -> None:
    """REQUIRED COUNTERFEIT: an add-then-delete secret has a CLEAN net diff.

    PROTECTED: today's gate sweeps merge-base..candidate and refuses, naming the
    path that is not in the tip tree. REVERTED: with the three-dot net-diff scan
    restored at that one call site, the SAME branch earns "MERGE GATE: PASS" —
    which is the hole, and the blob is in main's history forever.
    """
    case = sweep_repo.branches["secret"]

    protected = _run_gate(sweep_repo, case)
    print(f"PROTECTED [secrets-history] rc={protected.returncode}\n{_output(protected)}")
    assert protected.returncode != 0, (
        "AN ADD-THEN-DELETE SECRET SURVIVED THE NET DIFF: "
        f"{case.name} commits secrets/counterfeit.env and deletes it before its tip, "
        "so `git diff HEAD...<candidate>` is clean — but `git merge --no-ff` lands the "
        "blob in main's history permanently and the gate said PASS:\n"
        f"{_output(protected)}"
    )
    assert "secrets" in _output(protected), _output(protected)
    assert "secrets/counterfeit.env" in _output(protected), (
        "the refusal must NAME the path that is not in the tip tree, or an operator "
        f"cannot act on it:\n{_output(protected)}"
    )
    assert "MERGE GATE: PASS" not in _output(protected), _output(protected)

    reverted = _run_gate(sweep_repo, case, gate_script=_gate_with_net_diff_secret_scan(tmp_path))
    print(f"REVERTED [secrets-history] rc={reverted.returncode}\n{_output(reverted)}")
    assert reverted.returncode == 0, (
        "the revert-check did not reproduce the defect — the pre-fix net-diff scan "
        f"should have passed this secret-bearing branch:\n{_output(reverted)}"
    )
    assert "MERGE GATE: PASS" in _output(reverted), _output(reverted)


def test_secret_whose_content_duplicates_an_existing_blob_is_refused(
    sweep_repo: SweepRepo,
) -> None:
    """THE OBJECT-DEDUP HOLE (2026-08-05 cross-lineage review).

    ``git rev-list --objects A..B`` enumerates OBJECTS and marks everything
    reachable from ``A`` UNINTERESTING, so a blob whose CONTENT already exists
    at the merge-base is never printed — and neither is the NEW PATH it was
    committed at. This branch commits ``secrets/dup.env`` containing exactly
    ``base.txt``'s bytes, then deletes it: clean net diff, and the first
    implementation of the sweep also reported ``secrets ok``. The empty file is
    the universal case (main carries 119 zero-length tracked files), so this is
    the ordinary shape of the bug, not a crafted one.

    The gate now enumerates PATHS via ``rev-list | diff-tree --stdin -r -m``,
    which is per-commit and content-blind, so the path is named.
    """
    case = sweep_repo.branches["secret-duplicate-content"]
    refused = _run_gate(sweep_repo, case)
    print(f"PROTECTED [secret-dup-content] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        "A SECRET WHOSE CONTENT DUPLICATES AN EXISTING BLOB SURVIVED: the path is new, "
        "but the object is not, so an object-level enumeration never lists it. The gate "
        f"said PASS:\n{_output(refused)}"
    )
    assert "secrets/dup.env" in _output(refused), (
        "the refusal must NAME the duplicate-content path, which is precisely what an "
        f"object-level sweep cannot do:\n{_output(refused)}"
    )
    assert "MERGE GATE: PASS" not in _output(refused), _output(refused)


def test_secret_smuggled_through_a_merged_side_branchlet_is_refused(
    sweep_repo: SweepRepo,
) -> None:
    """The offending commits are reachable only through a merge's second parent.

    ``git rev-list --objects merge-base..candidate`` walks every parent, so the
    blob is still swept. A tip-tree diff sees nothing, and anything that follows
    a single line of descent would miss it too.
    """
    case = sweep_repo.branches["merge-smuggle"]
    refused = _run_gate(sweep_repo, case)
    print(f"PROTECTED [merge-smuggle] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        "A SECRET MERGED IN FROM A SIDE BRANCHLET SURVIVED: the add and the delete both "
        "sit on the second parent of a real merge commit inside the candidate range, and "
        f"the gate said PASS:\n{_output(refused)}"
    )
    assert "secrets" in _output(refused), _output(refused)
    assert "secrets/smuggled.env" in _output(refused), _output(refused)
    assert "MERGE GATE: PASS" not in _output(refused), _output(refused)


def test_migration_modified_then_reverted_is_refused(sweep_repo: SweepRepo) -> None:
    """Editing a migration main ALREADY HAS is not undone by putting it back.

    The second commit restores the file byte-identically, so the net diff has
    nothing to say about it — but the mutated blob and the mutating commit are
    both in main's history after the merge.
    """
    case = sweep_repo.branches["migration-revert"]
    refused = _run_gate(sweep_repo, case)
    print(f"PROTECTED [migration-revert] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        "A MIGRATION EDIT-THEN-REVERT SURVIVED THE NET DIFF: "
        f"{BASE_MIGRATION} exists at the merge-base, was modified in commit 1 and "
        f"restored in commit 2, and the append-only gate said PASS:\n{_output(refused)}"
    )
    assert "migrations-append-only" in _output(refused), _output(refused)
    assert f"modified {BASE_MIGRATION}" in _output(refused), _output(refused)
    assert "MERGE GATE: PASS" not in _output(refused), _output(refused)


@pytest.mark.parametrize(
    ("case_key", "expected_path"),
    [
        ("migration-delete", BASE_MIGRATION),
        ("migration-rename", BASE_MIGRATION),
        ("migration-space", SPACED_MIGRATION),
        ("migration-evil-merge", BASE_MIGRATION),
    ],
)
def test_nonappend_migration_shapes_are_refused(
    sweep_repo: SweepRepo, case_key: str, expected_path: str
) -> None:
    """PERMANENT COVERAGE for the round-1 fail-open shapes (F002/F003/F004).

    Mutation testing showed reverting any one fix — M-only filtering,
    whitespace-splitting awk, --no-merges — left the committed suite green.
    Each shape here refuses and NAMES the full offending migration path:
    delete (D), rename (R100 split to D+A by --no-renames), a space-bearing
    filename surviving tab-delimited parsing, and an evil-merge mutate+restore
    visible only to the -m per-parent walk.
    """
    case = sweep_repo.branches[case_key]
    refused = _run_gate(sweep_repo, case)
    print(f"PROTECTED [{case_key}] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        f"A NON-APPEND-ONLY MIGRATION SHAPE SURVIVED THE SWEEP ({case_key}): "
        f"{expected_path} exists at the merge-base and the gate said PASS:\n"
        f"{_output(refused)}"
    )
    assert "migrations-append-only" in _output(refused), _output(refused)
    assert expected_path in _output(refused), _output(refused)
    assert "MERGE GATE: PASS" not in _output(refused), _output(refused)


def test_migration_edit_refused_even_under_literal_pathspecs_env(sweep_repo: SweepRepo) -> None:
    """FAIL-OPEN GUARD (cross-lineage review, 2026-08-13): pathspec-magic env.

    With ``GIT_LITERAL_PATHSPECS=1`` exported, git reads the check's
    ':(glob)…*.sql' pathspec as a literal path; both walks then return empty at
    rc 0 and a real migration edit minted a signed PASS. The gate now unsets the
    four pathspec-magic env vars immediately before the walks; this test pins
    that the refusal survives a caller environment that sets them.
    """
    case = sweep_repo.branches["migration-revert"]
    refused = _run_gate(sweep_repo, case, env_extra={"GIT_LITERAL_PATHSPECS": "1"})
    print(f"PROTECTED [migration-revert+literal-env] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        "GIT_LITERAL_PATHSPECS=1 IN THE CALLER ENVIRONMENT DISARMED THE "
        f"APPEND-ONLY SWEEP — the gate must unset pathspec-magic vars:\n{_output(refused)}"
    )
    assert "migrations-append-only" in _output(refused), _output(refused)
    assert f"modified {BASE_MIGRATION}" in _output(refused), _output(refused)


def test_migrations_readme_touched_in_history_still_passes(sweep_repo: SweepRepo) -> None:
    """FALSE-POSITIVE GUARD — the append-only doctrine governs *.sql files only.

    PR #380 (2026-08-13) was refused at `modified omniagentos/db/migrations/
    README.md`: an early lane commit updated the pointer doc, main later
    converged to identical content, the net diff was EMPTY — and the
    directory-wide history walk still refused a file the migration runner never
    reads. The check is now pathspec-scoped to '*.sql'; this test pins that a
    README-only history touch passes while test_migration_modified_then_reverted
    above keeps pinning that a real migration edit still refuses.
    """
    case = sweep_repo.branches["migration-readme-touch"]
    accepted = _run_gate(sweep_repo, case)
    print(f"CONTROL [migration-readme-touch] rc={accepted.returncode}\n{_output(accepted)}")
    assert accepted.returncode == 0, (
        "THE SWEEP REFUSED A LANE FOR TOUCHING THE MIGRATIONS README — the "
        "append-only check must be scoped to *.sql migration files, not the "
        f"directory:\n{_output(accepted)}"
    )
    assert "MERGE GATE: PASS" in _output(accepted), _output(accepted)
    assert re.search(r"migrations-append-only\s+ok", _output(accepted)), _output(accepted)


def test_new_migration_iterated_on_within_the_lane_still_passes(sweep_repo: SweepRepo) -> None:
    """FALSE-POSITIVE GUARD — the sweep must not outlaw normal lane work.

    Adding ``0002_new.sql`` in commit 1 and fixing its SQL in commit 2 produces a
    history ``M`` for a file main does not have. That is iterating on your own
    not-yet-landed migration, which is legal; only a file present at the
    merge-base can be modified non-append-only.
    """
    case = sweep_repo.branches["migration-iterate"]
    accepted = _run_gate(sweep_repo, case)
    print(f"CONTROL [migration-iterate] rc={accepted.returncode}\n{_output(accepted)}")
    assert accepted.returncode == 0, (
        "THE SWEEP REFUSED A LANE FOR ITERATING ON ITS OWN NEW MIGRATION — "
        "history-derived `M` entries must be filtered to files that exist in the "
        f"MERGE-BASE tree:\n{_output(accepted)}"
    )
    assert "MERGE GATE: PASS" in _output(accepted), _output(accepted)
    assert re.search(r"migrations-append-only\s+ok", _output(accepted)), _output(accepted)


def test_tracked_env_added_then_deleted_across_commits_is_refused(sweep_repo: SweepRepo) -> None:
    """A tracked interpreter tree lands in history even if the tip drops it."""
    case = sweep_repo.branches["tracked-env"]
    refused = _run_gate(sweep_repo, case)
    print(f"PROTECTED [tracked-env-history] rc={refused.returncode}\n{_output(refused)}")
    assert refused.returncode != 0, (
        "A TRACKED .venv BLOB SURVIVED THE NET DIFF: it was committed and then deleted "
        f"before the candidate tip, and the gate said PASS:\n{_output(refused)}"
    )
    assert "tracked-env" in _output(refused), _output(refused)
    assert ".venv/blob.txt" in _output(refused), _output(refused)
    assert "MERGE GATE: PASS" not in _output(refused), _output(refused)


def test_clean_multi_commit_branch_still_passes(sweep_repo: SweepRepo) -> None:
    """CONTROL — a gate that refuses every multi-commit branch is not a gate."""
    case = sweep_repo.branches["control"]
    accepted = _run_gate(sweep_repo, case)
    print(f"CONTROL [clean-two-commits] rc={accepted.returncode}\n{_output(accepted)}")
    assert accepted.returncode == 0, (
        "THE HISTORY SWEEP REFUSED AN ORDINARY TWO-COMMIT BRANCH — the swept set is "
        f"over-broad and every honest lane now pays for it:\n{_output(accepted)}"
    )
    assert "MERGE GATE: PASS" in _output(accepted), _output(accepted)
    assert re.search(r"secrets\s+ok", _output(accepted)), _output(accepted)
    assert re.search(r"reachability\s+ok", _output(accepted)), _output(accepted)
