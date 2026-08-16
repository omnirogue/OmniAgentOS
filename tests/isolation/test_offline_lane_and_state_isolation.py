"""Proofs for the root-conftest state isolation and offline-lane guards.

Every test here is a regression fence for a leak that was OBSERVED, not
hypothesised:

* a default-path ``append_manifest(default_ledger_dir(), ...)`` appended a real
  line to the checkout's ``ledger/runs-202607.jsonl``;
* a default-path ``write_note(default_vault_dir(), ...)`` created
  ``vault/swarm/<id>.md`` in the working tree;
* ``tests/knowledge/test_e2e_real.py`` made 18 real POSTs to Ollama on :11434
  from the OFFLINE default lane, carrying no ``live_ollama`` marker;
* ``tests/chats/test_dto.py::TestClassify::test_route_returns_shape`` POSTed to
  the local LiteLLM proxy on :4000 for the same reason.

The guards live in ``tests/conftest.py``; this module imports them by name so a
guard that is deleted or turned into a no-op fails here loudly rather than
leaving a green suite behind.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Imported HERE, at collection time, on purpose: it binds
# ``default_vault_dir`` with ``from omniagentos.contracts import ...``, so this
# import is what puts a STALE alias in ``sys.modules`` before the session guard
# is installed. Importing it inside the test instead would resolve the name from
# an already-guarded ``omniagentos.contracts`` and the alias test would pass no
# matter what the rebinding pass did (measured: it did exactly that).
from omniagentos.scheduler import morning_report
from tests.conftest import (
    _LIVE_ALLOWED,
    LEDGER_DIR_ENV,
    LIVE_OPT_IN_MARKERS,
    VAULT_DIR_ENV,
    OfflineLaneViolation,
    local_provider_ports,
    operator_state_dirs,
    provider_cli_names,
    refusal_for_connect,
    refusal_for_spawn,
)

OPERATOR_LEDGER, OPERATOR_VAULT = operator_state_dirs()


# ---------------------------------------------------------------------------
# ledger / vault isolation
# ---------------------------------------------------------------------------


def test_the_operator_dirs_this_module_guards_are_the_real_ones() -> None:
    """Anchor: the paths below name live state, not an invented pair."""
    repo_root = Path(__file__).resolve().parents[2]
    assert OPERATOR_LEDGER == repo_root / "ledger"
    assert OPERATOR_VAULT == repo_root / "vault"


def test_default_ledger_dir_is_pinned_off_the_operator_ledger() -> None:
    from omniagentos.contracts import default_ledger_dir

    resolved = Path(default_ledger_dir()).resolve()
    assert resolved != OPERATOR_LEDGER
    assert os.environ.get(LEDGER_DIR_ENV), f"{LEDGER_DIR_ENV} must be pinned session-wide"


def test_default_vault_dir_is_pinned_off_the_operator_vault() -> None:
    from omniagentos.contracts import default_vault_dir

    resolved = Path(default_vault_dir()).resolve()
    assert resolved != OPERATOR_VAULT
    assert os.environ.get(VAULT_DIR_ENV), f"{VAULT_DIR_ENV} must be pinned session-wide"


def test_a_default_path_ledger_write_lands_outside_the_operator_ledger() -> None:
    """The exact call that leaked, through the production seam, unpinned by the test."""
    from omniagentos.contracts import (
        HarnessProfile,
        HarnessType,
        RunManifest,
        RunState,
        default_ledger_dir,
        utc_now_iso,
    )
    from omniagentos.ledger import append_manifest

    manifest = RunManifest(
        run_id="isolation-probe",
        task_id="tsk_isolation_probe",
        harness=HarnessProfile(harness=HarnessType.MOCK),
        state=RunState.COMPLETED,
        started_at=utc_now_iso(),
        finished_at=utc_now_iso(),
    )
    written = Path(append_manifest(default_ledger_dir(), manifest)).resolve()
    assert written.is_file()
    assert OPERATOR_LEDGER not in written.parents


def test_a_default_path_vault_write_lands_outside_the_operator_vault() -> None:
    from omniagentos.contracts import NoteType, VaultFrontmatter, default_vault_dir
    from omniagentos.vault.frontmatter import render_frontmatter
    from omniagentos.vault.write import write_note

    frontmatter = VaultFrontmatter(id="isolation-probe", type=NoteType.RUN)
    content = render_frontmatter(frontmatter) + "\n# isolation probe\n"
    written = Path(write_note(default_vault_dir(), "swarm/isolation-probe.md", content)).resolve()
    assert written.is_file()
    assert OPERATOR_VAULT not in written.parents


def test_an_unpinned_ledger_resolution_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 2: losing the pin must ERROR, never silently return live state."""
    from omniagentos.contracts import default_ledger_dir

    monkeypatch.delenv(LEDGER_DIR_ENV, raising=False)
    with pytest.raises(RuntimeError, match="OPERATOR ledger directory"):
        default_ledger_dir()


