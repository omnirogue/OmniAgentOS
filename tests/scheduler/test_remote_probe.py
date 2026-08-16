"""tests for omniagentos.scheduler.remote_probe — the cached, single-flight,
read-only SSH probe of the initech-roi-calculator host. Everything
machine-dependent (subprocess, disk, the clock, background threads) is
injected; no test here ever shells out to a real `ssh`."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.remote_probe import (
    CHARGEBLAST_AUTO_REFUND,
    CHARGEBLAST_RECONCILE,
    SSH_HOST_ALIAS,
    ParsedRemoteProbe,
    RemoteProbeCache,
    docker_service_health,
    remote_cron_present,
    run_probe,
    sanitize_remote_text,
)

NOW = datetime(2026, 8, 15, 6, 0, 0, tzinfo=UTC)


def _raise(exc: BaseException) -> Callable[..., object]:
    def _runner(*_a: object, **_k: object) -> object:
        raise exc

    return _runner


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_SAMPLE_STDOUT = (
    "===CRON===\n"
    "0 8,20 * * * /opt/kb-maintain.sh --check\n"
    "10 6 * * * /opt/globex-rollout-check.sh\n"
    "===DOCKER_PS===\n"
    f"{CHARGEBLAST_AUTO_REFUND}\tUp 3 days\n"
    f"{CHARGEBLAST_RECONCILE}\tExited (1) 2 hours ago\n"
    "===LOG_REFUND===\n"
    '{"refunded": 2, "status": "ok"}\n'
    "===LOG_RECONCILE===\n"
    "reconcile pass complete\n"
    "===CRON_LOG_MTIMES===\n"
    "1755230400 /var/log/kb-maintain.log\n"
    "===END===\n"
)


# --------------------------------------------------------------------------- run_probe / argv shape


def test_run_probe_never_interpolates_dynamic_data_into_argv() -> None:
    """The whole security surface: the argv passed to the runner must be a
    fixed literal — same list on every call, host alias hardcoded."""
    captured: list[object] = []

    def runner(argv: object, **kwargs: object) -> _Result:
        captured.append(argv)
        return _Result(0, _SAMPLE_STDOUT)

    run_probe(runner=runner, now=NOW)
    run_probe(runner=runner, now=NOW)
    assert captured[0] == captured[1]  # identical argv both times
    argv = captured[0]
    assert argv[0] == "ssh"
    assert "-o" in argv and "BatchMode=yes" in argv and "ConnectTimeout=5" in argv
    assert SSH_HOST_ALIAS in argv
    # No shell metacharacters from a caller could ever reach this list — it is
    # built once at import time from constants, never from a request.


def test_run_probe_parses_all_sections() -> None:
    parsed = run_probe(runner=lambda *a, **k: _Result(0, _SAMPLE_STDOUT), now=NOW)
    assert parsed.ok is True
    assert parsed.crontab_lines == (
        "0 8,20 * * * /opt/kb-maintain.sh --check",
        "10 6 * * * /opt/globex-rollout-check.sh",
    )
    assert parsed.docker_status[CHARGEBLAST_AUTO_REFUND] == "Up 3 days"
    assert parsed.docker_status[CHARGEBLAST_RECONCILE] == "Exited (1) 2 hours ago"
    assert parsed.log_tail[CHARGEBLAST_AUTO_REFUND] == ['{"refunded": 2, "status": "ok"}']
    assert parsed.cron_log_mtimes["/var/log/kb-maintain.log"] == "2025-08-15T04:00:00Z"
    assert parsed.probed_at == "2026-08-15T06:00:00Z"


def test_run_probe_rejects_a_completely_empty_result_as_not_a_measurement() -> None:
    """OBS-01: on the real host 2 chargeblast containers always run, so every
    section coming back empty is indistinguishable from every remote command
    silently failing behind its own `|| true` — never confidently claimed as
    "nothing is scheduled here"."""
    stdout = "===CRON===\n===DOCKER_PS===\n===LOG_REFUND===\n===LOG_RECONCILE===\n===CRON_LOG_MTIMES===\n===END===\n"
    parsed = run_probe(runner=lambda *a, **k: _Result(0, stdout), now=NOW)
    assert parsed.ok is False
    assert "empty" in parsed.error.lower()


def test_run_probe_requires_the_end_sentinel() -> None:
    """OBS-01: a dropped connection mid-stream must read as truncated, never
    as a (partial) successful measurement."""
    truncated = "===CRON===\n0 * * * * /job\n===DOCKER_PS===\n"
    parsed = run_probe(runner=lambda *a, **k: _Result(0, truncated), now=NOW)
    assert parsed.ok is False
    assert "truncated" in parsed.error.lower() or "===end===" in parsed.error.lower()


def test_run_probe_empty_stdout_is_also_rejected() -> None:
    parsed = run_probe(runner=lambda *a, **k: _Result(0, ""), now=NOW)
    assert parsed.ok is False


@pytest.mark.parametrize(
    ("runner", "expected_fragment"),
    [
        (_raise(FileNotFoundError("ssh missing")), "not found"),
        (_raise(subprocess.TimeoutExpired(cmd="ssh", timeout=10)), "timed out"),
        (_raise(OSError("boom")), "could not be run"),
        (lambda *a, **k: _Result(255, "", "Connection refused"), "exited 255"),
    ],
)
def test_run_probe_failure_modes_never_raise_and_name_the_cause(
    runner: object, expected_fragment: str
) -> None:
    """Never raises — every SSH failure degrades to ok=False with a reason."""
    parsed = run_probe(runner=runner, now=NOW)
    assert parsed.ok is False
    assert parsed.crontab_lines == ()
    assert parsed.docker_status == {}
    assert expected_fragment in parsed.error


# --------------------------------------------------------------------------- health derivation


def test_docker_service_health_healthy_when_up_and_normal_log() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 3 days"},
        log_tail={CHARGEBLAST_AUTO_REFUND: ['{"refunded": 2, "status": "ok"}']},
    )
    health, reason, last_result = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "healthy"
    assert last_result == '{"refunded": 2, "status": "ok"}'
    assert "Up 3 days" in reason


def test_docker_service_health_failing_when_container_absent() -> None:
    parsed = ParsedRemoteProbe(ok=True, error="", probed_at="2026-08-15T06:00:00Z")
    health, reason, last_result = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "failing"
    assert last_result is None
    assert "not present" in reason


def test_docker_service_health_failing_when_exited() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_RECONCILE: "Exited (1) 2 hours ago"},
    )
    health, reason, last_result = docker_service_health(parsed, CHARGEBLAST_RECONCILE)
    assert health == "failing"
    assert last_result == "Exited (1) 2 hours ago"


def test_docker_service_health_failing_when_log_tail_looks_like_an_error() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 10 minutes"},
        log_tail={CHARGEBLAST_AUTO_REFUND: ["Traceback (most recent call last):", "KeyError: 'amount'"]},
    )
    health, reason, _ = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "failing"
    assert "error" in reason.lower()


def test_docker_service_health_unknown_when_up_but_no_log_captured() -> None:
    parsed = ParsedRemoteProbe(
        ok=True, error="", probed_at="2026-08-15T06:00:00Z", docker_status={CHARGEBLAST_AUTO_REFUND: "Up 1 minute"}
    )
    health, _, _ = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "unknown"


def test_remote_cron_present_matches_fragment() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        crontab_lines=("0 8,20 * * * /opt/kb-maintain.sh --check",),
    )
    present, line = remote_cron_present(parsed, "kb-maintain")
    assert present is True
    assert "kb-maintain" in line
    absent, empty_line = remote_cron_present(parsed, "stripe")
    assert absent is False
    assert empty_line == ""


# --------------------------------------------------------------------------- RemoteProbeCache: TTL + single-flight


def test_cache_reports_pending_until_first_successful_probe(tmp_path: Path) -> None:
    cache = RemoteProbeCache(
        cache_path=tmp_path / "remote.json",
        runner=lambda *a, **k: _Result(255, "", "unreachable"),
        clock=lambda: NOW,
        background=False,  # deterministic: refresh happens inline, no thread
    )
    snap = cache.get()
    assert snap.available is False
    assert "pending/failed" in snap.reason
    assert snap.parsed is None


def test_cache_serves_fresh_result_without_reprobing(tmp_path: Path) -> None:
    calls = {"n": 0}

    def runner(*a: object, **k: object) -> _Result:
        calls["n"] += 1
        return _Result(0, _SAMPLE_STDOUT)

    clock_time = {"now": NOW}
    cache = RemoteProbeCache(
        cache_path=tmp_path / "remote.json", runner=runner, clock=lambda: clock_time["now"], background=False
    )
    # background=False makes the very first .get() perform its refresh
    # synchronously, so the caller sees the outcome immediately — no need for
    # a second call just to observe a probe that already finished.
    first = cache.get()
    assert first.available is True
    assert first.parsed is not None
    assert calls["n"] == 1
    second = cache.get()  # still fresh — served from cache, no second probe
    assert second.available is True
    assert calls["n"] == 1


def test_cache_refreshes_again_once_ttl_expires(tmp_path: Path) -> None:
    calls = {"n": 0}

    def runner(*a: object, **k: object) -> _Result:
        calls["n"] += 1
        return _Result(0, _SAMPLE_STDOUT)

    clock_time = {"now": NOW}
    cache = RemoteProbeCache(
        cache_path=tmp_path / "remote.json",
        runner=runner,
        clock=lambda: clock_time["now"],
        ttl_s=600,
        background=False,
    )
    cache.get()
    cache.get()  # populates + confirms fresh
    assert calls["n"] == 1
    clock_time["now"] = NOW + timedelta(seconds=601)
    stale_snapshot = cache.get()  # stale cache still served instantly...
    assert stale_snapshot.available is True  # ...but a refresh was triggered synchronously
    assert calls["n"] == 2


def test_cache_persists_to_disk_and_survives_a_new_instance(tmp_path: Path) -> None:
    cache_path = tmp_path / "remote.json"
    cache1 = RemoteProbeCache(
        cache_path=cache_path, runner=lambda *a, **k: _Result(0, _SAMPLE_STDOUT), clock=lambda: NOW, background=False
    )
    cache1.get()
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert CHARGEBLAST_AUTO_REFUND in data["docker_status"]

    # A brand-new cache instance loads the persisted result before any probe runs.
    calls = {"n": 0}

    def runner(*a: object, **k: object) -> _Result:
        calls["n"] += 1
        return _Result(0, _SAMPLE_STDOUT)

    cache2 = RemoteProbeCache(cache_path=cache_path, runner=runner, clock=lambda: NOW, background=False)
    snap = cache2.get()
    assert snap.available is True
    assert calls["n"] == 0  # served straight from disk, no probe needed


def test_cache_single_flight_guard_skips_a_refresh_already_in_progress(tmp_path: Path) -> None:
    """A second caller arriving while a refresh is in flight must not trigger
    a second ssh process — the `_refreshing` flag is the single-flight guard."""
    calls = {"n": 0}

    def runner(*a: object, **k: object) -> _Result:
        calls["n"] += 1
        return _Result(0, _SAMPLE_STDOUT)

    cache = RemoteProbeCache(cache_path=tmp_path / "remote.json", runner=runner, clock=lambda: NOW, background=False)
    # Simulate "a refresh is already in flight" directly on the guard.
    with cache._lock:  # noqa: SLF001 - exercising the internal guard is the point of this test
        cache._refreshing = True
    cache._trigger_refresh()  # noqa: SLF001
    assert calls["n"] == 0  # guarded out — no probe was run


def test_cache_background_refresh_runs_in_a_real_thread_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With background=True (the production default), a stale/empty cache
    kicks a real thread. Verified deterministically by capturing the actual
    Thread object and `.join()`-ing it (a bounded blocking wait) — never a
    hand-rolled sleep/poll loop."""
    calls = {"n": 0}
    threads: list[threading.Thread] = []
    real_thread_cls = threading.Thread

    def runner(*a: object, **k: object) -> _Result:
        calls["n"] += 1
        return _Result(0, _SAMPLE_STDOUT)

    def capturing_thread(*a: object, **k: object) -> threading.Thread:
        thread = real_thread_cls(*a, **k)
        threads.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", capturing_thread)
    cache = RemoteProbeCache(cache_path=tmp_path / "remote.json", runner=runner, clock=lambda: NOW, background=True)
    cache.get()
    # get() must never call the runner itself (only the spawned thread does) —
    # a genuinely blocking implementation would have called it inline before
    # ever reaching threading.Thread(...). Whether the fast in-memory fake
    # thread happens to finish before get() returns is a real race and not
    # asserted on; what's asserted is that a THREAD, not the caller, ran it.
    assert len(threads) == 1
    threads[0].join(timeout=5)
    assert calls["n"] == 1
    assert cache.get().available is True  # now populated by the background thread


