"""Tests for proxy compatibility benchmark module.

These tests inject a fake runner so no provider CLI is ever launched and no network
call is ever made.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.benchmarks.proxy_compat import (
    HONORS,
    IGNORES,
    INCONCLUSIVE,
    ProbeResult,
    classify,
    closed_port,
    main,
    probe_all,
    probe_cli,
    probe_env,
    render_markdown,
)


def _runner(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raises: BaseException | None = None,
) -> Callable[..., Any]:
    """Helper runner that records invocation kwargs on the returned function's calls attribute."""
    calls: list[dict[str, Any]] = []

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append({"args": args, **kwargs})
        if raises is not None:
            raise raises
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_success_means_the_cli_ignored_the_proxy() -> None:
    runner = _runner(returncode=0, stdout='{"text":"OK"}')
    res = probe_cli("grok", runner=runner, port=1)
    assert res.verdict == IGNORES
    assert "directly" in res.detail


def test_connection_refused_means_the_cli_honoured_the_proxy() -> None:
    runner = _runner(returncode=1, stderr="Error: connect ECONNREFUSED 127.0.0.1:54321")
    res = probe_cli("grok", runner=runner, port=54321)
    assert res.verdict == HONORS


def test_proxy_word_in_output_is_honoured() -> None:
    runner = _runner(returncode=1, stderr="unable to connect to proxy")
    res = probe_cli("grok", runner=runner)
    assert res.verdict == HONORS


def test_timeout_is_inconclusive_not_a_verdict() -> None:
    runner = _runner(raises=subprocess.TimeoutExpired(cmd="grok", timeout=1))
    res = probe_cli("grok", runner=runner)
    assert res.verdict == INCONCLUSIVE
    assert "timed out" in res.detail
    assert res.verdict != IGNORES
    assert res.verdict != HONORS


def test_unrelated_failure_is_inconclusive() -> None:
    runner = _runner(returncode=2, stderr="Error: invalid API key")
    res = probe_cli("grok", runner=runner)
    assert res.verdict == INCONCLUSIVE
    assert "invalid API key" in res.detail


def test_unknown_cli_is_inconclusive() -> None:
    res = probe_cli("nosuchcli")
    assert res.verdict == INCONCLUSIVE
    assert res.returncode is None
    assert res.command == []


def test_probe_env_sets_every_proxy_variable() -> None:
    base = {"PATH": "/bin", "NO_PROXY": "api.x.ai"}
    env = probe_env(4242, base=base)
    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        assert env[var] == "http://127.0.0.1:4242"
    assert env["NO_PROXY"] == ""
    assert env["no_proxy"] == ""
    assert env["PATH"] == "/bin"
    assert base["NO_PROXY"] == "api.x.ai"
    assert "HTTPS_PROXY" not in base


def test_the_probe_env_reaches_the_subprocess() -> None:
    runner = _runner(returncode=0)
    probe_cli("grok", runner=runner)
    assert len(runner.calls) == 1  # type: ignore[attr-defined]
    kwargs = runner.calls[0]  # type: ignore[attr-defined]
    assert kwargs["env"]["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False


def test_closed_port_is_in_range() -> None:
    port = closed_port()
    assert 1024 <= port <= 65535
    assert isinstance(port, int)
    port2 = closed_port()
    assert isinstance(port2, int)


def test_classify_is_pure() -> None:
    assert classify(0, "", timed_out=False) == (
        IGNORES,
        "call succeeded while HTTPS_PROXY pointed at a closed port, "
        "so the CLI dialled the provider directly",
    )
    assert classify(1, "ECONNREFUSED", timed_out=False) == (
        HONORS,
        "failed with a proxy/connection error, so the CLI routed through HTTPS_PROXY",
    )
    assert classify(None, "", timed_out=True) == (
        INCONCLUSIVE,
        "timed out after the probe budget; a hang matches BOTH a CLI blocking "
        "on a dead proxy and one blocking on denied direct egress",
    )
    assert classify(3, "boom", timed_out=False) == (
        INCONCLUSIVE,
        "exited 3 with no proxy-related error; output tail: boom",
    )


def test_probe_all_preserves_order() -> None:
    runner = _runner(returncode=0)
    results = probe_all(["grok", "claude"], runner=runner)
    clis = [r.cli for r in results]
    assert clis == ["grok", "claude"]


def test_markdown_is_deterministic() -> None:
    runner = _runner(returncode=0)
    results = probe_all(["grok", "claude"], runner=runner)
    md1 = render_markdown(results)
    md2 = render_markdown(results)
    assert md1 == md2
    assert md1.startswith("# Per-CLI proxy compatibility")
    assert md1.endswith("\n")

    for line in md1.splitlines():
        assert line == line.rstrip()

    for verdict in [HONORS, IGNORES, INCONCLUSIVE]:
        assert verdict in md1


def test_cli_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run = _runner(returncode=0, stdout='{"text":"OK"}')
    monkeypatch.setattr(subprocess, "run", fake_run)
    # Pin "the probed CLI is installed": on hosts without the grok binary,
    # shutil.which returns None and the verdict degrades to INCONCLUSIVE
    # before the faked subprocess is ever consulted.
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    out_file = tmp_path / "sub" / "p.json"
    rc = main(["--clis", "grok", "--format", "json", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()

    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    # The JSON projection is emitted with sort_keys=True so two runs are
    # byte-identical, which reorders the keys -- pin the key SET, not the order.
    assert set(item) == {
        "cli",
        "verdict",
        "returncode",
        "duration_s",
        "detail",
        "command",
    }
    assert item["cli"] == "grok"
    assert item["verdict"] == IGNORES
    # to_dict() itself keeps the declared field order.
    assert list(
        ProbeResult(
            cli="c", verdict=IGNORES, returncode=0, duration_s=0.0, detail="d", command=[]
        ).to_dict()
    ) == [
        "cli",
        "verdict",
        "returncode",
        "duration_s",
        "detail",
        "command",
    ]