def test_an_unpinned_vault_resolution_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagentos.contracts import default_vault_dir

    monkeypatch.delenv(VAULT_DIR_ENV, raising=False)
    with pytest.raises(RuntimeError, match="OPERATOR vault directory"):
        default_vault_dir()


def test_a_relative_pin_that_resolves_onto_the_operator_vault_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIAGENTOS_VAULT_DIR=vault`` IS the operator vault when cwd is the repo.

    A pin that merely looks non-default is not isolation; the guard has to judge
    the RESOLVED path. (``tests/scheduler/test_morning_report.py`` set exactly
    this and only escaped writing because it stubbed ``write_note``.)
    """
    from omniagentos.contracts import default_vault_dir

    monkeypatch.chdir(OPERATOR_VAULT.parent)
    monkeypatch.setenv(VAULT_DIR_ENV, "vault")
    with pytest.raises(RuntimeError, match="OPERATOR vault directory"):
        default_vault_dir()


def test_the_guard_reaches_stale_import_time_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from omniagentos.contracts import default_vault_dir`` must be guarded too.

    ``scheduler.morning_report`` binds the name at import time (see the
    module-level import above — it has to happen at COLLECTION time to be a
    stale alias at all), so a guard installed only on ``omniagentos.contracts``
    would leave it pointing at the original function — the independent-bindings
    trap the psycopg guard documents. This is the test that the rebinding pass
    actually happened.
    """
    from omniagentos import contracts

    assert morning_report.default_vault_dir is contracts.default_vault_dir
    monkeypatch.delenv(VAULT_DIR_ENV, raising=False)
    with pytest.raises(RuntimeError, match="OPERATOR vault directory"):
        morning_report.default_vault_dir()


# ---------------------------------------------------------------------------
# offline-lane network guard
# ---------------------------------------------------------------------------


def test_the_guarded_provider_ports_are_derived_from_production() -> None:
    """Not a hand-typed list: both endpoints come from the shipping defaults."""
    from omniagentos.filesearch.embeddings import OLLAMA_HOST
    from omniagentos.routing.api_policy import litellm_api_base

    ports = local_provider_ports()
    assert int(OLLAMA_HOST.rsplit(":", 1)[1]) in ports
    assert int(litellm_api_base().split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1]) in ports


def test_outbound_tcp_connect_is_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        with pytest.raises(OfflineLaneViolation, match="outbound network connection"):
            sock.connect(("93.184.216.34", 80))


def test_the_violation_survives_a_broad_except_exception() -> None:
    """The reason ``OfflineLaneViolation`` is a ``BaseException``.

    Every live seam in this repo swallows ``Exception`` by contract
    (``OllamaEmbedding.embed``'s ``except Exception: continue``,
    ``classify_chat_project``'s "LLM client unavailable",
    ``run_fable_json``'s "must degrade, not crash"). A guard that raised
    ``Exception`` would be eaten by exactly those handlers and the live call
    would go back to being silent — which is what it did in the first cut:
    ``test_e2e_real`` degraded to a SKIP and ``test_route_returns_shape``
    stayed green.
    """
    with pytest.raises(OfflineLaneViolation):
        try:
            socket.create_connection(("93.184.216.34", 80), timeout=2)
        except Exception:  # noqa: BLE001 -- deliberately mirrors the production handlers
            pytest.fail("the offline-lane violation was swallowed by `except Exception`")


def test_a_live_ollama_call_on_loopback_is_refused() -> None:
    """Loopback is allowed in general; a local MODEL DAEMON is not."""
    ollama_port = next(port for port, label in local_provider_ports().items() if label == "Ollama")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        with pytest.raises(OfflineLaneViolation, match="live Ollama call"):
            sock.connect(("127.0.0.1", ollama_port))


