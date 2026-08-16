"""Append-only JSONL storage for terminal run manifests."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from omniagentos.contracts import RunManifest

_MONTH_FILENAME_RE = re.compile(r"runs-(?P<month>\d{6})\.jsonl")
_FINISHED_AT_RE = re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})")


def manifest_line(m: RunManifest) -> str:
    """Serialize a manifest as one deterministic JSONL record."""
    return json.dumps(m.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"


def append_manifest(ledger_dir: str, m: RunManifest) -> str:
    """Durably append *m* unless its run ID is already in its month file."""
    path = Path(ledger_dir) / f"runs-{_month_for(m.finished_at)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+", encoding="utf-8") as ledger_file:
        _lock_exclusively(ledger_file)
        try:
            ledger_file.seek(0)
            if _contains_run_id(ledger_file, m.run_id):
                return str(path)

            ledger_file.write(manifest_line(m))
            ledger_file.flush()
            os.fsync(ledger_file.fileno())
        finally:
            _unlock(ledger_file)

    return str(path)


def read_manifests(
    ledger_dir: str, run_id: str | None = None, limit: int = 100
) -> list[RunManifest]:
    """Read valid manifest records from newest to oldest.

    Invalid JSON or invalid manifest data is logged and skipped so a damaged line
    never prevents access to the rest of the append-only ledger.
    """
    if limit <= 0:
        return []

    manifests: list[RunManifest] = []
    for path in _month_files_newest_first(Path(ledger_dir)):
        try:
            with path.open(encoding="utf-8") as ledger_file:
                lines = ledger_file.readlines()
        except OSError as error:
            logging.warning("Unable to read ledger file %s: %s", path, error)
            continue

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                manifest = RunManifest.model_validate_json(line)
            except ValueError as error:
                logging.warning("Skipping corrupt ledger line in %s: %s", path, error)
                continue

            if run_id is not None and manifest.run_id != run_id:
                continue
            manifests.append(manifest)
            if len(manifests) >= limit:
                return manifests

    return manifests


def _month_for(finished_at: str | None) -> str:
    if finished_at is not None:
        match = _FINISHED_AT_RE.match(finished_at)
        if match is not None and 1 <= int(match["month"]) <= 12:
            return f"{match['year']}{match['month']}"
    return datetime.now(UTC).strftime("%Y%m")


def _month_files_newest_first(ledger_dir: Path) -> Iterator[Path]:
    try:
        files = [
            path
            for path in ledger_dir.iterdir()
            if path.is_file() and _MONTH_FILENAME_RE.fullmatch(path.name) is not None
        ]
    except FileNotFoundError:
        return
    except OSError as error:
        logging.warning("Unable to list ledger directory %s: %s", ledger_dir, error)
        return

    yield from sorted(files, key=lambda path: path.name, reverse=True)


def _contains_run_id(ledger_file: TextIO, run_id: str) -> bool:
    for line in ledger_file:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("run_id") == run_id:
            return True
    return False


def _lock_exclusively(ledger_file: TextIO) -> None:
    """Use an advisory lock when the platform provides ``fcntl``."""
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(ledger_file.fileno(), fcntl.LOCK_EX)


def _unlock(ledger_file: TextIO) -> None:
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(ledger_file.fileno(), fcntl.LOCK_UN)