# --------------------------------------------------------------------------- HEALTH-01: stale cache after persistent failure


def test_cache_degrades_to_unavailable_once_evidence_exceeds_2x_ttl_and_refresh_is_failing(
    tmp_path: Path,
) -> None:
    """A once-good snapshot must NOT keep reporting `available=True` forever
    just because it was good once — past 2x TTL with refreshes actively
    failing, the cache must say so honestly instead of serving stale-green."""
    old = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T05:00:00Z",  # 1 hour before `now` below
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 3 days"},
        log_tail={CHARGEBLAST_AUTO_REFUND: ['{"ok":true}']},
    )
    cache_path = tmp_path / "remote.json"
    cache_path.write_text(json.dumps(old.to_dict()), encoding="utf-8")

    def timed_out(*_a: object, **_k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

    now = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)
    cache = RemoteProbeCache(cache_path=cache_path, runner=timed_out, clock=lambda: now, ttl_s=600, background=False)
    snap = cache.get()
    assert snap.available is False
    assert "last successful probe at 2026-08-15T05:00:00Z" in snap.reason
    assert "refresh failing since" in snap.reason
    assert "timed out" in snap.reason.lower()


def test_cache_keeps_serving_stale_but_not_yet_2x_ttl_evidence_as_available(tmp_path: Path) -> None:
    """Grace period: mildly stale evidence (past TTL but under 2x TTL) still
    reports available while a background refresh is attempted."""
    old = ParsedRemoteProbe(
        ok=True, error="", probed_at="2026-08-15T05:55:00Z", docker_status={CHARGEBLAST_AUTO_REFUND: "Up 3 days"}
    )
    cache_path = tmp_path / "remote.json"
    cache_path.write_text(json.dumps(old.to_dict()), encoding="utf-8")

    def timed_out(*_a: object, **_k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

    now = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)  # 5 minutes stale; ttl=600s -> not yet 2x
    cache = RemoteProbeCache(cache_path=cache_path, runner=timed_out, clock=lambda: now, ttl_s=600, background=False)
    snap = cache.get()
    assert snap.available is True
    assert snap.parsed is not None


