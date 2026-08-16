"""launchd_reconcile — the diff logic, on fixtures, never on the live machine.

A reconciler whose own tests read ``launchctl list`` would report whatever this
box happens to be running, so every source here is a tmp_path or an injected
:class:`LaunchctlProbe`. The assertions that matter are the honesty ones: an
unreadable source must NOT manufacture drift, and a placeholder template must
NOT vanish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.scheduler.system_jobs import LaunchctlProbe
from scripts.ops import launchd_reconcile as lr

PREFIXES = ("com.omniagentos.", "com.omniagentos.", "com.threeloops.")

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>/bin/true</string></array>
</dict>
</plist>
"""


def _write_plist(path: Path, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PLIST.format(label=label), encoding="utf-8")
    return path


def _approved(path: Path, labels: list[str]) -> Path:
    body = ["version: 1", "actuator: false", "approved:"]
    for label in labels:
        body += [f"  - label: {label}", "    reason: fixture", "    approved_on: 2026-08-01"]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _probe(labels: list[str]) -> LaunchctlProbe:
    return LaunchctlProbe({label: 0 for label in labels}, True)


@pytest.fixture
def estate(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    rendered = tmp_path / "LaunchAgents"
    rendered.mkdir()
    _write_plist(repo / "configs" / "launchd" / "a.plist", "com.omniagentos.a")
    _write_plist(repo / "configs" / "launchd" / "b.plist", "com.omniagentos.b")
    _write_plist(rendered / "com.omniagentos.a.plist", "com.omniagentos.a")
    approved = _approved(tmp_path / "approved.yaml", ["com.omniagentos.a"])
    return {"repo": repo, "rendered": rendered, "approved": approved}


# ------------------------------------------------------------ label reads ---


def test_repo_scan_reads_declared_labels(estate: dict[str, Path]) -> None:
    read = lr.read_repo_templates(estate["repo"], PREFIXES)
    assert read.available
    assert read.labels == {"com.omniagentos.a", "com.omniagentos.b"}


def test_placeholder_template_falls_back_to_the_filename(tmp_path: Path) -> None:
    """Installers render ``{{LABEL}}``/``__LABEL__``. The declared set must still
    contain the job — dropping it would read as 'this template does not exist'."""
    repo = tmp_path / "repo"
    _write_plist(repo / "scripts" / "com.omniagentos.morning.plist.template", "{{LABEL}}")
    read = lr.read_repo_templates(repo, PREFIXES)
    assert read.labels == {"com.omniagentos.morning"}
    assert read.notes == []


def test_unresolvable_template_is_reported_never_dropped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_plist(repo / "scripts" / "wq-worker.plist.template", "__LABEL__")
    read = lr.read_repo_templates(repo, PREFIXES)
    assert read.labels == set()
    assert [note["path"] for note in read.notes] == ["scripts/wq-worker.plist.template"]


def test_versioned_homebrew_label_is_a_label_not_a_placeholder(tmp_path: Path) -> None:
    rendered = tmp_path / "LaunchAgents"
    _write_plist(rendered / "homebrew.mxcl.postgresql@17.plist", "homebrew.mxcl.postgresql@17")
    read = lr.read_rendered(rendered, PREFIXES)
    assert read.available
    assert read.labels == set()  # out of scope
    assert read.notes == []  # ...but NOT filed as unresolved


def test_unreadable_plist_takes_its_whole_source_down(tmp_path: Path) -> None:
    """A file that cannot be OPENED is an instrument error, not a missing job.

    Noting it and continuing let a rogue unreadable LaunchAgent read as "not
    rendered", suppress ``rendered_but_not_in_repo``, and exit 0 aligned — the
    favourable absence this script exists to expose (Class-A review F2).
    """
    rendered = tmp_path / "LaunchAgents"
    rendered.mkdir()
    rogue = _write_plist(rendered / "com.omniagentos.rogue.plist", "com.omniagentos.rogue")
    rogue.chmod(0o000)
    try:
        read = lr.read_rendered(rendered, PREFIXES)
        assert not read.available
        assert read.labels == set()
        assert [note["path"] for note in read.unreadable] == [str(rogue)]
        assert read.notes == []  # NOT filed as a merely-unresolvable label
    finally:
        rogue.chmod(0o600)


def test_unreadable_rendered_plist_never_yields_an_aligned_run(tmp_path: Path) -> None:
    """End to end on the repro's shape: empty repo, empty approved list, a rogue
    unreadable plist on disk. The run must NOT claim alignment."""
    rendered = tmp_path / "LaunchAgents"
    rendered.mkdir()
    rogue = _write_plist(rendered / "com.omniagentos.rogue.plist", "com.omniagentos.rogue")
    rogue.chmod(0o000)
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        report = lr.build_report(
            repo_root=repo,
            launch_agents_dir=rendered,
            approved_path=_approved(tmp_path / "approved.yaml", []),
            prefixes=PREFIXES,
            probe=_probe([]),
        )
        assert report["verdict"] == "unmeasured"
        assert report["exit_code"] == 2
        assert report["unavailable_sources"] == ["rendered"]
        assert [n["path"] for n in report["unreadable_files"]] == [str(rogue)]
        assert report["drift"]["rendered_but_not_in_repo"] is None
    finally:
        rogue.chmod(0o600)


def test_an_unresolvable_template_floors_the_verdict_at_drift(tmp_path: Path) -> None:
    """Read but unmappable is not an instrument error — and not alignment either."""
    repo = tmp_path / "repo"
    rendered = tmp_path / "LaunchAgents"
    rendered.mkdir()
    _write_plist(repo / "scripts" / "wq-worker.plist.template", "__LABEL__")
    report = lr.build_report(
        repo_root=repo,
        launch_agents_dir=rendered,
        approved_path=_approved(tmp_path / "approved.yaml", []),
        prefixes=PREFIXES,
        probe=_probe([]),
    )
    assert report["drift_total"] == 0  # nothing to diff...
    assert report["verdict"] == "drift"  # ...but it must not read as aligned
    assert report["exit_code"] == 1
    assert len(report["unresolved_templates"]) == 1


def test_out_of_prefix_labels_are_out_of_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_plist(repo / "x" / "com.owner.work-drive-sync.plist", "com.owner.work-drive-sync")
    assert lr.read_repo_templates(repo, PREFIXES).labels == set()
    assert lr.read_repo_templates(repo, ("com.owner.",)).labels == {"com.owner.work-drive-sync"}


def test_approved_manifest_reads_only_labels(estate: dict[str, Path]) -> None:
    read = lr.read_approved(estate["approved"], PREFIXES)
    assert read.available
    assert read.labels == {"com.omniagentos.a"}


def test_missing_approved_manifest_is_unavailable_not_empty(tmp_path: Path) -> None:
    read = lr.read_approved(tmp_path / "nope.yaml", PREFIXES)
    assert not read.available
    assert read.labels == set()
    assert "unreadable" in read.reason


def test_missing_launch_agents_dir_is_unavailable_not_empty(tmp_path: Path) -> None:
    read = lr.read_rendered(tmp_path / "nope", PREFIXES)
    assert not read.available
    assert read.labels == set()


def test_live_read_filters_to_scope_and_honours_the_probe(tmp_path: Path) -> None:
    probe = _probe(["com.omniagentos.a", "com.apple.something"])
    assert lr.read_live(PREFIXES, probe).labels == {"com.omniagentos.a"}
    unavailable = lr.read_live(PREFIXES, LaunchctlProbe({}, False, "not macOS"))
    assert not unavailable.available
    assert unavailable.reason == "not macOS"


# ------------------------------------------------------------------ diff ----


def test_aligned_estate_reports_no_drift(estate: dict[str, Path]) -> None:
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"],
        prefixes=PREFIXES,
        probe=_probe(["com.omniagentos.a"]),
    )
    # 'b' is declared in the repo but never rendered — the one real difference.
    assert report["drift"]["repo_but_not_rendered"] == ["com.omniagentos.b"]
    assert report["drift"]["live_but_not_approved"] == []
    assert report["drift"]["approved_but_not_live"] == []
    assert report["verdict"] == "drift"
    assert report["exit_code"] == 1


