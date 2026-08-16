"""Hub ingestion of a LOCAL ai-transcripts clone written by the dev uploader.

The clone is read in place (no rsync, no ingest_root), so every row must carry
the uploading dev's employee id rather than the hub's owner — that attribution
is the entire reason this path exists.
"""

import json
import sqlite3
from pathlib import Path

from omniagentos.fleetcap.extract import extract

CLAUDE_SESSION = (
    '{"type":"user","timestamp":"2026-08-13T10:00:00Z","cwd":"/repo",'
    '"message":{"content":"ship the uploader"}}\n'
    '{"type":"assistant","timestamp":"2026-08-13T10:00:05Z",'
    '"message":{"model":"claude-test","content":"done"}}\n'
)


def _config(tmp_path: Path, clone: Path, **overrides: object) -> Path:
    device: dict[str, object] = {
        "device": "dev-uploads",
        "mode": "local",
        "path": str(clone),
        "owner_map": {"owner": "emp_owner", "bob": "emp_bob", "alice": "emp_alice"},
    }
    device.update(overrides)
    config = tmp_path / "devices.yaml"
    config.write_text(
        json.dumps(
            {
                "hub": {"device": "mac-studio", "owner": "emp_owner"},
                "devices": [device],
            }
        ),
        encoding="utf-8",
    )
    return config


def _upload(clone: Path, dev: str, name: str, body: str = CLAUDE_SESSION) -> Path:
    path = clone / "transcripts" / dev / "2026-08-13" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _run(connection: sqlite3.Connection, tmp_path: Path, config: Path) -> int:
    # An explicit empty source keeps the sweep off this machine's real profiles:
    # every row these tests see comes from the clone under tmp_path.
    return extract(
        connection,
        sources=[tmp_path / "no-native-sources"],
        ingest_root=tmp_path / "ingest",
        spool=tmp_path / "spool",
        config=config,
        since_days=36500,
    )