# --------------------------------------------------------------------------- SEC-01: sanitizer + HEALTH-02 docker cases


def test_sanitize_remote_text_redacts_common_live_secret_shapes() -> None:
    cases = {
        "token sk_live_abc123XYZ end": "sk_live_abc123XYZ",
        "token ghp_ABCDEFabcdef1234 end": "ghp_ABCDEFabcdef1234",
        "token xoxb-1234-5678-abcdEF end": "xoxb-1234-5678-abcdEF",
        "key AKIAABCDEFGHIJKLMNO end": "AKIAABCDEFGHIJKLMNO",
        "token pit-abc123 end": "pit-abc123",
        "password=hunter2extra": "password=hunter2extra",
        "API_TOKEN=sk_live_REPRO_ONLY /opt/kb-maintain.sh": "sk_live_REPRO_ONLY",
    }
    for text, secret in cases.items():
        sanitized = sanitize_remote_text(text)
        assert secret not in sanitized, (text, sanitized)
        assert "[redacted]" in sanitized


def test_sanitize_remote_text_redacts_a_private_key_block() -> None:
    block = "prefix -----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY----- suffix"
    sanitized = sanitize_remote_text(block)
    assert "MIIB" not in sanitized
    assert "[redacted]" in sanitized


def test_sanitize_remote_text_clamps_length() -> None:
    sanitized = sanitize_remote_text("x" * 500)
    assert len(sanitized) <= 220
    assert sanitized.startswith("x" * 200)


