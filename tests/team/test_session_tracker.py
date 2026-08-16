"""Session tracker + collector: rendering, metrics, and drop-file mechanics."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.team import session_collector, session_tracker, slack_blocks


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture()
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "var" / "loopqueue" / "proposals").mkdir(parents=True)
    (tmp_path / "var" / "loopqueue" / "candidates").mkdir(parents=True)
    (tmp_path / "var" / "team-sessions").mkdir(parents=True)
    # Pin the resolved var root to the fixture so the tracker and the tests
    # read the same team-sessions directory (OMNIAGENTOS_VAR is the canonical
    # first key in runtime_paths.CANONICAL_VAR_ENV_KEYS).
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    return tmp_path


def _db(tmp_path: Path) -> str:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE task_sessions (id TEXT, ended_at TEXT)"
    )
    connection.execute("CREATE TABLE employees (id TEXT, name TEXT)")
    connection.executemany(
        "INSERT INTO employees VALUES (?, ?)",
        [("emp_owner", "the operator"), ("emp_alice", "Alice"), ("emp_bob", "Bob")],
    )
    connection.executemany(
        "INSERT INTO task_sessions VALUES (?, ?)",
        [("s1", None), ("s2", None), ("s3", "2026-08-11T00:00:00Z")],
    )
    connection.commit()
    connection.close()
    return str(path)


def _ledger(repo_root: Path, events: list[dict[str, Any]]) -> None:
    path = repo_root / "var" / "loopqueue" / "ledger.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_overall_metrics_from_ledger_and_dirs(repo_root: Path, tmp_path: Path) -> None:
    now = time.time()
    (repo_root / "var" / "loopqueue" / "proposals" / "p1.json").write_text("{}")
    (repo_root / "var" / "loopqueue" / "candidates" / "c1.json").write_text("{}")
    _ledger(
        repo_root,
        [
            {"ts": _iso(now - 300), "event": "merged", "id": "a", "detail": {"result": "pass"}},
            {"ts": _iso(now - 400), "event": "gated", "id": "b", "detail": {"result": "fail"}},
            {"ts": _iso(now - 7200), "event": "merged", "id": "old", "detail": {"result": "pass"}},
            {"ts": _iso(now - 500), "event": "submitted", "id": "queued-1", "detail": {}},
            {"ts": _iso(now - 500), "event": "submitted", "id": "a", "detail": {}},
        ],
    )
    overall = session_tracker.gather_overall(repo_root, _db(tmp_path))
    assert overall.proposals == 1
    assert overall.candidates == 1
    assert overall.merged_last_hour == 1  # the old merge is outside the hour
    assert overall.failed_merges_last_hour == 1
    assert overall.merge_queue == 1  # 'a' merged; 'queued-1' still open
    assert overall.active_sessions == 2


def test_bottleneck_first_match(repo_root: Path, tmp_path: Path) -> None:
    now = time.time()
    _ledger(
        repo_root,
        [{"ts": _iso(now - 60), "event": "gated", "id": "x", "detail": {"result": "fail"}}],
    )
    overall = session_tracker.gather_overall(repo_root, _db(tmp_path))
    assert overall.bottleneck.startswith("merge gate red")


def test_render_person_sections_and_staleness(repo_root: Path, tmp_path: Path) -> None:
    now = time.time()
    drop = repo_root / "var" / "team-sessions"
    (drop / "emp_alice.json").write_text(
        json.dumps(
            {
                "employee_id": "emp_alice",
                "host": "alice-mbp",
                "generated_at": _iso(now - 60),
                "sessions": [
                    {
                        "harness": "claude",
                        "id": "abc",
                        "project": "OmniAgentOS",
                        "description": "fix webhook retries",
                        "active": True,
                        "last_active": _iso(now - 60),
                    }
                ],
            }
        )
    )
    (drop / "emp_bob.json").write_text(
        json.dumps(
            {
                "employee_id": "emp_bob",
                "host": "srv-1",
                "generated_at": _iso(now - 3 * 3600),
                "sessions": [],
            }
        )
    )
    reports = session_tracker.read_person_reports(repo_root)
    text = session_tracker.render(
        session_tracker.Overall(),
        [("emp_owner", "the operator"), ("emp_alice", "Alice"), ("emp_bob", "Bob")],
        reports,
    )
    assert "the operator: no session report received" in text
    assert "Alice: 1 active / 1 recent on alice-mbp" in text
    assert "fix webhook retries" in text
    assert "Bob: 0 active / 0 recent on srv-1 (stale report)" in text


def test_collector_scans_claude_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-Users-x-RepoOne"
    project.mkdir(parents=True)
    transcript = project / "session-1.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "build the tracker"}})
        + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    report = session_collector.collect("emp_owner", window_min=75, active_min=15)
    assert report["employee_id"] == "emp_owner"
    assert len(report["sessions"]) == 1
    session = report["sessions"][0]
    assert session["description"] == "build the tracker"
    assert session["active"] is True
    assert session["project"].endswith("RepoOne")


def test_collector_atomic_write_is_readable(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "emp_owner.json"
    session_collector._write_atomic(target, {"schema": 1, "employee_id": "emp_owner"})
    assert json.loads(target.read_text())["employee_id"] == "emp_owner"


def test_webhook_trims_to_fit_never_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_collector, "_post_json", lambda url, payload, token: posted.append(payload) or True
    )
    big = {
        "schema": 1,
        "employee_id": "emp_alice",
        "host": "srv",
        "generated_at": _iso(time.time()),
        "sessions": [
            {"harness": "claude", "id": f"s{i}", "project": "p", "active": True,
             "description": "x" * 140, "last_active": _iso(time.time())}
            for i in range(20)
        ],
    }
    assert session_collector._post_webhook("http://hook", big) is True
    assert len(posted) == 1
    text = posted[0]["text"]
    assert len(text) <= session_collector._WEBHOOK_TEXT_LIMIT
    encoded = text.split(None, 2)[2].split("\n", 1)[0]
    import base64 as b64

    decoded = json.loads(b64.b64decode(encoded))
    assert 0 < len(decoded["sessions"]) < 20  # trimmed, not silently truncated
    blocks = posted[0]["blocks"]
    assert blocks[0]["text"]["text"].startswith("*AI session report — emp_alice*\n")
    assert "active" in blocks[0]["text"]["text"]
    assert "recent" in blocks[0]["text"]["text"]
    assert "older sessions omitted" in blocks[0]["text"]["text"]


def test_future_clocked_report_reads_stale(repo_root: Path) -> None:
    drop = repo_root / "var" / "team-sessions"
    (drop / "emp_alice.json").write_text(
        json.dumps(
            {
                "employee_id": "emp_alice",
                "host": "skewed",
                "generated_at": _iso(time.time() + 3 * 3600),
                "sessions": [],
            }
        )
    )
    reports = session_tracker.read_person_reports(repo_root)
    assert reports["emp_alice"]["_age_seconds"] > session_tracker.DROP_FRESH_SECONDS


def test_ledger_tolerates_torn_lines(repo_root: Path, tmp_path: Path) -> None:
    now = time.time()
    path = repo_root / "var" / "loopqueue" / "ledger.jsonl"
    good = json.dumps(
        {"ts": _iso(now - 120), "event": "merged", "id": "ok", "detail": {"result": "pass"}}
    )
    path.write_text('{"ts": "2026-08-11T00:00:00Z", "event": "mer\n' + good + "\n{torn tail", encoding="utf-8")
    overall = session_tracker.gather_overall(repo_root, _db(tmp_path))
    assert overall.merged_last_hour == 1


def test_webhook_trim_keeps_most_recent_active(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_collector, "_post_json", lambda url, payload, token: posted.append(payload) or True
    )
    now = time.time()
    big = {
        "schema": 1,
        "employee_id": "emp_alice",
        "host": "srv",
        "generated_at": _iso(now),
        "sessions": [
            {"harness": "claude", "id": f"s{i}", "project": "p", "active": True,
             "description": "x" * 140, "last_active": _iso(now - i * 600)}
            for i in range(20)
        ],
    }
    assert session_collector._post_webhook("http://hook", big) is True
    import base64 as b64

    encoded = posted[0]["text"].split(None, 2)[2].split("\n", 1)[0]
    kept = json.loads(b64.b64decode(encoded))["sessions"]
    kept_ids = {s["id"] for s in kept}
    assert "s0" in kept_ids  # the most recent survives the trim
    assert "s19" not in kept_ids  # the stalest is dropped first


def test_slack_ingest_refuses_traversal_employee_id(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64 as b64

    evil = {
        "employee_id": "../../evil",
        "host": "x",
        "generated_at": _iso(time.time()),
        "sessions": [],
    }
    text = "SESSIONREPORT ../../evil " + b64.b64encode(
        json.dumps(evil).encode()
    ).decode("ascii")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"ok": True, "messages": [{"text": text}]}).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=20: _Resp())
    session_tracker.maybe_ingest_slack_reports(repo_root, "C123")
    assert not (repo_root / "var" / "evil.json").exists()
    assert not (repo_root.parent / "evil.json").exists()
    assert not (repo_root / "var" / "team-sessions" / "../../evil.json").resolve().exists()


def test_collector_scans_all_claude_account_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for cfg, session_name, goal in (
        (".claude", "s-default", "default profile work"),
        (".claude-account-2", "s-acct2", "account two work"),
        (".claude-account-12", "s-acct12", "account twelve work"),
    ):
        project = home / cfg / "projects" / "-Users-x-Repo"
        project.mkdir(parents=True)
        (project / f"{session_name}.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": goal}}) + "\n"
        )
    monkeypatch.setenv("HOME", str(home))
    report = session_collector.collect("emp_owner", window_min=75, active_min=15)
    accounts = {s["account"] for s in report["sessions"]}
    assert accounts == {"default", "account-2", "account-12"}
    assert report["recent_count"] == 3
    assert report["active_count"] == 3


def test_collector_scans_named_claude_account_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for account, goal in (
        ("bob_initech", "review the dashboard integration"),
        ("support_hooli", "verify the governance pack"),
    ):
        project = home / ".claude-accounts" / account / "projects" / "-srv-grok"
        project.mkdir(parents=True)
        (project / f"{account}.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": goal}})
            + "\n"
        )
    monkeypatch.setenv("HOME", str(home))

    report = session_collector.collect("emp_bob", window_min=75, active_min=15)

    assert {session["account"] for session in report["sessions"]} == {
        "bob_initech",
        "support_hooli",
    }
    assert report["recent_count"] == 2
    assert report["active_count"] == 2


def test_collector_codex_excluded_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    rollouts = home / ".codex" / "sessions" / "2026" / "08" / "11"
    rollouts.mkdir(parents=True)
    (rollouts / "rollout-1.jsonl").write_text(
        json.dumps({"payload": {"role": "user", "content": "codex subagent work"}}) + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    assert session_collector.collect("emp_owner", 75, 15)["sessions"] == []
    with_codex = session_collector.collect("emp_owner", 75, 15, include_codex=True)
    assert len(with_codex["sessions"]) == 1
    assert with_codex["sessions"][0]["harness"] == "codex"


def test_collector_counts_are_pre_cap_and_cap_keeps_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap must never distort the totals.

    Activity is mtime-derived (active == modified within active_min), so the
    newest files are the active ones by definition; the guarantee under test
    is that active_count/recent_count reflect the FULL scan while the capped
    list keeps the newest entries.
    """
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-Users-x-Repo"
    project.mkdir(parents=True)
    now = time.time()
    for i in range(25):
        f = project / f"s{i}.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": f"goal {i}"}}) + "\n"
        )
        age = 60 * (5 if i < 22 else 30)  # 22 active (5m), 3 in-window inactive (30m)
        os.utime(f, (now - age, now - age))
    monkeypatch.setenv("HOME", str(home))
    report = session_collector.collect("emp_owner", window_min=75, active_min=15)
    assert report["active_count"] == 22  # pre-cap truth, not len(sessions)
    assert report["recent_count"] == 25
    assert len(report["sessions"]) == session_collector.MAX_SESSIONS
    assert sum(1 for s in report["sessions"] if s["active"]) == session_collector.MAX_SESSIONS