def test_each_dev_subdir_lands_with_that_devs_employee_id(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    clone = tmp_path / "ai-transcripts"
    _upload(clone, "bob", "mw0001__claude__session-s.jsonl")
    _upload(clone, "alice", "alice-mbp__claude__session-u.jsonl")

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 2
    rows = real_connection.execute(
        "SELECT session_id, device, device_owner, cli, account, capture_method, dispatcher "
        "FROM sessions ORDER BY session_id"
    ).fetchall()
    assert rows == [
        (
            "dev-upload:transcripts/alice/2026-08-13/alice-mbp__claude__session-u.jsonl",
            "dev-upload:alice",
            "emp_alice",
            "claude",
            "alice-mbp",
            "dev-upload-v1",
            "emp_alice",
        ),
        (
            "dev-upload:transcripts/bob/2026-08-13/mw0001__claude__session-s.jsonl",
            "dev-upload:bob",
            "emp_bob",
            "claude",
            "mw0001",
            "dev-upload-v1",
            "emp_bob",
        ),
    ]


def test_same_basename_different_date_are_two_rows(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    """A later upload with the same filename must not overwrite an earlier one."""
    clone = tmp_path / "ai-transcripts"
    for day in ("2026-08-12", "2026-08-13"):
        path = clone / "transcripts" / "bob" / day / "mw0001__claude__s.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CLAUDE_SESSION, encoding="utf-8")

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 2
    assert real_connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2


def test_crafted_non_object_json_skips_one_file_not_the_sweep(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    """A valid-JSON but non-object line must not abort ingestion for everyone."""
    clone = tmp_path / "ai-transcripts"
    _upload(clone, "bob", "mw0001__claude__poison.jsonl", body="[]\n{}\n")
    _upload(clone, "alice", "alice-mbp__claude__good.jsonl")

    # The good file still ingests; the poison one is skipped, not fatal.
    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 1
    owners = [r[0] for r in real_connection.execute("SELECT device_owner FROM sessions")]
    assert owners == ["emp_alice"]


def test_transcript_metrics_are_parsed_not_estimated(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    clone = tmp_path / "ai-transcripts"
    _upload(clone, "owner", "mw0002__claude__session-a.jsonl")

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 1
    row = real_connection.execute(
        "SELECT n_user, n_assistant, wall_s, cwd, models, outcome FROM sessions"
    ).fetchone()
    assert row[:4] == (1, 1, 5.0, "/repo")
    assert json.loads(row[4]) == ["claude-test"]
    assert row[5] == "success?"


def test_kimi_and_codex_uploads_use_their_own_parsers(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    clone = tmp_path / "ai-transcripts"
    _upload(
        clone,
        "bob",
        "box__kimi__sess-7.jsonl",
        '{"type":"turn.prompt","time":1786600000}\n'
        '{"type":"usage.record","time":1786600001,'
        '"usage":{"input_tokens":100,"output_tokens":50}}\n',
    )
    _upload(
        clone,
        "bob",
        "box__codex__rollout-1.jsonl",
        '{"type":"event_msg","timestamp":"2026-08-13T10:00:00Z",'
        '"payload":{"type":"user_message"}}\n',
    )

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 2
    assert real_connection.execute(
        "SELECT cli, tokens_in, tokens_out FROM sessions WHERE cli='kimi'"
    ).fetchone() == ("kimi", 100, 50)
    assert real_connection.execute(
        "SELECT count(*) FROM sessions WHERE cli='codex'"
    ).fetchone() == (1,)


def test_unknown_cli_is_skipped_and_counted_never_coerced(
    tmp_path: Path, real_connection: sqlite3.Connection, capsys
) -> None:
    clone = tmp_path / "ai-transcripts"
    _upload(clone, "bob", "box__aider__run.log", "not a transcript\n")
    _upload(clone, "bob", "box__grok__session.jsonl", "{}\n")
    _upload(clone, "bob", "no-separators.log", "loose file\n")
    _upload(clone, "bob", "box__claude__kept.jsonl")

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 1
    assert real_connection.execute("SELECT count(*) FROM sessions").fetchone() == (1,)
    report = capsys.readouterr().out
    assert "dev-upload skipped 3 file(s)" in report
    assert "aider=1" in report and "grok=1" in report and "unlabelled=1" in report


def test_missing_clone_is_a_clean_no_op(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    config = _config(tmp_path, tmp_path / "never-cloned")

    assert _run(real_connection, tmp_path, config) == 0
    assert real_connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)


def test_empty_clone_is_a_clean_no_op(tmp_path: Path, real_connection: sqlite3.Connection) -> None:
    clone = tmp_path / "ai-transcripts"
    (clone / "transcripts").mkdir(parents=True)

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 0
    assert real_connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)


def test_an_unmapped_dev_dir_still_ingests_without_an_owner(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    clone = tmp_path / "ai-transcripts"
    _upload(clone, "newhire", "box__claude__session-n.jsonl")

    assert _run(real_connection, tmp_path, _config(tmp_path, clone)) == 1
    assert real_connection.execute(
        "SELECT device, device_owner, dispatch_class FROM sessions"
    ).fetchone() == ("dev-upload:newhire", None, "unknown")


def test_reingesting_the_same_upload_updates_one_row(
    tmp_path: Path, real_connection: sqlite3.Connection
) -> None:
    clone = tmp_path / "ai-transcripts"
    path = _upload(clone, "bob", "mw0001__claude__session-s.jsonl")
    config = _config(tmp_path, clone)

    assert _run(real_connection, tmp_path, config) == 1
    path.write_text(
        CLAUDE_SESSION
        + '{"type":"user","timestamp":"2026-08-13T10:05:00Z","message":{"content":"more"}}\n',
        encoding="utf-8",
    )
    assert _run(real_connection, tmp_path, config) == 1
    assert real_connection.execute("SELECT count(*), max(n_user) FROM sessions").fetchone() == (
        1,
        2,
    )


def test_a_local_device_is_never_treated_as_an_rsync_device(tmp_path: Path, monkeypatch) -> None:
    from omniagentos.fleetcap import pull

    monkeypatch.setattr(pull, "_binary", lambda name: f"/usr/bin/{name}")
    config = json.loads(
        (_config(tmp_path, tmp_path / "ai-transcripts")).read_text(encoding="utf-8")
    )
    assert pull.commands(config, tmp_path / "ingest") == []


def test_daily_never_reports_a_local_clone_as_a_dark_device(tmp_path: Path) -> None:
    """A local clone has no ingest_root tree, so freshness there is unmeasurable."""
    from omniagentos.fleetcap import daily

    devices, error = daily._config_devices(_config(tmp_path, tmp_path / "ai-transcripts"))
    assert (devices, error) == ([], None)