def test_sanitize_remote_text_is_a_noop_on_ordinary_text() -> None:
    assert sanitize_remote_text('{"refunded": 2, "status": "ok"}') == '{"refunded": 2, "status": "ok"}'


def test_docker_service_health_last_result_and_reason_never_leak_a_secret() -> None:
    secret = "sk_live_REPRO_ONLY"
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 1 minute"},
        log_tail={CHARGEBLAST_AUTO_REFUND: [f"customer=jane@example.test token={secret}"]},
    )
    health, reason, last_result = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert secret not in reason
    assert last_result is None or secret not in last_result


def test_remote_cron_present_never_leaks_a_secret() -> None:
    secret = "sk_live_REPRO_ONLY"
    parsed = ParsedRemoteProbe(
        ok=True, error="", probed_at="2026-08-15T06:00:00Z", crontab_lines=(f"0 8 * * * API_TOKEN={secret} /x.sh",)
    )
    _, line = remote_cron_present(parsed, "x.sh")
    assert secret not in line


def test_docker_service_health_fails_on_explicit_unhealthy_status() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 1 hour (unhealthy)"},
        log_tail={CHARGEBLAST_AUTO_REFUND: ['{"ok":true}']},
    )
    health, _, _ = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "failing"


def test_docker_service_health_unknown_when_logs_are_unreadable_not_healthy() -> None:
    parsed = ParsedRemoteProbe(
        ok=True,
        error="",
        probed_at="2026-08-15T06:00:00Z",
        docker_status={CHARGEBLAST_AUTO_REFUND: "Up 1 hour"},
        log_tail={CHARGEBLAST_AUTO_REFUND: ["permission denied while reading the Docker socket"]},
    )
    health, _, _ = docker_service_health(parsed, CHARGEBLAST_AUTO_REFUND)
    assert health == "unknown"


