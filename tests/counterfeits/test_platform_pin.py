"""Explicit per-entry platform pins (issue #104, corpus side).

``test_all_skipped_is_not_a_verdict`` closed the SILENT half of issue #104:
an all-skipped must_fail set is an instrument error, never a verdict. That is
correct and stays. But the WorkFS entries mutate product code that genuinely
does not exist off Darwin (``renameatx_np``), so on ``ubuntu-latest`` the
corpus could only ever fail — the guard cannot be asked there.

The corpus therefore carries an EXPLICIT, load-validated ``platforms`` pin:

* absent (the norm) — the entry runs everywhere, all-skipped stays fail-closed;
* present — on a foreign host the entry is reported ``skipped_platform``,
  printed in the report and counted in the totals, never silently dropped;
* a typo'd or empty pin is refused at load time, because a pin that matches
  no real platform would skip the entry on EVERY host — the favourable-absence
  shape this gate exists to refuse.

These tests monkeypatch ``harness.sys.platform`` so they assert the decision
on both sides of the pin regardless of the host they run on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.counterfeits import harness
from tests.counterfeits.harness import (
    CounterfeitControlError,
    CounterfeitEntry,
    CounterfeitError,
    EntryResult,
    assert_entry_caught,
    format_report,
    load_manifest,
    run_control,
    run_entry,
)


def _entry(platforms: tuple[str, ...] = ()) -> CounterfeitEntry:
    return CounterfeitEntry(
        id="cf-pin-probe",
        patch="patches/probe.patch",
        rationale="probe",
        must_fail=(
            "tests/workfs/test_containment.py::test_string_prefix_counterfeit_fails_loudly",
        ),
        failure_re="assert",
        platforms=platforms,
    )


def _manifest(tmp_path: Path, body: str) -> Path:
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "probe.patch").write_text("--- a\n+++ b\n")
    manifest = tmp_path / "corpus.toml"
    manifest.write_text(body)
    return manifest


_BASE_ROW = """
[[counterfeit]]
id = "cf-pin-probe"
patch = "patches/probe.patch"
rationale = "probe"
must_fail = ["tests/x.py::test_y"]
failure_re = "assert"
"""


# --- parsing ----------------------------------------------------------------


def test_absent_platforms_means_everywhere(tmp_path: Path) -> None:
    entries = load_manifest(_manifest(tmp_path, _BASE_ROW))
    assert entries[0].platforms == ()
    assert entries[0].runs_on_this_platform


def test_platforms_list_parses(tmp_path: Path) -> None:
    entries = load_manifest(_manifest(tmp_path, _BASE_ROW + 'platforms = ["darwin", "linux"]\n'))
    assert entries[0].platforms == ("darwin", "linux")


def test_platforms_bare_string_parses(tmp_path: Path) -> None:
    entries = load_manifest(_manifest(tmp_path, _BASE_ROW + 'platforms = "darwin"\n'))
    assert entries[0].platforms == ("darwin",)


def test_unknown_platform_name_is_refused_at_load(tmp_path: Path) -> None:
    """A typo ("macos") would pin the entry off every host — refuse loudly."""
    with pytest.raises(CounterfeitError, match="unknown values.*macos"):
        load_manifest(_manifest(tmp_path, _BASE_ROW + 'platforms = ["macos"]\n'))


def test_empty_platforms_list_is_refused_at_load(tmp_path: Path) -> None:
    with pytest.raises(CounterfeitError, match="empty list"):
        load_manifest(_manifest(tmp_path, _BASE_ROW + "platforms = []\n"))


def test_non_string_platforms_is_refused_at_load(tmp_path: Path) -> None:
    with pytest.raises(CounterfeitError, match="string or list of strings"):
        load_manifest(_manifest(tmp_path, _BASE_ROW + "platforms = [1]\n"))


# --- run_entry --------------------------------------------------------------


def test_foreign_pin_skips_before_any_scratch_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.sys, "platform", "linux")

    def _boom(
        *_a: Any, **_k: Any
    ) -> None:  # pragma: no cover - the assertion is that it is unreachable
        raise AssertionError("acquire_scratch must not run for a platform-skipped entry")

    monkeypatch.setattr(harness, "acquire_scratch", _boom)
    result = run_entry(_entry(platforms=("darwin",)))
    assert result.status == "skipped_platform"
    assert result.ok
    assert "pinned to platforms" in result.detail
    assert result.duration_s is not None


def test_matching_pin_still_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the pinned platform the entry runs exactly as before (no soft path)."""
    monkeypatch.setattr(harness.sys, "platform", "darwin")

    def _reached(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("reached scratch")

    monkeypatch.setattr(harness, "acquire_scratch", _reached)
    with pytest.raises(RuntimeError, match="reached scratch"):
        # Reaching acquire_scratch proves the pin did not divert execution.
        run_entry(_entry(platforms=("darwin",)))


# --- ok / assert_entry_caught ----------------------------------------------


def test_skipped_platform_is_ok_and_assertable() -> None:
    result = EntryResult(entry=_entry(("darwin",)), status="skipped_platform", detail="pin")
    assert result.ok
    assert_entry_caught(result)  # must not raise


def test_collection_error_is_still_not_ok() -> None:
    """The silent all-skipped case keeps failing — the pin is the only exemption."""
    result = EntryResult(entry=_entry(), status="collection_error", detail="ZERO tests")
    assert not result.ok
    with pytest.raises(CounterfeitError):
        assert_entry_caught(result)


# --- run_control ------------------------------------------------------------


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["pytest"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_control_excludes_foreign_pinned_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.sys, "platform", "linux")
    captured: dict[str, object] = {}

    def _capture(**kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["nodes"] = list(kwargs["nodes"])
        return _proc("..\n2 passed in 0.10s\n")

    monkeypatch.setattr(harness, "run_pytest_nodes", _capture)
    pinned = _entry(platforms=("darwin",))
    everywhere = CounterfeitEntry(
        id="cf-everywhere",
        patch="patches/probe.patch",
        rationale="probe",
        must_fail=("tests/other.py::test_a", "tests/other.py::test_b"),
        failure_re="assert",
    )
    run_control([pinned, everywhere])
    assert captured["nodes"] == ["tests/other.py::test_a", "tests/other.py::test_b"]


def test_control_refuses_when_everything_is_pinned_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corpus with nothing runnable is an instrument error, not a green."""
    monkeypatch.setattr(harness.sys, "platform", "linux")
    monkeypatch.setattr(
        harness,
        "run_pytest_nodes",
        lambda **_k: pytest.fail("control must not run pytest with zero nodes"),
    )
    with pytest.raises(CounterfeitControlError, match="platform-pinned elsewhere"):
        run_control([_entry(platforms=("darwin",))])


# --- report -----------------------------------------------------------------


def test_report_prints_and_counts_platform_skips() -> None:
    results = [
        EntryResult(entry=_entry(("darwin",)), status="skipped_platform", detail="pin"),
        EntryResult(entry=_entry(), status="caught", detail="red for recorded reason"),
    ]
    report = format_report(results)
    assert "SKIP_PLAT" in report
    assert "skipped_platform=1" in report
    assert "caught=1" in report
