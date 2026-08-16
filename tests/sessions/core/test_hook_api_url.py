"""Hook control-plane URL derivation (parked-approval hang, cause #1).

Bench run swr_8474e958870543388267: the bridge session's hook POSTed to the
compiled-in ``http://127.0.0.1:8484`` while this stack's API was on :8499, so
the approval request was never recorded and the session parked forever. These
tests pin the resolution order end to end: explicit env > supervisor-stamped
value > last-resort default (which must WARN, naming the sibling-product risk).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions import hook_client
from omniagentos.sessions import supervisor as supervisor_module
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    API_BASE_URL_ENV,
    API_PORT_ENV,
    SESSION_API_URL_ENV,
    SessionSupervisor,
    resolved_api_base_url,
)

from .test_supervisor import FakeProcess, fake_factory, wait_for_state


@pytest.fixture(autouse=True)
def _clean_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (SESSION_API_URL_ENV, API_BASE_URL_ENV, API_PORT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hook_client, "_LAST_RESORT_WARNED", False)


# --- hook side ---------------------------------------------------------------


def test_explicit_env_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSION_API_URL_ENV, "http://127.0.0.1:8499/")
    monkeypatch.setenv(API_BASE_URL_ENV, "http://127.0.0.1:8485")
    assert hook_client.base_url() == "http://127.0.0.1:8499"
    assert hook_client._api_url("/api/sessions/hook-eval") == (
        "http://127.0.0.1:8499/api/sessions/hook-eval"
    )


def test_supervisor_stamped_value_is_used_when_explicit_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_BASE_URL_ENV, "http://127.0.0.1:8499")
    assert hook_client.base_url() == "http://127.0.0.1:8499"


def test_last_resort_default_targets_this_product_and_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The compiled-in fallback must name THIS product's port, and still warn.

    This test previously asserted the default was ``:8484`` -- the SIBLING
    product's port -- which encoded the defect as intended behaviour. Nothing
    listens on :8484 here, so every hook POST reaching the fallback was refused
    and swallowed into a ``deny``, and supervisor.py:1605 then correctly
    declined to create an approval row. Zero approvals existed for four days.

    The warning is retained and asserted: reaching this path still means the
    supervisor never stamped a URL, so the port remains a guess.
    """
    with caplog.at_level(logging.WARNING, logger=hook_client.__name__):
        assert hook_client.base_url() == hook_client.API_BASE_URL
    assert hook_client.API_BASE_URL == "http://127.0.0.1:8485"
    assert "8484" not in hook_client.API_BASE_URL
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "8485" in message
    assert "GUESS" in message.upper()
    assert SESSION_API_URL_ENV in message and API_BASE_URL_ENV in message


def test_last_resort_warning_is_emitted_once_per_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hook runs per tool call; the warning must not become its own flood."""
    with caplog.at_level(logging.WARNING, logger=hook_client.__name__):
        for _ in range(5):
            hook_client.base_url()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


# --- supervisor side ---------------------------------------------------------


def test_resolved_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolved_api_base_url({}) == "http://127.0.0.1:8485"  # default when no port configured
    assert resolved_api_base_url({API_PORT_ENV: "8485"}) == "http://127.0.0.1:8485"
    assert resolved_api_base_url({API_PORT_ENV: "8499"}) == "http://127.0.0.1:8499"
    assert (
        resolved_api_base_url({API_PORT_ENV: "8499", API_BASE_URL_ENV: "http://host:1/"})
        == "http://host:1"
    )
    assert (
        resolved_api_base_url(
            {
                API_PORT_ENV: "8499",
                API_BASE_URL_ENV: "http://host:1",
                SESSION_API_URL_ENV: "http://host:2",
            }
        )
        == "http://host:2"
    )
    # A port that is not a usable number: use default
    assert resolved_api_base_url({API_PORT_ENV: "not-a-port"}) == "http://127.0.0.1:8485"
    assert resolved_api_base_url({API_PORT_ENV: "0"}) == "http://127.0.0.1:8485"


def test_spawn_stamps_the_running_stacks_api_url_into_the_session_env(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the hook inherits the port THIS stack serves (:8499 here)."""
    monkeypatch.setenv(API_PORT_ENV, "8499")
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "claude-ref"},
            {"type": "result", "subtype": "success"},
        ]
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: process),
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work", None, "title")
    wait_for_state(sessions_dal, session_id, "completed")
    env = captures[0][1]["env"]
    assert env[API_BASE_URL_ENV] == "http://127.0.0.1:8499"
    # The scrub is untouched: nothing else leaked in with it.
    assert "OPENAI_API_KEY" not in env


