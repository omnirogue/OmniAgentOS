"""CLI-level receipt coverage for ``harness.main`` terminal paths.

Sol delta-verify (6a3f5707): the durable ``pool_workers=…  entry_timeout=…``
line only appeared on the completed-result path that calls ``format_report``.
Refusal/control-failure early returns printed no receipt. These tests invoke
``main(argv)`` end-to-end (with mocks only where a real corpus run would be
slow or nondeterministic) and assert the receipt is present with the right
values on every defined terminal path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.counterfeits.harness import (
    CORPUS_DIR,
    RECEIPT_UNRESOLVED,
    CounterfeitControlError,
    CounterfeitEntry,
    CounterfeitError,
    EntryResult,
    format_receipt_line,
    main,
)

pytestmark = pytest.mark.counterfeit_gate


def _combined(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_format_receipt_line_sentinels() -> None:
    assert format_receipt_line(None, None) == (
        f"pool_workers={RECEIPT_UNRESOLVED}  entry_timeout={RECEIPT_UNRESOLVED}"
    )
    assert format_receipt_line(2, 120.0) == "pool_workers=2  entry_timeout=120.0s"


def test_main_receipt_on_config_refusal_malformed_pool_workers(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config refusal before resolve → receipt with unresolved sentinels."""
    monkeypatch.setenv("OMNIAGENTOS_CF_POOL_WORKERS", "nope")
    rc = main(["--skip-control"])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED" in out
    assert f"pool_workers={RECEIPT_UNRESOLVED}" in out
    assert f"entry_timeout={RECEIPT_UNRESOLVED}" in out


