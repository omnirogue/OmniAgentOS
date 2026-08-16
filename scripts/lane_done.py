#!/usr/bin/env python3
"""Lane Done Contract: mechanical guard certifying lane work admissibility before review.

USAGE:
    python scripts/lane_done.py verify --worktree <path> [--base <ref>] [--claim-sha <sha> ...]
                                        [--owned-path <glob> ...] [--require-owned-path <glob> ...]
                                        [--db-path <path>] [--serving-root <path>]

Exit 0 if lane is admissible; nonzero with one failed check per line. Emits JSON receipt to
stdout. Every refused receipt carries a machine-readable `reason` code — no ambiguous state
exits 0, and no ambiguous state exits nonzero without saying exactly why.

This tool is the #1 convergent recommendation of a 3-model speed council: no review is
scheduled until this script mechanically proves a lane's work exists.

DONE.json manifest (mandatory):
    Every worktree verified here MUST carry a `<worktree>/DONE.json` file — the lane's own
    self-report, written by the lane as it finishes (untracked; it is deliberately exempt from
    the clean-tree check so a lane doesn't have to commit its own status file). Required fields:

        head_sha      (str)        the commit the lane claims as its work — a full, resolved,
                                    lowercase 40-hex commit id (never "HEAD", a branch name, or
                                    an abbreviation — those are tautological, not proof of a
                                    specific commit); checked for membership in `base..HEAD`
                                    (see claim-SHA checks below), and WARNED (not refused) if it
                                    is not the worktree's literal current tip.
        owned_paths   (list[str])  glob patterns (segment-aware: a lone '*' does not cross '/';
                                    a '**' segment matches zero-or-more whole segments, e.g.
                                    'src/**' matches both 'src/x.py' and 'src/deep/x.py') the
                                    lane is scoped to; every file in `base...HEAD` must match
                                    at least one.

    Optional, self-describing fields `base` / `lane` are cross-checked against the script's own
    independently-derived `--base` / worktree branch when present (mismatch -> warning, never a
    refusal — the manifest may simply be stale).

    Field names deliberately match `~/.omniagentos/ops/DONE-contract/template.json` (the report.json
    shape `lane-done-check` consumes) so one manifest vocabulary serves both tools. A worktree
    with no DONE.json is refused `missing_done_manifest`; one that exists but is broken (bad
    JSON, wrong types, a directory instead of a file, empty/malformed required fields) is refused
    the more precise `malformed_done_manifest` — there is no flag to skip either.

    `--claim-sha` / `--owned-path` remain available as REPEATABLE CLI flags but are additive
    only: they add MORE SHAs/patterns to check on top of the manifest's, never a substitute for
    it. A bare `verify --worktree .` with no flags still checks all seven checks — the manifest,
    not flag presence, is what makes claim-SHA/owned-path/DB checks mandatory now.

    `--require-owned-path` is a SEPARATE, optional coordinator-supplied ceiling: self-declared
    owned_paths is conformance (the lane could self-declare "**"); supplying this makes it
    enforcement (every declared pattern must itself be covered by one of these globs).

`--serving-root` / $OMNIAGENTOS_SERVING_ROOT only ever ADD a second protected root on top of the
built-in default — they can never remove protection for it, and an override that does not
resolve to an existing directory is refused rather than silently ignored.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

DEFAULT_SERVING_ROOT = "/Users/youruser/OmniAgentOS"
DONE_JSON_NAME = "DONE.json"
DONE_JSON_REQUIRED_FIELDS = ("head_sha", "owned_paths")
# head_sha must be a real, full, lowercase 40-hex commit id — never "HEAD", a branch
# name, or an abbreviated SHA (all of which used to satisfy the manifest tautologically:
# {"head_sha": "HEAD"} always equals whatever HEAD currently resolves to, proving nothing).
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------------------
# Exceptions — the two ways a check can fail without a caller explicitly handling it.
# Both are caught exactly once, at the top of verify_lane(), and converted into a refusal
# receipt with a named `reason`. Nothing below this module boundary is allowed to leak an
# uncaught traceback in place of a machine-readable refusal.
# --------------------------------------------------------------------------------------


class GitCommandError(RuntimeError):
    """A git invocation whose stdout a caller needs to TRUST exited nonzero.

    This is the fix for the class of bug where a git error (corrupted .git/index, a
    missing object, a wedged lock file, ...) silently reads as an empty-but-successful
    result — e.g. a corrupted index makes `git status --porcelain` fail, and reading its
    (empty) stdout anyway made the tree look clean. Every call site that needs git's
    stdout to be meaningful must go through git_run() (which raises this), never read
    `.stdout` off an unchecked subprocess.run().
    """

    def __init__(self, cmd: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(cmd)} failed (exit {returncode}): {stderr.strip()}")


class WorktreeUnreadableError(RuntimeError):
    """The worktree path itself can't be used as a subprocess cwd (missing, not a
    directory, unreadable). Raised instead of letting subprocess.run's FileNotFoundError /
    NotADirectoryError propagate as an uncaught traceback."""

    def __init__(self, path: Path, original: OSError) -> None:
        self.path = path
        self.original = original
        super().__init__(f"cannot use {path} as a working directory: {original}")


class _Refused(Exception):
    """Internal control-flow signal only: LaneVerifier.refuse() has already recorded the
    failing check, reason, and message — this just unwinds the check pipeline."""


# --------------------------------------------------------------------------------------
# Low-level git helpers. Every git subprocess call in this file funnels through
# _run_git() so the FileNotFoundError-on-missing-cwd fix applies uniformly.
# --------------------------------------------------------------------------------------


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git; return the CompletedProcess UNCHECKED so callers can decide whether a
    nonzero exit is a meaningful signal (existence/ancestry probes) or a real error (see
    git_run()). Raises WorktreeUnreadableError if `cwd` itself can't be entered."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        raise WorktreeUnreadableError(cwd, exc) from exc


def git_run(cwd: Path, *args: str) -> str:
    """Run git command in worktree; return stdout stripped.

    Raises GitCommandError if git exits nonzero. A failed invocation's stdout (often
    empty) must NEVER be read as a valid result — that is exactly how a corrupted
    .git/index used to make `tree_clean` read True.
    """
    result = _run_git(cwd, *args)
    if result.returncode != 0:
        raise GitCommandError(args, result.returncode, result.stderr)
    return result.stdout.strip()


def git_check(cwd: Path, *args: str) -> bool:
    """Run git command, return True iff exit code is 0.

    Only for probes where the exit code ITSELF is the semantic answer (does this ref
    exist? is quiet-verify satisfied?) and every nonzero code means the same "no" — not a
    substitute for git_run() when a nonzero code could mean either "no" or "git broke".
    """
    return _run_git(cwd, *args).returncode == 0


def is_git_worktree(path: Path) -> bool:
    """Return True if path is inside a git worktree (not a bare repo)."""
    result = _run_git(path, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def get_git_root(path: Path) -> Path | None:
    """Return the git repository root, or None if not in a git repo."""
    result = _run_git(path, "rev-parse", "--show-toplevel")
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return None


def get_merge_base(cwd: Path, ref1: str, ref2: str) -> str | None:
    """Get merge base between two refs, or None if no merge base exists."""
    result = _run_git(cwd, "merge-base", ref1, ref2)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    """Return True if ancestor is an ancestor of descendant.

    `git merge-base --is-ancestor` documents exactly two non-error outcomes: 0 (is an
    ancestor) and 1 (is not). Any OTHER exit code is a genuine git error (bad ref,
    unreadable object, ...) and must not be silently folded into "not an ancestor" —
    that would let a corrupted-repo edge case skip the base-freshness refusal below
    instead of naming the failure.
    """
    result = _run_git(cwd, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitCommandError(
        ("merge-base", "--is-ancestor", ancestor, descendant), result.returncode, result.stderr
    )


def ref_resolves(cwd: Path, ref: str) -> bool:
    """Return True if ref resolves to an existing commit object.

    Uses `<ref>^{commit}`, not a bare `<ref>`: plain `rev-parse --verify --quiet` on a
    syntactically well-formed 40-hex string returns rc 0 even when NO object with that
    name exists in the repository — it validates the STRING SHAPE, not object
    presence, for any already-full-length hex input (proven empirically: a random
    40-hex string "resolves" while a bad short ref correctly does not). `^{commit}`
    forces git to actually dereference and peel to a commit, which is what every
    caller here means by "resolves" — a fabricated claim SHA that merely LOOKS like a
    real SHA must be refused as unresolvable, not deferred to a less precise
    downstream "not in range" refusal.
    """
    return git_check(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def get_changed_files(cwd: Path, base: str, head: str) -> list[str]:
    """Get list of files changed between base...head. Raises GitCommandError on failure
    — NEVER returns [] to mean "the diff failed", only to mean "the diff is empty",
    since those two are checked for completely different reasons downstream."""
    output = git_run(cwd, "diff", "--name-only", f"{base}...{head}")
    return [f for f in output.split("\n") if f]


def get_commit_count(cwd: Path, base: str, head: str) -> int:
    """Get count of commits in base..head. Raises GitCommandError on failure — never
    returns 0 to mean "git broke", only to mean "the range truly is empty"."""
    output = git_run(cwd, "log", "--oneline", f"{base}..{head}")
    return len([line for line in output.split("\n") if line])


def get_range_shas(cwd: Path, base: str, head: str) -> set[str]:
    """Full SHAs reachable from head but NOT from base — the lane's OWN commits.

    This is the fix for "any ancestor of HEAD passes": main's tip and the root commit
    are both ancestors of HEAD (HEAD descends from them) but neither is a member of
    base..HEAD, so neither proves this lane did any work of its own. Claim SHAs are
    checked against membership in THIS set, not mere ancestry of HEAD.
    """
    output = git_run(cwd, "rev-list", f"{base}..{head}")
    return {line for line in output.split("\n") if line}


def gitignore_adds_entries(cwd: Path, base: str, head: str) -> bool:
    """True if the base...head diff ADDS any .gitignore line (not merely touches the
    file). Advisory only — surfaced as a receipt warning, never a refusal: a lane that
    widens what git ignores in the same commit as its own work is easy to miss in
    review, but not inherently wrong."""
    diff = git_run(cwd, "diff", "--unified=0", f"{base}...{head}", "--", ".gitignore")
    return any(line.startswith("+") and not line.startswith("+++") for line in diff.split("\n"))


def _is_under(path: Path, root: Path) -> bool:
    """True if path is root itself or nested anywhere under it."""
    return path == root or path.is_relative_to(root)


# --------------------------------------------------------------------------------------
# Segment-aware ownership globbing. fnmatch.fnmatch's '*' crosses '/' (it is a plain
# regex translation with no path-separator awareness), so a naive fnmatch_patterns
# treated "src/*" as matching "src/deep/x.py" — a lane declaring "src/*" would silently
# own everything under src/ recursively, not just its direct children. A '**' SEGMENT
# is the deliberate escape hatch for "recursively": 'src/**' matches 'src/foo.py' AND
# 'src/deep/x.py' (zero or more path segments), matching common gitignore/pathspec
# convention, while a lone '*' still never crosses '/'.
# --------------------------------------------------------------------------------------


def _match_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *rest = pattern_parts
    if head == "**":
        # '**' matches zero-or-more whole segments: either stop consuming pattern here
        # (zero segments) or consume one path segment and try again (one more segment).
        if _match_parts(path_parts, tuple(rest)):
            return True
        return bool(path_parts) and _match_parts(path_parts[1:], pattern_parts)
    if not path_parts:
        return False
    if not fnmatch.fnmatch(path_parts[0], head):
        return False
    return _match_parts(path_parts[1:], tuple(rest))


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Segment-aware glob match: a lone '*' matches within a single path segment only —
    'src/*' matches 'src/foo.py' but NOT 'src/deep/x.py' (both directions matter: the
    fix must not stop matching the direct-child case it used to get right). A '**'
    segment matches zero-or-more whole segments — 'src/**' matches BOTH 'src/foo.py'
    AND 'src/deep/x.py'."""
    return _match_parts(PurePosixPath(path).parts, PurePosixPath(pattern).parts)


