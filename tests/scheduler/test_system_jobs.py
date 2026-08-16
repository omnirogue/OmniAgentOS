"""tests for omniagentos.scheduler.system_jobs — the read-only catalog behind
GET /api/system-jobs. Everything machine-dependent (launchctl, LaunchAgents,
log files, the clock) is injected, so these never touch the real system."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.remote_probe import ParsedRemoteProbe, RemoteProbeSnapshot
from omniagentos.scheduler.system_jobs import (
    CATALOG,
    PARSE_ERROR_KEY,
    CalendarEntry,
    CrontabProbe,
    LaunchctlProbe,
    Schedule,
    SnapshotCache,
    _merge_calendar_defaults,
    crontab_list,
    derive_health,
    describe_schedule,
    launchctl_list,
    list_system_jobs,
    load_csi_routines,
    next_fire,
    parse_crontab,
    parse_plist,
    scan_installed_plists,
    schedule_from_plist,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


def _raise(exc: BaseException) -> Callable[..., object]:
    """A subprocess runner that fails the way `exc` says it does."""

    def _runner(*_a: object, **_k: object) -> object:
        raise exc

    return _runner


class _Rc:
    """A completed run with a non-zero exit and no usable stdout."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""


# --------------------------------------------------------------------------- parser


def test_parse_rendered_plist_roundtrips(tmp_path: Path) -> None:
    rendered = tmp_path / "job.plist"
    rendered.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.job</string>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    data = parse_plist(rendered)
    assert data["Label"] == "com.example.job"
    assert data["StartInterval"] == 300
    assert data["RunAtLoad"] is False


def test_parse_template_tolerates_placeholders(tmp_path: Path) -> None:
    template = tmp_path / "job.plist.template"
    template.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>{{LABEL}}</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>{{HOUR}}</integer><key>Minute</key><integer>{{MINUTE}}</integer></dict>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    data = parse_plist(template)
    assert data["Label"] == "{{LABEL}}"
    schedule = schedule_from_plist(data)
    assert schedule is not None and schedule.kind == "calendar"
    # Placeholder hour/minute surface as None (never as a guessed number).
    assert schedule.entries[0].hour is None
    assert schedule.entries[0].minute is None


