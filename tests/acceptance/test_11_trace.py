"""AT4 area 11 — Trace & logging.

Acceptance claims under test:

  11.1  Every event is logged and delivered — no event is dropped between the
        store and a live subscriber, and a lagging subscriber is told so rather
        than silently losing frames.
  11.2  Every artifact is linked — a run's artifacts and output digest survive
        the append-only ledger round-trip and stay attached to their run_id.
  11.3  Every decision is traceable — decisions and their outcomes form an
        append-only, linkable chain; a judged verdict carries base/head/tree/
        diff/judge-config hashes (migration 083 ``improve_verdicts``).
  11.4  Experiments are reproducible — a recorded run carries enough to re-run
        it: the environment hash, the surface content hashes, the experiment
        snapshot hash, and the blind presentation seed.

Hermetic: ``tmp_path`` for every file, ``:memory:`` / migrated tmp SQLite for
every DB, hand-built in-process event stores. No network, no model call.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.api.eventbus import FRAME_EVENT, EventHub
from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
)
from omniagentos.lab.eval.blind import build_blind_pairs
from omniagentos.learning.api import attach_outcome, log_decision, promote_champion

# ---------------------------------------------------------------------------
# 11.1 — every event is logged and delivered
# ---------------------------------------------------------------------------


class _RecordingEventStore:
    """A minimal but REAL ``EventSource``: an in-process, id-ordered event log.

    Not a mock of the hub -- the hub under test polls this exactly as it polls
    ``SqliteStore``. It records how it was queried so the test can prove the
    hub advanced its cursor instead of re-reading from zero.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.after_ids: list[int] = []

    def insert_event(self, type_: str, payload: dict[str, Any]) -> int:
        event_id = len(self.events) + 1
        self.events.append({"id": event_id, "type": type_, "payload_json": json.dumps(payload)})
        return event_id

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        self.after_ids.append(after_id)
        return [event for event in self.events if event["id"] > after_id][:limit]

    def latest_event_id(self) -> int:
        return len(self.events)

    def get_heartbeats(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.acceptance_smoke
def test_every_event_reaches_a_live_subscriber_in_order() -> None:
    """No gaps, no reordering, no duplicates across the tail cursor.

    The events are inserted AFTER the subscription so they must arrive through
    the tailer, not through the priming snapshot -- otherwise a hub that never
    polled at all would pass.
    """
    store = _RecordingEventStore()
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        max_tailer_restarts=0,
    )

    async def _collect(subscription: Any, received: list[int], want: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(received) < want:
            frames, lagged = await subscription.drain(0.05)
            assert not lagged, "a dozen events must not overflow a 2048-slot queue"
            received.extend(int(payload["id"]) for kind, payload in frames if kind == FRAME_EVENT)

    async def _run() -> list[int]:
        subscription = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            received: list[int] = []
            # Two waves, so the tailer must ADVANCE its cursor between polls
            # rather than serving everything from one catch-all query.
            for index in range(6):
                store.insert_event("run.updated", {"seq": index})
            await _collect(subscription, received, 6)
            for index in range(6, 12):
                store.insert_event("run.updated", {"seq": index})
            await _collect(subscription, received, 12)
            return received
        finally:
            subscription.close()
            hub.stop(timeout=2.0)

    received = asyncio.run(_run())

    assert received == list(range(1, 13)), (
        f"every inserted event must be delivered exactly once, in id order; got {received}"
    )
    # The cursor advanced past the first wave: the hub never re-queried from 0.
    assert max(store.after_ids) >= 6, (
        "the tailer must poll from its advanced cursor, not replay from zero"
    )


@pytest.mark.acceptance_daily
def test_the_hub_reports_lag_instead_of_silently_dropping_events() -> None:
    """A subscriber that cannot keep up is TOLD, so a gap is never invisible.

    Loss is acceptable under backpressure; unreported loss is not. The frames
    are pushed straight at the subscription so the queue overflows
    deterministically without depending on tailer timing.
    """
    store = _RecordingEventStore()
    hub = EventHub(store, sessions_reader_factory=lambda: None, poll_interval_s=0.01)

    async def _run() -> tuple[list[Any], bool]:
        subscription = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            # Deliberately exceed SUBSCRIBER_QUEUE_MAXSIZE (2048).
            for index in range(2100):
                subscription._publish([(FRAME_EVENT, {"id": index})])
            await asyncio.sleep(0.05)
            return await subscription.drain(0.05)
        finally:
            subscription.close()
            hub.stop(timeout=2.0)

    frames, lagged = asyncio.run(_run())

    assert lagged is True, "an overflowed subscriber must be flagged as lagged"
    # A lagged batch withholds event frames rather than handing back a
    # silently incomplete sequence the client would treat as authoritative.
    assert not [kind for kind, _payload in frames if kind == FRAME_EVENT]


def test_the_ring_buffer_replays_missed_events_after_a_gap() -> None:
    """A reconnecting client can recover the events it missed."""
    store = _RecordingEventStore()
    hub = EventHub(
        store,
        sessions_reader_factory=lambda: None,
        poll_interval_s=0.01,
        max_tailer_restarts=0,
    )

    async def _run() -> tuple[list[dict[str, Any]], bool]:
        subscription = hub.subscribe(wants_heartbeats=False, wants_sessions=False)
        try:
            for index in range(5):
                store.insert_event("run.updated", {"seq": index})
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and len(hub.ring_replay(0)[0]) < 5:
                await subscription.drain(0.05)
            return hub.ring_replay(2)
        finally:
            subscription.close()
            hub.stop(timeout=2.0)

    replayed, can_replay = asyncio.run(_run())

    assert can_replay is True, "the ring still holds these events, so replay must be possible"
    assert [int(row["id"]) for row in replayed] == [3, 4, 5], (
        "replay from id 2 must return exactly the events after it"
    )


# ---------------------------------------------------------------------------
# 11.2 — every artifact is linked
# ---------------------------------------------------------------------------


def _manifest(
    run_id: str,
    *,
    artifacts: list[str] | None = None,
    state: RunState = RunState.COMPLETED,
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id="task-trace",
        discipline="at4",
        harness=HarnessProfile(
            harness=HarnessType.FUSION,
            version="2026.07",
            env_hash="sha256:env-abcdef",
            params={"temperature": 0.0},
        ),
        agent="worker",
        model="mock-model",
        state=state,
        started_at="2026-07-27T09:00:00Z",
        finished_at="2026-07-27T09:05:00Z",
        usage=AgentUsage(wall_ms=4321, input_tokens=100, output_tokens=200, cost_usd=0.05),
        output_digest="sha256:output-123",
        artifacts=artifacts if artifacts is not None else [],
        vault_note="vault/runs/run-trace.md",
        trace_id="trace-abc",
    )


@pytest.mark.acceptance_smoke
def test_artifacts_stay_linked_to_their_run_through_the_ledger(tmp_path: Path) -> None:
    """Artifacts, output digest, vault note and trace id all round-trip."""
    from omniagentos.ledger import append_manifest, read_manifests

    ledger_dir = str(tmp_path / "ledger")
    artifacts = ["artifacts/report.md", "artifacts/diff.patch"]
    append_manifest(ledger_dir, _manifest("run-artifacts", artifacts=artifacts))
    # A second, artifact-less run proves the lookup is per-run, not global.
    append_manifest(ledger_dir, _manifest("run-bare", artifacts=[]))

    [found] = read_manifests(ledger_dir, run_id="run-artifacts", limit=50)

    assert found.artifacts == artifacts
    assert found.output_digest == "sha256:output-123"
    assert found.vault_note == "vault/runs/run-trace.md"
    assert found.trace_id == "trace-abc"
    [bare] = read_manifests(ledger_dir, run_id="run-bare", limit=50)
    assert bare.artifacts == []


@pytest.mark.acceptance_daily
def test_a_corrupt_ledger_line_never_hides_the_surrounding_trace(tmp_path: Path) -> None:
    """One damaged record must not take the rest of the audit trail with it."""
    from omniagentos.ledger import append_manifest, read_manifests

    ledger_dir = tmp_path / "ledger"
    path = Path(append_manifest(str(ledger_dir), _manifest("run-a")))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    append_manifest(str(ledger_dir), _manifest("run-b"))

    run_ids = {manifest.run_id for manifest in read_manifests(str(ledger_dir), limit=50)}

    assert run_ids == {"run-a", "run-b"}


# ---------------------------------------------------------------------------
# 11.3 — every decision is traceable
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_smoke
def test_decisions_and_outcomes_form_an_append_only_linked_chain(tmp_path: Path) -> None:
    """A decision, its outcome and the promotion it caused are all recoverable.

    ``path=`` is passed on every call on purpose: with ``path=None`` these
    helpers append to a module-global list that no fixture resets, which would
    leak state between tests.
    """
    log_path = tmp_path / "decisions.jsonl"

    log_decision({"decision_id": "dec-1", "chose": "challenger-v2"}, path=log_path)
    log_decision({"decision_id": "dec-2", "chose": "champion-v1"}, path=log_path)
    attach_outcome("dec-1", {"result": "regression", "detail": "latency +40%"}, path=log_path)
    promote_champion("challenger-v3", evidence={"experiment_id": "exp-9"}, path=log_path)

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    # Append-only: written order is preserved, nothing overwritten.
    assert [row["kind"] for row in rows] == [
        "decision",
        "decision",
        "outcome",
        "champion_promoted",
    ]
    # The outcome is linkable back to exactly one decision.
    outcome = next(row for row in rows if row["kind"] == "outcome")
    assert outcome["decision_id"] == "dec-1"
    decision_ids = {row["decision_id"] for row in rows if row["kind"] == "decision"}
    assert outcome["decision_id"] in decision_ids
    # A promotion carries the evidence that justified it.
    promotion = next(row for row in rows if row["kind"] == "champion_promoted")
    assert promotion["evidence"] == {"experiment_id": "exp-9"}


def test_appending_a_decision_never_rewrites_an_earlier_one(tmp_path: Path) -> None:
    """The log is append-only in behaviour, not just in intent."""
    log_path = tmp_path / "decisions.jsonl"
    log_decision({"decision_id": "dec-1", "chose": "a"}, path=log_path)
    first = log_path.read_text(encoding="utf-8")

    log_decision({"decision_id": "dec-1", "chose": "b"}, path=log_path)

    after = log_path.read_text(encoding="utf-8")
    assert after.startswith(first), "an earlier decision record must remain byte-identical"
    assert len(after.splitlines()) == 2


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


_VERDICT_COLUMNS = (
    "verdict_id, attempt_id, tier, judge_model, stage, vote, "
    "base_sha, head_sha, tree_hash, diff_hash, judge_config_hash, created_at"
)


@pytest.mark.acceptance_daily
def test_a_judged_verdict_records_the_exact_code_state_it_judged(migrated_db: str) -> None:
    """``improve_verdicts`` pins base/head/tree/diff/judge-config hashes.

    Without all five a verdict is unattributable: you could not tell WHICH diff,
    against WHICH base, under WHICH judge configuration produced the vote. Each
    is ``NOT NULL`` in migration 083; this proves the constraint is live.
    """
    connection = _connect(migrated_db)
    try:
        connection.execute(
            f"INSERT INTO improve_verdicts ({_VERDICT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "verd-1",
                "att-1",
                "T1",
                "opus-critic",
                "premium",
                "approve",
                "base-sha",
                "head-sha",
                "tree-hash",
                "diff-hash",
                "judge-config-hash",
                "2026-07-27T09:00:00Z",
            ),
        )
        row = connection.execute(
            "SELECT * FROM improve_verdicts WHERE verdict_id = 'verd-1'"
        ).fetchone()
        assert dict(row)["base_sha"] == "base-sha"
        assert dict(row)["diff_hash"] == "diff-hash"
        assert dict(row)["judge_config_hash"] == "judge-config-hash"

        # Each provenance hash is mandatory: dropping any one must be refused.
        for missing in ("base_sha", "head_sha", "tree_hash", "diff_hash", "judge_config_hash"):
            columns = [name.strip() for name in _VERDICT_COLUMNS.split(",")]
            values: dict[str, Any] = {
                "verdict_id": f"verd-missing-{missing}",
                "attempt_id": "att-1",
                "tier": "T1",
                "judge_model": "opus-critic",
                "stage": "premium",
                "vote": "approve",
                "base_sha": "b",
                "head_sha": "h",
                "tree_hash": "t",
                "diff_hash": "d",
                "judge_config_hash": "j",
                "created_at": "2026-07-27T09:00:00Z",
            }
            values[missing] = None
            with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
                connection.execute(
                    f"INSERT INTO improve_verdicts ({_VERDICT_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(values[name] for name in columns),
                )
    finally:
        connection.close()


