"""Tolerant importers for historical Fusion and ``/improve`` executions.

Fusion imports deliberately emit one manifest per package session.  A package
listed only in ``ownership.json`` still produces a manifest, which preserves
partial runs that never created a session directory.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
    new_id,
)

logger = logging.getLogger(__name__)


def _read_json(path: Path, default: Any) -> Any:
    """Read JSON without allowing an imperfect historical export to abort import."""
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        logger.warning("Missing optional Fusion file: %s", path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Could not read JSON file %s: %s", path, exc)
    return default


def _as_mapping(value: Any, context: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is not None:
        logger.warning("Expected an object for %s; using an empty object", context)
    return {}


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _terminal_state(*values: Any) -> RunState:
    """Map arbitrary source state labels to the terminal ledger state space."""
    labels_list: list[str] = []
    for value in values:
        label = _as_string(value)
        if label:
            labels_list.append(label.lower())
    labels = " ".join(labels_list)
    if any(term in labels for term in ("cancel", "canceled", "abandon", "stopped")):
        return RunState.CANCELLED
    if any(term in labels for term in ("fail", "error", "abort", "timeout", "reject")):
        return RunState.FAILED
    return RunState.COMPLETED


def _parse_timestamp(value: Any, context: str) -> str | None:
    """Return a UTC ISO timestamp when possible, retaining parseable ISO input."""
    raw = _as_string(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable timestamp for %s: %r", context, raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wall_ms(started_at: str | None, finished_at: str | None) -> int:
    if started_at is None or finished_at is None:
        return 0
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _imported_usage(started_at: str | None, finished_at: str | None) -> AgentUsage:
    return AgentUsage(
        wall_ms=_wall_ms(started_at, finished_at),
        cost_usd=None,
        estimated=True,
        source="imported",
    )


def _session_statuses(run_path: Path) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    sessions_dir = run_path / "sessions"
    try:
        candidates: Iterable[Path] = sessions_dir.glob("*/status.json")
        for status_path in candidates:
            status = _as_mapping(_read_json(status_path, {}), str(status_path))
            if status:
                status.setdefault("_session_dir", status_path.parent.name)
                statuses.append(status)
    except OSError as exc:
        logger.warning("Could not inspect Fusion sessions in %s: %s", sessions_dir, exc)
    return statuses


def _findings_count(findings: list[Any], package: str, package_count: int) -> int:
    scoped = 0
    has_scope = False
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        found_package = finding.get("package", finding.get("packageName"))
        if found_package is not None:
            has_scope = True
            if _as_string(found_package) == package:
                scoped += 1
    if has_scope:
        return scoped
    return len(findings) if package_count == 1 else 0


def import_fusion_run(run_dir: str) -> list[RunManifest]:
    """Convert a Fusion directory to one terminal manifest per package session.

    Missing and malformed optional files are warned about and represented by
    conservative defaults, rather than causing historical imports to fail.
    """
    run_path = Path(run_dir)
    manifest = _as_mapping(_read_json(run_path / "manifest.json", {}), "manifest.json")
    ownership_doc = _as_mapping(_read_json(run_path / "ownership.json", {}), "ownership.json")
    ownership = _as_mapping(ownership_doc.get("packages"), "ownership.packages")
    validation = _as_mapping(_read_json(run_path / "validation.json", {}), "validation.json")
    raw_findings = _read_json(run_path / "findings" / "findings.json", [])
    findings = raw_findings if isinstance(raw_findings, list) else []
    if raw_findings is not None and not isinstance(raw_findings, list):
        logger.warning("Expected findings/findings.json to be an array")

    statuses = _session_statuses(run_path)
    # Sessions are the primary source of rows: the same package may have been
    # worked in more than once.  Ownership-only packages are appended below.
    entries: list[tuple[str, dict[str, Any]]] = []
    packages_with_sessions: set[str] = set()
    for status in statuses:
        package = _as_string(status.get("package"))
        if package is None:
            package = (
                _as_string(status.get("sessionId"))
                or _as_string(status.get("_session_dir"))
                or "run"
            )
        else:
            packages_with_sessions.add(package)
        entries.append((package, status))
    for package in ownership:
        if package not in packages_with_sessions:
            entries.append((package, {}))
    if not entries:
        entries = [("run", {})]

    source_run_id = _as_string(manifest.get("runId")) or run_path.name or new_id("fusion")
    task = _as_string(manifest.get("task"))
    task_id = task or f"fusion-{source_run_id}"
    manifest_started_at = _parse_timestamp(manifest.get("createdAt"), "manifest.createdAt")
    manifest_finished_at = _parse_timestamp(manifest.get("updatedAt"), "manifest.updatedAt")
    manifests: list[RunManifest] = []
    for index, (package, status) in enumerate(entries, start=1):
        owner = _as_mapping(ownership.get(package), f"ownership.packages.{package}")
        started_at = _parse_timestamp(
            status.get("startedAt", manifest.get("createdAt")), f"{package}.startedAt"
        )
        finished_at = _parse_timestamp(
            status.get("updatedAt", manifest.get("updatedAt")), f"{package}.updatedAt"
        )
        all_pass = validation.get("allPass")
        extra: dict[str, Any] = {
            "source_run_id": source_run_id,
            "package": package,
            "findings_count": _findings_count(findings, package, len(entries)),
            "validation": {"allPass": all_pass if isinstance(all_pass, bool) else None},
            "fableApproval": manifest.get("fableApproval"),
        }
        if status.get("sessionId") is not None:
            extra["session_id"] = status["sessionId"]
        if manifest.get("phase") is not None:
            extra["phase"] = manifest["phase"]
        manifests.append(
            RunManifest(
                run_id=source_run_id if len(entries) == 1 else f"{source_run_id}:{package}:{index}",
                task_id=task_id,
                discipline=_as_string(manifest.get("mode")),
                harness=HarnessProfile(
                    harness=HarnessType.FUSION,
                    version="imported",
                    params={"source_run_dir": str(run_path), "package": package},
                ),
                agent=_as_string(owner.get("agent", status.get("agent"))),
                model=_as_string(owner.get("model", status.get("model"))),
                state=_terminal_state(
                    status.get("state"), manifest.get("status"), manifest.get("phase")
                ),
                started_at=started_at,
                finished_at=finished_at,
                usage=_imported_usage(manifest_started_at, manifest_finished_at),
                receipts=[],
                artifacts=[],
                extra=extra,
            )
        )
    return manifests


_IMPROVE_COLUMNS = (
    "wave",
    "exp_id",
    "model",
    "commit",
    "metric",
    "baseline",
    "delta",
    "status",
    "description",
)


def import_improve_tsv(path: str) -> list[RunManifest]:
    """Convert an ``/improve`` TSV export to one terminal manifest per row."""
    tsv_path = Path(path)
    try:
        stream = tsv_path.open(encoding="utf-8", newline="")
    except FileNotFoundError:
        logger.warning("Improve results file does not exist: %s", tsv_path)
        return []
    except OSError as exc:
        logger.warning("Could not read improve results file %s: %s", tsv_path, exc)
        return []

    manifests: list[RunManifest] = []
    try:
        with stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if reader.fieldnames is None:
                logger.warning("Improve results file has no header: %s", tsv_path)
                return []
            missing = [column for column in _IMPROVE_COLUMNS if column not in reader.fieldnames]
            if missing:
                logger.warning(
                    "Improve results missing columns (%s): %s", ", ".join(missing), tsv_path
                )
            for row_number, row in enumerate(reader, start=1):
                experiment_id = _as_string(row.get("exp_id"))
                task_id = experiment_id or f"improve-{row_number}"
                extra = {
                    column: _as_string(row.get(column))
                    for column in _IMPROVE_COLUMNS
                    if column != "commit"
                }
                extra["commit"] = _as_string(row.get("commit"))
                manifests.append(
                    RunManifest(
                        run_id=f"improve:{experiment_id or row_number}:{row_number}",
                        task_id=task_id,
                        harness=HarnessProfile(
                            harness=HarnessType.IMPROVE,
                            version="imported",
                            params={"source_tsv": str(tsv_path), "row": row_number},
                        ),
                        model=_as_string(row.get("model")),
                        state=_terminal_state(row.get("status")),
                        usage=_imported_usage(None, None),
                        receipts=[],
                        artifacts=[],
                        extra=extra,
                    )
                )
    except (csv.Error, OSError, UnicodeError) as exc:
        logger.warning("Could not parse improve results file %s: %s", tsv_path, exc)
    return manifests
