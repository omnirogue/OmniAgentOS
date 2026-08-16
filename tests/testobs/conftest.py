"""Fixture builders for the test-observability readers.

Every source is written under a tmp ``var`` root that ``OMNIAGENTOS_TESTOBS_VAR``
points at, so no test ever reads (or could write) the real var/ tree or the
control-plane database.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.testobs.readers import _clear_cache as clear_cache

# The two tables the readers touch, verbatim from var/northstar-cert/results.sqlite3.
_CERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pulse_series (
  metric TEXT NOT NULL, date TEXT NOT NULL, value REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (metric, date)
);
CREATE TABLE IF NOT EXISTS eval_results (
  id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, arm TEXT NOT NULL,
  suite_id TEXT NOT NULL, suite_version INTEGER NOT NULL, split TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}', per_case_json TEXT NOT NULL DEFAULT '{}',
  replicate INTEGER NOT NULL DEFAULT 0, deterministic_passed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
"""


def day_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def ts_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ts_offset(days: int, offset_hours: int = -5) -> tuple[str, str]:
    """A timestamp written at ``offset_hours``, plus the UTC date it truly falls on.

    Pinned at 02:30 UTC so the offset rendering lands on the PREVIOUS calendar
    date: slicing the first ten characters answers with the wrong day, parsing
    answers with the right one.
    """
    moment = (datetime.now(UTC) - timedelta(days=days)).replace(
        hour=2, minute=30, second=0, microsecond=0
    )
    local = moment + timedelta(hours=offset_hours)
    sign = "+" if offset_hours >= 0 else "-"
    text = f"{local.strftime('%Y-%m-%dT%H:%M:%S')}{sign}{abs(offset_hours):02d}:00"
    return text, moment.date().isoformat()


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """The X-Session-Token header the gated testobs reads require.

    TOKEN_PATH is redirected into tmp so the real var/secrets token is never
    read or created (same pattern as tests/pulse/conftest.py).
    """
    from omniagentos.sessions import token

    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def var_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty tmp var/ root the readers resolve to. Cache cleared both ways."""
    root = tmp_path / "var"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_TESTOBS_VAR", str(root))
    clear_cache()
    yield root
    clear_cache()


def encode_reason(reason: str) -> str:
    """Encode exactly as scripts/northstar_cert/record_results.py does."""
    return base64.urlsafe_b64encode(reason.encode("utf-8")).decode("ascii")


# The reason the round-trip test asserts on. It is chosen so the two base64
# alphabets DISAGREE on it — the recorder writes urlsafe, and a reason whose two
# encodings happen to be identical would let a standard-alphabet decoder look
# correct. The assertion keeps that property from eroding into a masked bug.
FAIL_REASON = "pytest_failure:Failed: Timeout (>1800.0s)"
assert set(encode_reason(FAIL_REASON)) & set("-_"), (
    f"FAIL_REASON {FAIL_REASON!r} encodes identically in both base64 alphabets, "
    "so it cannot distinguish a urlsafe decoder from a standard one — pick another"
)


def seed_northstar(
    root: Path,
    *,
    run_id: str = "nscert-t1-20260813T101005Z",
    scored: bool = True,
    created_at: str | None = None,
) -> Path:
    """A two-day pulse series plus one run: 1 PASS, 1 gate FAIL, 1 NOT_EVALUABLE, 1 junk.

    ``scored=False`` writes a run whose every row is unscored — a present source
    that measured nothing, which must NOT read as available. Calling this twice
    on one root adds a SECOND run (rows are namespaced by ``run_id``), which is
    how latest-run selection is exercised.
    """
    path = root / "northstar-cert" / "results.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_CERT_SCHEMA)
    if not conn.execute("SELECT COUNT(*) FROM pulse_series").fetchone()[0]:
        conn.executemany(
            "INSERT INTO pulse_series (metric, date, value) VALUES (?, ?, ?)",
            [
                ("nsc.distance", day_ago(2), 25.1),
                ("nsc.distance", day_ago(1), 23.7),
                ("nsc.gate_pass_rate", day_ago(1), 88.2),
                ("nsc.void_ratio", day_ago(1), 0.0),
            ],
        )

    def case(check_id: str, verdict: str | None, reason: str | None = None) -> tuple[str, str]:
        counts = {k: 0.0 for k in ("pass", "fail", "not_evaluable", "void")}
        if verdict is not None:
            counts[verdict] = 1.0
        per_case: dict[str, object] = {check_id: dict(counts)}
        if reason is not None:
            per_case[f"__nsc_reason__:{encode_reason(reason)}"] = {}
        return json.dumps(counts), json.dumps(per_case)

    created = created_at or f"{day_ago(1)}T10:43:29Z"
    rows = (
        [
            (f"evr_1_{run_id}", *case("NSC-C02-04", "pass")),
            (f"evr_2_{run_id}", *case("NSC-C11-01", "fail", FAIL_REASON)),
            (f"evr_3_{run_id}", *case("NSC-C01-01", "not_evaluable", "precondition_missing:glue")),
        ]
        if scored
        else [(f"evr_1_{run_id}", *case("NSC-C02-04", None)),
              (f"evr_2_{run_id}", *case("NSC-C11-01", None))]
    )
    conn.executemany(
        "INSERT INTO eval_results (id, experiment_id, arm, suite_id, suite_version, split,"
        " metrics_json, per_case_json, created_at)"
        f" VALUES (?, '{run_id}', 'champion', 'evs_northstar_cert_v2', 2, 'dev', ?, ?, ?)",
        [(row_id, metrics, per_case, created) for row_id, metrics, per_case in rows],
    )
    # A row whose JSON is corrupt: skipped and counted, never raised.
    conn.execute(
        "INSERT INTO eval_results (id, experiment_id, arm, suite_id, suite_version, split,"
        " metrics_json, per_case_json, created_at)"
        f" VALUES ('evr_bad_{run_id}', '{run_id}', 'champion', 'evs_northstar_cert_v2', 2, 'dev',"
        " '{not json', '{}', ?)",
        (created,),
    )
    conn.commit()
    conn.close()
    return path


def write_junit(path: Path, *, tests: int, failures: int, errors: int, skipped: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{tests}" time="1.0"/></testsuites>',
        encoding="utf-8",
    )
    return path


def write_ledger(
    root: Path, rows: list[dict[str, object]], *, extra_lines: list[str] | None = None
) -> Path:
    """Write this month's feature-health ledger, plus raw ``extra_lines`` verbatim."""
    path = root / "feature-health" / f"ledger-{datetime.now(UTC).strftime('%Y%m')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps({"schema": "feature-health.v1", **row}) for row in rows]
    body += extra_lines or []
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def append_ledger(root: Path, moment: datetime, row: dict[str, object]) -> Path:
    """Append one record to the ledger shard that OWNS ``moment``'s month."""
    path = root / "feature-health" / f"ledger-{moment:%Y%m}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": "feature-health.v1", "ts": moment.isoformat(), **row}))
        handle.write("\n")
    return path


