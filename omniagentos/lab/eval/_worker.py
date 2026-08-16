"""Private subprocess worker for protected evaluator database access.

This module is invoked by ProtectedGrader via subprocess to keep held-out
expected values OUT of the parent (campaign/judge/curator) process address space.
It receives the protected DB path and per-run encryption key only in its spawn
environment. Plaintext is decrypted only here and is never stored on disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from omniagentos.lab.eval._crypto import (
    EVAL_KEY_ENV,
    DecryptionError,
    decrypt_expected,
    encrypt_expected,
)
from omniagentos.lab.eval.paths import PROTECTED_DB_ENV
from omniagentos.lab.eval.scoring import aggregate_case_scores, score_case

_SCHEMA = """
CREATE TABLE IF NOT EXISTS protected_expected (
    case_id       TEXT PRIMARY KEY,
    expected_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    """Open the protected database, which is passed via environment variable."""
    raw_path = os.environ.get(PROTECTED_DB_ENV)
    if not raw_path:
        raise RuntimeError("protected path is not configured")
    path = Path(raw_path).expanduser().resolve()
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(_SCHEMA)
    return connection


def _score(connection: sqlite3.Connection, request: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Score outputs against held-out expected values.

    Returns only the aggregate metrics and per-case scores (floats) — never
    the expected values themselves.
    """
    case_ids = request.get("case_ids", [])
    outputs = request.get("outputs", {})
    if not isinstance(case_ids, list) or not isinstance(outputs, dict):
        raise ValueError("invalid score request")

    per_case: dict[str, dict[str, float]] = {}
    for raw_case_id in case_ids:
        case_id = str(raw_case_id)
        row = connection.execute(
            "SELECT expected_json FROM protected_expected WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            # Treat missing expected as a scoring failure, not a worker error.
            # The parent will convert this to MissingExpectedError.
            per_case[case_id] = {}
            continue
        try:
            expected = decrypt_expected(key, row["expected_json"])
        except DecryptionError:
            # Surface corrupt/wrong-key rows through the parent's existing
            # MissingExpectedError integrity path, not as a silent bad score.
            per_case[case_id] = {}
            continue
        output = outputs.get(case_id, {})
        per_case[case_id] = score_case(expected, output)

    aggregate = aggregate_case_scores(per_case)
    metrics = {
        "accuracy": aggregate.get("correct", 0.0),
        "mean_score": aggregate.get("score", 0.0),
    }
    return {
        "metrics": metrics,
        "per_case": per_case,
    }


def _run(request: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Dispatch a request to initialize, put, or score."""
    operation = request.get("operation")
    if operation == "initialize":
        connection = _connect()
        connection.close()
        return {"ok": True}

    with _connect() as connection:
        if operation in {"put", "put_many"}:
            now = request.get("now")
            if not isinstance(now, str):
                raise ValueError("now timestamp is required")
            cases: dict[str, Any]
            if operation == "put":
                case_id = request.get("case_id")
                expected = request.get("expected")
                if not isinstance(case_id, str) or not isinstance(expected, dict):
                    raise ValueError("invalid put request")
                cases = {case_id: expected}
            else:
                raw_cases = request.get("cases")
                if not isinstance(raw_cases, dict) or not raw_cases:
                    raise ValueError("invalid put_many request")
                cases = raw_cases
            for case_id, expected in cases.items():
                if not isinstance(case_id, str) or not case_id or not isinstance(expected, dict):
                    raise ValueError("invalid expected case")
                payload = encrypt_expected(key, expected)
                connection.execute(
                    "INSERT INTO protected_expected "
                    "(case_id, expected_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET "
                    "expected_json = excluded.expected_json, updated_at = excluded.updated_at",
                    (case_id, payload, now, now),
                )
            return {"ok": True}

        if operation == "score":
            return _score(connection, request, key)

    raise ValueError("unknown operation")


def main() -> int:
    """Read JSON request from stdin, process it, write JSON response to stdout.

    Never serialize exception details: they may contain protected data.
    Return 0 on success, 1 on any error (details not leaked).
    """
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        key_raw = os.environ.get(EVAL_KEY_ENV)
        if not key_raw:
            raise RuntimeError("protected encryption key is not configured")
        response = _run(request, key_raw.encode("ascii"))
    except Exception:
        # Never serialize exception details to stdout: they may contain protected data.
        return 1
    sys.stdout.write(json.dumps(response, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