def fnmatch_patterns(path: str, patterns: list[str]) -> bool:
    """Return True if path matches any of the glob patterns (segment-aware)."""
    return any(path_matches_pattern(path, pattern) for pattern in patterns)


def owned_paths_outside_required(declared: list[str], required: list[str]) -> list[str]:
    """Return the declared owned_paths patterns NOT covered by any `required` glob.

    Self-declared owned_paths (DONE.json / --owned-path) is CONFORMANCE: the lane says
    what it touched and the script checks the diff against that claim, but the lane
    could self-declare "**" and conform trivially. `--require-owned-path` is
    ENFORCEMENT: a coordinator-supplied ceiling set BEFORE the lane runs that the lane
    cannot loosen by editing its own manifest.

    "Subset" is approximated by treating each declared PATTERN STRING as if it were
    itself a candidate path and matching it against the required globs (segment-aware,
    '**'-capable, same matcher as the real per-file check) — a practical heuristic, not
    a full formal glob-containment solver: "src/*" is judged covered by "src/**" (every
    string "src/*" could denote is itself a "src/**" match), and "**" is correctly
    judged NOT covered by "src/**" (the literal string "**" does not match "src/**").
    """
    return [pattern for pattern in declared if not fnmatch_patterns(pattern, required)]


# --------------------------------------------------------------------------------------
# DONE.json manifest — the mandatory source of claim SHAs and owned paths (see module
# docstring). Untracked by design: it is the lane's own status file, not lane content,
# so the tree_clean check exempts exactly this one filename at the worktree root.
# --------------------------------------------------------------------------------------