def test_spawn_stamps_default_url_when_port_cannot_be_derived(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit port: stamp the standard default (8485) so the hook reaches the right stack."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: FakeProcess([])),
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work", None, "title")
    wait_for_state(sessions_dal, session_id, "completed")
    assert captures[0][1]["env"][API_BASE_URL_ENV] == "http://127.0.0.1:8485"


def test_stamped_url_is_not_inherited_from_the_daemon_by_the_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the supervisor must STAMP: build_clean_env drops these names outright,
    so setting OMNI_SESSION_API_URL on the daemon never reaches the hook."""
    monkeypatch.setenv(SESSION_API_URL_ENV, "http://127.0.0.1:8499")
    clean = supervisor_module.build_clean_env(session_id="ses_x")
    assert SESSION_API_URL_ENV not in clean
    assert json.dumps(clean)  # plain str->str mapping, still serializable


def test_launch_env_sh_exports_grok_api_port_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-FIRST regression test: launch-env.sh must export OMNIAGENTOS_API_PORT with
    default 8485 so any process sourcing it gets the correct API port, and
    resolved_api_base_url() can stamp the session env correctly. This was broken
    until 2026-07-31 when OMNIAGENTOS_API_PORT was only exported from
    launch-omniagentos.sh: approval POSTs went to the wrong port and
    sessions parked forever with zero approval rows.
    """
    # Find the repo root relative to this test file
    test_file = Path(__file__)
    repo_root = test_file.parent.parent.parent.parent
    launch_env_sh = repo_root / "scripts" / "launch-env.sh"
    assert launch_env_sh.exists(), f"Expected {launch_env_sh} to exist"

    # Source launch-env.sh in a clean environment (unset the port vars).
    #
    # OMNIAGENTOS_LAUNCH_ENV_LOADED must be unset too, or this test asserts
    # nothing: it is the script's same-shell dedup marker, and while it is set
    # launch-env.sh `return 0`s at its guard WITHOUT reaching the
    # `: "${OMNIAGENTOS_API_PORT:=8485}"` default. The operator's own shell exports it,
    # and pytest inherits the whole environment — so from a launch-env-sourced
    # shell (which is how merge-gate.sh runs) this test read back
    # "OMNIAGENTOS_API_PORT=" and failed for a reason that has nothing to do with the
    # 2026-07-31 defect it exists to pin. Found 2026-08-05 in-gate, alongside
    # the same class in the counterfeit corpus's control pass. The premise here
    # is "a shell that has NOT already sourced launch-env"; state it.
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"""
            unset OMNIAGENTOS_API_PORT OMNIAGENTOS_DASH_PORT OMNIAGENTOS_API_BASE_URL OMNI_SESSION_API_URL
            unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED
            . "{launch_env_sh}"
            echo "OMNIAGENTOS_API_PORT=$OMNIAGENTOS_API_PORT"
            echo "OMNIAGENTOS_DASH_PORT=$OMNIAGENTOS_DASH_PORT"
            """,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    output = result.stdout
    assert "OMNIAGENTOS_API_PORT=8485" in output, f"Expected OMNIAGENTOS_API_PORT=8485; got:\n{output}"
    assert "OMNIAGENTOS_DASH_PORT=3003" in output, f"Expected OMNIAGENTOS_DASH_PORT=3003; got:\n{output}"

    # Also test the supervisor's resolved_api_base_url() with this env
    env_with_port = {API_PORT_ENV: "8485"}
    resolved = resolved_api_base_url(env_with_port)
    assert resolved == "http://127.0.0.1:8485", (
        f"resolved_api_base_url must return correct URL when OMNIAGENTOS_API_PORT=8485; got {resolved}"
    )
