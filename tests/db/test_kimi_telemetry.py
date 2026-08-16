from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos import contracts
from omniagentos.db.migrate import _migration_files
from omniagentos.db.store import SqliteStore
from scripts.spend import record_provider_call as recorder
from tests.support.db_template import make_store


def test_kimi_telemetry_latest_schema_dal_roundtrip(tmp_path: Path) -> None:
    """Migration 095 telemetry must be writable through the real run DAL."""

    db_path = tmp_path / "kimi-telemetry.db"
    store = make_store(SqliteStore, str(db_path))
    now = contracts.utc_now_iso()
    task_id = contracts.new_id("tsk")
    run_id = contracts.new_id("run")
    try:
        store.create_task(
            {
                "id": task_id,
                "title": "Kimi telemetry round trip",
                "state": "ready",
                "created_at": now,
                "updated_at": now,
            }
        )
        store.enqueue_run(
            {
                "id": run_id,
                "task_id": task_id,
                "harness": "cli-kimi",
                "state": "queued",
                "trace_id": "trace-kimi-telemetry",
                "queued_at": now,
                "created_at": now,
                "updated_at": now,
                "cached_tokens_read": 150_000,
                "cached_tokens_written": 2_000,
                "cache_miss_tokens": 5_000,
                "gateway_source": "discord",
                "hourly_turn_count": 5,
            }
        )

        row = store.get_run(run_id)
        assert row is not None
        assert row["cached_tokens_read"] == 150_000
        assert row["cached_tokens_written"] == 2_000
        assert row["cache_miss_tokens"] == 5_000
        assert row["gateway_source"] == "discord"
        assert row["hourly_turn_count"] == 5
        assert round(row["cached_tokens_read"] / 155_000, 5) == 0.96774
    finally:
        store.close()

    migration_095 = next(path for version, path in _migration_files() if version == 95)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 95"
        ).fetchone() == (hashlib.sha256(migration_095.read_bytes()).hexdigest(),)
        latest = max(version for version, _ in _migration_files())
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            latest,
        )


# ---------------------------------------------------------------------------
# Kimi CLI spend rows written through the DAL (spend-truth-0809)
#
# The estate Kimi shim used to append its coarse cost row by interpolating a
# 20-column INSERT into a shell string and piping it to /usr/bin/sqlite3. These
# tests pin the row shape that replaces it: written by
# ``SqliteStore.record_provider_call`` via ``scripts/spend/record_provider_call``,
# with parameter binding, explicit attribution marking, and no invented money.
# ---------------------------------------------------------------------------

#: Exactly the fields the shim can honestly state about an interactive Kimi run.
#: Note what is ABSENT: request_id / execution_id. The CLI has no such identity,
#: and inventing one was the defect.
SHIM_PAYLOAD: dict[str, Any] = {
    "stage": "worker",
    "attempt_index": 0,
    "provider": "moonshot",
    "transport": "cli",
    "requested_model": "kimi-cli-unobserved",
    "effective_model": "kimi-cli-unobserved",
    "model_lineage": "kimi",
    "billing_provider": "moonshot",
    "adapter_key": "estate-kimi-shim",
    "request_state": "indeterminate",
    "provider_outcome": "exit-code-0",
    "cost_quality": "estimated",
    "cost_upper_bound_usd_nanos": 5_000_000_000,
    "cost_source": "estate-kimi-shim:coarse-flat-estimate-v1:$5.00-unmeasured",
}

SHIM_REASON = "kimi-cli-invocation-has-no-request-or-execution-context"


@pytest.fixture
def ledger_path(tmp_path: Path) -> str:
    """A migrated SCRATCH ledger. Never the live var/runtime DB."""

    path = tmp_path / "spend-truth-ledger.sqlite3"
    make_store(SqliteStore, str(path)).close()
    return str(path)


def _rows(ledger: str) -> list[sqlite3.Row]:
    with sqlite3.connect(ledger) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM provider_call_usage").fetchall()