def read_done_manifest(worktree_root: Path) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Read <worktree_root>/DONE.json. Returns (manifest, error, reason) — manifest is
    None whenever error/reason are set (never a partial/trust-me manifest). `reason` is
    exactly one of two codes, deliberately distinguished (round-2 review, finding 21):

        missing_done_manifest   the file is simply absent — the ordinary "the lane
                                 hasn't dropped a manifest yet" case.
        malformed_done_manifest every OTHER failure: a directory sitting at that path,
                                 an unreadable file, invalid JSON, a non-object top
                                 level, a missing/empty required field, a wrong field
                                 type, or (finding 17) a head_sha that isn't a real,
                                 full, resolved commit id — "HEAD", a branch name, and
                                 an abbreviated SHA are all rejected here as tautological
                                 rather than as proof of a specific commit.
    """
    manifest_path = worktree_root / DONE_JSON_NAME
    if not manifest_path.exists():
        return None, f"{manifest_path} does not exist", "missing_done_manifest"

    if not manifest_path.is_file():
        return (
            None,
            f"{manifest_path} exists but is not a regular file (e.g. a directory)",
            "malformed_done_manifest",
        )

    try:
        raw = manifest_path.read_text()
    except OSError as exc:
        return None, f"{manifest_path} unreadable: {exc}", "malformed_done_manifest"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{manifest_path} is not valid JSON: {exc}", "malformed_done_manifest"

    if not isinstance(data, dict):
        return None, f"{manifest_path} must contain a JSON object", "malformed_done_manifest"

    missing = [f for f in DONE_JSON_REQUIRED_FIELDS if not data.get(f)]
    if missing:
        return (
            None,
            f"{manifest_path} missing required field(s): {', '.join(missing)}",
            "malformed_done_manifest",
        )

    owned_paths = data["owned_paths"]
    if not isinstance(owned_paths, list) or not all(isinstance(p, str) for p in owned_paths):
        return (
            None,
            f"{manifest_path} field 'owned_paths' must be a list of strings",
            "malformed_done_manifest",
        )

    head_sha = data["head_sha"]
    if not isinstance(head_sha, str) or not HEAD_SHA_RE.match(head_sha):
        return (
            None,
            f"{manifest_path} field 'head_sha' must be a full 40-char lowercase commit "
            f"SHA, not a ref name / abbreviation / symbolic value like 'HEAD' — got "
            f"{head_sha!r}",
            "malformed_done_manifest",
        )

    return data, None, None


# --------------------------------------------------------------------------------------
# Receipt + check pipeline.
# --------------------------------------------------------------------------------------


@dataclass
class LaneDoneReceipt:
    """Receipt describing the state of a lane's work."""

    worktree: str
    branch: str
    head_sha: str
    base_sha: str
    merge_base_ok: bool
    commit_count: int
    changed_files: list[str]
    checks: dict[str, str | bool]
    verdict: str  # "admissible" | "refused"
    reason: str | None = None  # machine-readable refusal reason code; None iff admissible
    warnings: list[str] = field(default_factory=list)  # non-blocking advisories

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LaneVerifier:
    """Accumulates receipt state across the check pipeline. Every failure path funnels
    through refuse(), which unconditionally records a checks[...] entry, a `reason`
    code, and a human message before raising — no check can fail silently, and no
    refusal receipt can lack a reason. That is the fail-closed doctrine this file exists
    to enforce, made structurally true rather than merely a convention every check has
    to remember to follow."""

    def __init__(self, worktree: str) -> None:
        self.worktree = worktree
        self.branch = "unknown"
        self.head_sha = ""
        self.base_sha = ""
        self.merge_base_ok = False
        self.commit_count = 0
        self.changed_files: list[str] = []
        self.checks: dict[str, str | bool] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.reason: str | None = None

    def receipt(self, verdict: str) -> LaneDoneReceipt:
        return LaneDoneReceipt(
            worktree=self.worktree,
            branch=self.branch,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            merge_base_ok=self.merge_base_ok,
            commit_count=self.commit_count,
            changed_files=self.changed_files,
            checks=self.checks,
            verdict=verdict,
            reason=self.reason,
            warnings=self.warnings,
        )

    def ok(self, check: str) -> None:
        self.checks[check] = True

    def refuse(self, check: str, reason: str, message: str) -> NoReturn:
        self.checks[check] = False
        self.reason = reason
        self.errors.append(f"FAIL: {message}")
        raise _Refused()