def test_parse_rejects_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.plist"
    bad.write_text("this is not xml at all {{{", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_plist(bad)


def test_schedule_from_plist_interval_and_calendar_shapes() -> None:
    assert schedule_from_plist({"StartInterval": 900}) == Schedule(kind="interval", seconds=900)
    single = schedule_from_plist({"StartCalendarInterval": {"Hour": 2, "Minute": 30}})
    assert single is not None and single.entries == (CalendarEntry(hour=2, minute=30),)
    twice = schedule_from_plist(
        {"StartCalendarInterval": [{"Hour": 3, "Minute": 30}, {"Hour": 15, "Minute": 30}]}
    )
    assert twice is not None and len(twice.entries) == 2
    weekly = schedule_from_plist({"StartCalendarInterval": {"Weekday": 0, "Hour": 9, "Minute": 0}})
    assert weekly is not None and weekly.entries[0].weekday == 0
    assert schedule_from_plist({}) is None


def test_merge_calendar_defaults_fills_template_placeholders() -> None:
    parsed = Schedule(kind="calendar", entries=(CalendarEntry(), CalendarEntry()))
    default = Schedule(kind="calendar", entries=(CalendarEntry(3, 30), CalendarEntry(15, 30)))
    merged = _merge_calendar_defaults(parsed, default)
    assert merged.entries == (CalendarEntry(3, 30), CalendarEntry(15, 30))
    # A literal value in the template always wins over the installer default.
    parsed_literal = Schedule(kind="calendar", entries=(CalendarEntry(6, 30), CalendarEntry()))
    merged2 = _merge_calendar_defaults(parsed_literal, default)
    assert merged2.entries[0] == CalendarEntry(6, 30)
    assert merged2.entries[1] == CalendarEntry(15, 30)


# --------------------------------------------------------------------------- describe


def test_describe_schedule_humanizes_every_shape() -> None:
    assert describe_schedule(Schedule(kind="interval", seconds=300)) == "every 5 minutes"
    assert describe_schedule(Schedule(kind="interval", seconds=3600)) == "hourly"
    assert describe_schedule(Schedule(kind="interval", seconds=900)) == "every 15 minutes"
    assert describe_schedule(Schedule(kind="interval", seconds=43200)) == "every 12h"
    assert (
        describe_schedule(Schedule(kind="calendar", entries=(CalendarEntry(2, 0),)))
        == "daily 02:00 local"
    )
    assert (
        describe_schedule(
            Schedule(kind="calendar", entries=(CalendarEntry(3, 30), CalendarEntry(15, 30)))
        )
        == "twice daily 03:30 + 15:30 local"
    )
    assert (
        describe_schedule(Schedule(kind="calendar", entries=(CalendarEntry(9, 0, 0),)))
        == "Sun 09:00 local"
    )
    assert "0 8,20 * * *" in describe_schedule(
        Schedule(kind="cron", cron="0 8,20 * * *", note="UTC")
    )
    assert describe_schedule(Schedule(kind="unknown")) == "—"


# --------------------------------------------------------------------------- next fire


def test_next_fire_interval_anchors_to_last_run() -> None:
    schedule = Schedule(kind="interval", seconds=300)
    last = NOW - timedelta(seconds=120)
    assert next_fire(schedule, NOW, last) == last + timedelta(seconds=300)
    # No observed run → honest None rather than an invented phase.
    assert next_fire(schedule, NOW, None) is None


def test_next_fire_calendar_picks_todays_upcoming_slot() -> None:
    # Machine-local "now" for the test is NOW itself (tz-aware).
    schedule = Schedule(kind="calendar", entries=(CalendarEntry(3, 30), CalendarEntry(15, 30)))
    local_now = NOW.astimezone()
    fire = next_fire(schedule, NOW, None)
    assert fire is not None
    assert fire > local_now
    assert (fire.hour, fire.minute) in ((3, 30), (15, 30))


def test_next_fire_weekly_lands_on_sunday() -> None:
    schedule = Schedule(kind="calendar", entries=(CalendarEntry(9, 0, 0),))
    fire = next_fire(schedule, NOW, None)
    assert fire is not None
    assert (fire.weekday() + 1) % 7 == 0  # launchd Weekday 0 == Sunday
    assert (fire.hour, fire.minute) == (9, 0)


def test_next_fire_cron_matches_expected_utc_slots() -> None:
    # kb-drift-check: 0 8,20 * * * UTC — from Tuesday 12:00 UTC the next slot is 20:00.
    schedule = Schedule(kind="cron", cron="0 8,20 * * *", note="UTC")
    fire = next_fire(schedule, NOW, None)
    assert fire is not None
    assert fire == datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
    # A Sunday-gated cron (0 = Sunday) skips ahead to the coming Sunday.
    sunday = next_fire(Schedule(kind="cron", cron="0 9 * * 0"), NOW, None)
    assert sunday is not None and (sunday.weekday() + 1) % 7 == 0


# --------------------------------------------------------------------------- health


def test_health_remote_is_never_healthy() -> None:
    state, reason = derive_health(
        executor="remote_cron",
        loaded=None,
        last_exit=None,
        last_run=None,
        schedule=Schedule(kind="cron", cron="0 8,20 * * *"),
        now=NOW,
        health_note="no observability",
    )
    assert state == "unknown" and "no observability" in reason


def test_health_not_loaded_when_label_absent() -> None:
    state, _ = derive_health(
        executor="launchd",
        loaded=False,
        last_exit=None,
        last_run=None,
        schedule=Schedule(kind="interval", seconds=300),
        now=NOW,
    )
    assert state == "not_loaded"


def test_health_failing_beats_everything_else() -> None:
    state, reason = derive_health(
        executor="launchd",
        loaded=True,
        last_exit=126,
        last_run=NOW - timedelta(minutes=3),
        schedule=Schedule(kind="interval", seconds=300),
        now=NOW,
    )
    assert state == "failing" and "126" in reason


def test_health_unknown_when_loaded_but_never_observed() -> None:
    state, _ = derive_health(
        executor="launchd",
        loaded=True,
        last_exit=0,
        last_run=None,
        schedule=Schedule(kind="interval", seconds=300),
        now=NOW,
    )
    assert state == "unknown"


def test_health_healthy_then_stale_past_double_cadence() -> None:
    schedule = Schedule(kind="interval", seconds=900)
    fresh = derive_health(
        executor="launchd",
        loaded=True,
        last_exit=0,
        last_run=NOW - timedelta(minutes=10),
        schedule=schedule,
        now=NOW,
    )
    assert fresh[0] == "healthy"
    old = derive_health(
        executor="launchd",
        loaded=True,
        last_exit=0,
        last_run=NOW - timedelta(minutes=31),
        schedule=schedule,
        now=NOW,
    )
    assert old[0] == "stale"


def test_health_is_unknown_when_the_cadence_cannot_be_derived() -> None:
    """No derivable cadence means the staleness rule never ran — say so.

    `healthy` is only reachable by SURVIVING the staleness check. When
    `_cycle_seconds` yields None there is no check to survive, and falling
    through to "Fired within its expected cadence" asserts a comparison that
    never happened. Both live shapes are covered: the catalog's KeepAlive
    WebSocket (kind='window') and a discovered daemon plist that declares no
    interval at all (kind='unknown').
    """
    for schedule in (
        Schedule(kind="window", note="KeepAlive — one long-lived WebSocket"),
        Schedule(kind="unknown", note="keep-alive / on-demand"),
    ):
        state, reason = derive_health(
            executor="launchd",
            loaded=True,
            last_exit=0,
            last_run=NOW - timedelta(days=1000),
            schedule=schedule,
            now=NOW,
        )
        assert state == "unknown", f"{schedule.kind} rendered {state}"
        assert "cadence" in reason


def test_comms_slack_socket_never_renders_healthy_from_a_dead_log(tmp_path: Path) -> None:
    """End-to-end through the function GET /api/system-jobs calls.

    comms-slack-socket is loaded with last exit 0 and a log nothing has written
    to in ~3 years. Its own catalog entry says machine state cannot judge it
    ("no exit code to gate on"), so the panel must not paint it green.
    """
    log = tmp_path / "var" / "log" / "comms-slack-socket.log"
    log.parent.mkdir(parents=True)
    log.write_text("last line the socket ever wrote\n", encoding="utf-8")
    stale_mtime = (NOW - timedelta(days=1000)).timestamp()
    os.utime(log, (stale_mtime, stale_mtime))

    snap = list_system_jobs(
        repo_root=tmp_path,
        launchd_dir=tmp_path / "empty-launchagents",
        now=NOW,
        launchctl={"com.omniagentos.comms-slack-socket": 0},
    )
    job = next(j for j in snap["jobs"] if j["key"] == "comms-slack-socket")
    assert job["loaded"] is True
    assert job["last_run_at"] == "2023-11-01T12:00:00Z"
    assert job["health"] == "unknown"


def test_discovered_keepalive_daemon_never_renders_healthy_from_a_dead_log(
    tmp_path: Path,
) -> None:
    """A KeepAlive plist declares no cadence, so its log mtime proves nothing."""
    log = tmp_path / "api.log"
    log.write_text("last line\n", encoding="utf-8")
    stale_mtime = (NOW - timedelta(days=1000)).timestamp()
    os.utime(log, (stale_mtime, stale_mtime))
    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    (launchd_dir / "com.omniagentos.api.plist").write_text(
        f"""<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.api</string>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{log}</string>
</dict></plist>""",
        encoding="utf-8",
    )

    snap = list_system_jobs(
        repo_root=tmp_path,
        launchd_dir=launchd_dir,
        now=NOW,
        launchctl={"com.omniagentos.api": 0},
    )
    job = next(j for j in snap["jobs"] if j["key"] == "discovered-com.omniagentos.api")
    assert job["schedule"]["kind"] == "unknown"
    assert job["last_run_at"] == "2023-11-01T12:00:00Z"
    assert job["health"] == "unknown"


def test_health_daily_job_stale_after_two_days() -> None:
    schedule = Schedule(kind="calendar", entries=(CalendarEntry(2, 0),))
    state, _ = derive_health(
        executor="launchd",
        loaded=True,
        last_exit=0,
        last_run=NOW - timedelta(days=2, hours=1),
        schedule=schedule,
        now=NOW,
    )
    assert state == "stale"


# --------------------------------------------------------------------------- launchctl / installed scan


def test_launchctl_list_parses_table_and_tolerates_failure() -> None:
    class _Result:
        returncode = 0
        stdout = "PID\tStatus\tLabel\n-\t1\tcom.example.failing\n123\t0\tcom.example.running\n-\t0\tcom.example.idle\n"

    probe = launchctl_list(runner=lambda *a, **k: _Result())
    assert probe.available is True
    assert probe.entries == {
        "com.example.failing": 1,
        "com.example.running": 0,
        "com.example.idle": 0,
    }

    # A probe that could not run is UNAVAILABLE, not an empty measurement. This
    # assertion used to read `== {}`, which is the shape that let every job on a
    # host without launchctl report loaded=false.
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("launchctl missing")

    failed = launchctl_list(runner=boom)
    assert failed.available is False
    assert failed.entries == {}
    assert failed.reason


@pytest.mark.parametrize(
    ("runner", "expected_fragment"),
    [
        (_raise(FileNotFoundError("launchctl missing")), "not found"),
        (_raise(subprocess.TimeoutExpired(cmd="launchctl", timeout=5)), "timed out"),
        (_raise(OSError("boom")), "could not be run"),
        (lambda *a, **k: _Rc(3), "exited 3"),
    ],
)
def test_launchctl_probe_failures_are_unavailable_and_named(
    runner: object, expected_fragment: str
) -> None:
    """Every failure mode reports available=False with a reason naming the cause.

    MUST FAIL against main, where all four returned {} and were indistinguishable
    from "launchd is running nothing".
    """
    probe = launchctl_list(runner=runner)
    assert probe.available is False
    assert probe.entries == {}
    assert expected_fragment in probe.reason


def test_unreadable_probe_yields_loaded_none_never_false(tmp_path: Path) -> None:
    """An unreadable launchctl makes loaded None + health unknown, never false/healthy.

    MUST FAIL against main, where an empty dict made every launchd job report
    loaded=false with health 'not_loaded' — a positive claim that the automation
    is configured and currently off.
    """
    snap = list_system_jobs(
        repo_root=tmp_path,
        launchd_dir=tmp_path / "nonexistent",
        launchctl=LaunchctlProbe({}, False, "launchctl list exited 3; machine state was not read."),
    )
    launchd_jobs = [j for j in snap["jobs"] if j["executor"] == "launchd" and j["label"]]
    assert launchd_jobs, "fixture must exercise at least one launchd job"
    for job in launchd_jobs:
        assert job["loaded"] is None, f"{job['key']} reported {job['loaded']!r}, expected None"
        assert job["health"] == "unknown"
        assert "could not be read" in job["health_reason"] or "exited 3" in job["health_reason"]
    assert snap["launchctl"]["available"] is False
    assert snap["counts"]["loaded"] == 0
    assert snap["counts"]["loaded_unknown"] >= 1


def test_measured_absence_still_reports_loaded_false(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: a healthy probe that simply lacks the label is still false.

    MUST FAIL against a fix that collapses every case to None — which would trade
    one favourable absence for a blanket 'unknown' and lose the real off signal.
    """
    snap = list_system_jobs(
        repo_root=tmp_path,
        launchd_dir=tmp_path / "nonexistent",
        launchctl=LaunchctlProbe({"com.example.unrelated": 0}, True),
    )
    launchd_jobs = [j for j in snap["jobs"] if j["executor"] == "launchd" and j["label"]]
    assert launchd_jobs
    assert all(j["loaded"] is False for j in launchd_jobs)
    assert all(j["health"] == "not_loaded" for j in launchd_jobs)
    assert snap["launchctl"]["available"] is True


def test_two_null_causes_are_distinguishable() -> None:
    """The two loaded=None causes carry different reasons.

    MUST FAIL against a fix that returns None with no discriminator — two null
    causes with one reason is the same favourable absence one level up.
    """
    remote, remote_reason = derive_health(
        executor="remote_cron",
        loaded=None,
        last_exit=None,
        last_run=None,
        schedule=Schedule(kind="interval", seconds=3600),
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    unread, unread_reason = derive_health(
        executor="launchd",
        loaded=None,
        last_exit=None,
        last_run=None,
        schedule=Schedule(kind="interval", seconds=3600),
        now=datetime(2026, 8, 8, tzinfo=UTC),
        unmeasured_reason="launchctl list exited 3; machine state was not read.",
    )
    assert remote == unread == "unknown"
    assert remote_reason != unread_reason
    assert "Remote job" in remote_reason
    assert "exited 3" in unread_reason


def test_unreadable_probe_does_not_render_healthy_from_log_mtime(tmp_path: Path) -> None:
    """The dangerous fall-through: fresh logs must not make an unmeasured job green.

    MUST FAIL against a fix that adds loaded=None without a derive_health branch,
    where a recent log mtime alone carried the job to 'healthy'.
    """
    state, reason = derive_health(
        executor="launchd",
        loaded=None,
        last_exit=0,
        last_run=datetime(2026, 8, 8, tzinfo=UTC),
        schedule=Schedule(kind="interval", seconds=3600),
        now=datetime(2026, 8, 8, tzinfo=UTC),
        unmeasured_reason="launchctl not found on this host (not macOS?).",
    )
    assert state == "unknown"
    assert "not found" in reason


def test_scan_installed_plists_finds_jobs_and_stale_backups(tmp_path: Path) -> None:
    (tmp_path / "com.omniagentos.demo.plist").write_text(
        """<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.demo</string>
<key>StartInterval</key><integer>600</integer>
</dict></plist>""",
        encoding="utf-8",
    )
    (tmp_path / "com.omniagentos.api.plist.bak-swarm").write_text("backup", encoding="utf-8")
    (tmp_path / "com.apple.notours.plist").write_text("ignored", encoding="utf-8")
    installed, stale = scan_installed_plists(tmp_path)
    assert set(installed) == {"com.omniagentos.demo"}
    assert installed["com.omniagentos.demo"]["StartInterval"] == 600
    assert len(stale) == 1 and stale[0].name.endswith(".bak-swarm")


def test_scan_installed_plists_never_drops_an_unparseable_but_present_plist(
    tmp_path: Path,
) -> None:
    """Regression for sha256:e63c660be4da172c0c3cd52522d8310620393dd — a `--`
    inside an XML comment makes the file strictly unparseable while
    plutil/launchd still accept it, and the ORIGINAL bug silently dropped the
    entry from the installed map (56 files on disk -> 55 in the map), which
    renders a loaded job as not installed. An instrument error must never
    read as a favourable absence."""
    (tmp_path / "com.omniagentos.broken.plist").write_text(
        """<?xml version="1.0"?>
<!-- this comment illegally contains -- a double dash -->
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.broken</string>
<key>StartInterval</key><integer>600</integer>
</dict></plist>""",
        encoding="utf-8",
    )
    installed, _stale = scan_installed_plists(tmp_path)
    # The file must NOT vanish — it must be kept under its real Label,
    # extracted via the best-effort fallback since strict parsing failed.
    assert set(installed) == {"com.omniagentos.broken"}
    assert PARSE_ERROR_KEY in installed["com.omniagentos.broken"]


def test_list_system_jobs_reports_unparseable_installed_plist_as_present_not_absent(
    tmp_path: Path,
) -> None:
    """A discovered job whose plist fails to parse must still show
    plist_present=True and loaded=True (when launchctl says so) — never
    plist_present=False, which is exactly the favourable-absence bug this
    fixes."""
    (tmp_path / "com.omniagentos.broken.plist").write_text(
        """<?xml version="1.0"?>
<!-- another illegal -- comment -->
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.broken</string>
</dict></plist>""",
        encoding="utf-8",
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={"com.omniagentos.broken": 0},
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    job = jobs["discovered-com.omniagentos.broken"]
    assert job["plist_present"] is True
    assert job["loaded"] is True
    assert job["health"] == "unknown"
    assert "failed to parse" in job["health_reason"]
    assert "NOT actually absent" in job["purpose"]


# --------------------------------------------------------------------------- CSI config


def test_load_csi_routines_reads_the_real_config() -> None:
    routines = load_csi_routines(REPO_ROOT / "configs" / "self_improvement.yaml")
    keys = {r["key"] for r in routines}
    # configs/self_improvement.yaml declares exactly these 8 enabled routines.
    assert keys == {
        "csi-design",
        "csi-self_learning",
        "csi-routing",
        "csi-skills",
        "csi-tool_calls",
        "csi-speed",
        "csi-quality",
        "csi-tech_debt",
    }
    assert all(r["health"] == "unknown" for r in routines)  # never rendered healthy
    assert any("7-day" in r["schedule"]["description"] for r in routines)
    assert load_csi_routines(REPO_ROOT / "configs" / "does-not-exist.yaml") == []


# --------------------------------------------------------------------------- catalog integrity + assembly


def test_catalog_templates_and_sources_exist() -> None:
    """Grounding guarantee: every file the catalog cites must exist in the tree.

    ``HANDOFF/`` is internal operator handoff material (remote-infra runbooks,
    personnel-specific notes) and is not carried into this checkout, so the
    seven ``remote_cron``/``remote_docker`` entries whose only grounding is
    ``HANDOFF/LOOPS-VISIBILITY.md`` are exempted from the existence check —
    they document boxes this checkout does not run against either way. Every
    other entry (every local ``launchd``/``csi_pipeline`` job, and any other
    source a remote entry might cite) is still held to the full guarantee.
    """
    for entry in CATALOG:
        if entry.template is not None:
            assert (REPO_ROOT / entry.template).is_file(), f"{entry.key}: missing {entry.template}"
        assert entry.source, entry.key
        for chunk in entry.source.split(" + "):
            path = chunk.split(" ")[0]
            if path.startswith("HANDOFF/"):
                continue
            assert (REPO_ROOT / path).exists(), f"{entry.key}: missing source {path}"


def test_list_system_jobs_assembles_all_families(tmp_path: Path) -> None:
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,  # empty: nothing installed, nothing loaded
        now=NOW,
        launchctl={},
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    # launchd catalog + 8 CSI routines + 7 remote entries.
    assert len(jobs) == len(CATALOG) + 8
    assert jobs["routines-tick"]["schedule"]["description"] == "every 5 minutes"
    assert jobs["routines-tick"]["managed_candidate"] is False
    # Template parse + installer defaults merge into real clock times.
    assert jobs["steward-briefing"]["schedule"]["description"] == "daily 07:30 local"
    assert jobs["reliability-audit"]["schedule"]["description"] == "twice daily 06:30 + 18:30 local"
    assert jobs["reliability-weekly"]["schedule"]["description"] == "Sun 09:00 local"
    assert (
        jobs["selfimprove-curator"]["schedule"]["description"] == "twice daily 03:30 + 15:30 local"
    )
    # With launchctl empty every launchd job is honestly not_loaded.
    assert jobs["modelintel-daily"]["health"] == "not_loaded"
    # Remote jobs never claim health.
    assert jobs["kb-drift-check"]["health"] == "unknown"
    assert jobs["kb-drift-check"]["loaded"] is None
    # CSI config routines landed.
    assert jobs["csi-tech_debt"]["executor"] == "csi_pipeline"
    # Counts are internally consistent.
    assert snap["counts"]["total"] == len(snap["jobs"])
    assert snap["generated_at"] is not None


def test_list_system_jobs_marks_loaded_and_failing_from_launchctl(tmp_path: Path) -> None:
    snap = list_system_jobs(
        repo_root=tmp_path,  # hermetic root: no repo logs/templates leak into health
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={
            "com.omniagentos.routines": 0,
            "com.omniagentos.reflection-nightly": 126,
        },
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert jobs["routines-tick"]["loaded"] is True
    assert jobs["routines-tick"]["health"] == "unknown"  # loaded, but no logs in tmp tree
    assert jobs["reflection-nightly"]["health"] == "failing"
    assert "126" in jobs["reflection-nightly"]["health_reason"]
    assert snap["counts"]["failing"] >= 1


def test_list_system_jobs_surfaces_discovered_and_stale(tmp_path: Path) -> None:
    (tmp_path / "com.omniagentos.unknown-job.plist").write_text(
        """<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.unknown-job</string>
<key>StartInterval</key><integer>3600</integer>
</dict></plist>""",
        encoding="utf-8",
    )
    (tmp_path / "com.omniagentos.runner.plist.bak-swarm").write_text("backup", encoding="utf-8")
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={"com.omniagentos.unknown-job": 0},
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    discovered = jobs["discovered-com.omniagentos.unknown-job"]
    assert discovered["category"] == "Discovered (no repo definition)"
    assert discovered["loaded"] is True
    assert discovered["schedule"]["description"] == "hourly"
    stale = jobs["stale-com.omniagentos.runner.plist.bak-swarm"]
    assert stale["category"] == "Stale backup"
    assert "removal" in stale["health_reason"]


def test_list_system_jobs_surfaces_launchctl_only_loaded_jobs(tmp_path: Path) -> None:
    """A job loaded from a rendered plist (no LaunchAgents file, no catalog
    entry) still surfaces — an operator comparing with `launchctl list` should
    find the same set here."""
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={"com.omniagentos.ghost": 0, "com.apple.notours": 0},
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    ghost = jobs["discovered-com.omniagentos.ghost"]
    assert ghost["loaded"] is True
    assert ghost["plist_present"] is False
    assert ghost["source"] == "launchctl list"
    # Non-product labels never leak into the listing.
    assert "discovered-com.apple.notours" not in jobs


def test_installed_plist_schedule_wins_over_catalog_default(tmp_path: Path) -> None:
    """The machine's installed plist is the truth when it disagrees with the repo."""
    (tmp_path / "com.omniagentos.routines.plist").write_text(
        """<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.routines</string>
<key>StartInterval</key><integer>600</integer>
<key>StandardOutPath</key><string>/nonexistent/routines.log</string>
</dict></plist>""",
        encoding="utf-8",
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={"com.omniagentos.routines": 0},
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert jobs["routines-tick"]["schedule"]["description"] == "every 10 minutes"
    assert jobs["routines-tick"]["plist_present"] is True


# --------------------------------------------------------------------------- additive contract (2026-08-15)


_ORIGINAL_JOB_FIELDS = {
    "key",
    "name",
    "executor",
    "category",
    "label",
    "purpose",
    "source",
    "module",
    "schedule",
    "env_overrides",
    "loaded",
    "plist_present",
    "last_exit_status",
    "last_run_at",
    "next_fire_at",
    "health",
    "health_reason",
    "managed_candidate",
    "candidate_reason",
}
_ORIGINAL_COUNT_FIELDS = {"total", "loaded", "loaded_unknown", "failing", "stale"}
_ORIGINAL_SNAPSHOT_FIELDS = {"generated_at", "launchctl", "counts", "jobs"}


def test_additive_contract_every_pre_existing_field_still_present(tmp_path: Path) -> None:
    """Frozen-contract regression sentinel (plan.md): every field that existed
    before the 2026-08-15 loops-health-ui work is STILL present on every job
    and on the snapshot/counts — new fields must be additive-only."""
    snap = list_system_jobs(repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={})
    assert _ORIGINAL_SNAPSHOT_FIELDS <= snap.keys()
    assert _ORIGINAL_COUNT_FIELDS <= snap["counts"].keys()
    for job in snap["jobs"]:
        assert _ORIGINAL_JOB_FIELDS <= job.keys(), job["key"]
        assert "last_result" in job  # the new additive field, present (nullable) everywhere


def test_counts_gain_healthy_unknown_not_loaded_additively(tmp_path: Path) -> None:
    snap = list_system_jobs(
        repo_root=tmp_path,  # hermetic: no repo logs leak into health
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={"com.omniagentos.routines": 0},
    )
    counts = snap["counts"]
    for field_name in ("healthy", "unknown", "not_loaded"):
        assert field_name in counts
    # Internally consistent: every job's health lands in exactly one bucket.
    total_bucketed = (
        counts["healthy"] + counts["unknown"] + counts["not_loaded"] + counts["failing"] + counts["stale"]
    )
    assert total_bucketed == counts["total"]


def test_snapshot_gains_remote_probe_field_default_pending(tmp_path: Path) -> None:
    snap = list_system_jobs(repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={})
    assert snap["remote_probe"]["available"] is False
    assert "not configured" in snap["remote_probe"]["reason"]
    assert snap["remote_probe"]["probed_at"] is None


# --------------------------------------------------------------------------- local crontab source


def test_crontab_list_parses_lines_and_skips_comments() -> None:
    class _Result:
        returncode = 0
        stdout = "# a comment\n0 * * * * ~/.grok/watchdog-xai-poster.sh >> ~/.grok/watchdog.log 2>&1\n\n"
        stderr = ""

    probe = crontab_list(runner=lambda *a, **k: _Result())
    assert probe.available is True
    assert probe.lines == ("0 * * * * ~/.grok/watchdog-xai-poster.sh >> ~/.grok/watchdog.log 2>&1",)


def test_crontab_list_no_crontab_for_user_is_an_empty_success() -> None:
    """`crontab -l` exits non-zero with "no crontab for <user>" when none is
    installed — that is an ABSENCE, not a probe failure, and must not be
    reported as unavailable."""

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "no crontab for youruser"

    probe = crontab_list(runner=lambda *a, **k: _Result())
    assert probe.available is True
    assert probe.lines == ()


@pytest.mark.parametrize(
    ("runner", "expected_fragment"),
    [
        (_raise(FileNotFoundError("crontab missing")), "not found"),
        (_raise(subprocess.TimeoutExpired(cmd="crontab", timeout=3)), "timed out"),
        (_raise(OSError("boom")), "could not be run"),
        (lambda *a, **k: _Rc(2), "exited 2"),
    ],
)
def test_crontab_list_probe_failures_are_unavailable_and_named(runner: object, expected_fragment: str) -> None:
    probe = crontab_list(runner=runner)
    assert probe.available is False
    assert probe.lines == ()
    assert expected_fragment in probe.reason


def test_parse_crontab_known_watchdog_line_gets_a_sensible_purpose_and_cron_schedule() -> None:
    lines = ("0 * * * * ~/.grok/watchdog-xai-poster.sh >> ~/.grok/watchdog.log 2>&1",)
    jobs = parse_crontab(lines, NOW)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["executor"] == "local_cron"
    assert job["category"] == "Automation crew"
    assert "X/xAI" in job["purpose"]
    assert job["schedule"]["kind"] == "cron"
    assert job["schedule"]["description"] == "cron 0 * * * *"
    assert job["next_fire_at"] is not None  # cron machinery derives a real next fire


def test_parse_crontab_health_uses_log_redirect_mtime_when_present(tmp_path: Path) -> None:
    log = tmp_path / "watchdog.log"
    log.write_text("ok", encoding="utf-8")
    os.utime(log, (NOW.timestamp() - 60, NOW.timestamp() - 60))  # 1 minute old, well within cadence
    line = f"0 * * * * ~/.grok/watchdog-xai-poster.sh >> {log} 2>&1"
    job = parse_crontab((line,), NOW)[0]
    assert job["health"] == "healthy"
    assert job["last_run_at"] is not None


def test_parse_crontab_health_is_stale_past_double_cadence(tmp_path: Path) -> None:
    log = tmp_path / "watchdog.log"
    log.write_text("ok", encoding="utf-8")
    stale_time = (NOW - timedelta(hours=3)).timestamp()
    os.utime(log, (stale_time, stale_time))  # hourly cron, no output in 3h > 2x cadence
    line = f"0 * * * * ~/.grok/watchdog-xai-poster.sh >> {log} 2>&1"
    job = parse_crontab((line,), NOW)[0]
    assert job["health"] == "stale"


def test_parse_crontab_health_is_unknown_without_a_log_redirect() -> None:
    job = parse_crontab(("*/5 * * * * /usr/bin/true",), NOW)[0]
    assert job["health"] == "unknown"
    assert "cannot be honestly derived" in job["health_reason"]
    # No recon narrative for this made-up command -> explicitly inferred, not invented.
    assert "Inferred" in job["purpose"]


def test_parse_crontab_skips_malformed_lines() -> None:
    assert parse_crontab(("not a valid cron line",), NOW) == []


def test_list_system_jobs_wires_the_crontab_source_in_additively(tmp_path: Path) -> None:
    line = "0 * * * * ~/.grok/watchdog-xai-poster.sh >> ~/.grok/watchdog.log 2>&1"
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={},
        crontab=(line,),
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert "local-cron-watchdog-xai-poster-sh" in jobs
    assert jobs["local-cron-watchdog-xai-poster-sh"]["executor"] == "local_cron"
    # Every pre-existing catalog+CSI job is still present — additive, not a replacement.
    assert len(jobs) == len(CATALOG) + 8 + 1


def test_list_system_jobs_default_crontab_is_empty_never_touches_real_machine(tmp_path: Path) -> None:
    """No `crontab=` kwarg must never shell out for real — list_system_jobs
    stays pure/hermetic; the real read only happens in cached_list_system_jobs."""
    snap = list_system_jobs(repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={})
    assert not any(j["executor"] == "local_cron" for j in snap["jobs"])


# --------------------------------------------------------------------------- remote probe wiring


def _remote_snapshot(parsed: ParsedRemoteProbe) -> RemoteProbeSnapshot:
    return RemoteProbeSnapshot(True, "", parsed.probed_at, parsed)


def test_list_system_jobs_chargeblast_healthy_from_remote_probe(tmp_path: Path) -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={"initech-crm-chargeblast-auto-refund-1": "Up 3 days"},
        log_tail={"initech-crm-chargeblast-auto-refund-1": ['{"refunded": 3, "status": "ok"}']},
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, remote_probe=_remote_snapshot(parsed)
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    job = jobs["chargeblast-auto-refund"]
    assert job["health"] == "healthy"
    assert job["last_result"] == '{"refunded": 3, "status": "ok"}'


def test_list_system_jobs_chargeblast_failing_when_container_absent(tmp_path: Path) -> None:
    parsed = ParsedRemoteProbe(ok=True, error="", probed_at="2026-08-15T06:00:00Z")
    snap = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, remote_probe=_remote_snapshot(parsed)
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert jobs["chargeblast-auto-refund"]["health"] == "failing"
    assert jobs["chargeblast-reconcile"]["health"] == "failing"


def test_list_system_jobs_chargeblast_failing_when_log_tail_shows_error(tmp_path: Path) -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={"initech-crm-chargeblast-reconcile-1": "Up 1 hour"},
        log_tail={"initech-crm-chargeblast-reconcile-1": ["Traceback (most recent call last):", "ValueError"]},
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, remote_probe=_remote_snapshot(parsed)
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert jobs["chargeblast-reconcile"]["health"] == "failing"


def test_list_system_jobs_remote_cron_present_gives_unknown_with_evidence(tmp_path: Path) -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        crontab_lines=("0 8,20 * * * /opt/kb-maintain.sh --check",),
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, remote_probe=_remote_snapshot(parsed)
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    job = jobs["kb-drift-check"]
    assert job["health"] == "unknown"
    assert "Confirmed present" in job["health_reason"]
    assert job["last_result"] == "0 8,20 * * * /opt/kb-maintain.sh --check"


def test_list_system_jobs_remote_probe_pending_keeps_honest_unknown(tmp_path: Path) -> None:
    pending = RemoteProbeSnapshot(False, "remote probe pending/failed: no successful probe yet.", None, None)
    snap = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, remote_probe=pending
    )
    jobs = {j["key"]: j for j in snap["jobs"]}
    assert jobs["chargeblast-auto-refund"]["health"] == "unknown"
    assert "pending/failed" in jobs["chargeblast-auto-refund"]["health_reason"]
    assert snap["remote_probe"]["available"] is False


# --------------------------------------------------------------------------- discovered-job enrichment


def test_discovered_job_purpose_and_category_enrichment(tmp_path: Path) -> None:
    (tmp_path / "com.omniagentos.otelcol.plist").write_text(
        """<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.otelcol</string>
</dict></plist>""",
        encoding="utf-8",
    )
    snap = list_system_jobs(repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={})
    jobs = {j["key"]: j for j in snap["jobs"]}
    otelcol = jobs["discovered-com.omniagentos.otelcol"]
    assert otelcol["category"] == "Ops / maintenance"
    assert "OpenTelemetry" in otelcol["purpose"]


def test_discovered_job_unknown_label_keeps_the_generic_fallback(tmp_path: Path) -> None:
    (tmp_path / "com.omniagentos.some-brand-new-job.plist").write_text(
        """<?xml version="1.0"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.omniagentos.some-brand-new-job</string>
</dict></plist>""",
        encoding="utf-8",
    )
    snap = list_system_jobs(repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={})
    jobs = {j["key"]: j for j in snap["jobs"]}
    job = jobs["discovered-com.omniagentos.some-brand-new-job"]
    assert job["category"] == "Discovered (no repo definition)"


# --------------------------------------------------------------------------- SnapshotCache (TTL)


def test_snapshot_cache_serves_cached_result_within_ttl() -> None:
    calls = {"n": 0}
    clock_time = {"now": NOW}

    def builder() -> dict[str, object]:
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = SnapshotCache(ttl_s=30.0, clock=lambda: clock_time["now"])
    first = cache.get(builder)
    second = cache.get(builder)
    assert first == second == {"n": 1}
    assert calls["n"] == 1  # second call served from cache, no rebuild


def test_snapshot_cache_rebuilds_after_ttl_expires() -> None:
    calls = {"n": 0}
    clock_time = {"now": NOW}

    def builder() -> dict[str, object]:
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = SnapshotCache(ttl_s=30.0, clock=lambda: clock_time["now"])
    cache.get(builder)
    clock_time["now"] = NOW + timedelta(seconds=31)
    result = cache.get(builder)
    assert result == {"n": 2}
    assert calls["n"] == 2


def test_snapshot_cache_fresh_bypasses_the_ttl() -> None:
    calls = {"n": 0}

    def builder() -> dict[str, object]:
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = SnapshotCache(ttl_s=30.0, clock=lambda: NOW)
    cache.get(builder)
    result = cache.get(builder, fresh=True)
    assert result == {"n": 2}
    assert calls["n"] == 2


def test_snapshot_cache_true_single_flight_concurrent_misses_serialize() -> None:
    """CONC-01: two concurrent callers hitting an empty/expired cache must
    invoke `builder` exactly once — the second blocks on the lock and then
    gets served the first's freshly-cached result, never rebuilds."""
    calls = {"n": 0}
    lock = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()

    def builder() -> dict[str, int]:
        with lock:
            calls["n"] += 1
            call_number = calls["n"]
        if call_number == 1:
            first_entered.set()
            release_first.wait(timeout=2)
        return {"call": call_number}

    cache = SnapshotCache(clock=lambda: NOW)
    t1 = threading.Thread(target=lambda: cache.get(builder))
    t2 = threading.Thread(target=lambda: cache.get(builder))
    t1.start()
    assert first_entered.wait(timeout=2)
    t2.start()
    t2.join(timeout=1)  # t2 should still be blocked on the lock, not finished
    release_first.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- OBS-02: local_cron availability


def test_snapshot_gains_local_cron_availability_field(tmp_path: Path) -> None:
    genuine_empty = list_system_jobs(
        repo_root=REPO_ROOT, launchd_dir=tmp_path, now=NOW, launchctl={}, crontab=CrontabProbe((), True, "")
    )
    timed_out = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={},
        crontab=CrontabProbe((), False, "crontab -l timed out"),
    )
    assert genuine_empty["local_cron"] == {"available": True, "reason": ""}
    assert timed_out["local_cron"]["available"] is False
    assert "timed out" in timed_out["local_cron"]["reason"]
    # A failed crontab read must never be silently indistinguishable from a
    # genuinely empty one — the two full snapshots must differ.
    assert genuine_empty != timed_out
    # And it must not fabricate local_cron jobs it never measured.
    assert not any(j["executor"] == "local_cron" for j in timed_out["jobs"])


# --------------------------------------------------------------------------- CONTRACT-01: remote cron mtime evidence


def test_list_system_jobs_remote_cron_uses_captured_mtime_for_health() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T08:05:00Z",
        crontab_lines=("0 8,20 * * * /srv/initech-crm/scripts/dev/kb-maintain.sh",),
        cron_log_mtimes={"/srv/initech-crm/logs/kb-drift-cron.log": "2026-08-15T08:00:04Z"},
    )
    now = datetime(2026, 8, 15, 8, 5, 0, tzinfo=UTC)
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=REPO_ROOT / "does-not-exist",
        now=now,
        launchctl={},
        remote_probe=RemoteProbeSnapshot(True, "", parsed.probed_at, parsed),
    )
    job = {j["key"]: j for j in snap["jobs"]}["kb-drift-check"]
    assert job["health"] == "healthy"
    assert job["last_run_at"] == "2026-08-15T08:00:04Z"


