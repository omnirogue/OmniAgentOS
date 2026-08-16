#!/usr/bin/env python3
"""Standalone launchd-vs-record mechanism-registry drift detector.

The health sentinel invokes the same pure :func:`detect_drift` function on its
30-minute cadence, which is more frequent than the required nightly pass.  This
script stays independently runnable for operator checks and a future dedicated
nightly launchd template.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from mechanism_registry import MECHANISM_REGISTRY_PATH, load_registry

_TIMEOUT_SECONDS = 15.0


def _loaded_labels() -> tuple[set[str], str | None]:
    """Return the current GUI launchd labels without allowing command failures to raise."""
    try:
        from health_sentinel import _launchctl_table  # noqa: PLC0415
    except ImportError:
        pass
    else:
        table, error = _launchctl_table()
        return set(table), error
    try:
        result = subprocess.run(
            ["/bin/launchctl", "list"],
            capture_output=True,
            check=False,
            text=True,
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), f"launchctl list failed ({type(exc).__name__}: {exc})"
    if result.returncode != 0:
        return set(), f"launchctl list failed (rc={result.returncode})"
    labels: set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3:
            labels.add(parts[2].strip())
    return labels, None


def detect_drift(
    *,
    registry_path: Path = MECHANISM_REGISTRY_PATH,
    loaded_labels: set[str] | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return every launchd label whose loaded state differs from the durable record."""
    if entries is None:
        entries, error = load_registry(registry_path)
        if error:
            return [], error
    if loaded_labels is None:
        loaded_labels, error = _loaded_labels()
        if error:
            return [], error
    drifts: list[dict[str, Any]] = []
    for entry in entries:
        label = entry["launchd_label"]
        if label is None:
            continue
        actual = label in loaded_labels
        declared = bool(entry["enabled"])
        if actual != declared:
            drifts.append(
                {
                    "id": entry["id"],
                    "launchd_label": label,
                    "launchd_enabled": actual,
                    "registry_enabled": declared,
                }
            )
    return drifts, None


def run(*, registry_path: Path = MECHANISM_REGISTRY_PATH) -> dict[str, Any]:
    """Build a total, JSON-serializable report for the CLI or sentinel caller."""
    drifts, error = detect_drift(registry_path=registry_path)
    if error:
        return {"status": "fail", "evidence": error, "drifts": [], "error": error}
    if drifts:
        sample = "; ".join(
            f"launchd says {'enabled' if item['launchd_enabled'] else 'disabled'}, registry says "
            f"{'enabled' if item['registry_enabled'] else 'disabled'} for mechanism {item['id']}"
            for item in drifts[:5]
        )
        return {"status": "fail", "evidence": sample, "drifts": drifts, "error": None}
    return {
        "status": "ok",
        "evidence": "launchd state matches the registry for every labeled mechanism",
        "drifts": [],
        "error": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff launchd state against the mechanism registry")
    parser.add_argument("--registry", type=Path, default=MECHANISM_REGISTRY_PATH)
    parser.add_argument("--json", action="store_true", help="emit the complete JSON report")
    args = parser.parse_args(argv)
    report = run(registry_path=args.registry)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"mechanism-drift-detector {report['status']}: {report['evidence']}")
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
