"""
Mechanical gate pack for config tests.

NOTE: Any caller capturing traces for `trace_diff` or `normalize_trace`
MUST run the captured process with deterministic settings, primarily `PYTHONHASHSEED=0`.
Set-iteration order and other non-deterministic behaviors are not something the normalizer
can undo after the fact. See `deterministic_env()` below.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "GateOutcome",
    "Change",
    "GitError",
    "collect_changes",
    "gate_build",
    "gate_existing_suite_unmodified",
    "gate_diff_scope",
    "AntiSabotageGateError",
    "PROTECTED_PATHS_PATH",
    "REQUIRED_TEST_ROOTS",
    "SELF_PROTECTED_FILES",
    "settle_gate",
    "DETERMINISM_ENV",
    "deterministic_env",
    "normalize_trace",
    "trace_diff",
    "CONFIG_PATH",
    "Status",
    "Bracket",
    "ConfigTestConfigError",
    "load_config",
    "bracket_for",
]

GateOutcome = tuple[bool, dict[str, Any]]


class GitError(RuntimeError):
    """Raised when a Git subprocess command fails, ensuring git operational failures are explicitly caught."""


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    cmd = ["git", "-C", str(repo), *args]
    try:
        res = subprocess.run(cmd, check=True, text=True, capture_output=True, env=env)
        return res.stdout
    except subprocess.CalledProcessError as e:
        raise GitError(f"Git command failed: {' '.join(cmd)}\n{e.stderr}") from e


@dataclass(frozen=True)
class Change:
    """Represents a specific file change detected in the git tree, tracking path, status, and previous location if renamed."""

    path: str
    status: str
    old_path: str | None = None


def collect_changes(
    repo: Path, base_ref: str, head_ref: str | None = None, *, include_untracked: bool = True
) -> list[Change]:
    """Collects modified, added, deleted, or renamed changes relative to `base_ref`.

    When `head_ref` is `None`, it evaluates the difference between `base_ref` and the uncommitted working tree
    (including untracked files if requested). This is the exact mode a running agent is graded in during execution.
    """
    changes: dict[str, Change] = {}

    args = ["diff", "--name-status", "-M", base_ref]
    if head_ref is not None:
        args.append(head_ref)

    out = _git(repo, *args)
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]  # Only the first letter, e.g., R from R100
        if status in ("R", "C"):
            old_path = parts[1]
            new_path = parts[2]
            changes[new_path] = Change(path=new_path, status=status, old_path=old_path)
        else:
            path = parts[1]
            changes[path] = Change(path=path, status=status)

    if head_ref is None and include_untracked:
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
        for line in untracked.splitlines():
            line = line.strip()
            if line:
                changes[line] = Change(path=line, status="A")

    return sorted(changes.values(), key=lambda c: c.path)


def gate_build(repo: Path, changed_paths: Sequence[str]) -> GateOutcome:
    """Verifies that all changed Python files can be compiled without syntax errors.

    - Zero compilable candidates is a PASS (nothing to build is not a build failure).
    - We set the `PYTHONPYCACHEPREFIX` environment variable to a temporary directory during build checks
      to ensure that the repository under test is kept completely unlittered by newly generated `__pycache__` artifacts.
    - We use one subprocess per compiled file rather than batch compiling, ensuring that compile failures
      are precisely and cleanly attributable to the offending file.
    """
    checked = []
    skipped = []
    failures = []

    with tempfile.TemporaryDirectory() as pycache_dir:
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = pycache_dir

        for p in changed_paths:
            if not p.endswith(".py"):
                skipped.append(p)
                continue
            abs_p = repo / p
            if not abs_p.is_file():
                skipped.append(p)
                continue

            checked.append(p)
            res = subprocess.run(
                [sys.executable, "-m", "py_compile", str(abs_p)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if res.returncode != 0:
                stderr = res.stderr.strip()
                lineno: int | None = None
                # Best effort to parse SyntaxError line number
                m = re.search(r'File ".*?", line (\d+)', stderr)
                if m:
                    try:
                        lineno = int(m.group(1))
                    except ValueError:
                        pass
                failures.append({"path": p, "error": stderr, "lineno": lineno})

    evidence = {
        "gate": "G1",
        "checked": checked,
        "skipped": skipped,
        "failures": failures,
    }
    return len(failures) == 0, evidence


def gate_existing_suite_unmodified(
    repo: Path,
    base_ref: str,
    head_ref: str | None = None,
    *,
    test_roots: Sequence[str] = ("tests",),
) -> GateOutcome:
    """Ensures that existing tests in specified test directories have not been modified or deleted.

    - The question of whether a test file 'existed at base' is answered directly from `git ls-tree`
      rather than scanning the disk, because the disk cannot reliably answer questions about the past.
    - The ADD-ONLY requirement for existing test directories is a deliberate security/grading constraint:
      while adding new tests is encouraged (and is the point of testing tasks), editing the existing suite
      is a common vector where an agent might fake a green status by breaking or disabling validation.
    """
    existed_at_base = set()
    for root in test_roots:
        try:
            out = _git(repo, "ls-tree", "-r", "--name-only", base_ref, "--", root)
            for line in out.splitlines():
                line = line.strip()
                if line:
                    existed_at_base.add(line)
        except GitError:
            # root might not exist at base
            pass

    changes = collect_changes(repo, base_ref, head_ref, include_untracked=True)

    violations = []
    added = []

    for c in changes:
        in_test_root = any(c.path.startswith(r + "/") or c.path == r for r in test_roots)
        old_in_test_root = False
        if c.old_path:
            old_in_test_root = any(
                c.old_path.startswith(r + "/") or c.old_path == r for r in test_roots
            )

        if not in_test_root and not old_in_test_root:
            continue

        is_violation = False
        if c.status != "A":
            if c.path in existed_at_base:
                is_violation = True
            elif c.old_path and c.old_path in existed_at_base:
                is_violation = True

        if is_violation:
            v_dict: dict[str, str] = {"path": c.path, "status": c.status}
            if c.old_path:
                v_dict["old_path"] = c.old_path
            violations.append(v_dict)
        elif c.status == "A" and in_test_root:
            added.append(c.path)

    evidence = {
        "gate": "G2",
        "base_ref": base_ref,
        "test_roots": list(test_roots),
        "violations": violations,
        "added": added,
    }
    return len(violations) == 0, evidence


def gate_diff_scope(
    repo: Path, base_ref: str, allowlist: Sequence[str], head_ref: str | None = None
) -> GateOutcome:
    """Verifies that all modified or added file paths match entries in the specified allowlist.

    Any attempt to modify or escape the workspace boundary will fail closed. An empty allowlist also fails closed.

    Allowlist Entry-Form Table:
    ------------------------------------------------------------------------------------------------------
    | Format             | Description / Example Match                                                   |
    |--------------------|-------------------------------------------------------------------------------|
    | Trailing slash `/` | Matches directories recursively. E.g., `pkg/` matches `pkg/a.py`, `pkg/b/c`    |
    | Glob symbols       | Matches patterns via fnmatch. E.g., `*.py` matches `any.py`                   |
    | Exact plain path   | Matches exact file paths. E.g., `exact.py` matches only `exact.py`            |
    ------------------------------------------------------------------------------------------------------

    CRITICAL FOOT-GUN WARNING:
    A bare directory name with NO trailing slash (e.g., `pkg`) is treated as an EXACT path match
    and will silently match nothing when trying to represent a directory. Always write `pkg/`
    to mean the directory.
    """
    changes = collect_changes(repo, base_ref, head_ref, include_untracked=True)

    checked = 0
    violations = set()

    for c in changes:
        paths_to_check = [c.path]
        if c.old_path:
            paths_to_check.append(c.old_path)

        for p in paths_to_check:
            checked += 1
            # Check for repo escapes
            if p.startswith("/") or ".." in p.split("/"):
                violations.add(p)
                continue

            matched = False
            for entry in allowlist:
                if entry.endswith("/"):
                    if p.startswith(entry):
                        matched = True
                        break
                elif any(m in entry for m in "*?["):
                    if fnmatch.fnmatchcase(p, entry):
                        matched = True
                        break
                else:
                    if p == entry:
                        matched = True
                        break
            if not matched:
                violations.add(p)

    evidence = {
        "gate": "G6",
        "allowlist": list(allowlist),
        "checked": checked,
        "violations": sorted(list(violations)),
    }
    return len(violations) == 0, evidence


# --------------------------------------------------------------------------
# Anti-sabotage settle gate (sha256:309db4693b3a16f645d90d87904c1d0d73bd414a08aed68710cdd45e4a985a6d)
# --------------------------------------------------------------------------
#
# `gate_existing_suite_unmodified` above had exactly one caller in the whole
# repo at the time this was written: itself, via its own tests (grep found
# zero non-test importers). Nothing in a live settle attempt ever asked
# whether the existing test suite had been tampered with, so a worker whose
# owned_paths happened to include its own tests could edit them to go green
# undetected — silently defeating every downstream verification gate,
# including the goal-level verifier, which is only as trustworthy as the
# suite it reads.
#
# `settle_gate` is the swarm-path equivalent this repo was missing: the one
# function a real settle call site imports and asks "is this attempt clean,
# or did it touch what it should not have?" It composes three checks and
# FAILS CLOSED on every one of them — an unreadable registry, a missing
# protected root, a raised exception anywhere in the chain (including from
# GateService) is a REFUSAL, never a silent pass.

PROTECTED_PATHS_PATH: Path = (
    Path(__file__).parent.parent.parent / "configs" / "protected-paths.yaml"
)

# The self-protection floor this gate depends on: both the registry that
# grants tier_p coverage and this module itself must remain listed in
# tier_p.files. If either is ever removed from configs/protected-paths.yaml,
# the config protecting the gate has been tampered with, and settle_gate
# refuses rather than running unprotected.
SELF_PROTECTED_FILES: tuple[str, ...] = (
    "configs/protected-paths.yaml",
    "scripts/benchmarks/configtest_gates.py",
)

# Test/spec roots this gate defends. Each MUST also be covered by
# configs/protected-paths.yaml tier_p, so a plan can never declare ownership
# of it; settle_gate cross-checks this against the live registry on every
# call rather than trusting that the two never drift apart.
REQUIRED_TEST_ROOTS: tuple[str, ...] = ("tests", "dashboard/e2e")


class AntiSabotageGateError(RuntimeError):
    """Raised internally when settle_gate cannot establish protection.

    Always caught inside ``settle_gate`` — this never escapes to a caller.
    It exists as a distinct type only so the various "cannot run" causes
    (missing file, malformed YAML, missing self-protection, uncovered root)
    share one code path to the fail-closed return.
    """


def _load_tier_p(path: Path) -> Mapping[str, Any]:
    """Strictly load ``tier_p`` out of a protected-paths registry.

    Deliberately independent of ``omniagentos.policy.protected_paths``
    (owned elsewhere): this gate must keep failing CLOSED even if that
    module's import graph breaks. Any failure here — missing file,
    unreadable, malformed YAML, wrong shape — raises
    ``AntiSabotageGateError``, which the caller treats as "gate unrunnable"
    and refuses settle.
    """
    try:
        if not path.is_file():
            raise AntiSabotageGateError(f"protected-paths registry missing or not a file: {path}")
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except AntiSabotageGateError:
        raise
    except Exception as exc:  # noqa: BLE001 — any load failure is a refusal, not a crash
        raise AntiSabotageGateError(
            f"protected-paths registry unreadable: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(data, Mapping):
        raise AntiSabotageGateError("protected-paths registry must be a mapping")
    tier_p = data.get("tier_p")
    if not isinstance(tier_p, Mapping):
        raise AntiSabotageGateError("protected-paths registry missing tier_p")
    return tier_p


def _tier_p_protects(tier_p: Mapping[str, Any], path_str: str) -> bool:
    """True if ``path_str`` is covered by tier_p.files or tier_p.directories."""
    files = tier_p.get("files")
    dirs = tier_p.get("directories")
    if not isinstance(files, list) or not isinstance(dirs, list):
        raise AntiSabotageGateError("tier_p.files/directories must be lists")
    if path_str in files:
        return True
    for d in dirs:
        if not isinstance(d, str):
            raise AntiSabotageGateError("tier_p.directories entries must be strings")
        d = d.rstrip("/")
        if path_str == d or path_str.startswith(d + "/"):
            return True
    return False


def settle_gate(
    repo: Path,
    base_ref: str,
    head_ref: str | None = None,
    *,
    protected_paths_path: Path | None = None,
    gate_service: Any | None = None,
) -> GateOutcome:
    """Run the test-tampering anti-sabotage check on a real settle attempt.

    Composes, in order:

      1. **Self-protection check** — ``configs/protected-paths.yaml`` and this
         module must both still be listed in ``tier_p.files``. If either has
         been removed, the config protecting the gate has been tampered
         with; refuse.
      2. **Coverage check** — every root this gate defends (``tests/``, spec
         dirs in ``REQUIRED_TEST_ROOTS``) must be present in ``tier_p`` so a
         plan cannot declare ownership of it.
      3. ``gate_existing_suite_unmodified`` over those roots.
      4. Routes the composed evidence through
         ``GateService.g5_local_verify`` so a settle refusal here matches
         every other production local-verify gate; a raise from
         ``GateService`` is caught and treated as a refusal, never a silent
         allow.

    Any exception anywhere in steps 1-4 is caught and reported as
    ``evidence["reason"] == "gate_unrunnable:..."`` with ``ok=False`` — the
    gate fails CLOSED, it never proceeds unprotected.
    """
    path = protected_paths_path or PROTECTED_PATHS_PATH
    evidence: dict[str, Any] = {
        "gate": "settle_anti_sabotage",
        "protected_paths_path": str(path),
    }

    try:
        tier_p = _load_tier_p(path)

        tier_p_files = tier_p.get("files")
        if not isinstance(tier_p_files, list):
            raise AntiSabotageGateError("tier_p.files must be a list")
        missing_self_protection = [f for f in SELF_PROTECTED_FILES if f not in tier_p_files]
        if missing_self_protection:
            raise AntiSabotageGateError(
                f"self-protection removed from registry: {missing_self_protection}"
            )

        uncovered_roots = [r for r in REQUIRED_TEST_ROOTS if not _tier_p_protects(tier_p, r)]
        if uncovered_roots:
            raise AntiSabotageGateError(f"test roots not protected in registry: {uncovered_roots}")

        tamper_ok, tamper_evidence = gate_existing_suite_unmodified(
            repo, base_ref, head_ref, test_roots=REQUIRED_TEST_ROOTS
        )
        evidence["tamper_check"] = tamper_evidence
    except Exception as exc:  # noqa: BLE001 — fail closed: an unrunnable gate refuses, never passes
        evidence["reason"] = f"gate_unrunnable:{type(exc).__name__}:{exc}"
        evidence["ok"] = False
        evidence["decision"] = "deny"
        return False, evidence

    if not tamper_ok:
        evidence["reason"] = "test_tampering_detected"
        evidence["ok"] = False
        evidence["decision"] = "deny"
        return False, evidence

    # Route through the same GateService every other production verify call
    # uses, so a settle refusal here is indistinguishable from any other G5
    # deny downstream — and so a GateService regression fails closed here
    # too, rather than silently passing an attempt this gate approved.
    try:
        if gate_service is None:
            from omniagentos.gates.service import GateService

            gate_service = GateService()
        decision = gate_service.g5_local_verify(
            {
                "verify_ok": True,
                "mechanical_pass": True,
                "tamper_check": tamper_evidence,
            }
        )
    except Exception as exc:  # noqa: BLE001 — GateService raising is a refusal, never a silent open
        evidence["reason"] = f"gate_service_unavailable:{type(exc).__name__}:{exc}"
        evidence["ok"] = False
        evidence["decision"] = "deny"
        return False, evidence

    evidence["decision"] = decision.decision
    ok = decision.decision == "allow"
    evidence["ok"] = ok
    if not ok:
        evidence["reason"] = decision.evidence.get("reason", "gate_service_denied")
    return ok, evidence


def _cli_settle_gate(argv: Sequence[str]) -> int:
    """CLI entry point matching the merge-gate registry convention.

    ``configs/gates.yaml``-style runners invoke a gate script as
    ``<script> {branch} {base}`` and read the exit code: 0 pass, 2
    "cannot run" (never counted as a pass), non-zero-non-2 refused. This
    lets a future gate-registry entry wire ``settle_gate`` in without any
    further change to this module.
    """
    if len(argv) < 2:
        print("usage: configtest_gates.py <branch> <base>", file=sys.stderr)
        return 2
    branch, base = argv[0], argv[1]
    repo = Path(__file__).resolve().parents[2]
    try:
        ok, evidence = settle_gate(repo, base, branch)
    except Exception as exc:  # noqa: BLE001 — CLI boundary: a crash must never read as a pass
        print(f"REFUSED: settle_gate crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    reason = str(evidence.get("reason") or "")
    if reason.startswith("gate_unrunnable") or reason.startswith("gate_service_unavailable"):
        print(f"REFUSED (cannot run): {reason}", file=sys.stderr)
        return 2
    if not ok:
        print(f"REFUSED: {reason}", file=sys.stderr)
        return 1
    print("PASS")
    return 0


DETERMINISM_ENV: dict[str, str] = {"PYTHONHASHSEED": "0"}


def deterministic_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Constructs an environment dictionary ensuring deterministic process settings, such as forcing `PYTHONHASHSEED=0`."""
    if base is None:
        base = dict(os.environ)
    return {**base, **DETERMINISM_ENV}