def test_list_system_jobs_remote_cron_mtime_stale_past_2x_cadence() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-16T08:05:00Z",
        crontab_lines=("0 8,20 * * * /srv/initech-crm/scripts/dev/kb-maintain.sh",),
        cron_log_mtimes={"/srv/initech-crm/logs/kb-drift-cron.log": "2026-08-15T08:00:04Z"},
    )
    now = datetime(2026, 8, 16, 8, 5, 0, tzinfo=UTC)  # 24h later; cadence is 12h
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=REPO_ROOT / "does-not-exist",
        now=now,
        launchctl={},
        remote_probe=RemoteProbeSnapshot(True, "", parsed.probed_at, parsed),
    )
    job = {j["key"]: j for j in snap["jobs"]}["kb-drift-check"]
    assert job["health"] == "stale"


def test_list_system_jobs_remote_cron_without_mtime_evidence_stays_unknown(tmp_path: Path) -> None:
    """A remote cron with NO grounded log path (the 4 others besides
    kb-drift-check) must stay honestly unknown even when confirmed present."""
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        crontab_lines=("7 */6 * * * /opt/stripe-stats-refresh.sh",),
    )
    snap = list_system_jobs(
        repo_root=REPO_ROOT,
        launchd_dir=tmp_path,
        now=NOW,
        launchctl={},
        remote_probe=RemoteProbeSnapshot(True, "", parsed.probed_at, parsed),
    )
    job = {j["key"]: j for j in snap["jobs"]}["public-stripe-stats-refresh"]
    assert job["health"] == "unknown"


