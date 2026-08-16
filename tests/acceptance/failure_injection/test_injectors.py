"""Self-test for the failure-injection suite — runnable on its own.

    uv run pytest -q tests/acceptance/failure_injection/

This file answers one question only: **does each injector actually inject its
fault?** An injector that silently no-ops would make every recovery assertion in
``test_09_failure_recovery.py`` vacuous — the run would sail through green and
"recovery" would be proven against a fault that never happened. So each case is
matched against the fault's signature (the recorded ``end_reason``), and a
control run with NO injection is asserted to produce none of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.failure_injection import (
    INJECTIONS,
    FailureInjection,
    RecoveryExpectation,
    injection_names,
    run_injection,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler


@pytest.fixture(autouse=True)
def _pin_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


class TestRegistryShape:
    def test_the_five_required_faults_are_registered(self) -> None:
        assert set(INJECTIONS) == {
            "timeout",
            "tool_failure",
            "failed_test",
            "merge_conflict",
            "rate_limit",
        }

    def test_every_injection_is_well_formed(self) -> None:
        for name, injection in INJECTIONS.items():
            assert isinstance(injection, FailureInjection)
            assert injection.name == name, f"{name} is filed under the wrong key"
            assert isinstance(injection.expect, RecoveryExpectation)
            assert injection.expect.attempt_end_reasons, f"{name} declares no fault signature"
            assert injection.expect.terminal_status, f"{name} declares no terminal status"
            assert any(spec["id"] == injection.target for spec in injection.specs), (
                f"{name} targets a task that is not in its plan"
            )

    def test_names_are_a_stable_parameterization_source(self) -> None:
        assert injection_names() == sorted(INJECTIONS)

    def test_unknown_injection_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="unknown injection"):
            run_injection("no_such_fault", tmp_path)


class TestEachInjectorReallyInjects:
    @pytest.mark.parametrize("name", injection_names())
    def test_fault_signature_appears_in_the_attempt_record(self, name: str, tmp_path: Path) -> None:
        result = run_injection(name, tmp_path)
        try:
            assert result.run_finished, f"{name}: the run never settled"
            expected = result.injection.expect.attempt_end_reasons
            assert result.end_reasons, f"{name}: no attempt was ever recorded"
            assert set(result.end_reasons) & expected, (
                f"{name}: the fault never fired. "
                f"expected one of {sorted(expected)}, saw {result.end_reasons}"
            )
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_declared_actions_are_emitted(self, name: str, tmp_path: Path) -> None:
        result = run_injection(name, tmp_path)
        try:
            assert result.run_finished, (
                f"{name}: run never settled; status={result.target_status}, "
                f"end_reasons={result.end_reasons}, actions={result.actions}"
            )
            missing = result.injection.expect.emitted_actions - set(result.actions)
            assert not missing, f"{name}: declared actions never emitted: {sorted(missing)}"
        finally:
            result.close()

    def test_an_uninjected_run_shows_none_of_the_fault_signatures(self, tmp_path: Path) -> None:
        """The control. Without this, the sweep above could pass against a
        harness that reports failures no matter what it is asked to do."""
        h = make_harness(tmp_path, [{"id": "target"}], max_concurrency=1, integration=False)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.status_of("target") == "done"
            reasons = {str(a["end_reason"]) for a in h.attempts_of("target")}
            assert reasons == {"completed"}, f"a clean run recorded a fault: {reasons}"
            assert not h.emitter.of("task_blocked")
        finally:
            h.close()


class TestDriverIsHermeticAndBounded:
    def test_virtual_time_is_used_instead_of_sleeping(self, tmp_path: Path) -> None:
        """The timeout injection crosses a 15-minute deadline. It must do so by
        advancing the injected clock, so the test costs no real wall time."""
        injection = INJECTIONS["timeout"]
        assert injection.fake_clock is True
        assert injection.advance_seconds >= 15 * 60

        import time

        started = time.monotonic()
        result = run_injection("timeout", tmp_path)
        elapsed = time.monotonic() - started
        try:
            assert result.run_finished
            assert elapsed < 60, f"the timeout injection slept for real ({elapsed:.1f}s)"
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_scenario_state_is_confined_to_tmp_path(self, name: str, tmp_path: Path) -> None:
        result = run_injection(name, tmp_path)
        try:
            assert Path(result.harness.db_path).resolve().is_relative_to(tmp_path.resolve())
            assert result.harness.workdir.resolve().is_relative_to(tmp_path.resolve())
        finally:
            result.close()
