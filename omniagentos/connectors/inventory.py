"""Name-only inventory of the operator's environment-variable vault.

This module deliberately never evaluates an env file or retains its values.  It
is a report generator: the only output it writes is a JSON report below
``var/``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from omniagentos.connectors import ConnectorRegistry, load_registry
from omniagentos.connectors.registry import default_vault_path, vault_key_names
from omniagentos.runtime_paths import resolve_var_root

BUCKETS = ("ADOPT", "DECLARE", "ARCHIVE", "DEAD")
DEFAULT_EXCLUDE_DIRS = (
    ".git",
    "__pycache__",
    ".venv",
    "var",
    ".pytest_cache",
    "node_modules",
    ".next",
)
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class InventoryBucket:
    """One classification bucket with its names."""

    names: frozenset[str]

    @property
    def count(self) -> int:
        return len(self.names)


@dataclass(frozen=True)
class InventoryReport:
    """Full inventory result: all four buckets plus report metadata."""

    adopt: InventoryBucket
    declare: InventoryBucket
    archive: InventoryBucket
    dead: InventoryBucket
    violations: tuple[str, ...]
    repo_path: str = ""
    env_file: str = ""
    timestamp: str = ""
    repo_names: frozenset[str] = frozenset()

    @property
    def total_scanned(self) -> int:
        return sum(bucket.count for bucket in (self.adopt, self.declare, self.archive, self.dead))

    @property
    def is_valid(self) -> bool:
        return not self.violations


def _parse_env_file(path: Path) -> frozenset[str]:
    """Extract variable names from assignment lines without reading values.

    Shares :func:`omniagentos.connectors.registry.vault_key_names` with
    ``parse_vault`` so the two readers of the operator's vault cannot answer
    "what is an assignment line" differently for the same file.
    """
    try:
        return vault_key_names(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Unable to read env file {path}") from exc


def _grep_repo_for_names(
    names: Iterable[str],
    repo_root: Path,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
) -> frozenset[str]:
    """Batch-search the repository and return names with at least one match."""
    search_names = tuple(sorted(set(names)))
    if not search_names:
        return frozenset()
    pattern = r"\b(?:" + "|".join(re.escape(name) for name in search_names) + r")\b"
    excludes = tuple(exclude_dirs)
    rg = shutil.which("rg")
    if rg:
        command = [
            rg,
            "--no-ignore",
            "--hidden",
            "--no-heading",
            "--no-filename",
            "--only-matching",
            "--regexp",
            pattern,
        ]
        command.extend(flag for directory in excludes for flag in ("--glob", f"!**/{directory}/**"))
        command.append(".")
    else:
        # The real bug this fallback previously had: without ``-h``, `grep -r`
        # prefixes every match with its file path (`path/to/file:MATCH`), which
        # corrupts the later ``result.stdout.split()``/set-intersection against
        # ``search_names`` -- a match is silently lost because the "word" that
        # comes out of ``.split()`` is the whole "path:match" string, not the
        # bare match (nothing to do with BSD vs GNU ``\\b`` semantics). ``-h``
        # suppresses the filename prefix; without ``-o`` we also lose word
        # isolation, so extraction is redone below via the original Python
        # ``pattern`` against grep's raw (filename-free) stdout. ``-I`` skips
        # binary files entirely -- without it, a binary file whose PATH happens
        # to contain a search name (not its content) still produces a "Binary
        # file <path> matches" line, and naive extraction from that line would
        # report a false positive keyed on the path, not a real content match.
        grep_pattern = "|".join(re.escape(name) for name in search_names)
        command = ["grep", "-rEhI"]
        command.extend(f"--exclude-dir={directory}" for directory in excludes)
        command.extend([grep_pattern, str(repo_root)])
    result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode, command, result.stdout, result.stderr
        )
    found = set(result.stdout.split()) if rg else set(re.findall(pattern, result.stdout))
    return frozenset(found.intersection(search_names))


def _bucket(name: str, bucket_names: dict[str, set[str]]) -> None:
    for names in bucket_names.values():
        names.discard(name)


def _classify_names(
    vault_names: frozenset[str],
    registry_names: frozenset[str],
    repo_names: frozenset[str],
    overrides: dict[str, str] | None = None,
) -> InventoryReport:
    """Classify every supplied name exactly once, honoring operator overrides."""
    overrides = overrides or {}
    violations: list[str] = []
    buckets: dict[str, set[str]] = {bucket: set() for bucket in BUCKETS}
    all_names = set(vault_names) | set(registry_names) | set(repo_names) | set(overrides)

    for name in sorted(all_names):
        override = overrides.get(name)
        if override is not None:
            if override not in BUCKETS:
                raise ValueError(f"Unknown inventory bucket {override!r} for {name}")
            if override == "ARCHIVE" and name in repo_names:
                violations.append(f"ARCHIVE override is repo-referenced: {name}")
                continue
            buckets[override].add(name)
            continue

        # ADOPT includes already-declared names so every name in the input vault
        # has a home; missing declarations are the primary ADOPT use case.
        if name in registry_names:
            buckets["ADOPT"].add(name)
        elif name in repo_names:
            buckets["DECLARE"].add(name)
        elif name in vault_names:
            buckets["ARCHIVE"].add(name)
        else:
            buckets["DEAD"].add(name)

    for name in tuple(buckets["ARCHIVE"]):
        if name in repo_names:
            _bucket(name, buckets)
            violations.append(f"ARCHIVE name is repo-referenced: {name}")

    # Named, not a starred generator: the four buckets are positional fields in a
    # fixed order, and unpacking a generator into them hides a field-order mistake
    # from both the reader and the type checker.
    return InventoryReport(
        adopt=InventoryBucket(frozenset(buckets["ADOPT"])),
        declare=InventoryBucket(frozenset(buckets["DECLARE"])),
        archive=InventoryBucket(frozenset(buckets["ARCHIVE"])),
        dead=InventoryBucket(frozenset(buckets["DEAD"])),
        violations=tuple(sorted(violations)),
    )


def _registry_names(registry: ConnectorRegistry) -> frozenset[str]:
    return frozenset(name for connector in registry.connectors.values() for name in connector.env)


def _read_overrides(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise FileNotFoundError(path) from exc
    overrides: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, bucket = line.partition("=")
        name, bucket = name.strip(), (bucket.strip().upper() if separator else "DEAD")
        if not _VALID_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid override name: {name!r}")
        overrides[name] = bucket
    return overrides


def _run_inventory(
    env_file: Path,
    registry: ConnectorRegistry | None = None,
    repo_root: Path | None = None,
    overrides_file: Path | None = None,
) -> InventoryReport:
    """Run the name-only inventory and fail on hard invariant violations."""
    if not env_file.is_file():
        raise FileNotFoundError(env_file)
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    vault_names = _parse_env_file(env_file)
    loaded_registry = registry or load_registry()
    registry_names = _registry_names(loaded_registry)
    search_names = vault_names | registry_names
    overrides = _read_overrides(overrides_file) if overrides_file else {}
    repo_names = _grep_repo_for_names(search_names | frozenset(overrides), root)
    report = _classify_names(vault_names, registry_names, repo_names, overrides)
    timestamp = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
    report = InventoryReport(
        report.adopt,
        report.declare,
        report.archive,
        report.dead,
        report.violations,
        str(root),
        str(env_file.resolve()),
        timestamp,
        repo_names,
    )
    if not report.is_valid:
        raise ValueError("; ".join(report.violations))
    return report


def write_report(report: InventoryReport, repo_root: Path | None = None) -> Path:
    """Write a JSON report under ``var/`` and return its path."""
    timestamp = report.timestamp or datetime.datetime.now(datetime.UTC).isoformat().replace(
        "+00:00", "Z"
    )
    # An explicit repo root is also an explicit report destination.  The
    # implicit destination is runtime state and must follow this process's
    # isolated var root instead of re-deriving the operator checkout from
    # ``__file__``.
    target_dir = (
        repo_root.resolve() / "var" if repo_root is not None else resolve_var_root()
    )
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"secrets-inventory-{timestamp.replace(':', '-')}.json"
    payload = {
        "timestamp": timestamp,
        "repo_path": report.repo_path or str(root),
        "env_file": report.env_file,
        "total_names_scanned": report.total_scanned,
        "totals": {bucket: getattr(report, bucket.lower()).count for bucket in BUCKETS},
        "details": {bucket: sorted(getattr(report, bucket.lower()).names) for bucket in BUCKETS},
        "repo_scan_summary": {
            "names_found_in_repo": len(report.repo_names),
            "names_not_in_repo": report.total_scanned - len(report.repo_names),
        },
        "violations": list(report.violations),
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=default_vault_path())
    parser.add_argument("--registry-path", type=Path)
    parser.add_argument("--overrides-file", type=Path)
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args()
    registry = load_registry(str(args.registry_path)) if args.registry_path else None
    report = _run_inventory(
        args.env_file,
        registry=registry,
        repo_root=args.repo_root,
        overrides_file=args.overrides_file,
    )
    print(write_report(report, args.repo_root))


if __name__ == "__main__":
    main()