def test_live_but_unapproved_is_named(estate: dict[str, Path]) -> None:
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"],
        prefixes=PREFIXES,
        probe=_probe(["com.omniagentos.a", "com.threeloops.gate-loop"]),
    )
    assert report["drift"]["live_but_not_approved"] == ["com.threeloops.gate-loop"]
    assert report["drift"]["live_but_not_rendered"] == ["com.threeloops.gate-loop"]
    row = next(r for r in report["labels"] if r["label"] == "com.threeloops.gate-loop")
    assert row == {
        "label": "com.threeloops.gate-loop",
        "live": True,
        "repo": False,
        "rendered": False,
        "approved": False,
        "aligned": False,
    }


def test_fully_aligned_label_set_exits_zero(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rendered = tmp_path / "LaunchAgents"
    rendered.mkdir()
    _write_plist(repo / "configs" / "a.plist", "com.omniagentos.a")
    _write_plist(rendered / "com.omniagentos.a.plist", "com.omniagentos.a")
    report = lr.build_report(
        repo_root=repo,
        launch_agents_dir=rendered,
        approved_path=_approved(tmp_path / "approved.yaml", ["com.omniagentos.a"]),
        prefixes=PREFIXES,
        probe=_probe(["com.omniagentos.a"]),
    )
    assert report["verdict"] == "aligned"
    assert report["exit_code"] == 0
    assert report["drift_total"] == 0
    assert report["labels"][0]["aligned"] is True


def test_unreadable_launchctl_suppresses_live_drift_and_exits_two(
    estate: dict[str, Path],
) -> None:
    """The whole point: an instrument that could not be read must never become
    15 fabricated 'approved but not live' findings."""
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"],
        prefixes=PREFIXES,
        probe=LaunchctlProbe({}, False, "launchctl not found on this host (not macOS?)."),
    )
    for name in (
        "live_but_not_approved",
        "approved_but_not_live",
        "live_but_not_rendered",
        "rendered_but_not_live",
    ):
        assert report["drift"][name] is None, name
    # Non-live drift is still computed — one dead instrument does not blind the rest.
    assert report["drift"]["repo_but_not_rendered"] == ["com.omniagentos.b"]
    assert report["verdict"] == "unmeasured"
    assert report["exit_code"] == 2
    assert report["unavailable_sources"] == ["live"]
    assert report["counts"]["live"] is None
    for row in report["labels"]:
        assert row["live"] is None
        assert row["aligned"] is False