def test_shim_shaped_call_lands_through_the_dal(ledger_path: str) -> None:
    """The coarse Kimi row is a normal DAL row, not a bespoke hand-built one."""

    result = recorder.record(
        dict(SHIM_PAYLOAD),
        db_path=ledger_path,
        unattributed_reason=SHIM_REASON,
    )
    assert result["ok"] is True

    rows = _rows(ledger_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["adapter_key"] == "estate-kimi-shim"
    assert row["billing_provider"] == "moonshot"
    assert row["stage"] == "worker"
    assert row["request_state"] == "indeterminate"
    assert row["provider_outcome"] == "exit-code-0"
    # No tokens means no registry-derived price. The write seam removes the
    # shim's legacy flat $5 fiction and makes the lookup failure visible.
    assert row["cost_quality"] == "unknown"
    assert row["cost_upper_bound_usd_nanos"] is None
    assert row["cost_source"] == "modelintel:unpriced:moonshot/kimi-cli-unobserved"
    assert row["cost_usd_decimal"] is None
    assert row["cost_usd_nanos"] is None
    assert row["settled_at"] is None


def test_quoted_values_round_trip_instead_of_corrupting_the_statement(
    ledger_path: str,
) -> None:
    """The shell-string INSERT broke on a quote; parameter binding does not."""

    payload = dict(SHIM_PAYLOAD)
    payload["provider_outcome"] = "exit-code-1 O'Brien's ');DROP TABLE provider_call_usage;--"
    payload["cost_source"] = "estate-kimi-shim:it's-an-estimate:$5.00"
    payload["provider"] = "moon'shot"
    payload["billing_provider"] = "moon'shot"

    recorder.record(payload, db_path=ledger_path, unattributed_reason=SHIM_REASON)

    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["provider_outcome"] == payload["provider_outcome"]
    assert rows[0]["cost_source"] == "modelintel:unpriced:moon'shot/kimi-cli-unobserved"
    assert rows[0]["provider"] == "moon'shot"
    # The injected DROP was stored as text, so the table is still here.
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_call_usage'"
        ).fetchone() == (1,)


def test_unattributable_row_is_marked_not_placeholder_filled(ledger_path: str) -> None:
    """Missing identities are named as missing, and stay quarantinable."""

    result = recorder.record(
        dict(SHIM_PAYLOAD),
        db_path=ledger_path,
        unattributed_reason=SHIM_REASON,
    )
    assert result["attributed"] is False
    assert result["unattributed_fields"] == ["request_id", "execution_id"]

    row = _rows(ledger_path)[0]
    call_id, request_id, execution_id = (
        row["call_id"],
        row["request_id"],
        row["execution_id"],
    )
    # The old shape: all three the same synthetic value. Never again.
    assert len({call_id, request_id, execution_id}) == 3
    assert recorder.is_unattributed(request_id)
    assert recorder.is_unattributed(execution_id)
    assert not recorder.is_unattributed(call_id)
    # The reason travels with the row.
    assert recorder.reason_slug(SHIM_REASON) in request_id
    assert recorder.reason_slug(SHIM_REASON) in execution_id

    # One quarantine query finds it -- the property the placeholder fill destroyed.
    with sqlite3.connect(ledger_path) as connection:
        quarantined = connection.execute(
            "SELECT COUNT(*) FROM provider_call_usage "
            "WHERE request_id LIKE ? OR execution_id LIKE ?",
            (f"{recorder.UNATTRIBUTED_PREFIX}%", f"{recorder.UNATTRIBUTED_PREFIX}%"),
        ).fetchone()
    assert quarantined == (1,)


def test_attributed_identities_are_kept_verbatim(ledger_path: str) -> None:
    """A caller that HAS identity is not marked and is not rewritten."""

    payload = dict(SHIM_PAYLOAD)
    payload["call_id"] = contracts.new_id("call")
    payload["request_id"] = contracts.new_id("req")
    payload["execution_id"] = contracts.new_id("exe")
    payload["run_id"] = contracts.new_id("run")

    result = recorder.record(payload, db_path=ledger_path)
    assert result["attributed"] is True
    assert result["unattributed_fields"] == []

    row = _rows(ledger_path)[0]
    assert row["request_id"] == payload["request_id"]
    assert row["execution_id"] == payload["execution_id"]
    assert row["run_id"] == payload["run_id"]


# ---------------------------------------------------------------------------
# Column POPULATION RATES over a batch (the binding falsifier)
#
# "The column is non-NULL" is the wrong instrument: it passes on a writer that
# fills a column for one row in ten, and it passes on a placeholder that is
# populated and meaningless. These tests measure the RATE per column across a
# batch and pin it exactly -- 1.0 for what the caller must always state, 0.0 for
# what it genuinely cannot observe (an invented value there is counterfeit
# money), and the real fraction for a mixed batch.
# ---------------------------------------------------------------------------

#: Populated on EVERY row written through this path, or the row is not honest.
ALWAYS_POPULATED = (
    "call_id",
    "request_id",
    "execution_id",
    "stage",
    "attempt_index",
    "provider",
    "transport",
    "requested_model",
    "effective_model",
    "model_lineage",
    "billing_provider",
    "adapter_key",
    "request_state",
    "provider_outcome",
    "cost_quality",
    "cost_source",
    "created_at",
)

#: An interactive Kimi CLI invocation cannot observe any of these. NULL is the
#: honest value; a filled one would be invented. ``cost_usd_decimal`` /
#: ``cost_usd_nanos`` are in here on purpose: a coarse estimate that acquires an
#: exact-cost column has become a counterfeit measured charge.
NEVER_INVENTED = (
    "run_id",
    "campaign_id",
    "reservation_id",
    "task_id",
    "attempt_id",
    "session_id",
    "work_id",
    "root_trace_id",
    "provider_request_id",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd_decimal",
    "cost_usd_nanos",
    "cost_upper_bound_usd_nanos",
    "pricing_revision",
    "settled_at",
)


