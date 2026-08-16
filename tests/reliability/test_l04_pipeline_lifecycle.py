"""L04 reliability analysis/pipeline lifecycle regressions.

Findings: C-03, C-04, H-24, H-38, M-46.

Uses fake models/judges/notifiers and temporary repositories only.
Does not edit sandbox implementation (L05) or improvement API routes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.reliability.analyzer import (
    analyze_event,
    parse_risk_level,
    validate_proposal,
)
from omniagentos.reliability.audit import (
    AuditDeps,
    _hydrate_production_deps,
    twice_daily,
    watch,
)
from omniagentos.reliability.governance import DiffEntry, GovernanceConfig
from omniagentos.reliability.pipeline import (
    _APPLY_LEASE_KEY,
    L03_FENCED_LEASE_CONTRACT,
    L09_CONTAINMENT_CONTRACT,
    FenceDisciplineError,
    ImprovementPipeline,
    JudgeOutcome,
    SandboxOutcome,
    _Signals,
    assess_pipeline_activation,
    l03_fenced_state_ready,
    l09_containment_ready,
)
from omniagentos.reliability.store import LeaseConflict, SqliteReliabilityStore
from omniagentos.reliability.taxonomy import (
    ChangeRisk,
    FailureClass,
    ImprovementStatus,
    Severity,
)

# --- Fakes ------------------------------------------------------------------


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def kinds(self) -> list[str]:
        return [c.get("kind") for c in self.calls]


class ExplodingNotifier:
    def __call__(self, **kwargs: Any) -> None:
        raise RuntimeError("notifier backend down")


class FailingNotificationDal:
    """Real notification-service target whose persistence operation fails."""

    def has_unresolved_for_ref(self, ref_type: str, ref_id: str) -> bool:
        return False

    def create(self, row: dict[str, Any]) -> dict[str, Any]:
        raise sqlite3.OperationalError("notification persistence unavailable")


class TakeoverBeforeGuardDal:
    """Land a takeover at the real DAL transaction boundary.

    The proxy runs after the pipeline's public ``assert_lease`` but immediately
    before ``NotificationsDal.create_guarded`` acquires ``BEGIN IMMEDIATE``.
    """

    def __init__(self, target: NotificationsDal, takeover: Any) -> None:
        self.target = target
        self.takeover = takeover
        self.taken = False

    def create_guarded(self, row: dict[str, Any], **kwargs: Any) -> Any:
        if not self.taken:
            self.taken = True
            self.takeover()
        return self.target.create_guarded(row, **kwargs)


@dataclass
class FakeSandbox:
    outcome: SandboxOutcome
    calls: int = 0

    def run(self, improvement: Any, repo_root: str) -> SandboxOutcome:
        self.calls += 1
        return self.outcome


@dataclass
class FakeJudge:
    outcome: JudgeOutcome
    calls: int = 0

    def evaluate(self, improvement: Any, sandbox: Any) -> JudgeOutcome:
        self.calls += 1
        return self.outcome


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: Any) -> None:
        self.t = self.t + timedelta(**kw)


def _sandbox_pass(path: str = "notes/x.txt") -> SandboxOutcome:
    return SandboxOutcome(
        passed=True,
        diff_entries=[DiffEntry(path=path, status="M")],
        declared_paths=[path],
        report={"ok": True},
        suggested_risk=1,
    )


def _judge_approve() -> JudgeOutcome:
    votes = [
        {
            "judge_agent": f"j-{fam}",
            "model_family": fam,
            "verdict": "approve",
            "scores": {},
            "reasoning": "ok",
            "conditions": "",
            "model": fam,
        }
        for fam in ("anthropic", "openai", "xai")
    ]
    return JudgeOutcome(
        panel_attempt_id="pan_test",
        families_seated=3,
        votes=votes,
        complete=True,
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteReliabilityStore:
    db = tmp_path / "l04.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return SqliteReliabilityStore(str(db))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "notes").mkdir()
    (root / "notes" / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)
    return root


def _seed_event(store: SqliteReliabilityStore, **kw: Any) -> str:
    return store.insert_reliability_event(
        failure_class=kw.get("failure_class", FailureClass.RUN_FAILED.value),
        severity=kw.get("severity", Severity.WARNING.value),
        signature=kw.get("signature", "run_failed|executor|l04"),
        occurrence_key=kw.get("occurrence_key", f"run_failed|l04|{utc_stamp()}"),
        source=kw.get("source", "test"),
        evidence_json=kw.get("evidence_json", {"error": "boom"}),
    )


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _proposal() -> dict[str, Any]:
    return {
        "change_type": "files",
        "files": [{"path": "notes/x.txt", "action": "modify", "content": "fixed\n"}],
        "config_edits": [],
        "plan": ["fix the failure"],
        "restart_required": False,
    }


def _adapter(payload: dict[str, Any]):
    def _fn(prompt: str) -> dict[str, Any]:
        return {"content": json.dumps(payload)}

    return _fn


# --- C-04: risk label schema ------------------------------------------------


class TestRiskLevelSchema:
    def test_parses_qualitative_labels(self) -> None:
        assert parse_risk_level("low") == ChangeRisk.L1
        assert parse_risk_level("medium") == ChangeRisk.L2
        assert parse_risk_level("high") == ChangeRisk.L3
        assert parse_risk_level("critical") == ChangeRisk.L4
        assert parse_risk_level("L2") == ChangeRisk.L2
        assert parse_risk_level(3) == ChangeRisk.L3

    def test_rejects_malformed(self) -> None:
        with pytest.raises(ValueError):
            parse_risk_level("banana")
        with pytest.raises(ValueError):
            parse_risk_level(True)

    def test_analyze_accepts_string_risk_labels(self, store: SqliteReliabilityStore) -> None:
        eid = _seed_event(store)
        payload = {
            "root_cause": "timeout",
            "title": "retry backoff",
            "summary": "increase backoff",
            "kind": "fix",
            "risk_level": "medium",
            "proposal": _proposal(),
        }
        imp_id = analyze_event(store, eid, adapter_fn=_adapter(payload))
        imp = store.get_improvement(imp_id)
        assert imp is not None
        # Floor is L2 for normal fixes; medium maps to L2 — no crash.
        assert imp.risk_level >= ChangeRisk.L2
        # risk_suggestion is the parsed model label; risk_level is max(suggestion, floor).
        assert imp.proposal_json.get("risk_suggestion") == ChangeRisk.L2
        assert imp.proposal_json.get("risk_suggestion_raw") == "medium"
        assert isinstance(imp.proposal_json.get("risk_suggestion"), int)

    def test_analyze_malformed_risk_falls_back_visibly(self, store: SqliteReliabilityStore) -> None:
        eid = _seed_event(store)
        payload = {
            "root_cause": "x",
            "title": "y",
            "summary": "z",
            "kind": "fix",
            "risk_level": "not-a-level",
            "proposal": _proposal(),
        }
        imp_id = analyze_event(store, eid, adapter_fn=_adapter(payload))
        imp = store.get_improvement(imp_id)
        assert imp is not None
        assert "risk_parse_error" in imp.proposal_json
        assert imp.risk_level >= ChangeRisk.L2  # deterministic floor still applied


# --- H-38: architecture context ---------------------------------------------


class TestArchitectureContext:
    def test_arch_context_supplied_and_visible(self, store: SqliteReliabilityStore) -> None:
        eid = _seed_event(store, failure_class="session_error")
        seen: dict[str, str] = {}

        def adapter(prompt: str) -> dict[str, Any]:
            seen["prompt"] = prompt
            return {
                "content": json.dumps(
                    {
                        "root_cause": "session",
                        "title": "t",
                        "summary": "s",
                        "kind": "fix",
                        "risk_level": "low",
                        "proposal": _proposal(),
                    }
                )
            }

        imp_id = analyze_event(
            store,
            eid,
            adapter_fn=adapter,
            arch_context_fn=lambda: "## Reliability\nSelf-improving failure detection.",
        )
        imp = store.get_improvement(imp_id)
        assert "Self-improving failure detection" in seen["prompt"]
        assert imp.proposal_json["arch_context_status"]["status"] == "ok"

    def test_missing_arch_context_is_visible(self, store: SqliteReliabilityStore) -> None:
        eid = _seed_event(store)

        def adapter(prompt: str) -> dict[str, Any]:
            return {
                "content": json.dumps(
                    {
                        "root_cause": "x",
                        "title": "t",
                        "summary": "s",
                        "kind": "fix",
                        "risk_level": 1,
                        "proposal": _proposal(),
                    }
                )
            }

        imp_id = analyze_event(
            store,
            eid,
            adapter_fn=adapter,
            arch_context_fn=lambda: "",
        )
        imp = store.get_improvement(imp_id)
        status = imp.proposal_json["arch_context_status"]
        assert status["status"] == "empty"
        assert imp.proposal_json.get("arch_context_degraded") is True

    def test_malformed_arch_context_fn_is_visible(self, store: SqliteReliabilityStore) -> None:
        eid = _seed_event(store)

        def boom() -> str:
            raise TypeError("load_arch_context() missing 1 required positional argument")

        imp_id = analyze_event(
            store,
            eid,
            adapter_fn=_adapter(
                {
                    "root_cause": "x",
                    "title": "t",
                    "summary": "s",
                    "kind": "fix",
                    "risk_level": 1,
                    "proposal": _proposal(),
                }
            ),
            arch_context_fn=boom,
        )
        imp = store.get_improvement(imp_id)
        assert imp.proposal_json["arch_context_status"]["status"] == "error"
        assert "missing" in (imp.proposal_json["arch_context_status"].get("error") or "")


# --- C-03: activation gates + collaborators ---------------------------------


class TestActivationGates:
    @pytest.mark.skip(reason="L03/L09 are integrated and open by default in this worktree")
    def test_dependency_gates_block_by_default(self) -> None:
        status = assess_pipeline_activation(
            sandbox_runner=object(),
            judge_panel=object(),
            force_activation=False,
        )
        assert status.active is False
        assert status.degraded is True
        reasons = " ".join(status.reasons)
        assert "l09_" in reasons or "l03_" in reasons

    def test_absent_collaborators_block_even_when_forced(self) -> None:
        status = assess_pipeline_activation(force_activation=True)
        assert status.active is False
        assert "sandbox_runner_absent" in status.reasons
        assert "judge_panel_absent" in status.reasons

    def test_pipeline_returns_degraded_without_collaborators(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        pipe = ImprovementPipeline(store, repo_root=repo, force_activation=True)
        imp_id = store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        result = pipe.sandbox_and_judge(imp_id)
        assert result.status == "degraded"
        assert store.get_improvement(imp_id).status == ImprovementStatus.PROPOSED.value
        assert "sandbox_runner_absent" in result.reason

    def test_fully_injected_sandbox_judge_path(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        sandbox = FakeSandbox(_sandbox_pass())
        judges = FakeJudge(_judge_approve())
        notifier = RecordingNotifier()
        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            sandbox_runner=sandbox,
            judge_panel=judges,
            notifier=notifier,
            force_activation=True,
            governance_config=GovernanceConfig(),
        )
        assert pipe.activation_status().active is True
        imp_id = store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        result = pipe.sandbox_and_judge(imp_id)
        assert sandbox.calls == 1
        assert judges.calls == 1
        assert result.status in {
            ImprovementStatus.AWAITING_HUMAN.value,
            ImprovementStatus.MONITORING.value,
            ImprovementStatus.APPLIED.value,
            ImprovementStatus.APPROVED.value,
        }
        assert result.status != "degraded"
        assert store.get_improvement(imp_id).status != ImprovementStatus.PROPOSED.value

    @pytest.mark.skip(reason="L03/L09 are integrated and open by default in this worktree")
    def test_production_hydrate_blocks_without_gates(
        self, store: SqliteReliabilityStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "1")
        deps = _hydrate_production_deps(
            store, AuditDeps(repo_root=str(tmp_path), force_activation=False)
        )
        assert deps.pipeline is not None
        status = deps.pipeline.activation_status()
        assert status.active is False
        # Collaborators must not be attached when L09/L03 are not integrated.
        assert deps.pipeline.sandbox_runner is None or not status.active

    def test_force_activation_with_injected_collaborators_in_audit(
        self, store: SqliteReliabilityStore, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")  # avoid live LLM
        _seed_event(store)  # Seed an event for twice_daily to pick up
        sandbox = FakeSandbox(_sandbox_pass())
        judges = FakeJudge(_judge_approve())
        notifier = RecordingNotifier()

        def analyze_fn(s, event_id, **kw):
            return analyze_event(
                s,
                event_id,
                adapter_fn=_adapter(
                    {
                        "root_cause": "x",
                        "title": "t",
                        "summary": "s",
                        "kind": "fix",
                        "risk_level": "low",
                        "proposal": _proposal(),
                    }
                ),
                arch_context_fn=lambda: "arch ok",
                origin="audit",
            )

        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            sandbox_runner=sandbox,
            judge_panel=judges,
            notifier=notifier,
            force_activation=True,
            governance_config=GovernanceConfig(),
        )
        result = twice_daily(
            store,
            once=True,
            vault_dir=None,
            pipeline=pipe,
            analyze_fn=analyze_fn,
            sandbox_runner=sandbox,
            judge_panel=judges,
            force_activation=True,
            notifier=notifier,
            detect_fn=lambda *a, **k: [],
            recover_fn=lambda s: {"claimed": 0},
        )
        stats = result["stats_json"]
        decide = stats.get("propose_decide") or {}
        assert decide.get("activation", {}).get("active") is True
        assert sandbox.calls >= 1
        assert judges.calls >= 1
        # The seeded event should have produced a proposal that advanced.
        assert decide.get("proposed") or decide.get("decisions")


# --- H-24: comparable rollback windows --------------------------------------


class TestComparableRollbackWindows:
    def test_rate_normalization_detects_regression_on_short_post_window(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        """72 events over 72h (rate 1/h) vs 3 over 1h (rate 3/h) must rollback.

        Absolute counts would under-detect (3 < 72); rates correctly detect.
        """
        clock = Clock(datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC))
        notifier = RecordingNotifier()
        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            sandbox_runner=FakeSandbox(_sandbox_pass()),
            judge_panel=FakeJudge(_judge_approve()),
            notifier=notifier,
            clock=clock,
            force_activation=True,
            governance_config=GovernanceConfig(
                observation_windows_hours={"L1": 24, "L2": 48, "L3": 48, "L4": 72}
            ),
        )
        imp_id = store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        # Manually place into monitoring with a long baseline exposure.
        applied_at = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = store._connection
        conn.execute(
            "UPDATE improvements SET status='monitoring', applied_sha=?, "
            "applied_at=?, risk_level=4, monitor_until=? WHERE id=?",
            (
                "deadbeef",
                applied_at,
                (clock() + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                imp_id,
            ),
        )
        conn.commit()
        pipe._monitor_baseline_put(
            imp_id,
            {"events": 72, "raw": 72, "hours": 72.0, "captured_at": applied_at},
        )
        # Advance 1 hour of post-apply exposure with elevated rate.
        clock.advance(hours=1)
        pipe._measure_window = lambda start, end: _Signals(events=3, raw=3, hours=1.0)

        # Rollback path needs a real applied commit for git revert — stub rollback.
        rolled = {"called": False}

        def _fake_rollback(imp_id, reason, decided_by="auto"):
            rolled["called"] = True
            store.transition_improvement(
                imp_id,
                ImprovementStatus.MONITORING.value,
                ImprovementStatus.ROLLED_BACK.value,
                actor="test",
                detail_json={"reason": reason},
            )
            from omniagentos.reliability.pipeline import RollbackResult

            return RollbackResult(success=True, reason=reason)

        pipe.rollback = _fake_rollback  # type: ignore[method-assign]
        status = pipe.monitor_tick(imp_id)
        assert rolled["called"] is True
        assert status == ImprovementStatus.ROLLED_BACK.value

    def test_equal_rates_do_not_false_positive(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        clock = Clock(datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC))
        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            force_activation=True,
            clock=clock,
            governance_config=GovernanceConfig(),
        )
        imp_id = store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        applied_at = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = store._connection
        conn.execute(
            "UPDATE improvements SET status='monitoring', applied_sha='x', "
            "applied_at=?, risk_level=1, monitor_until=? WHERE id=?",
            (
                applied_at,
                (clock() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                imp_id,
            ),
        )
        conn.commit()
        # Same rate: 24 over 24h vs 1 over 1h.
        pipe._monitor_baseline_put(
            imp_id, {"events": 24, "raw": 24, "hours": 24.0, "captured_at": applied_at}
        )
        clock.advance(hours=1)
        pipe._measure_window = lambda start, end: _Signals(events=1, raw=1, hours=1.0)
        status = pipe.monitor_tick(imp_id)
        assert status == ImprovementStatus.MONITORING.value

    def test_baseline_records_matching_window_hours(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            force_activation=True,
            governance_config=GovernanceConfig(
                observation_windows_hours={"L1": 24, "L2": 48, "L3": 48, "L4": 72}
            ),
        )
        baseline = pipe._measure_baseline(risk_level=ChangeRisk.L1)
        assert baseline["hours"] == 24.0
        baseline4 = pipe._measure_baseline(risk_level=ChangeRisk.L4)
        assert baseline4["hours"] == 72.0


# --- M-46: notifier failure durable + visible -------------------------------


class TestNotifierFailureVisibility:
    def test_pipeline_notify_failure_is_durable(
        self, store: SqliteReliabilityStore, repo: Path
    ) -> None:
        pipe = ImprovementPipeline(
            store,
            repo_root=repo,
            sandbox_runner=FakeSandbox(_sandbox_pass()),
            judge_panel=FakeJudge(_judge_approve()),
            notifier=ExplodingNotifier(),
            force_activation=True,
            governance_config=GovernanceConfig(),
        )
        imp_id = store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        # Sandbox fail path notifies then returns — main decision still recorded.
        pipe.sandbox_runner = FakeSandbox(
            SandboxOutcome(passed=False, report={"err": "tests failed"})
        )
        result = pipe.sandbox_and_judge(imp_id)
        assert result.status == ImprovementStatus.FAILED.value
        assert pipe.notify_failures, "notify failure must be recorded in memory"
        imp = store.get_improvement(imp_id)
        # Operator-visible durable mark on the improvement row.
        assert imp.last_error_json.get("notify_degraded") is True or any(
            "notify_failure" in (r[0] if not isinstance(r, str) else r)
            for r in store._connection.execute(
                "SELECT key FROM reliability_state WHERE key LIKE 'notify_failure:%'"
            ).fetchall()
        )

    def test_audit_notify_failure_marks_degraded_not_silent(
        self, store: SqliteReliabilityStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")
        result = watch(
            store,
            once=True,
            vault_dir=None,
            notifier=ExplodingNotifier(),
            detect_fn=lambda *a, **k: [],
            recover_fn=lambda s: {"claimed": 0},
        )
        stats = result["stats_json"]
        # Cursor-held path may not fire if no errors; seed a critical so notify runs.
        assert "errors" in stats
        # Run daily_summary which always notifies.
        result2 = __import__(
            "omniagentos.reliability.audit", fromlist=["daily_summary"]
        ).daily_summary(
            store,
            once=True,
            vault_dir=None,
            notifier=ExplodingNotifier(),
        )
        stats2 = result2["stats_json"]
        assert stats2.get("degraded") is True
        assert stats2.get("notify_failures", 0) >= 1
        assert any(e.get("stage") == "notify" for e in stats2.get("errors", []))
        # Durable state row written.
        rows = store._connection.execute(
            "SELECT key FROM reliability_state WHERE key LIKE 'notify_failure:%'"
        ).fetchall()
        assert rows, "notify failure must be durable in reliability_state"

    def test_real_default_audit_notifier_failure_is_not_dedupe_or_success(
        self, store: SqliteReliabilityStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.notifications import service

        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")
        monkeypatch.setattr(service, "_dal_for", lambda **_kwargs: FailingNotificationDal())

        result = __import__(
            "omniagentos.reliability.audit", fromlist=["daily_summary"]
        ).daily_summary(store, once=True, vault_dir=None)

        stats = result["stats_json"]
        assert stats.get("degraded") is True
        assert stats.get("notify_failures") == 1
        failure = next(e for e in stats["errors"] if e.get("stage") == "notify")
        assert failure["error"] == "notification persistence unavailable"
        assert failure["durable"] is True
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM reliability_state WHERE key LIKE 'notify_failure:%'"
            ).fetchone()[0]
            == 1
        )


# --- M-01: the real L03 (3c896de) generation-fenced lease -------------------


class L03FencedReliabilityStore(SqliteReliabilityStore):
    """Faithful port of the L03 @ 3c896de generation-fenced lease API.

    3c896de is not an ancestor of this worktree's HEAD, so the shipped contract is
    reproduced here verbatim — same SQL, same ``LeaseConflict`` messages, same
    generation bump — rather than approximated by a mock. A permissive mock is what
    let the previous fence tests pass while asserting nothing.

    ``assert_calls``/``renew_calls`` are recording only; they never alter behaviour.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.assert_calls: list[tuple[str, str, str]] = []
        self.renew_calls: list[tuple[str, str, str]] = []

    def acquire_lease(self, key: str, owner: str, duration_seconds: int = 3600) -> str:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")

        def _acquire(conn: sqlite3.Connection) -> str:
            token = uuid.uuid4().hex
            now_dt = datetime.now(UTC)
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            expires_at = (now_dt + timedelta(seconds=duration_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            if row:
                try:
                    previous = json.loads(row["value_json"])
                    if not isinstance(previous, dict):
                        raise ValueError("lease payload is not an object")
                    lease_exp = self._lease_expiry(previous["expires_at"])
                    previous_generation = int(previous.get("generation", 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise LeaseConflict(f"Lease {key} state is corrupt") from exc
                if previous.get("owner") != owner and lease_exp > now_dt:
                    raise LeaseConflict(f"Lease {key} held by {previous.get('owner')}")
                # A fresh token fences the same owner's prior invocation as well as
                # an expired prior owner.
                lease = {
                    "owner": owner,
                    "token": token,
                    "generation": previous_generation + 1,
                    "expires_at": expires_at,
                }
                conn.execute(
                    "UPDATE reliability_state SET value_json = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(lease), now, f"lease:{key}"),
                )
            else:
                lease = {
                    "owner": owner,
                    "token": token,
                    "generation": 1,
                    "expires_at": expires_at,
                }
                conn.execute(
                    "INSERT INTO reliability_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    (f"lease:{key}", json.dumps(lease), now),
                )
            return token

        return self._execute_with_retry(_acquire)

    @staticmethod
    def _lease_expiry(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("lease expiry must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _validated_lease(row: Any, *, key: str, owner: str, token: str) -> dict[str, Any]:
        if not row:
            raise LeaseConflict(f"Lease {key} not found")
        try:
            lease = json.loads(row["value_json"])
            if not isinstance(lease, dict):
                raise ValueError("lease payload is not an object")
            expires_at = L03FencedReliabilityStore._lease_expiry(lease["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LeaseConflict(f"Lease {key} state is corrupt") from exc
        if lease.get("owner") != owner or lease.get("token") != token:
            raise LeaseConflict(f"Lease {key} token invalid")
        if expires_at <= datetime.now(UTC):
            raise LeaseConflict(f"Lease {key} expired")
        return lease

    def assert_lease(self, key: str, owner: str, token: str) -> None:
        """Fence a mutation by proving this exact token is still current."""
        self.assert_calls.append((key, owner, token))

        def _assert(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            self._validated_lease(row, key=key, owner=owner, token=token)

        self._execute_with_retry(_assert)

    def renew_lease(self, key: str, owner: str, token: str, duration_seconds: int = 3600) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        self.renew_calls.append((key, owner, token))

        def _renew(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            lease = self._validated_lease(row, key=key, owner=owner, token=token)
            now_dt = datetime.now(UTC)
            lease["expires_at"] = (now_dt + timedelta(seconds=duration_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            conn.execute(
                "UPDATE reliability_state SET value_json = ?, updated_at = ? WHERE key = ?",
                (
                    json.dumps(lease),
                    now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    f"lease:{key}",
                ),
            )

        self._execute_with_retry(_renew)

    def release_lease(self, key: str, owner: str, token: str) -> None:
        def _release(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            if not row:
                return
            try:
                lease = json.loads(row["value_json"])
            except (TypeError, ValueError):
                return
            if (
                isinstance(lease, dict)
                and lease.get("owner") == owner
                and lease.get("token") == token
            ):
                conn.execute("DELETE FROM reliability_state WHERE key = ?", (f"lease:{key}",))

        self._execute_with_retry(_release)


@pytest.fixture
def l03_store(tmp_path: Path) -> L03FencedReliabilityStore:
    """A store that speaks the real L03 fenced-lease contract."""
    db = tmp_path / "l03.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return L03FencedReliabilityStore(str(db))


def _lease_row(store: SqliteReliabilityStore, key: str = _APPLY_LEASE_KEY) -> dict[str, Any]:
    row = store._connection.execute(
        "SELECT value_json FROM reliability_state WHERE key = ?", (f"lease:{key}",)
    ).fetchone()
    assert row is not None, f"lease:{key} not present"
    return json.loads(row["value_json"])


def _expire_lease(store: SqliteReliabilityStore, key: str = _APPLY_LEASE_KEY) -> None:
    """Age the persisted lease out so a *different* owner can legitimately take it."""
    lease = _lease_row(store, key)
    lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._lock:
        store._connection.execute(
            "UPDATE reliability_state SET value_json = ? WHERE key = ?",
            (json.dumps(lease), f"lease:{key}"),
        )
        store._connection.commit()


def _rewrite_lease_generation(
    store: SqliteReliabilityStore,
    mode: str,
    key: str = _APPLY_LEASE_KEY,
) -> None:
    """Commit a same-owner/same-token generation defect after section capture."""
    lease = _lease_row(store, key)
    if mode == "changed":
        lease["generation"] = int(lease["generation"]) + 1
    elif mode == "missing":
        lease.pop("generation", None)
    elif mode == "malformed":
        lease["generation"] = "not-an-integer"
    else:  # pragma: no cover - parametrization is closed
        raise AssertionError(f"unknown generation rewrite {mode}")
    with store._lock:
        store._connection.execute(
            "UPDATE reliability_state SET value_json = ? WHERE key = ?",
            (json.dumps(lease), f"lease:{key}"),
        )
        store._connection.commit()


class TestL03StoreContract:
    """Pin the reproduced 3c896de semantics the fence relies on."""

    def test_foreign_owner_blocked_while_live(self, l03_store: L03FencedReliabilityStore) -> None:
        l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        with pytest.raises(LeaseConflict, match="held by a"):
            l03_store.acquire_lease("k", owner="b", duration_seconds=60)

    def test_expired_lease_reclaimable_and_old_token_then_fenced(
        self, l03_store: L03FencedReliabilityStore
    ) -> None:
        tok_a = l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        l03_store.assert_lease("k", "a", tok_a)
        _expire_lease(l03_store, "k")
        tok_b = l03_store.acquire_lease("k", owner="b", duration_seconds=60)
        l03_store.assert_lease("k", "b", tok_b)
        # The displaced owner's token is now rejected — this is the fence.
        with pytest.raises(LeaseConflict, match="token invalid"):
            l03_store.assert_lease("k", "a", tok_a)

    def test_generation_increments_on_every_acquisition(
        self, l03_store: L03FencedReliabilityStore
    ) -> None:
        l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        assert _lease_row(l03_store, "k")["generation"] == 1
        l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        assert _lease_row(l03_store, "k")["generation"] == 2

    def test_reacquire_by_same_owner_fences_the_prior_token(
        self, l03_store: L03FencedReliabilityStore
    ) -> None:
        first = l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        second = l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        assert first != second
        l03_store.assert_lease("k", "a", second)
        with pytest.raises(LeaseConflict, match="token invalid"):
            l03_store.assert_lease("k", "a", first)

    def test_corrupt_lease_state_is_a_conflict_not_a_pass(
        self, l03_store: L03FencedReliabilityStore
    ) -> None:
        tok = l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        with l03_store._lock:
            l03_store._connection.execute(
                "UPDATE reliability_state SET value_json = ? WHERE key = ?",
                ("not-json", "lease:k"),
            )
            l03_store._connection.commit()
        with pytest.raises(LeaseConflict, match="corrupt"):
            l03_store.assert_lease("k", "a", tok)

    def test_release_only_honours_the_matching_token(
        self, l03_store: L03FencedReliabilityStore
    ) -> None:
        tok = l03_store.acquire_lease("k", owner="a", duration_seconds=60)
        l03_store.release_lease("k", "a", "wrong-token")
        l03_store.assert_lease("k", "a", tok)  # still held
        l03_store.release_lease("k", "a", tok)
        with pytest.raises(LeaseConflict, match="not found"):
            l03_store.assert_lease("k", "a", tok)


# --- Integration seams: fail closed until L03/L09 actually land -------------


@pytest.mark.skip(reason="L09 containment is integrated in this worktree")
def test_l09_seam_fails_closed_against_pre_l09_worktree() -> None:
    """The L09 seam names the *real* ce5e75e exports and fails closed without them.

    ce5e75e ships ``assert_writable(paths, *, patterns, repo_root)`` and
    ``assert_canonical_destination(path, *, root, allowed_prefix)``. This worktree
    predates it, so the seam must report the precise missing symbol; a seam that
    checked invented symbol names would fail closed for the wrong reason and would
    keep failing closed after L09 landed.
    """
    contract_names = {name for name, _ in L09_CONTAINMENT_CONTRACT}
    assert contract_names == {"assert_writable", "assert_canonical_destination"}

    ok, reason = l09_containment_ready()
    assert ok is False
    assert reason.startswith("l09_containment_not_integrated:"), reason
    gap_symbol = reason.rsplit(":", 1)[-1].split("(")[0]
    assert gap_symbol in contract_names, reason


def test_l09_contract_is_satisfied_only_by_the_real_signatures() -> None:
    """A permissive ``**kwargs`` shim must NOT open the containment gate."""
    from omniagentos.reliability.pipeline import _contract_gap

    class PermissiveShim:
        @staticmethod
        def assert_writable(*args: Any, **kwargs: Any) -> None: ...

        @staticmethod
        def assert_canonical_destination(*args: Any, **kwargs: Any) -> None: ...

    assert _contract_gap(PermissiveShim, L09_CONTAINMENT_CONTRACT) is not None

    class PartialL09:
        @staticmethod
        def assert_writable(paths: Any, *, patterns: Any = None, repo_root: Any = None) -> None: ...

    assert _contract_gap(PartialL09, L09_CONTAINMENT_CONTRACT) == (
        "missing:assert_canonical_destination"
    )

    class FaithfulL09:
        @staticmethod
        def assert_writable(paths: Any, *, patterns: Any = None, repo_root: Any = None) -> None: ...

        @staticmethod
        def assert_canonical_destination(
            path: Any, *, root: Any, allowed_prefix: str = "vault"
        ) -> None: ...

    assert _contract_gap(FaithfulL09, L09_CONTAINMENT_CONTRACT) is None


@pytest.mark.skip(reason="L03 fenced state is integrated in this worktree")
def test_l03_seam_fails_closed_against_pre_l03_store() -> None:
    """The pre-L03 store has no ``assert_lease``; the seam must say exactly that."""
    assert not hasattr(SqliteReliabilityStore, "assert_lease"), (
        "this test pins pre-L03 behaviour; when L03 lands the seam should return True"
    )
    ok, reason = l03_fenced_state_ready()
    assert ok is False
    assert reason == "l03_fenced_state_not_integrated:missing:assert_lease", reason


@pytest.mark.skip(reason="L03 fenced state is integrated in this worktree")
def test_l03_seam_accepts_the_real_3c896de_contract(
    l03_store: L03FencedReliabilityStore, store: SqliteReliabilityStore
) -> None:
    """The ported 3c896de store satisfies the declared fenced-lease contract."""
    contract_names = {name for name, _ in L03_FENCED_LEASE_CONTRACT}
    assert contract_names == {
        "acquire_lease",
        "assert_lease",
        "renew_lease",
        "release_lease",
    }
    ok, reason = l03_fenced_state_ready(l03_store)
    assert ok is True
    assert reason == "l03_fenced_lease_contract_met_on_store"
    # The pre-L03 store on the same code path is still rejected.
    ok, reason = l03_fenced_state_ready(store)
    assert ok is False
    assert reason == "l03_fenced_state_not_integrated:missing:assert_lease"


def test_validate_proposal_still_works() -> None:
    p = validate_proposal(_proposal())
    assert p["change_type"] == "files"


# --- M-01: exact 1:1 ordered fence -> mutation trace ------------------------


def _mutations(pipe: ImprovementPipeline) -> list[str]:
    """Assert strict fence/mutation alternation and return the mutation order.

    ``fence_trace`` interleaves ``("fence", name)`` and ``("mutation", name)``. A
    group-covering assertion, a missing assertion, or a mutation running under
    another mutation's ticket each break this shape, so the alternation check IS
    the one-to-one proof; the caller then pins the exact ordered names.
    """
    trace = pipe.fence_trace
    assert len(trace) % 2 == 0, f"dangling fence ticket in {trace}"
    names: list[str] = []
    for i in range(0, len(trace), 2):
        fence_kind, fence_name = trace[i]
        mut_kind, mut_name = trace[i + 1]
        assert fence_kind == "fence", f"expected a fence at {i}, got {trace[i]}"
        assert mut_kind == "mutation", (
            f"fence for {fence_name!r} was not immediately consumed; got {trace[i + 1]}"
        )
        assert fence_name == mut_name, f"fence armed for {fence_name!r} but {mut_name!r} ran"
        names.append(mut_name)
    return names


def _fenced_pipeline(
    l03_store: L03FencedReliabilityStore, repo: Path, **kwargs: Any
) -> ImprovementPipeline:
    pipe = ImprovementPipeline(
        l03_store,
        repo_root=repo,
        force_activation=True,
        governance_config=GovernanceConfig(),
        **kwargs,
    )
    assert pipe._has_fenced_api is True, "the real L03 contract must be detected"
    return pipe


def _approved(store: SqliteReliabilityStore) -> str:
    imp_id = store.create_improvement(
        origin="audit", kind="fix", title="t", proposal_json=_proposal()
    )
    store.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.APPROVED.value,
        actor="test",
    )
    return imp_id


class TestFenceToMutationTrace:
    """Every destructive step is preceded by its OWN fresh ``assert_lease`` (M-01)."""

    def test_apply_trace_is_exact_and_one_to_one(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)

        assert pipe.apply(imp_id).applied is True

        assert _mutations(pipe) == [
            "store:transition:applying",
            "store:update:attempt",
            "state:journal_put",  # phase=prepared
            "state:rollback_point_create",
            "store:update:rollback_point_id",
            "state:monitor_baseline_put",
            "fs:write:notes/x.txt",
            "state:journal_put",  # phase=files_written
            "git:add",
            "git:commit",
            "state:journal_put",  # phase=committed
            "store:update:applied_sha",
            "git:tag",
            "store:transition:applied",
            "state:journal_put",  # phase=recorded
            "notify:done",
            "store:update:monitor_until",
            "store:transition:monitoring",
        ]
        # Exactly one assert_lease per mutation — never one covering a group.
        assert len(l03_store.assert_calls) == 18
        assert {(k, o) for k, o, _ in l03_store.assert_calls} == {(_APPLY_LEASE_KEY, pipe.owner)}

    def test_apply_fences_every_step_named_in_the_blocker(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """Journal puts, rollback point, git ops and record_applied steps all fenced."""
        pipe = _fenced_pipeline(l03_store, repo)
        assert pipe.apply(_approved(l03_store)).applied is True
        muts = _mutations(pipe)

        assert muts.count("state:journal_put") == 4, "every journal put is fenced"
        assert muts.count("state:rollback_point_create") == 1
        # record_applied is three separately-fenced steps, not one group.
        rec = muts.index("store:update:applied_sha")
        assert muts[rec : rec + 3] == [
            "store:update:applied_sha",
            "git:tag",
            "store:transition:applied",
        ]
        # git add and git commit are separately fenced, not one "commit" group.
        add = muts.index("git:add")
        assert muts[add : add + 2] == ["git:add", "git:commit"]
        # _enter_monitoring is two separately-fenced steps.
        mon = muts.index("store:update:monitor_until")
        assert muts[mon : mon + 2] == [
            "store:update:monitor_until",
            "store:transition:monitoring",
        ]

    def test_rollback_trace_is_exact_and_one_to_one(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        assert pipe.apply(imp_id).applied is True

        assert pipe.rollback(imp_id, reason="test_regression").success is True

        assert _mutations(pipe) == [
            "store:transition:rolling_back",
            "state:journal_put",
            "git:revert",
            "git:revert_commit",
            "store:update:resolved_at",
            "state:rollback_point_restore",
            "store:transition:rolled_back",
            "notify:escalation",
        ]

    def test_rollback_conflict_fences_abort_reset_and_clean_separately(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """A genuine revert conflict — no stubbing of the code under test."""
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        assert pipe.apply(imp_id).applied is True

        # A later commit rewrites the same line, so reversing the apply conflicts.
        (repo / "notes" / "x.txt").write_text("diverged\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "diverge"], cwd=repo, check=True)
        (repo / "notes" / "junk.txt").write_text("junk\n", encoding="utf-8")

        result = pipe.rollback(imp_id, reason="test_regression")
        assert result.conflict is True
        assert result.reason == "revert_conflict"

        assert _mutations(pipe) == [
            "store:transition:rolling_back",
            "state:journal_put",
            "git:revert",
            "git:revert_abort",
            "git:reset",
            "git:clean",
            "store:transition:failed",
            "notify:escalation",
        ]
        # Clean abort: no half-reverted worktree left behind.
        assert (repo / "notes" / "x.txt").read_text(encoding="utf-8") == "diverged\n"
        assert not (repo / "notes" / "junk.txt").exists()

    def test_reconcile_restore_fences_reset_and_clean_separately(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = l03_store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        l03_store.transition_improvement(
            imp_id,
            ImprovementStatus.PROPOSED.value,
            ImprovementStatus.APPLYING.value,
            actor="test",
        )
        pipe._journal_put(
            imp_id, {"phase": "files_written", "pre_sha": pipe._git_head(), "attempt": 1}
        )
        # A crashed apply leaves debris the restore must clean.
        (repo / "notes" / "junk.txt").write_text("junk\n", encoding="utf-8")
        pipe.fence_trace = []

        assert pipe.reconcile_apply(imp_id).reason == "recovered_retreat"
        assert not (repo / "notes" / "junk.txt").exists(), "restore must clean"

        assert _mutations(pipe) == [
            "git:reset",
            "git:clean",
            "store:transition:approved",
        ]

    def test_reconcile_advance_fences_every_record_and_monitor_step(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        assert pipe.apply(imp_id).applied is True
        # Rewind to the crashed state: the commit landed, the record never did.
        l03_store.update_improvement_fields(imp_id, applied_sha=None)
        l03_store.transition_improvement(
            imp_id,
            ImprovementStatus.MONITORING.value,
            ImprovementStatus.APPLYING.value,
            actor="test",
        )

        assert pipe.reconcile_apply(imp_id).reason == "recovered_advance"

        # No "git:tag" — the tag already exists, so the idempotent write is skipped
        # and therefore arms no ticket. A skipped mutation must not burn a fence.
        assert _mutations(pipe) == [
            "store:update:applied_sha",
            "store:transition:applied",
            "state:journal_put",
            "notify:done",
            "store:update:monitor_until",
            "store:transition:monitoring",
        ]


class TestFenceDisciplineIsStructural:
    """Inserting a mutation without its own fresh assertion must FAIL, loudly."""

    def test_extra_mutation_under_one_ticket_is_rejected(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """Two mutations sharing one assertion — the exact pattern that was rejected."""
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)

        real_update = pipe._store_update_fields

        def _piggyback(imp: str, what: str, **fields: Any) -> None:
            real_update(imp, what, **fields)
            if what == "applied_sha":
                # A second durable write riding the SAME assert_lease.
                pipe.store.update_improvement_fields(imp, stage_started_at=utc_stamp())
                pipe._spend_fence("store:update:piggyback")

        pipe._store_update_fields = _piggyback  # type: ignore[method-assign]

        with pytest.raises(FenceDisciplineError, match="no fresh fence assertion"):
            pipe.apply(imp_id)

    def test_mutation_running_under_another_mutations_ticket_is_rejected(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            with pytest.raises(FenceDisciplineError, match="fence armed for"):
                with pipe._fenced_mutation("git:reset"):
                    pipe._spend_fence("git:clean")

    def test_arming_a_second_ticket_before_consuming_the_first_is_rejected(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            with pytest.raises(FenceDisciplineError, match="still unconsumed"):
                with pipe._fenced_mutation("git:reset"):
                    with pipe._fenced_mutation("git:clean"):
                        pass

    def test_armed_but_unconsumed_ticket_is_rejected(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """A fence with no mutation behind it is a bug, not a spare authorisation."""
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            with pytest.raises(FenceDisciplineError, match="never consumed"):
                with pipe._fenced_mutation("store:update:nothing"):
                    pass

    def test_undeclared_destructive_git_command_is_rejected(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """The structural guard catches a git mutation added without a ticket."""
        pipe = _fenced_pipeline(l03_store, repo)
        for args in (
            ["reset", "--hard", "HEAD"],
            ["clean", "-fd"],
            ["apply", "-"],
            ["tag", "imp/x", "HEAD"],
            ["branch", "-D", "tmp"],
            ["commit", "-m", "x"],
            ["checkout", "-b", "tmp"],
            ["add", "-A"],
            ["revert", "--no-commit", "HEAD"],
        ):
            with pytest.raises(FenceDisciplineError, match="without a fence ticket"):
                pipe._git(args, check=False)

    def test_read_only_git_commands_are_not_blocked(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        pipe = _fenced_pipeline(l03_store, repo)
        assert pipe._git(["tag", "-l", "nope"], check=False).returncode == 0
        assert pipe._git(["branch", "--list"], check=False).returncode == 0
        assert pipe._git(["rev-parse", "HEAD"], check=False).returncode == 0

    def test_renewal_is_not_an_authorisation(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """``renew_lease`` buys runway; it must never stand in for a fence ticket."""
        pipe = _fenced_pipeline(l03_store, repo)
        assert pipe.apply(_approved(l03_store)).applied is True
        assert l03_store.renew_calls, "renewals still happen before lengthy phases"
        assert len(l03_store.assert_calls) == len(_mutations(pipe))

    def test_custom_notifier_fails_closed_in_production_leased_section(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        notifier = RecordingNotifier()
        pipe = ImprovementPipeline(
            l03_store,
            repo_root=repo,
            notifier=notifier,
            governance_config=GovernanceConfig(),
        )
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            with pytest.raises(
                FenceDisciplineError,
                match="cannot prove atomic persistence fencing",
            ):
                pipe._notify(None, "info", "unprovable")
        assert notifier.calls == []
        assert pipe.fence_trace == []

    def test_custom_notifier_test_seam_preserves_compatibility_without_trace(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        notifier = RecordingNotifier()
        pipe = _fenced_pipeline(l03_store, repo, notifier=notifier)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            pipe._notify(None, "info", "test-only")
        assert len(notifier.calls) == 1
        assert pipe.fence_trace == []

    def test_real_default_notifier_and_failure_bookkeeping_are_all_fenced(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
    ) -> None:
        """Real persistence failure plus both degradation writes are distinct mutations."""
        l03_store._connection.execute(
            """
            CREATE TRIGGER reject_l04_notification
            BEFORE INSERT ON notifications
            WHEN NEW.ref_type = 'improvement'
            BEGIN
                SELECT RAISE(ABORT, 'notification persistence unavailable');
            END
            """
        )
        l03_store._connection.commit()
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        assert pipe.apply(imp_id).applied is True
        assert pipe.notify_failures, "notifier failure must stay visible (M-46)"
        failure = pipe.notify_failures[0]
        assert failure["error"] == "notification persistence unavailable"
        assert failure["durable_targets"] == [
            "reliability_state",
            "improvements",
        ]
        assert failure["durability_errors"] == []

        mutations = _mutations(pipe)
        journal_recorded = max(
            i for i, mutation in enumerate(mutations) if mutation == "state:journal_put"
        )
        assert mutations[journal_recorded + 1 : journal_recorded + 3] == [
            "state:notify_failure",
            "store:update:notify_failure",
        ]
        assert "notify:done" not in mutations, (
            "a rejected insert must not be traced as a completed mutation"
        )
        # The failed notification has a real outer L03 assertion but no mutation
        # trace; the two fallback writes each assert and commit independently.
        assert len(l03_store.assert_calls) == 20
        assert len(mutations) == 19
        assert l03_store.get_improvement(imp_id).last_error_json["notify_degraded"] is True
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM reliability_state WHERE key LIKE 'notify_failure:%'"
            ).fetchone()[0]
            == 1
        )

    def test_post_commit_observer_fault_preserves_persisted_trace_and_truth(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A committed insert is traced even if an ancillary observer fails."""
        from omniagentos.notifications import service

        real_record = service.record_notification_result
        observer_calls: list[str] = []

        def _record_with_failing_observer(**kwargs: Any) -> Any:
            def _fail_observer() -> None:
                observer_calls.append("called")
                raise RuntimeError("injected post-commit observer fault")

            kwargs["on_persisted"] = _fail_observer
            return real_record(**kwargs)

        monkeypatch.setattr(
            service,
            "record_notification_result",
            _record_with_failing_observer,
        )
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)

        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            pipe._notify(
                imp_id,
                "done",
                "landed despite observer fault",
                dedupe=False,
            )

        assert observer_calls == ["called"]
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE ref_type = 'improvement' AND ref_id = ?",
                (imp_id,),
            ).fetchone()[0]
            == 1
        )
        assert _mutations(pipe) == ["notify:done"]
        assert pipe.notify_failures == []
        assert l03_store._connection.in_transaction is False

    def test_precommit_result_fetch_fault_rolls_back_and_degrades_truthfully(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fetch fault leaves zero rows, no fictional trace, and durable failure."""
        from omniagentos.notifications import service

        target = NotificationsDal(l03_store._connection)

        def _fail_fetch(_notification_id: str) -> dict[str, Any]:
            raise sqlite3.OperationalError("injected guarded result fetch fault")

        monkeypatch.setattr(target, "_stored", _fail_fetch)
        monkeypatch.setattr(service, "_dal_for", lambda **_kwargs: target)
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)

        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            pipe._notify(
                imp_id,
                "done",
                "must not land",
                dedupe=False,
            )

        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE ref_type = 'improvement' AND ref_id = ?",
                (imp_id,),
            ).fetchone()[0]
            == 0
        )
        assert _mutations(pipe) == [
            "state:notify_failure",
            "store:update:notify_failure",
        ]
        assert pipe.notify_failures[0]["error"] == ("injected guarded result fetch fault")
        assert pipe.notify_failures[0]["durable_targets"] == [
            "reliability_state",
            "improvements",
        ]
        assert l03_store.get_improvement(imp_id).last_error_json["notify_degraded"] is True
        assert l03_store._connection.in_transaction is False

    def test_takeover_immediately_before_create_inserts_nothing_and_propagates(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outer assertion cannot authorize a later stale-owner insert."""
        from omniagentos.notifications import service

        target = NotificationsDal(l03_store._connection)

        def _takeover() -> None:
            _expire_lease(l03_store)
            l03_store.acquire_lease(_APPLY_LEASE_KEY, owner="intruder", duration_seconds=60)

        takeover_dal = TakeoverBeforeGuardDal(target, _takeover)
        monkeypatch.setattr(service, "_dal_for", lambda **_kwargs: takeover_dal)
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = _approved(l03_store)

        with pytest.raises(LeaseConflict, match="token invalid"):
            pipe.apply(imp_id)

        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE ref_type = 'improvement' AND ref_id = ?",
                (imp_id,),
            ).fetchone()[0]
            == 0
        )
        current = l03_store.get_improvement(imp_id)
        assert current.status == ImprovementStatus.APPLIED.value
        assert current.monitor_until is None
        assert pipe.notify_failures == []
        assert "notify:done" not in _mutations(pipe)
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM reliability_state WHERE key LIKE 'notify_failure:%'"
            ).fetchone()[0]
            == 0
        ), "claim loss must not recurse into notification degradation"
        assert takeover_dal.taken is True
        assert _lease_row(l03_store)["owner"] == "intruder"
        assert _lease_row(l03_store)["generation"] == 2
        assert l03_store._connection.in_transaction is False

    def test_takeover_before_dedupe_is_not_misreported_as_deduped(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The atomic guard runs before dedupe and rejects the stale owner."""
        from omniagentos.notifications import service

        imp_id = _approved(l03_store)
        target = NotificationsDal(l03_store._connection)
        target.create(
            {
                "kind": "done",
                "title": "existing",
                "ref_type": "improvement",
                "ref_id": imp_id,
            }
        )

        def _takeover() -> None:
            _expire_lease(l03_store)
            l03_store.acquire_lease(_APPLY_LEASE_KEY, owner="intruder", duration_seconds=60)

        takeover_dal = TakeoverBeforeGuardDal(target, _takeover)
        monkeypatch.setattr(service, "_dal_for", lambda **_kwargs: takeover_dal)
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)

        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            with pytest.raises(LeaseConflict, match="token invalid"):
                pipe._notify(
                    imp_id,
                    "done",
                    "dedupe takeover",
                    dedupe=True,
                )

        assert len(target.list()) == 1
        assert pipe.fence_trace == []
        assert pipe.notify_failures == []
        assert takeover_dal.taken is True
        assert _lease_row(l03_store)["owner"] == "intruder"
        assert _lease_row(l03_store)["generation"] == 2
        assert l03_store._connection.in_transaction is False

    @pytest.mark.parametrize("dedupe", [False, True], ids=["create", "dedupe"])
    @pytest.mark.parametrize("generation_mode", ["changed", "missing", "malformed"])
    def test_guarded_notification_requires_exact_acquired_generation(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        generation_mode: str,
        dedupe: bool,
    ) -> None:
        """Same owner/token cannot authorize another or corrupt generation."""
        pipe = _fenced_pipeline(l03_store, repo)
        ref_id = f"imp_generation_{generation_mode}_{dedupe}"
        target = NotificationsDal(l03_store._connection)
        expected_rows = 0
        if dedupe:
            target.create(
                {
                    "kind": "done",
                    "title": "existing generation notification",
                    "ref_type": "improvement",
                    "ref_id": ref_id,
                }
            )
            expected_rows = 1

        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token) as section:
            assert section.generation == 1
            _rewrite_lease_generation(l03_store, generation_mode)
            expected = (
                "generation invalid" if generation_mode == "changed" else "generation is corrupt"
            )
            with pytest.raises(LeaseConflict, match=expected):
                pipe._notify(
                    ref_id,
                    "done",
                    "must not persist under the wrong generation",
                    dedupe=dedupe,
                )

        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE ref_type = 'improvement' AND ref_id = ?",
                (ref_id,),
            ).fetchone()[0]
            == expected_rows
        )
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications "
                "WHERE title = 'must not persist under the wrong generation'"
            ).fetchone()[0]
            == 0
        )
        assert pipe.fence_trace == []
        assert pipe.notify_failures == []
        assert l03_store._connection.in_transaction is False

    def test_generation_changed_before_section_entry_rejects_acquired_identity(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
    ) -> None:
        """The rejected-SHA counterexample cannot redefine the acquired generation."""
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        _rewrite_lease_generation(l03_store, "changed")

        with pytest.raises(LeaseConflict, match="generation invalid"):
            with pipe._fenced_section(_APPLY_LEASE_KEY, token):
                raise AssertionError("a changed acquired identity must not open")

        assert pipe.fence_trace == []
        assert pipe.notify_failures == []
        assert (
            l03_store._connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        )

    def test_rejected_section_entry_clears_prior_success_trace(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
    ) -> None:
        """A rejected new region cannot inherit an earlier mutation's trace."""
        pipe = _fenced_pipeline(l03_store, repo)
        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        with pipe._fenced_section(_APPLY_LEASE_KEY, token):
            pipe._notify(
                "imp_prior_trace",
                "done",
                "prior success",
                dedupe=False,
            )
        assert _mutations(pipe) == ["notify:done"]
        l03_store.release_lease(_APPLY_LEASE_KEY, pipe.owner, token)

        token = pipe._acquire_fenced_lease(_APPLY_LEASE_KEY, duration_seconds=60)
        _rewrite_lease_generation(l03_store, "changed")
        with pytest.raises(LeaseConflict, match="generation invalid"):
            with pipe._fenced_section(_APPLY_LEASE_KEY, token):
                raise AssertionError("changed generation must not enter")

        assert pipe.fence_trace == []
        assert pipe.notify_failures == []
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE ref_id = 'imp_prior_trace'"
            ).fetchone()[0]
            == 1
        )

    def test_uncaptured_public_token_cannot_open_generation_fenced_section(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
    ) -> None:
        """Callers cannot substitute a post-hoc row read for acquisition identity."""
        pipe = _fenced_pipeline(l03_store, repo)
        token = l03_store.acquire_lease(
            _APPLY_LEASE_KEY,
            owner=pipe.owner,
            duration_seconds=60,
        )

        with pytest.raises(LeaseConflict, match="not captured at acquisition"):
            with pipe._fenced_section(_APPLY_LEASE_KEY, token):
                raise AssertionError("an uncaptured generation must not open")

        assert pipe.fence_trace == []
        assert pipe.notify_failures == []
        assert (
            l03_store._connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        )

    def test_generation_rewrite_during_acquisition_is_not_captured_as_owned(
        self,
        l03_store: L03FencedReliabilityStore,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A post-acquire rewrite cannot redefine which generation was acquired."""
        pipe = _fenced_pipeline(l03_store, repo)
        real_acquire = l03_store.acquire_lease

        def _acquire_then_rewrite(
            key: str,
            owner: str,
            duration_seconds: int = 3600,
        ) -> str:
            token = real_acquire(key, owner, duration_seconds)
            _rewrite_lease_generation(l03_store, "changed", key)
            return token

        monkeypatch.setattr(l03_store, "acquire_lease", _acquire_then_rewrite)

        with pytest.raises(LeaseConflict, match="generation invalid"):
            pipe._acquire_fenced_lease(
                _APPLY_LEASE_KEY,
                duration_seconds=60,
            )

        assert pipe._acquired_lease_identities == {}
        assert (
            l03_store._connection.execute(
                "SELECT COUNT(*) FROM reliability_state WHERE key = ?",
                (f"lease:{_APPLY_LEASE_KEY}",),
            ).fetchone()[0]
            == 0
        ), "a rejected capture releases the owner/token it just acquired"


class TestDisplacedOwnerIsFencedMidApply:
    def test_displacement_between_mutations_aborts_the_apply(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """A displaced owner is stopped at the NEXT mutation, not several later.

        No mock cheat flag: the lease is aged out and a second owner takes it through
        the real ``acquire_lease``, exactly as a stalled-then-resumed process would.
        """
        pipe = _fenced_pipeline(l03_store, repo, notifier=RecordingNotifier())
        imp_id = _approved(l03_store)

        real_apply_mutations = pipe._apply_mutations

        def _displaced(imp: Any) -> list[str]:
            written = real_apply_mutations(imp)
            _expire_lease(l03_store)
            l03_store.acquire_lease(_APPLY_LEASE_KEY, owner="intruder", duration_seconds=60)
            return written

        pipe._apply_mutations = _displaced  # type: ignore[method-assign]

        with pytest.raises(LeaseConflict, match="token invalid"):
            pipe.apply(imp_id)

        # Stopped at the very next fenced mutation — the files_written journal put.
        assert _mutations(pipe) == [
            "store:transition:applying",
            "store:update:attempt",
            "state:journal_put",
            "state:rollback_point_create",
            "store:update:rollback_point_id",
            "state:monitor_baseline_put",
            "fs:write:notes/x.txt",
        ]
        # Nothing was committed or recorded under the stolen lease.
        assert l03_store.get_improvement(imp_id).applied_sha is None
        assert l03_store.get_improvement(imp_id).status == ImprovementStatus.APPLYING.value

    def test_displaced_owner_cannot_reset_the_worktree_during_reconcile(
        self, l03_store: L03FencedReliabilityStore, repo: Path
    ) -> None:
        """Reconcile must never reset/clean without a live fence (M-01)."""
        pipe = _fenced_pipeline(l03_store, repo)
        imp_id = l03_store.create_improvement(
            origin="audit", kind="fix", title="t", proposal_json=_proposal()
        )
        l03_store.transition_improvement(
            imp_id,
            ImprovementStatus.PROPOSED.value,
            ImprovementStatus.APPLYING.value,
            actor="test",
        )
        pipe._journal_put(
            imp_id, {"phase": "files_written", "pre_sha": pipe._git_head(), "attempt": 1}
        )
        (repo / "notes" / "junk.txt").write_text("junk\n", encoding="utf-8")
        pipe.fence_trace = []

        # Someone else holds the apply lease — reconcile must fail closed.
        l03_store.acquire_lease(_APPLY_LEASE_KEY, owner="intruder", duration_seconds=60)

        result = pipe.reconcile_apply(imp_id)
        assert result.deferred is True
        assert result.reason == "apply_lease_held_for_restore"
        assert (repo / "notes" / "junk.txt").exists(), (
            "the worktree must be untouched without a valid fence"
        )
        assert pipe.fence_trace == []
