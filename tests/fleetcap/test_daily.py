import sqlite3
import time
from datetime import date
from pathlib import Path

from omniagentos.fleetcap import daily
from omniagentos.fleetcap.migrate import migrate


def test_daily_writes_brief_posts_sanitized_compact_text(
    tmp_path: Path, monkeypatch, real_fleet_db: Path
) -> None:
    db = real_fleet_db
    connection = sqlite3.connect(db)
    migrate(connection)
    now = time.time()
    for index in range(3):
        connection.execute(
            "INSERT INTO sessions (session_id,cli,device,dispatcher,dispatch_class,outcome,"
            "n_err,wall_s,model_s,tool_s,active_s,events,tools,cwd,end_ts,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"session-{index}",
                "claude",
                "laptop",
                "<!channel>",
                "daemon",
                "failed?",
                1,
                7200,
                3600,
                1800,
                5400,
                "permission denied",
                "{}",
                "/job",
                now,
                now,
            ),
        )
    connection.commit()
    connection.close()
    config = tmp_path / "devices.yaml"
    config.write_text('{"devices":[{"device":"laptop","owner":"emp_owner"}]}')
    posted: list[str] = []
    monkeypatch.setattr(daily, "post", lambda text, **_kwargs: posted.append(text) or True)
    brief_path, slack = daily.run(
        db,
        day=date(2026, 8, 13),
        output_dir=tmp_path / "briefs",
        ingest_root=tmp_path / "ingest",
        config=config,
        reflection_root=tmp_path / "reflection",
        dry_run=False,
        now=now,
    )
    assert brief_path.exists()
    assert "## Coverage (capture health)" in brief_path.read_text()
    assert "<!channel>" not in slack
    assert "[mention omitted]" in brief_path.read_text()
    assert len(slack.splitlines()) <= 15
    assert posted == [slack]


def test_daily_reports_hook_evidence_coverage(tmp_path: Path) -> None:
    rows = [
        {"dispatch_evidence": "interactive hook evidence: tty='ttys001'"},
        {"dispatch_evidence": "n_user>0, no hook evidence"},
    ]
    brief, slack = daily.render(
        rows,
        day=date(2026, 8, 13),
        ingest_root=tmp_path,
        devices=[],
        brief_path=tmp_path / "brief.md",
    )
    assert "Hook evidence: 1/2 sessions (50.0%)" in brief
    assert "Hook evidence: 1/2 sessions (50.0%)" in slack


def test_missing_metrics_are_reported_unavailable(real_fleet_db: Path, tmp_path: Path) -> None:
    connection = sqlite3.connect(real_fleet_db)
    connection.execute(
        "INSERT INTO sessions (session_id,cli,created_ts) VALUES ('old-schema-row','claude',1)"
    )
    rows = daily._rows(connection, 0)
    brief, _ = daily.render(
        rows,
        day=date(2026, 8, 13),
        ingest_root=tmp_path,
        devices=[],
        brief_path=tmp_path / "brief.md",
    )
    assert "Permission-prompt hotspots: unavailable (column absent)" in brief
    assert "tool unavailable (column absent)" in brief


def test_zero_session_window_reports_zero_not_unavailable(
    real_fleet_db: Path, tmp_path: Path
) -> None:
    connection = sqlite3.connect(real_fleet_db)
    rows = daily._rows(connection, time.time() + 100)
    brief, _ = daily.render(
        rows,
        day=date(2026, 8, 13),
        ingest_root=tmp_path,
        devices=[],
        brief_path=tmp_path / "brief.md",
    )
    assert "0 sessions" in brief
    assert "Permission-prompt hotspots: unavailable (column absent)" in brief


def test_permission_metric_fires_after_migration(real_fleet_db: Path, tmp_path: Path) -> None:
    connection = sqlite3.connect(real_fleet_db)
    migrate(connection)
    now = time.time()
    connection.execute(
        "INSERT INTO sessions (session_id,cli,created_ts,end_ts,events,tools,tool_s) "
        "VALUES ('permission-row','claude',?,?,?,?,?)",
        (now, now, '["permission denied"]', "{}", 12),
    )
    rows = daily._rows(connection, now - 1)
    brief, _ = daily.render(
        rows,
        day=date(2026, 8, 13),
        ingest_root=tmp_path,
        devices=[],
        brief_path=tmp_path / "brief.md",
    )
    assert "Permission-prompt hotspots: 1 sessions" in brief
    assert "tool 0.0h" in brief


def test_missing_config_is_explicit_in_brief(real_fleet_db: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing-devices.yaml"
    brief_path, _ = daily.run(
        real_fleet_db,
        day=date(2026, 8, 13),
        output_dir=tmp_path / "briefs",
        ingest_root=tmp_path / "ingest",
        config=missing,
        reflection_root=tmp_path / "reflection",
        dry_run=True,
    )
    assert f"config unavailable ({missing})" in brief_path.read_text()


def test_daily_defaults_are_repo_anchored_from_unrelated_cwd(
    real_fleet_db: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    brief_path, _ = daily.run(
        real_fleet_db,
        day=date(2026, 8, 13),
        output_dir=tmp_path / "briefs",
        ingest_root=tmp_path / "empty-ingest",
        config=daily.DEFAULT_CONFIG,
        reflection_root=daily.DEFAULT_REFLECTION_ROOT,
        dry_run=True,
    )
    brief = brief_path.read_text()
    assert "mw0001-owner" in brief
    assert "Dark >24h: none" not in brief


def test_sanitize_all_estate_secret_shapes_and_newlines() -> None:
    shapes = [
        "xai-LIVE",
        "ghp_LIVE",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaLIVE",
        "pit-LIVE",
        "EAALIVE",
        "-----BEGIN RSA PRIVATE KEY-----",
        "sk-ant-LIVE",
        "xoxb-1-2",
    ]
    assert all("omitted" in daily.sanitize(shape) for shape in shapes)
    assert "\n" not in daily.sanitize("one\ntwo")


def test_post_failure_returns_nonzero(tmp_path: Path, monkeypatch, real_fleet_db: Path) -> None:
    config = tmp_path / "devices.yaml"
    config.write_text('{"devices":[]}')
    monkeypatch.setattr(daily, "post", lambda *_args, **_kwargs: False)
    assert (
        daily.main(
            [
                "--db",
                str(real_fleet_db),
                "--output-dir",
                str(tmp_path / "briefs"),
                "--ingest-root",
                str(tmp_path / "ingest"),
                "--config",
                str(config),
                "--reflection-root",
                str(tmp_path / "reflection"),
            ]
        )
        != 0
    )