_RE_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_RE_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_RE_ADDR = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_RE_ABSPATH = re.compile(r"(?<![:/\w.-])/(?:[\w.-]+/)+([\w.-]+)")
_RE_LINE = re.compile(r"\bline \d+\b")
_RE_BASENAME_LINE = re.compile(r"([\w.-]+\.py):\d+\b")


def normalize_trace(text: str) -> str:
    """Normalizes a traceback or log trace to mask transient or machine-specific identifiers.

    We run the following ordered list of rules:
    1. _RE_TS: Replaces ISO8601/standard datetime timestamps with `<TS>`.
    2. _RE_UUID: Replaces standard RFC4122 UUIDs with `<UUID>`.
    3. _RE_ADDR: Replaces memory addresses starting with 0x with `<ADDR>`.
    4. _RE_ABSPATH: Collapses absolute file paths to `<PATH>/<basename>`. Which file raised an
       exception is important signal, but the random tmpdir/working directory it lived in is noise.
    5. _RE_LINE: Replaces 'line \\d+' segments with 'line <N>'.
    6. _RE_BASENAME_LINE: Replaces standard 'basename.py:\\d+' with 'basename.py:<N>'.
    """
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        line = _RE_TS.sub("<TS>", line)
        line = _RE_UUID.sub("<UUID>", line)
        line = _RE_ADDR.sub("<ADDR>", line)
        line = _RE_ABSPATH.sub(r"<PATH>/\1", line)
        line = _RE_LINE.sub("line <N>", line)
        line = _RE_BASENAME_LINE.sub(r"\1:<N>", line)
        lines.append(line)
    return "\n".join(lines) + ("\n" if lines and text.endswith("\n") else "")