# --------------------------------------------------------------------------- CRON-01: MAILTO / nicknames / collisions


def test_parse_crontab_skips_env_lines_but_propagates_the_name_to_every_job() -> None:
    lines = (
        "MAILTO=ops@example.test",
        "@reboot /opt/bootstrap.sh",
        "0 * * * * /usr/bin/true",
        "30 * * * * /usr/bin/true",
    )
    jobs = parse_crontab(lines, NOW)
    assert len(jobs) == 3  # the MAILTO line is not a job
    keys = [job["key"] for job in jobs]
    assert len(keys) == len(set(keys))  # collision-proof even with a repeated command
    assert any(job["module"] == "/opt/bootstrap.sh" for job in jobs)
    assert all("MAILTO" in job["env_overrides"] for job in jobs)


def test_parse_crontab_reboot_nickname_has_no_cadence_and_is_honestly_unknown() -> None:
    job = parse_crontab(("@reboot /opt/bootstrap.sh",), NOW)[0]
    assert job["schedule"]["kind"] == "unknown"
    assert job["health"] == "unknown"
    assert "reboot" in job["health_reason"].lower()
    assert job["next_fire_at"] is None


@pytest.mark.parametrize(
    ("nickname", "expected_cron"),
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
    ],
)
def test_parse_crontab_supports_standard_nicknames(nickname: str, expected_cron: str) -> None:
    job = parse_crontab((f"{nickname} /usr/bin/true",), NOW)[0]
    assert job["schedule"]["kind"] == "cron"
    assert job["schedule"]["description"] == f"cron {expected_cron}"


def test_parse_crontab_unrecognized_nickname_is_skipped_not_guessed() -> None:
    assert parse_crontab(("@notarealnickname /usr/bin/true",), NOW) == []


def test_parse_crontab_key_collision_across_two_cadences_same_command() -> None:
    jobs = parse_crontab(("0 * * * * /usr/bin/true", "30 * * * * /usr/bin/true"), NOW)
    keys = {job["key"] for job in jobs}
    assert len(keys) == 2