def test_collector_ignores_bare_trailing_dash_profile_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stray = home / ".claude-account-" / "projects" / "-Users-x-Repo"
    stray.mkdir(parents=True)
    (stray / "s.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "stray"}}) + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    assert session_collector.collect("emp_owner", 75, 15)["sessions"] == []


def _pool_status() -> dict[str, Any]:
    """A synthetic GET /v1/status payload spanning alive / drained / stale /
    absent-age machines (the flag matrix the report must never get wrong)."""
    return {
        "capacity": {
            "total_cores": 84,
            "total_perf_cores": 72,
            "total_slots": 12,
            "free_slots": 5,
            "in_flight": 7,
        },
        "machines": [
            {"machine_id": "live-box", "ncpu": 16, "load1": 1.68, "in_flight": 2,
             "max_concurrent": 3, "mem_free_gb": 101.6, "mem_gb": 128.0,
             "drain": 0, "last_seen_age_s": 9.0},
            {"machine_id": "drained-box", "ncpu": 24, "load1": 0.1, "in_flight": 0,
             "max_concurrent": 3, "mem_free_gb": 114.7, "mem_gb": 128.0,
             "drain": 1, "last_seen_age_s": 5.0},
            {"machine_id": "dead-box", "ncpu": 8, "load1": 0.0, "in_flight": 0,
             "max_concurrent": 2, "mem_free_gb": 10.0, "mem_gb": 16.0,
             "drain": 0, "last_seen_age_s": 999.0},
            {"machine_id": "noage-box", "ncpu": 8, "load1": None, "in_flight": 0,
             "max_concurrent": 2, "mem_free_gb": None, "mem_gb": 16.0,
             "drain": 0, "last_seen_age_s": None},
        ],
    }


def test_gather_capacity_aggregate_and_machine_flags() -> None:
    cap = session_tracker.gather_capacity(_pool_status())
    assert cap.available is True
    assert cap.aggregate == "🖥️ Pool: 84c · 7/12 slots used · 5 free"
    assert cap.machines[0] == (
        "live-box · 16c · load 1.68 · 2/3 slots · 101.6/128.0GB [alive]"
    )
    # LOAD-BEARING: a drained OR silent (incl. absent-age) box must never read
    # as idle-available — it carries a non-[alive] flag.
    assert cap.machines[1].endswith("[drained]")
    assert cap.machines[2].endswith("[stale]")  # last_seen_age_s > 120
    assert cap.machines[3].endswith("[stale]")  # absent age -> stale, not alive
    assert "?" in cap.machines[3]  # missing numbers read as '?', not a fake 0


def test_machine_flag_nan_age_fails_closed_to_stale() -> None:
    # A corrupt liveness signal (NaN age) must fail CLOSED to [stale]: nan > horizon
    # is False in IEEE-754, which would otherwise read a dead/corrupt box as [alive].
    assert session_tracker._machine_flag(0, float("nan")) == "[stale]"
    assert session_tracker._machine_flag(0, 5.0) == "[alive]"  # genuine live unchanged
    assert session_tracker._machine_flag(1, float("nan")) == "[drained]"  # drain wins


def test_machine_line_null_machine_id_renders_question_mark() -> None:
    # An explicit-null machine_id must render '?' (dict.get default only covers an
    # ABSENT key, not present-with-None) — never the literal string "None".
    cap = session_tracker.gather_capacity(
        {
            "capacity": {"total_cores": 8, "total_slots": 2, "free_slots": 2, "in_flight": 0},
            "machines": [{"machine_id": None, "ncpu": 8, "last_seen_age_s": 5}],
        }
    )
    assert cap.machines[0].startswith("? · ")
    assert "None" not in cap.machines[0]


def test_gather_capacity_unavailable_never_raises() -> None:
    # Status missing, malformed, or keys absent — every one degrades to the
    # marker, none raises (the hourly report must still post).
    for bad in (
        {},
        "garbage",
        {"capacity": {}, "machines": []},
        {"capacity": {"total_cores": 1}, "machines": []},  # partial keys
        {"capacity": None, "machines": None},
    ):
        cap = session_tracker.gather_capacity(bad)
        assert cap.available is False
        assert cap.aggregate == "🖥️ Pool: compute unavailable"
        assert cap.machines == []


def test_gather_capacity_unreachable_server_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default None arg fetches live; a dead/absent server (fetch -> None) must
    # degrade to the marker, not raise.
    monkeypatch.setattr(session_tracker, "_fetch_pool_status", lambda: None)
    cap = session_tracker.gather_capacity()
    assert cap.available is False and cap.machines == []


def test_render_and_slack_capacity_parity() -> None:
    cap = session_tracker.gather_capacity(_pool_status())
    overall = session_tracker.Overall()
    roster = [("emp_owner", "the operator")]
    text = session_tracker.render(overall, roster, {}, cap)
    _, blocks = slack_blocks.tracker_blocks(
        overall, roster, {}, stamp="t", fresh_seconds=7200
    )
    blocks = slack_blocks.append_capacity(blocks, cap.aggregate, cap.machines)
    slack_text = "\n".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    # Same numbers on both surfaces — the styled path can never say more or less
    # than the plaintext one.
    assert cap.aggregate in text and cap.aggregate in slack_text
    for line in cap.machines:
        assert line in text
        assert line in slack_text


def test_render_without_capacity_is_unchanged() -> None:
    # Backwards-compatible default: no capacity arg -> no COMPUTE section.
    text = session_tracker.render(session_tracker.Overall(), [("emp_owner", "the operator")], {})
    assert "COMPUTE" not in text


def test_append_capacity_is_one_group_not_per_machine() -> None:
    cap = session_tracker.gather_capacity(_pool_status())
    base: list[dict[str, Any]] = []
    out = slack_blocks.append_capacity(base, cap.aggregate, cap.machines)
    # One divider + one section regardless of machine count (block-ceiling safe).
    assert len(out) == 2
    assert out[0]["type"] == "divider"
    assert out[1]["type"] == "section"