@pytest.mark.acceptance_daily
def test_the_merge_saga_state_is_reconcilable_and_idempotent(migrated_db: str) -> None:
    """``improve_saga`` makes an unverified merged HEAD detectable after a crash.

    The saga's whole purpose is that a dispatcher restarting mid-merge can scan
    ``state`` and find the rows that need reconciling, and that a retried call
    cannot double-merge.
    """
    connection = _connect(migrated_db)
    try:
        connection.execute(
            "INSERT INTO improve_saga (attempt_id, state, merged_sha, idempotency_key, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("att-merged", "MERGED_SHA", "deadbeef", "idem-1", "2026-07-27T09:00:00Z"),
        )
        connection.execute(
            "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at)"
            " VALUES (?, ?, ?, ?)",
            ("att-done", "VERIFIED", "idem-2", "2026-07-27T09:00:00Z"),
        )

        unverified = connection.execute(
            "SELECT attempt_id FROM improve_saga WHERE state NOT IN ('VERIFIED', 'REVERTED')"
        ).fetchall()
        assert [row["attempt_id"] for row in unverified] == ["att-merged"]

        # A replayed call with the same idempotency key cannot create a second saga.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at)"
                " VALUES (?, ?, ?, ?)",
                ("att-replay", "MERGE_INTENT", "idem-1", "2026-07-27T09:01:00Z"),
            )
        # And an unknown state is refused rather than silently stranding a merge.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at)"
                " VALUES (?, ?, ?, ?)",
                ("att-bogus", "HALF_MERGED", "idem-3", "2026-07-27T09:02:00Z"),
            )
    finally:
        connection.close()