def trace_diff(left: str, right: str, *, context: int = 2, max_lines: int = 200) -> GateOutcome:
    """Computes a unified diff between two raw traces after normalizing them to filter out non-deterministic noise.

    To avoid a critical fail-open bug when `max_lines` is truncated (e.g., `max_lines=0`), equality is strictly
    derived from the normalized text itself (i.e., `norm_left == norm_right`) rather than from the rendered diff list.
    The truncated diff generation is strictly for display and presentation.
    """
    norm_left = normalize_trace(left)
    norm_right = normalize_trace(right)

    left_lines = norm_left.splitlines(keepends=True)
    right_lines = norm_right.splitlines(keepends=True)

    diff_gen = difflib.unified_diff(
        left_lines, right_lines, fromfile="left", tofile="right", n=context
    )

    diff_lines = []
    truncated = False
    for i, line in enumerate(diff_gen):
        if i >= max_lines:
            truncated = True
            break
        diff_lines.append(line.rstrip("\n"))

    equal = norm_left == norm_right
    evidence = {
        "gate": "trace_diff",
        "equal": equal,
        "diff": diff_lines,
        "truncated": truncated,
        "left_lines": len(left_lines),
        "right_lines": len(right_lines),
    }
    return equal, evidence


CONFIG_PATH: Path = Path(__file__).parent.parent.parent / "configs" / "configtest.yaml"