# --------------------------------------------------------------------------- SEC-02 / SEC-03: cache file safety


def test_cache_write_is_atomic_and_mode_0600_regardless_of_umask(tmp_path: Path) -> None:
    cache_path = tmp_path / "remote.json"
    previous_umask = os.umask(0)
    try:
        RemoteProbeCache(
            cache_path=cache_path, runner=lambda *a, **k: _Result(0, _SAMPLE_STDOUT), clock=lambda: NOW,
            background=False,
        ).refresh_sync()
    finally:
        os.umask(previous_umask)
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == 0o600


def test_cache_write_never_follows_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("DO NOT OVERWRITE", encoding="utf-8")
    link = tmp_path / "remote.json"
    link.symlink_to(victim)
    RemoteProbeCache(
        cache_path=link, runner=lambda *a, **k: _Result(0, _SAMPLE_STDOUT), clock=lambda: NOW, background=False
    ).refresh_sync()
    assert victim.read_text(encoding="utf-8") == "DO NOT OVERWRITE"


def test_malformed_cache_with_non_str_crontab_lines_never_raises(tmp_path: Path) -> None:
    """SEC-03: a tampered/corrupted cache file must degrade, never raise, into
    the caller — a non-str element is DROPPED, not stringified."""
    cache_path = tmp_path / "remote.json"
    cache_path.write_text(
        json.dumps(
            {
                "ok": True,
                "error": "",
                "probed_at": "2026-08-15T06:00:00Z",
                "crontab_lines": [123, "0 * * * * /ok.sh", None, {"nested": True}],
                "docker_status": {},
                "log_tail": {},
                "cron_log_mtimes": {},
            }
        ),
        encoding="utf-8",
    )
    cache = RemoteProbeCache(cache_path=cache_path, clock=lambda: NOW, background=False)
    snap = cache.get()
    assert snap.available is True
    assert snap.parsed is not None
    assert snap.parsed.crontab_lines == ("0 * * * * /ok.sh",)  # non-str entries dropped, valid one kept


def test_parsed_remote_probe_from_dict_returns_none_on_garbage() -> None:
    assert ParsedRemoteProbe.from_dict("not a dict") is None
    assert ParsedRemoteProbe.from_dict(None) is None
    assert ParsedRemoteProbe.from_dict(42) is None
