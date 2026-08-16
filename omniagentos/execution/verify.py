"""Live, non-mutating working-tree verification for execution.

``classify_risk`` correctly treats keyword matches as L4 when it is protecting
the reliability pipeline.  Execution uses the same classifier for its path and
tier knowledge, but deliberately keeps a keyword match *advisory*: ordinary
diffs commonly mention authentication or deletion.  Consequently
``execution_level`` is derived only from tiers and declaration mismatch, never
from a keyword-only reliability L4.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omniagentos.contracts import DeclaredScope, ObservedChange, ScopeEnforcement
from omniagentos.reliability.governance import DiffEntry, RiskResult, classify_risk

__all__ = [
    "execution_level_from_risk",
    "observe_working_tree",
    "scope_verdict",
    "verify_working_tree",
]


def _norm(path: str | Path | None) -> str:
    """Return a stable, repo-relative spelling suitable for comparisons."""
    if path is None:
        return ""
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    return "" if normalized == "." else normalized.rstrip("/")


def _dedupe(paths: Iterable[str | Path]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _norm(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _scope_parts(
    declared: DeclaredScope | None | Iterable[str],
) -> tuple[list[str] | None, list[str]]:
    """Return flattened declarations and create roots; ``None`` disables scope."""
    if declared is None:
        return None, []
    if isinstance(declared, Mapping):
        values: list[str] = []
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        ):
            values.extend(str(item) for item in (declared.get(field) or ()))
        return _dedupe(values), _dedupe(declared.get("create_roots") or ())
    if isinstance(declared, DeclaredScope) or any(
        hasattr(declared, field)
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        )
    ):
        values = []
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        ):
            values.extend(str(item) for item in (getattr(declared, field, None) or ()))
        return _dedupe(values), _dedupe(getattr(declared, "create_roots", None) or ())
    if isinstance(declared, (str, Path)):
        return _dedupe([declared]), []
    return _dedupe(declared), []


def _under(path: str, root: str) -> bool:
    """Component-aware root coverage without scope telemetry dependencies."""
    return root in ("", ".") or path == root or path.startswith(root + "/")


def _git(root: Path, args: list[str], *, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _resolved_paths(root: Path, entry: DiffEntry) -> None:
    resolved: list[str] = []
    for raw in filter(None, (entry.path, entry.old_path)):
        candidate = root / raw
        try:
            if candidate.is_symlink():
                entry.is_symlink = True
            real = Path(os.path.realpath(candidate))
            try:
                resolved.append(_norm(real.relative_to(root)))
            except ValueError:
                resolved.append(str(real))
        except OSError:
            # A deleted/broken path cannot be resolved; the literal remains authoritative.
            continue
    entry.resolved_paths = _dedupe(resolved)


def _entries_from_index(root: Path, base_ref: str) -> list[DiffEntry]:
    """Snapshot all worktree changes into a disposable index and read that diff."""
    git_dir = Path(_git(root, ["rev-parse", "--git-dir"]).strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    real_index = git_dir / "index"
    fd, temp_name = tempfile.mkstemp(prefix="omniagentos-verify-index-")
    os.close(fd)
    temp_index = Path(temp_name)
    try:
        # Git treats an existing zero-byte file as a corrupt index.  Start absent
        # when there is no real index, otherwise take a private byte-for-byte copy.
        temp_index.unlink(missing_ok=True)
        if real_index.exists():
            shutil.copy2(real_index, temp_index)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(temp_index)
        _git(root, ["add", "-A"], env=env)
        name_status = _git(
            root, ["diff", "--cached", "--name-status", "-M", "-C", base_ref], env=env
        )
        entries: list[DiffEntry] = []
        for line in name_status.splitlines():
            fields = line.split("\t")
            if not fields or not fields[0].strip():
                continue
            status = fields[0].strip()
            old_path = fields[1].strip() if status[:1] in {"R", "C"} and len(fields) >= 3 else None
            path = fields[2].strip() if old_path is not None else fields[-1].strip()
            content = _git(root, ["diff", "--cached", base_ref, "--", path], env=env)
            entry = DiffEntry(
                status=status, path=_norm(path), old_path=_norm(old_path), content=content
            )
            _resolved_paths(root, entry)
            entries.append(entry)
        return entries
    finally:
        temp_index.unlink(missing_ok=True)
        Path(f"{temp_index}.lock").unlink(missing_ok=True)


def observe_working_tree(repo_root: str | Path, *, base_ref: str = "HEAD") -> ObservedChange:
    """Observe changed paths using a disposable index; never alter the real index."""
    root = Path(repo_root).resolve()
    try:
        entries = _entries_from_index(root, base_ref)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ObservedChange(source="unobserved", base_ref=base_ref)
    paths = _dedupe(path for entry in entries for path in (entry.path, entry.old_path) if path)
    return ObservedChange(
        source="git-index",
        base_ref=base_ref,
        head_ref="HEAD",
        paths=paths,
        status_by_path={entry.path: entry.status for entry in entries},
    )


def execution_level_from_risk(risk: RiskResult, *, suggested_level: int = 1) -> int:
    """Map reliability facts to execution rungs, excluding keyword-only L4s."""
    try:
        base = int(suggested_level)
    except (TypeError, ValueError):
        base = 1
    base = max(1, min(4, base))
    if risk.tier_p or risk.declared_mismatch:
        return 4
    if risk.tier_s:
        return max(base, 3)
    return base


def scope_verdict(
    *,
    declared: list[str] | None,
    create_roots: list[str],
    entries: list[DiffEntry],
    observed: ObservedChange,
    suggested_level: int,
    enforcement: ScopeEnforcement,
    repo_root: Path,
) -> dict[str, Any]:
    """Build the serializable ScopeVerdict-compatible execution result."""
    actual = _dedupe(path for entry in entries for path in (entry.path, entry.old_path) if path)
    if declared is None:
        covered, missing, risk_declarations = list(actual), [], None
    else:
        covered = [
            path
            for path in actual
            if path in declared or any(_under(path, root) for root in create_roots)
        ]
        missing = [path for path in actual if path not in covered]
        # The reliability classifier accepts exact paths only.  Add the concrete
        # descendants that a declared create root covers, preserving its mismatch fact.
        risk_declarations = _dedupe([*declared, *covered])
    risk = classify_risk(
        entries, risk_declarations, suggested_level=suggested_level, repo_root=repo_root
    )
    missing_creates: list[str] = []
    missing_must_modify: list[str] = []
    # These obligations only exist for a real DeclaredScope / scope-shaped object.
    # Callers that pass a flat iterable intentionally have no create/must contract.
    precision = 1.0 if not actual else len(covered) / len(actual)
    reasons = list(risk.reasons)
    # H-13: 'unobserved' is NOT clean. Zero observed paths after a failed
    # snapshot is inconclusive evidence, not proof that nothing changed.
    unobserved = observed.source == "unobserved"
    if unobserved:
        reasons.append("working-tree observation was inconclusive")
    if missing:
        reasons.append(f"undeclared paths: {', '.join(missing)}")
    execution_level = execution_level_from_risk(risk, suggested_level=suggested_level)
    if unobserved:
        # Fail closed onto the highest rung so callers that only read level still
        # refuse a clean mechanical pass on observation failure.
        execution_level = 4
    return {
        "ok": (not missing) and (not unobserved),
        "degraded": unobserved,
        "enforcement": enforcement.value,
        "declared": declared or [],
        "actual": actual,
        "missing": missing,
        "covered": covered,
        "precision": 0.0 if unobserved else precision,
        "risk_level": risk.level,
        "keyword_advisory": risk.keyword_hit,
        "execution_level": execution_level,
        "tier": risk.tier,
        "undeclared": missing,
        "missing_creates": missing_creates,
        "missing_must_modify": missing_must_modify,
        "byproducts": [],
        "keyword_hit": risk.keyword_hit,
        "reasons": reasons,
        "observed_source": observed.source,
    }


def verify_working_tree(
    repo_root: str | Path,
    declared: DeclaredScope | None | Iterable[str] = None,
    *,
    suggested_level: int = 1,
    enforcement: ScopeEnforcement | str = ScopeEnforcement.OBSERVE,
    base_ref: str = "HEAD",
) -> dict[str, Any]:
    """Return a live, structured scope verdict without mutating the worktree/index."""
    root = Path(repo_root).resolve()
    try:
        enforcement_value = ScopeEnforcement(enforcement)
    except ValueError:
        enforcement_value = ScopeEnforcement.OBSERVE
    declared_paths, create_roots = _scope_parts(declared)
    observation_error: str | None = None
    try:
        entries = _entries_from_index(root, base_ref)
        observed = ObservedChange(
            source="git-index",
            base_ref=base_ref,
            head_ref="HEAD",
            paths=_dedupe(
                path for entry in entries for path in (entry.path, entry.old_path) if path
            ),
            status_by_path={entry.path: entry.status for entry in entries},
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # H-13: never substitute an empty change set for a failed observation.
        entries = []
        observed = ObservedChange(source="unobserved", base_ref=base_ref)
        observation_error = f"{type(exc).__name__}:{exc}"
    verdict = scope_verdict(
        declared=declared_paths,
        create_roots=create_roots,
        entries=entries,
        observed=observed,
        suggested_level=suggested_level,
        enforcement=enforcement_value,
        repo_root=root,
    )
    if observation_error is not None:
        verdict["observation_error"] = observation_error
        verdict["reasons"] = [
            *list(verdict.get("reasons") or ()),
            f"observation_error:{observation_error}",
        ]
    if isinstance(declared, DeclaredScope) or any(
        hasattr(declared, name) for name in ("files_to_create", "must_modify")
    ):
        creates = _dedupe(getattr(declared, "files_to_create", ()) or ())
        must_modify = _dedupe(getattr(declared, "must_modify", ()) or ())
        verdict["missing_creates"] = [path for path in creates if not (root / path).exists()]
        verdict["missing_must_modify"] = [
            path for path in must_modify if path not in verdict["actual"]
        ]
        for path in verdict["missing_creates"]:
            verdict["reasons"].append(f"missing-create:{path}")
        for path in verdict["missing_must_modify"]:
            verdict["reasons"].append(f"missing-must-modify:{path}")
    return verdict