class Status(StrEnum):
    """Enumerates the possible benchmark execution outcomes, mapping directly to database-compatible strings."""

    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    CRASH = "crash"
    INVALID = "invalid"


@dataclass(frozen=True)
class Bracket:
    """Defines the performance and cost constraints (budget ceilings) for an execution bracket."""

    category: str
    tier: str
    max_tokens: int
    max_cost_usd: float
    wall_clock_ceiling_s: int


class ConfigTestConfigError(ValueError):
    """Raised when config parsing/validation fails, preventing invalid budgets or structural errors from propagating."""


def load_config(path: Path | None = None) -> dict[tuple[str, str], Bracket]:
    """Loads and parses the configtest yaml configuration, validating constraints.

    Any drift in the supported statuses listed in the yaml configuration compared to the system Status enum values
    triggers a hard error rather than fallback behavior, ensuring configurations do not drift silently.
    Budget values are verified to be strictly positive.
    """
    if path is None:
        path = CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    statuses = data.get("statuses", [])
    enum_statuses = [s.value for s in Status]
    if statuses != enum_statuses:
        raise ConfigTestConfigError(f"statuses drift: {statuses} != {enum_statuses}")

    defaults = data.get("defaults", {})
    brackets = {}

    for cat, tiers in data.get("brackets", {}).items():
        for tier, override in tiers.items():
            max_tokens = override.get("max_tokens", defaults.get("max_tokens"))
            max_cost = override.get("max_cost_usd", defaults.get("max_cost_usd"))
            wall_clock = override.get("wall_clock_ceiling_s", defaults.get("wall_clock_ceiling_s"))

            if max_tokens is None or max_cost is None or wall_clock is None:
                raise ConfigTestConfigError(f"Missing budget in {cat} x {tier}")
            if max_tokens <= 0 or max_cost <= 0 or wall_clock <= 0:
                raise ConfigTestConfigError(f"Non-positive budget in {cat} x {tier}")

            brackets[(cat, tier)] = Bracket(
                category=cat,
                tier=tier,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
                wall_clock_ceiling_s=wall_clock,
            )
    return brackets


def bracket_for(
    category: str, tier: str, *, config: dict[tuple[str, str], Bracket] | None = None
) -> Bracket:
    """Retrieves the budget bracket for a specific category and tier.

    If the category or tier is unknown, we explicitly raise rather than falling back, ensuring that callers
    record a status of `Status.INVALID` rather than silently inheriting a default budget that nobody chose.
    """
    if config is None:
        config = load_config()
    key = (category, tier)
    if key not in config:
        raise ConfigTestConfigError(f"Unknown bracket {category} x {tier}")
    return config[key]


if __name__ == "__main__":
    raise SystemExit(_cli_settle_gate(sys.argv[1:]))