@pytest.mark.acceptance_daily
def test_healer_decisions_and_outcomes_are_immutable_once_written(migrated_db: str) -> None:
    """A decision trail you can edit is not a trail. Triggers must be live."""
    connection = _connect(migrated_db)
    try:
        connection.execute(
            "INSERT INTO healer_decisions (decision_id, failure_ref, fixer_model, reviewer_model,"
            " verdict, attested_confidence, rationale, files_touched_json, gates_evidence_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "dec-1",
                "failure-1",
                "fixer",
                "reviewer",
                "confirm_upgrade",
                0.8,
                "because",
                "[]",
                "[]",
                "2026-07-27T09:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="healer_decisions is immutable"):
            connection.execute(
                "UPDATE healer_decisions SET verdict = 'reject' WHERE decision_id = 'dec-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="healer_decisions is immutable"):
            connection.execute("DELETE FROM healer_decisions WHERE decision_id = 'dec-1'")
        # The original row is intact.
        row = connection.execute(
            "SELECT verdict FROM healer_decisions WHERE decision_id = 'dec-1'"
        ).fetchone()
        assert row["verdict"] == "confirm_upgrade"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 11.4 — experiments are reproducible
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_smoke
def test_the_blind_presentation_seed_reproduces_the_exact_judging_order() -> None:
    """Recording the seed is only worth anything if it actually replays.

    ``build_blind_pairs`` shuffles the presentation order so a judge cannot
    infer the arm from position. The returned seed must reproduce that order
    exactly, while the unlinkable tokens must NOT be predictable from it.
    """
    case_outputs = {
        f"case-{index}": {"champion": {"text": f"c{index}"}, "challenger": {"text": f"x{index}"}}
        for index in range(6)
    }

    first_pairs, first_map, seed = build_blind_pairs(case_outputs, seed=None)
    replay_pairs, replay_map, replay_seed = build_blind_pairs(case_outputs, seed=seed)

    assert replay_seed == seed
    assert [pair["case_id"] for pair in replay_pairs] == [
        pair["case_id"] for pair in first_pairs
    ], "the recorded seed must reproduce the presentation order"
    # ... but the tokens are a fresh CSPRNG draw, so the seed leaks nothing.
    assert set(replay_map) != set(first_map)
    # And a different seed genuinely produces a different order (so the
    # assertion above is not vacuously true for a stable sort).
    orders = {
        tuple(pair["case_id"] for pair in build_blind_pairs(case_outputs, seed=s)[0])
        for s in range(12)
    }
    assert len(orders) > 1


@pytest.mark.acceptance_daily
def test_recorded_provenance_carries_everything_needed_to_re_run(tmp_path: Path) -> None:
    """A persisted verdict pins seed, replicates, effective n, effect and MDE.

    That is the reproducibility payload: how many times it was run, how the
    outputs were presented, how big the effect was, and how big it had to be.
    """
    from omniagentos.lab.store import LabStore, PanelMember, VerdictProvenance

    store = LabStore(str(tmp_path / "lab.db"))
    record = VerdictProvenance(
        experiment_id="exp-repro",
        panel_composition=(
            PanelMember(identity="opus-critic", lineage="anthropic"),
            PanelMember(identity="codex-critic", lineage="openai"),
        ),
        replicate_count=3,
        effective_n=3,
        agreement=0.9,
        mde=0.05,
        observed_effect=0.25,
        blind_presentation_seed=123456,
    )
    store.record_verdict_provenance(record)

    loaded = store.get_verdict_provenance("exp-repro")

    assert loaded is not None
    assert loaded.blind_presentation_seed == 123456
    assert loaded.replicate_count == 3
    assert loaded.effective_n == 3
    assert loaded.observed_effect == pytest.approx(0.25)
    assert loaded.mde == pytest.approx(0.05)
    assert {member.lineage for member in loaded.panel_composition} == {"anthropic", "openai"}
    # A single-lineage panel is not a reproducible verdict; the store says so.
    assert store.invalidated_verdicts() == []
    store.record_verdict_provenance(
        VerdictProvenance(
            experiment_id="exp-monoculture",
            panel_composition=(PanelMember(identity="opus-critic", lineage="anthropic"),),
            replicate_count=3,
            effective_n=3,
            agreement=0.9,
            mde=0.05,
            observed_effect=0.25,
            blind_presentation_seed=1,
        )
    )
    flagged = store.invalidated_verdicts()
    assert [item.provenance.experiment_id for item in flagged] == ["exp-monoculture"]
    assert "SINGLE_LINEAGE_PANEL" in flagged[0].reasons


@pytest.mark.acceptance_daily
def test_an_experiment_records_the_snapshot_it_ran_against(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """The persisted experiment pins the exact surfaces/suite it was run on.

    ``snapshot_hash`` is what makes a re-run comparable: change any input
    surface and the hash must change, so a later replay cannot silently be a
    different experiment.
    """
    import omniagentos.lab.campaign as campaign

    from .conftest import make_experiment

    store, evaluator, suite_id = offline_lab
    exp_id = make_experiment(
        store, suite_id, challenger_prompt="WINNING", exp_id="exp_snapshot", replicates=2
    )
    campaign.run_experiment(store, evaluator, exp_id, dry_run=True)

    row = store.get_experiment(exp_id)
    assert row is not None
    snapshot = str(row["snapshot_hash"])
    assert snapshot, "a run experiment must record a snapshot hash"

    # Re-running the SAME inputs reproduces the same hash...
    from omniagentos.lab.contracts import Experiment, Surface

    experiment = Experiment.model_validate(
        {**dict(row), "budgets": json.loads(row["budgets_json"])}
        if "budgets_json" in row.keys()
        else dict(row)
    )
    champion = store.get_surface(experiment.champion_surface_id)
    challenger = store.get_surface(experiment.challenger_surface_id)
    suite = store.get_eval_suite(experiment.eval_suite_id)
    assert campaign._snapshot_hash(experiment, champion, challenger, suite) == snapshot

    # ...and changing an input surface changes it.
    mutated = dict(challenger or {})
    mutated["content_hash"] = "sha256:tampered"
    assert campaign._snapshot_hash(experiment, champion, mutated, suite) != snapshot
    # Guard against a hash that ignores its arguments entirely.
    assert Surface.model_validate(challenger).content_hash != "sha256:tampered"


def test_every_run_manifest_pins_its_execution_environment(tmp_path: Path) -> None:
    """``env_hash`` + params + model + usage are what make a run re-runnable."""
    from omniagentos.ledger import append_manifest, read_manifests

    ledger_dir = str(tmp_path / "ledger")
    append_manifest(ledger_dir, _manifest("run-env"))

    [found] = read_manifests(ledger_dir, run_id="run-env", limit=50)

    assert found.harness.env_hash == "sha256:env-abcdef"
    assert found.harness.params == {"temperature": 0.0}
    assert found.harness.version == "2026.07"
    assert found.model == "mock-model"
    assert found.usage is not None
    assert found.usage.wall_ms == 4321