def _run_checks(
    v: LaneVerifier,
    worktree: str,
    base: str,
    claim_shas: list[str],
    owned_paths: list[str],
    require_owned_paths: list[str],
    db_path: str | None,
    serving_root: str,
) -> None:
    worktree_path = Path(worktree).resolve()
    serving_root_path = Path(serving_root).resolve()
    default_serving_root_path = Path(DEFAULT_SERVING_ROOT).resolve()

    # An override can only ADD a second protected root, never remove protection for the
    # built-in default (round-2 review finding 18): --serving-root /nonexistent used to
    # SILENTLY replace the check target, so a bogus/irrelevant override made the real
    # live checkout sail through unchecked. The override itself must also resolve to a
    # real directory — a typo'd path is refused, not silently ignored (a no-op override
    # is indistinguishable from "protection successfully loosened" to anyone reading the
    # receipt, so it must not be possible to supply one that does nothing detectably).
    protected_roots = {default_serving_root_path}
    if serving_root_path != default_serving_root_path:
        if not serving_root_path.is_dir():
            v.refuse(
                "serving_root_override_valid",
                "invalid_serving_root_override",
                f"--serving-root override {serving_root_path} does not resolve to an "
                "existing directory",
            )
        v.ok("serving_root_override_valid")
        protected_roots.add(serving_root_path)

    # Check: worktree is a real git worktree.
    if not is_git_worktree(worktree_path):
        v.refuse(
            "is_git_worktree", "not_a_git_worktree", f"{worktree} is not inside a git worktree"
        )

    git_root = get_git_root(worktree_path)
    if git_root is None:
        v.refuse(
            "is_git_worktree", "not_a_git_worktree", f"could not determine git root for {worktree}"
        )
    v.ok("is_git_worktree")

    # Check: resolved root is not ANY protected serving root (never certify a live
    # serving checkout; an override ADDS a root, it never substitutes for the default).
    if git_root in protected_roots:
        v.refuse(
            "not_serving_root",
            "serving_root_worktree",
            f"worktree root {git_root} must not be a live serving checkout "
            f"({', '.join(sorted(str(r) for r in protected_roots))})",
        )
    v.ok("not_serving_root")

    # Current branch and HEAD.
    v.branch = git_run(git_root, "rev-parse", "--abbrev-ref", "HEAD")
    v.head_sha = git_run(git_root, "rev-parse", "HEAD")
    if not v.head_sha:
        v.refuse("head_sha_resolves", "head_sha_unresolved", "could not determine HEAD commit")
    v.ok("head_sha_resolves")

    # Check: HEAD is not detached. `--abbrev-ref HEAD` literally returns the string
    # "HEAD" (not a branch name) when detached — a detached-HEAD worktree used to
    # sail through with branch:"HEAD" in the receipt and nothing refusing it.
    if v.branch == "HEAD":
        v.refuse(
            "not_detached_head",
            "detached_head",
            f"HEAD is detached at {v.head_sha}; check out a real branch before certifying",
        )
    v.ok("not_detached_head")

    # Check: git status --porcelain is empty (DONE.json itself is exempt — see module
    # docstring: it is the lane's own untracked status file, not lane content).
    status = git_run(git_root, "status", "--porcelain")
    dirty_lines = [line for line in status.split("\n") if line and line != f"?? {DONE_JSON_NAME}"]
    if dirty_lines:
        v.refuse("tree_clean", "tree_dirty", "working tree is not clean; uncommitted changes exist")
    v.ok("tree_clean")

    # Base ref must resolve.
    if not ref_resolves(git_root, base):
        v.refuse("base_resolves", "base_unresolved", f"base ref '{base}' does not resolve")
    v.base_sha = git_run(git_root, "rev-parse", base)
    v.ok("base_resolves")

    # Check: base is not stale against its remote-tracking counterpart. A local `main`
    # that is behind origin/main can certify a lane as admissible against content that
    # is no longer the real merge target — a false GREEN. No --allow-stale-base escape
    # hatch: no existing repo convention for one was found, so this defaults refused.
    origin_base = f"origin/{base}"
    if ref_resolves(git_root, origin_base):
        origin_base_sha = git_run(git_root, "rev-parse", origin_base)
        if origin_base_sha != v.base_sha:
            base_behind = is_ancestor(git_root, v.base_sha, origin_base_sha)
            if base_behind:
                v.refuse(
                    "base_fresh",
                    "base_stale",
                    f"local '{base}' ({v.base_sha[:12]}) is behind {origin_base} "
                    f"({origin_base_sha[:12]}); fetch and re-run before certifying",
                )
            elif not is_ancestor(git_root, origin_base_sha, v.base_sha):
                # Neither is an ancestor of the other: local base and origin/<base> have
                # DIVERGED (e.g. a force-push or rebase elsewhere) — round-2 finding 22.
                # Advisory only: not necessarily wrong (a locally-rebased base is
                # legitimate), but the merge target is ambiguous and worth a human look.
                v.warnings.append(
                    f"reason: base_diverged — local '{base}' ({v.base_sha[:12]}) and "
                    f"{origin_base} ({origin_base_sha[:12]}) have diverged (neither is "
                    "an ancestor of the other); confirm the intended merge target"
                )
        v.ok("base_fresh")
    else:
        v.checks["base_fresh"] = f"skipped (no {origin_base})"

    # Check: merge base exists.
    merge_base = get_merge_base(git_root, base, v.head_sha)
    v.merge_base_ok = merge_base is not None
    if not v.merge_base_ok:
        v.refuse(
            "merge_base_exists",
            "no_merge_base",
            f"no merge base between '{base}' and HEAD; lane history may be orphaned or re-rooted",
        )
    v.ok("merge_base_exists")

    # Check: commit count is non-zero.
    v.commit_count = get_commit_count(git_root, base, v.head_sha)
    if v.commit_count == 0:
        v.refuse("commit_count_nonzero", "empty_lane", f"no commits in {base}..HEAD; lane is empty")
    v.ok("commit_count_nonzero")

    # Check: diff vs base is non-empty. commit_count alone is not enough — `git commit
    # --allow-empty` and a merge-only branch (git merge --no-ff main, zero own work)
    # both have commit_count > 0 but contribute NO content change; this is the check
    # that actually catches both (a merge commit whose content equals base's diffs to
    # nothing against base, same as an allow-empty commit's tree).
    v.changed_files = get_changed_files(git_root, base, v.head_sha)
    if not v.changed_files:
        v.refuse(
            "non_empty_diff",
            "empty_diff",
            f"diff {base}...HEAD is empty; lane contributes no content change "
            "(allow-empty commit or merge-only branch)",
        )
    v.ok("non_empty_diff")

    if ".gitignore" in v.changed_files and gitignore_adds_entries(git_root, base, v.head_sha):
        v.warnings.append(
            "diff adds .gitignore entries — verify nothing legitimate got newly excluded"
        )

    # Check: DONE.json manifest is present with required fields. Mandatory,
    # unconditional of any CLI flag — this is what makes the claim-SHA and owned-path
    # checks below mandatory instead of opt-in no-ops. `missing_done_manifest` (file
    # absent) and `malformed_done_manifest` (present but broken) are distinct reasons
    # (round-2 finding 21) for a more precise diagnosis.
    manifest, manifest_error, manifest_reason = read_done_manifest(git_root)
    if manifest is None:
        v.refuse(
            "done_manifest_present",
            manifest_reason or "malformed_done_manifest",
            manifest_error or "DONE.json missing",
        )
    v.ok("done_manifest_present")

    # Advisory cross-checks (round-2 review addendum): DONE.json MAY also carry "base"
    # and "lane" fields (self-describing, matching lane-done-check's report.json shape).
    # When present, cross-check them against what the script independently derived.
    # Mismatches are advisory, never a refusal — --base and the worktree's real branch
    # remain the authoritative values regardless; a mismatch just means the manifest may
    # be stale and is worth a human's eyes.
    declared_base = manifest.get("base")
    if isinstance(declared_base, str) and declared_base and declared_base != base:
        v.warnings.append(
            f"DONE.json declares base={declared_base!r} but verification ran against "
            f"--base {base!r} — manifest may be stale"
        )
    declared_lane = manifest.get("lane")
    if isinstance(declared_lane, str) and declared_lane and declared_lane != v.branch:
        v.warnings.append(
            f"DONE.json declares lane={declared_lane!r} but the worktree's actual "
            f"branch is {v.branch!r} — manifest may be stale"
        )

    # Check: claim SHAs (manifest's head_sha plus any additional --claim-sha values)
    # resolve AND are members of base..HEAD — not merely ancestors of HEAD. Any ancestor
    # of HEAD used to pass, including main's tip and the root commit, neither of which
    # proves this lane did anything: both are also ancestors of base.
    range_shas = get_range_shas(git_root, base, v.head_sha)
    all_claim_shas = list(dict.fromkeys([manifest["head_sha"], *claim_shas]))
    for claim_sha in all_claim_shas:
        check_key = f"claim_sha_{claim_sha}"
        if not ref_resolves(git_root, claim_sha):
            v.refuse(
                check_key, "claim_sha_unresolvable", f"claim SHA '{claim_sha}' does not resolve"
            )
        full_sha = git_run(git_root, "rev-parse", claim_sha)
        if full_sha not in range_shas:
            v.refuse(
                check_key,
                "claim_sha_not_in_range",
                f"claim SHA '{claim_sha}' is not an ancestor of HEAD within {base}..HEAD "
                "(borrowed from base, or unrelated to this lane)",
            )
        v.ok(check_key)

    # Advisory (round-2 finding 17, second half): DONE.json's head_sha is validated
    # above as a real, in-range commit, but it need not be the LITERAL current tip —
    # requiring exact tip equality would be too strict for a manifest written a commit
    # or two before the very latest (still genuine) work. A mismatch is worth a human's
    # eyes, not a refusal.
    if manifest["head_sha"] != v.head_sha:
        v.warnings.append(
            f"DONE.json head_sha {manifest['head_sha'][:12]} is not the worktree's "
            f"current HEAD {v.head_sha[:12]} (still a valid, in-range commit — manifest "
            "may simply be one or more commits stale)"
        )

    # Check: owned paths (manifest's owned_paths plus any additional --owned-path
    # values) cover every changed file.
    all_owned_paths = list(dict.fromkeys([*manifest["owned_paths"], *owned_paths]))
    violated_paths = [f for f in v.changed_files if not fnmatch_patterns(f, all_owned_paths)]
    if violated_paths:
        v.refuse(
            "owned_paths",
            "owned_path_violation",
            f"files outside ownership: {', '.join(violated_paths)}",
        )
    v.ok("owned_paths")

    # Check: coordinator-supplied ownership ceiling (round-2 finding 20). Self-declared
    # owned_paths (above) is CONFORMANCE — the lane self-declares what it touched, and a
    # lane could self-declare "**". When the coordinator supplies --require-owned-path,
    # every declared pattern must itself be covered by one of these globs — a
    # pre-lane-imposed ENFORCEMENT boundary the lane cannot loosen by editing its own
    # manifest. Optional: with no --require-owned-path, self-declaration alone stands
    # (documented as conformance, not enforcement — see the flag's own --help text).
    if require_owned_paths:
        uncovered = owned_paths_outside_required(all_owned_paths, require_owned_paths)
        if uncovered:
            v.refuse(
                "owned_paths_within_required",
                "owned_paths_not_subset_of_required",
                "owned_paths pattern(s) not covered by --require-owned-path: "
                f"{', '.join(uncovered)}",
            )
        v.ok("owned_paths_within_required")
    else:
        v.checks["owned_paths_within_required"] = "skipped (no --require-owned-path)"

    # Check: DB path is set and is not the live DB. Unset/empty is now a refusal, not a
    # skip — a lane that never says where its DB is could have been running against the
    # ambient default (the operator's live var/omniagentos.db) the whole time.
    effective_db_path = db_path if db_path else os.environ.get("OMNIAGENTOS_DB")
    if not effective_db_path:
        v.refuse(
            "db_path_not_live",
            "db_env_unset",
            "OMNIAGENTOS_DB is unset (or empty) and no --db-path was given; a lane must "
            "certify against an isolated scratch DB, never the ambient default",
        )
    db_path_resolved = Path(effective_db_path).resolve()
    # Same dual-root protection as not_serving_root above (finding 18): a
    # --serving-root override must never REMOVE the built-in default's var/ from what
    # counts as "live", only add another one.
    if any(_is_under(db_path_resolved, root / "var") for root in protected_roots):
        v.refuse(
            "db_path_not_live",
            "db_path_under_serving_root",
            f"DB path {db_path_resolved} is under a live serving root's var/ directory "
            f"({', '.join(sorted(str(r) for r in protected_roots))})",
        )
    v.ok("db_path_not_live")