def _populated(value: Any) -> bool:
    """A column counts as populated when it is neither NULL nor blank text."""

    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def column_population(rows: list[sqlite3.Row], columns: tuple[str, ...]) -> dict[str, float]:
    """Share of ``rows`` in which each column is populated. Empty batch = no rate."""

    assert rows, "a population rate over zero rows is not a measurement"
    return {
        column: sum(1 for row in rows if _populated(row[column])) / len(rows)
        for column in columns
    }


def _write_batch(ledger: str, count: int, *, attributed: int = 0) -> None:
    """``count`` shim-shaped rows, the first ``attributed`` of them with identity."""

    for index in range(count):
        payload = dict(SHIM_PAYLOAD)
        payload["call_id"] = f"estate-kimi-shim-batch-{index:03d}"
        payload["provider_outcome"] = f"exit-code-{index % 3}"
        if index < attributed:
            payload["request_id"] = contracts.new_id("req")
            payload["execution_id"] = contracts.new_id("exe")
            payload["run_id"] = contracts.new_id("run")
        recorder.record(payload, db_path=ledger, unattributed_reason=SHIM_REASON)


def test_required_columns_are_populated_on_every_row(ledger_path: str) -> None:
    """Rate 1.0, not "the median row has it"."""

    _write_batch(ledger_path, 25)
    rows = _rows(ledger_path)
    assert len(rows) == 25
    rates = column_population(rows, ALWAYS_POPULATED)
    assert rates == dict.fromkeys(ALWAYS_POPULATED, 1.0), (
        "columns below 100% population: "
        f"{ {column: rate for column, rate in rates.items() if rate < 1.0} }"
    )


def test_unobservable_columns_are_never_invented(ledger_path: str) -> None:
    """Rate 0.0. A filled value here is money this path cannot have measured."""

    _write_batch(ledger_path, 25)
    rates = column_population(_rows(ledger_path), NEVER_INVENTED)
    assert rates == dict.fromkeys(NEVER_INVENTED, 0.0), (
        "columns invented by a path that cannot observe them: "
        f"{ {column: rate for column, rate in rates.items() if rate > 0.0} }"
    )


def test_population_is_not_attribution(ledger_path: str) -> None:
    """100% populated identities can still be 100% UNATTRIBUTED.

    This is the whole reason a non-NULL count is the wrong instrument: the old
    shim's rows scored 100% on ``request_id``/``execution_id`` while carrying no
    attribution at all. Both readings must be available and they must disagree.
    """

    _write_batch(ledger_path, 20)
    rows = _rows(ledger_path)

    populated = column_population(rows, ("request_id", "execution_id"))
    assert populated == {"request_id": 1.0, "execution_id": 1.0}

    attributed = sum(
        1
        for row in rows
        if not recorder.is_unattributed(row["request_id"])
        and not recorder.is_unattributed(row["execution_id"])
    ) / len(rows)
    assert attributed == 0.0, "a marker id must not be counted as attribution"

    # And the old shape -- one synthetic value in all three keys -- is absent.
    collapsed = sum(
        1 for row in rows if len({row["call_id"], row["request_id"], row["execution_id"]}) < 3
    )
    assert collapsed == 0


def test_mixed_batch_reports_the_real_fraction(ledger_path: str) -> None:
    """The instrument has teeth: a partly-populated column reports its fraction.

    Without this, ``test_required_columns_are_populated_on_every_row`` proves
    nothing -- a measurement that can only ever return 1.0 is not a measurement.
    """

    _write_batch(ledger_path, 20, attributed=6)
    rows = _rows(ledger_path)
    assert len(rows) == 20

    assert column_population(rows, ("run_id",)) == {"run_id": 0.3}
    attributed = sum(1 for row in rows if not recorder.is_unattributed(row["request_id"]))
    assert attributed / len(rows) == 0.3
    # The always-populated set is unaffected by the mix.
    assert column_population(rows, ALWAYS_POPULATED) == dict.fromkeys(ALWAYS_POPULATED, 1.0)


def test_replaying_one_call_id_stays_one_row(ledger_path: str) -> None:
    """Marker ids are derived from the call id, so a replay is still a replay.

    A random marker per attempt would make the DAL's idempotent replay path
    raise a payload conflict and turn a retried shim into a double charge.
    """

    payload = dict(SHIM_PAYLOAD)
    payload["call_id"] = "estate-kimi-shim-11111111-2222-3333-4444-555555555555"
    payload["created_at"] = "2026-08-09T12:00:00Z"

    first = recorder.record(dict(payload), db_path=ledger_path, unattributed_reason=SHIM_REASON)
    second = recorder.record(dict(payload), db_path=ledger_path, unattributed_reason=SHIM_REASON)

    assert first["request_id"] == second["request_id"]
    assert first["execution_id"] == second["execution_id"]
    assert len(_rows(ledger_path)) == 1