def test_main_receipt_on_config_refusal_bad_manifest(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """load_corpus raises after env resolve → receipt has REAL workers/timeout."""
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not valid toml [[[\n")
    rc = main(["--manifest", str(bad), "--skip-control"])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED" in out
    # Env resolved before load failed — real values, not sentinels.
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out
    assert RECEIPT_UNRESOLVED not in out.split("pool_workers=")[1].split()[0]


def test_main_receipt_on_unknown_entry_id(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)
    rc = main(["--skip-control", "--entry", "this-id-does-not-exist-xyzzy-99"])
    assert rc == 2
    out = _combined(capsys)
    assert "unknown ids" in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_on_control_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    def _boom(*_a: object, **_k: object) -> None:
        raise CounterfeitControlError("forced-control-failure")

    monkeypatch.setattr("tests.counterfeits.harness.run_control", _boom)
    rc = main(["--entry", "score-zero-work-special-case"])
    assert rc == 1
    out = _combined(capsys)
    assert "COUNTERFEIT GATE CONTROL FAILED" in out
    assert "forced-control-failure" in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_on_run_entries_refusal(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact Sol forced-pool-refusal probe: CounterfeitError from run_entries."""
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    def _boom(*_a: object, **_k: object) -> list[EntryResult]:
        raise CounterfeitError("forced-pool-refusal")

    monkeypatch.setattr("tests.counterfeits.harness.run_entries", _boom)
    rc = main(["--skip-control", "--entry", "score-zero-work-special-case"])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED: forced-pool-refusal" in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_on_gate_red(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE RED must go through format_receipt_line (delegation, not string side-effect)."""
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    entry = CounterfeitEntry(
        id="score-zero-work-special-case",
        patch="patches/score-zero-work-special-case.patch",
        rationale="r",
        must_fail=("tests/objective/test_score_properties.py::test_x",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    survived = EntryResult(
        entry=entry,
        status="survived",
        detail="counterfeit SURVIVED: forced for CLI receipt test",
    )
    monkeypatch.setattr(
        "tests.counterfeits.harness.run_entries",
        MagicMock(return_value=[survived]),
    )
    # Spy the helper format_report must call — string match alone was true
    # before the delegation existed (format_report inlined the same text).
    with patch(
        "tests.counterfeits.harness.format_receipt_line",
        wraps=format_receipt_line,
    ) as spy:
        rc = main(["--skip-control", "--entry", "score-zero-work-special-case"])
    assert rc == 1
    out = _combined(capsys)
    assert "GATE RED" in out
    assert "SURVIVED" in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out
    spy.assert_called()
    spy.assert_any_call(1, 120.0)


def test_main_receipt_on_gate_green(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE GREEN must go through format_receipt_line (delegation, not string side-effect)."""
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    entry = CounterfeitEntry(
        id="score-zero-work-special-case",
        patch="patches/score-zero-work-special-case.patch",
        rationale="r",
        must_fail=("tests/objective/test_score_properties.py::test_x",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    caught = EntryResult(
        entry=entry,
        status="caught",
        detail="caught: forced for CLI receipt test",
    )
    monkeypatch.setattr(
        "tests.counterfeits.harness.run_entries",
        MagicMock(return_value=[caught]),
    )
    with patch(
        "tests.counterfeits.harness.format_receipt_line",
        wraps=format_receipt_line,
    ) as spy:
        rc = main(["--skip-control", "--entry", "score-zero-work-special-case"])
    assert rc == 0
    out = _combined(capsys)
    assert "GATE GREEN" in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out
    spy.assert_called()
    spy.assert_any_call(1, 120.0)


def test_main_receipt_reflects_explicit_pool_width(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pooled opt-in must show N>1 on the receipt (not only the default width)."""
    monkeypatch.setenv("OMNIAGENTOS_CF_POOL_WORKERS", "2")
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    def _boom(*_a: object, **_k: object) -> list[EntryResult]:
        raise CounterfeitError("forced-pool-refusal")

    monkeypatch.setattr("tests.counterfeits.harness.run_entries", _boom)
    rc = main(["--skip-control", "--entry", "score-zero-work-special-case"])
    assert rc == 2
    out = _combined(capsys)
    assert "pool_workers=2" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_on_nonexistent_manifest(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing manifest → typed load_manifest OSError wrap, not outer catch-all.

    Pins the exact CounterfeitError message shape
    (``{path}: unreadable manifest: …``). If that wrapper were deleted, the outer
    catch-all would emit ``unexpected FileNotFoundError:…`` instead — this test
    must go RED in that case.
    """
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)
    missing = "/tmp/cf-no-such-manifest-xyzzy-does-not-exist.toml"
    rc = main(["--manifest", missing, "--skip-control"])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED" in out
    # Typed wrapper signature — not the outer "unexpected <Type>:" shape.
    assert f"{missing}: unreadable manifest:" in out
    assert "unexpected FileNotFoundError" not in out
    assert "unexpected OSError" not in out
    assert "Traceback" not in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_on_control_timeout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung control → typed TimeoutExpired→CounterfeitControlError wrap (exit 1).

    Pins CONTROL FAILED + exit 1. If the run_control wrapper were deleted, the
    outer catch-all would return 2 with ``unexpected TimeoutExpired`` — this
    test must go RED in that case.
    """
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    def _timeout(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=300.0)

    monkeypatch.setattr("tests.counterfeits.harness.run_pytest_nodes", _timeout)
    rc = main(["--entry", "score-zero-work-special-case"])
    assert rc == 1, f"typed control wrap must exit 1, got {rc}"
    out = _combined(capsys)
    assert "COUNTERFEIT GATE CONTROL FAILED" in out
    assert "timed out" in out.lower()
    assert "unexpected TimeoutExpired" not in out
    assert "unexpected" not in out  # outer catch-all shape must not appear
    assert "Traceback" not in out
    assert "pool_workers=1" in out
    assert "entry_timeout=120.0s" in out


def test_main_receipt_when_parse_args_raises_value_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol probe: unexpected Exception from parse_args must still emit receipt."""
    import argparse

    def _boom(self: object, *a: object, **k: object) -> None:
        raise ValueError("forced-parse-args-failure")

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _boom)
    rc = main([])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED: unexpected ValueError: forced-parse-args-failure" in out
    # Emergency path uses repr() form (workers still unresolved).
    assert "pool_workers=" in out
    assert "entry_timeout=" in out
    assert "Traceback" not in out


def test_main_receipt_when_entries_list_allocation_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """entries list allocation is inside the outer try — forced failure still receipts."""

    def _oom() -> list[CounterfeitEntry]:
        raise MemoryError("forced-entries-list-allocation-failure")

    monkeypatch.setattr("tests.counterfeits.harness._new_entries_list", _oom)
    rc = main([])
    assert rc == 2
    out = _combined(capsys)
    assert "COUNTERFEIT GATE REFUSED: unexpected MemoryError" in out
    assert "forced-entries-list-allocation-failure" in out
    assert "pool_workers=" in out
    assert "entry_timeout=" in out
    assert "Traceback" not in out


def test_main_receipt_when_receipt_unresolved_global_is_deleted(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-try sentinels must be LOAD_CONST literals, not LOAD_GLOBAL RECEIPT_UNRESOLVED.

    Sol probe: deleting harness.RECEIPT_UNRESOLVED after import used to raise a
    bare NameError with empty stdout/stderr before the outer try began. With
    literal seeds, the pre-try bind cannot NameError; main enters the protected
    body. Use a fast unknown-id refusal (not full corpus) so the test stays
    cheap while still exercising main() past the pre-try seed.
    """
    import tests.counterfeits.harness as harness

    # Faithful to Sol: attribute gone after import (auto-restored by monkeypatch).
    monkeypatch.delattr(harness, "RECEIPT_UNRESOLVED", raising=False)
    assert not hasattr(harness, "RECEIPT_UNRESOLVED")

    # Fast path past pre-try seed: resolve env, load corpus, refuse unknown id.
    # Must not bare-escape NameError; must print a receipt-shaped line.
    rc = main(["--skip-control", "--entry", "no-such-id-xyzzy-literal-sentinel"])
    out = _combined(capsys)
    assert rc == 2
    assert out.strip() != ""
    assert "pool_workers=" in out
    assert "entry_timeout=" in out
    assert "Traceback" not in out


def test_main_receipt_when_pretty_formatter_double_faults(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol probe: format_receipt_line failure must not double-fault the handler.

    Inject TypeError from the pretty formatter while format_report runs; the
    outer handler must emit via emit_emergency_receipt (repr form), not call
    format_receipt_line again.
    """
    monkeypatch.delenv("OMNIAGENTOS_CF_POOL_WORKERS", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CF_ENTRY_TIMEOUT", raising=False)

    entry = CounterfeitEntry(
        id="score-zero-work-special-case",
        patch="patches/score-zero-work-special-case.patch",
        rationale="r",
        must_fail=("tests/objective/test_score_properties.py::test_x",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    caught = EntryResult(
        entry=entry,
        status="caught",
        detail="caught: forced",
    )
    monkeypatch.setattr(
        "tests.counterfeits.harness.run_entries",
        MagicMock(return_value=[caught]),
    )

    call_count = {"n": 0}

    def _fmt_boom(*_a: object, **_k: object) -> str:
        call_count["n"] += 1
        raise TypeError("forced-formatter-failure")

    monkeypatch.setattr("tests.counterfeits.harness.format_receipt_line", _fmt_boom)
    rc = main(["--skip-control", "--entry", "score-zero-work-special-case"])
    assert rc == 2
    out = _combined(capsys)
    assert "unexpected TypeError: forced-formatter-failure" in out
    # Emergency receipt present (repr form: pool_workers=1  entry_timeout=120.0).
    assert "pool_workers=" in out
    assert "entry_timeout=" in out
    assert "Traceback" not in out
    # Pretty formatter was used once (inside format_report); emergency path must
    # not re-enter it (would be a second call and a second raise).
    assert call_count["n"] == 1, (
        f"format_receipt_line called {call_count['n']} times — emergency path "
        f"must not re-invoke the failed pretty formatter"
    )