def test_unmeasured_outranks_drift(estate: dict[str, Path]) -> None:
    """An instrument error must not be reported as a candidate defect, even when
    other sources DO show drift."""
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"] / "missing.yaml",
        prefixes=PREFIXES,
        probe=_probe(["com.omniagentos.a"]),
    )
    assert report["drift_total"] > 0
    assert report["verdict"] == "unmeasured"
    assert report["exit_code"] == 2


def test_every_drift_class_is_present_in_the_report(estate: dict[str, Path]) -> None:
    """The JSON shape is stable: an absent key would read as 'not checked'."""
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"],
        prefixes=PREFIXES,
        probe=_probe(["com.omniagentos.a"]),
    )
    assert set(report["drift"]) == set(lr.DRIFT_CLASSES)


# --------------------------------------------------------------- rendering --


def test_table_marks_an_unread_source_as_question_mark_not_absent(
    estate: dict[str, Path],
) -> None:
    report = lr.build_report(
        repo_root=estate["repo"],
        launch_agents_dir=estate["rendered"],
        approved_path=estate["approved"],
        prefixes=PREFIXES,
        probe=LaunchctlProbe({}, False, "launchctl not found"),
    )
    table = lr.render_table(report)
    assert "NOT CHECKED" in table
    assert "! source 'live' unreadable: launchctl not found" in table
    assert "REPORT ONLY" in table
    assert "verdict: unmeasured" in table


def test_cli_json_mode_exits_with_the_verdict(
    estate: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    monkeypatch.setattr(lr, "launchctl_list", lambda: _probe(["com.omniagentos.a"]))
    code = lr.main(
        [
            "--json",
            "--repo-root",
            str(estate["repo"]),
            "--launch-agents-dir",
            str(estate["rendered"]),
            "--approved",
            str(estate["approved"]),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exit_code"] == 1
    assert payload["drift"]["repo_but_not_rendered"] == ["com.omniagentos.b"]


def test_script_never_mentions_a_mutating_launchctl_verb() -> None:
    """Mechanical guard on the whole point of this script: report only."""
    source = Path(lr.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]  # skip the module docstring
    for verb in ("load", "unload", "bootstrap", "bootout", "kickstart", "enable", "disable"):
        assert f'"{verb}"' not in body, f"launchctl {verb} must never appear in this script"