def verify_lane(
    worktree: str,
    base: str = "main",
    claim_shas: list[str] | None = None,
    owned_paths: list[str] | None = None,
    require_owned_paths: list[str] | None = None,
    db_path: str | None = None,
    serving_root: str = DEFAULT_SERVING_ROOT,
) -> tuple[bool, LaneDoneReceipt, list[str]]:
    """Verify lane admissibility. Return (success, receipt, errors).

    Every exit path is one of: all checks passed (admissible), a check explicitly
    refused with a named reason (_Refused, raised by LaneVerifier.refuse), the worktree
    path itself couldn't be entered (WorktreeUnreadableError), or a git invocation whose
    result the checks needed to trust failed (GitCommandError). All three refusal paths
    return a receipt with `reason` set and errors describing what happened — none of
    them can leak an uncaught traceback or a silent success.
    """
    v = LaneVerifier(worktree)
    try:
        _run_checks(
            v,
            worktree,
            base,
            claim_shas or [],
            owned_paths or [],
            require_owned_paths or [],
            db_path,
            serving_root,
        )
    except _Refused:
        return False, v.receipt("refused"), v.errors
    except WorktreeUnreadableError as exc:
        v.checks["worktree_readable"] = False
        v.reason = "worktree_not_found"
        v.errors.append(f"FAIL: worktree unreadable: {exc}")
        return False, v.receipt("refused"), v.errors
    except GitCommandError as exc:
        v.checks["git_commands_ok"] = False
        v.reason = "git_command_failed"
        v.errors.append(
            f"FAIL: git command failed: git {' '.join(exc.cmd)} "
            f"(exit {exc.returncode}): {exc.stderr.strip()}"
        )
        return False, v.receipt("refused"), v.errors

    return True, v.receipt("admissible"), []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lane Done Contract: mechanical guard certifying lane work admissibility"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="Verify lane admissibility")
    verify_parser.add_argument("--worktree", required=True, help="Worktree path")
    verify_parser.add_argument("--base", default="main", help="Base ref (default: main)")
    verify_parser.add_argument(
        "--claim-sha",
        action="append",
        dest="claim_shas",
        help="Additional claim SHA to verify (repeatable). DONE.json's head_sha is always "
        "checked; this adds MORE SHAs on top of it, never a substitute for the manifest.",
    )
    verify_parser.add_argument(
        "--owned-path",
        action="append",
        dest="owned_paths",
        help="Additional owned-path glob (repeatable), unioned with DONE.json's owned_paths.",
    )
    verify_parser.add_argument(
        "--require-owned-path",
        action="append",
        dest="require_owned_paths",
        help="Coordinator-supplied ownership CEILING (repeatable). Self-declared "
        "owned_paths (DONE.json / --owned-path) is CONFORMANCE ONLY — the lane could "
        "self-declare '**'. Supplying this makes ownership ENFORCEMENT: every declared "
        "owned_paths pattern must itself be covered by one of these globs, a boundary "
        "set BEFORE the lane runs that it cannot loosen by editing its own manifest.",
    )
    verify_parser.add_argument("--db-path", help="Database path (else $OMNIAGENTOS_DB)")
    verify_parser.add_argument(
        "--serving-root",
        default=os.environ.get("OMNIAGENTOS_SERVING_ROOT", DEFAULT_SERVING_ROOT),
        help="Live serving checkout root; worktrees/DBs under it are refused "
        "(default: %(default)s, or $OMNIAGENTOS_SERVING_ROOT)",
    )

    args = parser.parse_args()

    if args.command == "verify":
        success, receipt, errors = verify_lane(
            worktree=args.worktree,
            base=args.base,
            claim_shas=args.claim_shas or [],
            owned_paths=args.owned_paths or [],
            require_owned_paths=args.require_owned_paths or [],
            db_path=args.db_path,
            serving_root=args.serving_root,
        )

        # Emit JSON receipt to stdout
        print(json.dumps(receipt.to_dict(), indent=2))

        # Emit warnings and errors to stderr
        for warning in receipt.warnings:
            print(f"WARN: {warning}", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)

        return 0 if success else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