def fh_row(feature: str, tier: str, **over: object) -> dict[str, object]:
    """A completed feature-health record. ``report_path`` sets the stream."""
    return {"feature": feature, "tier": tier, "env": "isolated", "passed": 10, "failed": 0,
            "errors": 0, "status": "ok", "failures": [], **over}


def fh_abort(feature: str, tier: str, **over: object) -> dict[str, object]:
    """A did-not-run record: the load guard refused before the suite started."""
    return {"feature": feature, "tier": tier, "env": "isolated", "status": "error",
            "aborted": True, "did_not_run": True, "abort_reason": "load-guard",
            "passed": 0, "failed": 0, "errors": 0, "failures": [], **over}


def fh_did_not_run(feature: str, tier: str, **over: object) -> dict[str, object]:
    """A zero-observation record: the run STARTED and observed no testcases.

    Exactly what scripts/feature_health/fh.py:660 stamps (status error +
    did_not_run, no ``aborted`` flag) when collection fails or the selection
    deselects everything. An instrument error, not a load-guard abort.
    """
    return {"feature": feature, "tier": tier, "env": "isolated", "status": "error",
            "did_not_run": True, "passed": 0, "failed": 0, "errors": 0,
            "failures": [], **over}


def add_eval_row(
    root: Path, *, run_id: str, created_at: str, check_id: str, verdict: str = "pass"
) -> None:
    """One extra eval_results row, so a single run can hold mixed-offset stamps."""
    counts = {k: 0.0 for k in ("pass", "fail", "not_evaluable", "void")}
    counts[verdict] = 1.0
    path = root / "northstar-cert" / "results.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO eval_results (id, experiment_id, arm, suite_id, suite_version, split,"
        " metrics_json, per_case_json, created_at)"
        " VALUES (?, ?, 'champion', 'evs_northstar_cert_v2', 2, 'dev', ?, ?, ?)",
        (f"evr_{run_id}_{check_id}", run_id, json.dumps(counts),
         json.dumps({check_id: dict(counts)}), created_at),
    )
    conn.commit()
    conn.close()


def write_receipt(root: Path, name: str, payload: object, *, age_days: float = 0.0) -> Path:
    path = root / "gate-evidence" / "records" / "merge-gate" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    if age_days:
        stamp = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
    return path


def receipt(
    passed: int, failed: int, *, days_ago: int = 0, started_at: str | None = None
) -> dict[str, object]:
    if started_at is not None:
        return {
            "schema": "omniagentos.gate-evidence.v3",
            "checks_collected": passed + failed,
            "checks_passed": passed,
            "checks_failed": failed,
            "started_at": started_at,
        }
    return {
        "schema": "omniagentos.gate-evidence.v3",
        "candidate_sha": "0" * 40,
        "checks_collected": passed + failed,
        "checks_passed": passed,
        "checks_failed": failed,
        "checks_skipped": 0,
        "exit_code": 0 if not failed else 1,
        "started_at": f"{day_ago(days_ago)}T18:09:11Z",
        "finished_at": f"{day_ago(days_ago)}T18:09:14Z",
    }