def test_a_live_litellm_proxy_call_on_loopback_is_refused() -> None:
    proxy_port = next(
        port for port, label in local_provider_ports().items() if label == "LiteLLM proxy"
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        with pytest.raises(OfflineLaneViolation, match="live LiteLLM proxy call"):
            sock.connect(("127.0.0.1", proxy_port))


def test_a_test_owned_loopback_listener_is_still_allowed() -> None:
    """The guard must not break the many tests that bind their own server."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(listener.getsockname())
    finally:
        listener.close()


def test_a_datagram_route_probe_is_still_allowed() -> None:
    """``tests/lease/test_sandbox_escape.py::_non_loopback_ipv4``'s exact shape.

    A connected UDP socket performs a route lookup and sends nothing; refusing
    it would change what that test asserts rather than close a leak.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
    finally:
        probe.close()


def test_refusal_for_connect_judges_stream_sockets_only() -> None:
    ports = local_provider_ports()
    assert refusal_for_connect(socket.AF_INET, socket.SOCK_STREAM, ("8.8.8.8", 53), ports)
    assert refusal_for_connect(socket.AF_INET, socket.SOCK_DGRAM, ("8.8.8.8", 53), ports) is None
    assert (
        refusal_for_connect(socket.AF_UNIX, socket.SOCK_STREAM, ("8.8.8.8", 53), ports) is None
    )
    assert (
        refusal_for_connect(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 5432), ports) is None
    )
    assert refusal_for_connect(socket.AF_INET6, socket.SOCK_STREAM, ("::1", 5432, 0, 0), ports) is (
        None
    )


# ---------------------------------------------------------------------------
# offline-lane provider-CLI guard
# ---------------------------------------------------------------------------


def test_provider_cli_names_are_derived_from_the_adapter_registry() -> None:
    from omniagentos.adapters.registry import _ADAPTERS

    names = provider_cli_names()
    assert names == {key[len("cli-") :] for key in _ADAPTERS if key.startswith("cli-")}
    assert "claude" in names and "codex" in names


def test_a_real_provider_cli_spawn_is_refused(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A ``claude`` staged OUTSIDE the session tmp root is treated as the real CLI."""
    outside = Path(tempfile.mkdtemp(prefix="offline-guard-outside-")).resolve()
    stub = outside / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    roots = [Path(tmp_path_factory.getbasetemp()).resolve()]
    assert refusal_for_spawn([str(stub)], None, provider_cli_names(), roots)
    with pytest.raises(OfflineLaneViolation, match="provider CLI spawn"):
        subprocess.run([str(stub)], check=False)


def test_a_stub_provider_cli_under_the_session_tmp_root_is_allowed(tmp_path: Path) -> None:
    """``tests/entrypoints/conftest.py`` installs exactly this and must keep working."""
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    assert subprocess.run([str(stub)], check=False).returncode == 0
    assert (
        subprocess.run(
            ["claude"], check=False, env={**os.environ, "PATH": str(tmp_path)}
        ).returncode
        == 0
    )


def test_a_non_provider_binary_is_never_judged() -> None:
    assert refusal_for_spawn(["git", "status"], None, provider_cli_names(), []) is None
    assert refusal_for_spawn([sys.executable, "-c", "pass"], None, provider_cli_names(), []) is None


# ---------------------------------------------------------------------------
# the marker contract the guard enforces
# ---------------------------------------------------------------------------


def test_the_guard_is_engaged_for_an_unmarked_test() -> None:
    assert _LIVE_ALLOWED["value"] is False


@pytest.mark.live
def test_the_guard_is_lifted_for_a_live_marked_test() -> None:
    assert _LIVE_ALLOWED["value"] is True


def test_the_opt_in_markers_are_the_lanes_own_liveness_exclusions() -> None:
    """The guard's opt-out set must not drift from ``pyproject``'s exclusions.

    Read out of the shipped ``addopts`` rather than retyped here. Pinned in BOTH
    directions: every opt-in marker has to be one the default lane already
    excludes (otherwise the guard lifts for tests that still run by default), and
    the remainder of that exclusion list has to be exactly the non-liveness
    exclusions (otherwise a new live marker could be added to the lane and
    silently stay guarded — or worse, silently unguarded). ``e2e`` stays guarded:
    it is explicit, slow, and may be stateful, but must not permit model traffic.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    addopts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["addopts"]
    expression = addopts.split("not", 1)[1].strip().strip("'\"").strip("()")
    excluded = {name.strip() for name in expression.split(" or ")}

    assert LIVE_OPT_IN_MARKERS <= excluded, f"lane excludes {excluded}"
    assert excluded - LIVE_OPT_IN_MARKERS == {
        "perf",
        "counterfeit_gate",
        "feature_health",
        "e2e",
        # cef7ac6f deliberately excludes observational LiveSim, but it is not
        # a provider-traffic opt-in and therefore must remain guarded here.
        "livesim",
    }


def test_the_real_ollama_e2e_suite_carries_the_live_ollama_marker() -> None:
    """It requires REAL Ollama by design; unmarked it ran in the OFFLINE lane."""
    from tests.knowledge import test_e2e_real

    declared = getattr(test_e2e_real, "pytestmark", [])
    if not isinstance(declared, list):
        declared = [declared]
    assert "live_ollama" in {mark.name for mark in declared}
