"""Root test configuration.

Two global side-effect guards live here, because the notification write seam is
best-effort by contract (``record_notification`` swallows every failure) and so
NEITHER leak announces itself:

  * ``_isolate_default_db`` — every helper that takes ``db_path=None`` falls back
    to ``contracts.default_db_path()``, i.e. the operator's LIVE
    ``var/omniagentos.db``. A test that drove a board card to done therefore wrote
    a real "Task complete" row into the operator's notification feed (230 of them, body
    "test goal", before this fixture). Pointing ``OMNIAGENTOS_DB`` at a session
    tmp file makes that fallback harmless; a test that sets its own path still
    wins, since function-scoped monkeypatch overrides this.
  * ``_no_notification_push`` — the OS delivery seam. Previously stubbed only in
    the intake and notifications suites, so any OTHER suite that emitted a
    notification popped a real terminal-notifier banner on macOS mid-run.

AC-policy fix7 added an app-level deny-by-default gate
(``omniagentos.api.main.require_session_token``) that requires the local session
token on EVERY mutating request. The dedicated regression probe
(``tests/api/test_control_plane_auth.py``) exercises that gate directly (401
without a token, works with one). Every OTHER test drives the app for its
business logic, not to re-test auth, so the autouse fixture below overrides the
gate to a no-op there — exactly as a production dashboard call would arrive
already authorized. A test that needs the REAL gate active opts in with
``@pytest.mark.real_auth``.

L-11/L-15 add session-wide ``OMNIAGENTOS_VAR`` isolation so reliability
reflexion writeback (lessons / skill proposals) never lands in the operator
checkout corpus. The connector registry is seeded into that isolated root too,
so tests exercise the same path contract as production without reading an
operator registry. M-41 registers the required-suite skip surface plugin.

The knowledge subsystem needed the SAME treatment and had none. ``config.dsn()``
falls back to ``contracts.DEFAULT_DSN`` —
``postgresql://knowledge_agent@localhost/omniagentos_knowledge``, the operator's
LIVE knowledge Postgres — and on this host that DSN connects under trust auth
with ``INSERT`` granted on ``episodes``/``facts``/``recall_log``/
``file_embeddings``. Every default-path caller (``knowledge.recall._get_store``,
``KnowledgeStore()``, ``filesearch.semantic``, ``goals``/``briefing``/``comms``
bridges, ``api.deps``) therefore read and could WRITE production from a test
process. ``_isolate_knowledge_pg`` and ``_refuse_production_knowledge_db`` below
close that, in two independent layers.

``ledger/`` and ``vault/`` are the two remaining members of that same family and
had NO isolation at all: ``contracts.default_ledger_dir()`` /
``default_vault_dir()`` fall back to ``<repo>/ledger`` and ``<repo>/vault``, the
operator's live run ledger and note vault, and a shell that sourced
``scripts/launch-env.sh`` exports both pointing at the operator's live
``var/`` tree. Proven reachable, not merely reachable in theory: a default-path
``append_manifest(default_ledger_dir(), ...)`` appended a real line to
``ledger/runs-202607.jsonl`` and a default-path ``write_note(default_vault_dir(),
...)`` created ``vault/swarm/<id>.md`` in the working tree.
``tests/entrypoints/test_api_startup_recovers_stale_swarms.py::_isolate_vault``
had already found the vault half the hard way ("a green run dirtied the repo,
~2 of 3 runs") and pinned it for ONE module. ``_isolate_ledger_and_vault`` below
closes both, session-wide, in the same two-layer shape as the knowledge fixtures:
pin the env, then refuse the resolution.

``_forbid_live_calls`` closes the last member: the default lane is an OFFLINE
lane (``pyproject`` excludes ``live``/``live_cli``/``live_ollama``/``perf``
precisely so it is), yet nothing enforced that. A test that reaches a live model
or a provider CLI without one of those markers is MISMARKED, and the symptom is a
silent multi-second block in ``select.poll()`` instead of a named failure. Two
such tests were live at the time this landed, both found by recording every
``socket.connect``/``Popen`` in a full fast-lane run:
``tests/knowledge/test_e2e_real.py`` (18 real POSTs to Ollama on 11434 — its own
docstring says "REAL Ollama bge-m3", and ``tests/knowledge/conftest.py``
registers the ``live_ollama`` marker it never applied) and
``tests/chats/test_dto.py::TestClassify::test_route_returns_shape`` (the only
member of its class that did not inject the fake ``client=``, so the route built
a real ``ShortCallClient`` and POSTed to the local LiteLLM proxy on 4000).
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

import pytest

# M-41: required-suite skip/absence surfacing into the default pytest/CI signal.
pytest_plugins = ["omniagentos.testpolicy.pytest_plugin", "pytester"]


@pytest.fixture(autouse=True)
def _isolate_health_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep prompt health injection independent of the operator snapshot."""
    from omniagentos.health import digest as health_digest

    monkeypatch.setattr(
        health_digest,
        "HEALTH_SNAPSHOT_PATH",
        tmp_path / "health-sentinel" / "latest.json",
    )


@pytest.fixture(autouse=True, scope="session")
def _isolate_default_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Never let tests inherit a live ``OMNIAGENTOS_DB`` from the environment.

    ``make test``, ``make smoke``, and any host shell that sourced
    ``scripts/launch-env.sh`` may export a product control-plane path. Honouring
    that preset previously wrote real notification rows into the operator DB.
    Always pin a session-scoped temp path; function-scoped monkeypatches (smoke
    helpers, dedicated isolation probes) still win for individual tests.
    ``scripts/test-comprehensive.sh`` also pins its own isolated DB before
    invoke — that pin is replaced here with a session temp path equally safe.
    """
    previous = os.environ.get("OMNIAGENTOS_DB")
    fallback = tmp_path_factory.mktemp("default-db") / "omniagentos.db"
    os.environ["OMNIAGENTOS_DB"] = str(fallback)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OMNIAGENTOS_DB", None)
        else:
            os.environ["OMNIAGENTOS_DB"] = previous


@pytest.fixture(autouse=True, scope="session")
def _isolate_spend_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Never let a test settle paid spend against the PRODUCTION spend ledger.

    F5 (2026-08-12 Class-A review): the OpenRouter adapter now routes every call
    through the spend guard, whose DB resolves to a FIXED production path
    (``omniagentos.adapters.spend_db.PRODUCTION_SPEND_DB``) when
    ``OMNIAGENTOS_SPEND_DB`` is unset. Any OpenRouter test that mocks the HTTP
    layer but lets the settle run would therefore write into the real operator
    ledger. Pin a session-scoped scratch path so NO test defaults to production;
    function-scoped ``monkeypatch.delenv`` still wins for the dedicated
    resolution-precedence tests, and ``monkeypatch.setenv`` for tests that want
    their own path.
    """
    previous = os.environ.get("OMNIAGENTOS_SPEND_DB")
    fallback = tmp_path_factory.mktemp("spend-db") / "spend-ledger.sqlite3"
    os.environ["OMNIAGENTOS_SPEND_DB"] = str(fallback)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OMNIAGENTOS_SPEND_DB", None)
        else:
            os.environ["OMNIAGENTOS_SPEND_DB"] = previous


@pytest.fixture(autouse=True, scope="session")
def _startup_seeding_off() -> Iterator[None]:
    """Keep the API lifespan's startup seeding/indexing off under pytest.

    The lifespan seeds the built-in routines and syncs/indexes the vault
    playbook on startup (default-on in production). Under pytest
    ``default_vault_dir()`` falls back to the REAL repo vault, so an
    unguarded lifespan entry would sync+index dozens of real notes inside
    any test that merely enters the app. Pin both flags off session-wide —
    same restore-previous-value pattern as ``_isolate_default_db``; the
    dedicated lifespan tests re-enable them with function-scoped
    ``monkeypatch.setenv``, which wins over this pin.

    The two resume flags are pinned off for the same reason: any test that
    merely enters the lifespan otherwise spawns a REAL orch-resume daemon
    thread (default 3s delay) that later migrates and warms the process-wide
    store singletons mid-suite — an order-dependence source under xdist.

    ``OMNIAGENTOS_MINT_TOKEN_ON_STARTUP`` gets the same treatment: left on,
    the first test in the session to enter the lifespan would mint
    ``var/secrets/sessions-token`` under the session-wide isolated var root
    (harmless in isolation, but an unpinned, order-dependent side effect any
    other test could stumble on). The dedicated lifespan test re-enables it
    with function-scoped ``monkeypatch.setenv``, same as the flags above.
    """
    keys = (
        "OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP",
        "OMNIAGENTOS_INDEX_VAULT_ON_STARTUP",
        "OMNIAGENTOS_ORCH_RESUME_ON_STARTUP",
        "OMNIAGENTOS_SWARM_RESUME_ON_STARTUP",
        "OMNIAGENTOS_MINT_TOKEN_ON_STARTUP",
    )
    previous = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ[key] = "0"
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@pytest.fixture(autouse=True, scope="session")
def _budget_enforcement_advisory_by_default() -> Iterator[None]:
    """Pin ``OMNIAGENTOS_BUDGET_ENFORCEMENT`` to the CODE default under pytest.

    ``scripts/launch-env.sh`` exports ``block`` (operator decision 2026-08-04),
    and ``omniagentos.budget.policy.blocks()`` reads that variable at call time
    inside PRODUCTION paths a test may enter — swarm admission, the improve
    dispatcher, the session reaper, the provider CLI cap. Unlike the other
    launch-env exports, nothing pinned it back, so from 2026-08-04 the suite
    answered differently depending on whether the shell that started pytest had
    sourced that file.

    That is layer 2 of the chronic in-gate "COUNTERFEIT GATE CONTROL FAILED":
    ``merge-gate.sh`` runs from an operator shell, so its counterfeit worker
    inherited ``block``, the simulation campaigns' recovery attempts were
    refused admission, and two ``tests/simharness/test_orchestration.py`` nodes
    in the corpus's ``must_fail`` union went red — while the identical corpus
    passed 85/85 from a plain worktree. An operator's runtime posture is not a
    test premise: every test that wants enforcement asks for it with a
    function-scoped ``monkeypatch.setenv``, which still wins over this pin.
    """
    from omniagentos.budget.policy import ADVISORY, ENFORCEMENT_ENV

    previous = os.environ.get(ENFORCEMENT_ENV)
    os.environ[ENFORCEMENT_ENV] = ADVISORY
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENFORCEMENT_ENV, None)
        else:
            os.environ[ENFORCEMENT_ENV] = previous


@pytest.fixture(autouse=True, scope="session")
def _isolate_var_and_reflexion(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """L-11/L-15: session-wide isolated OMNIAGENTOS_VAR / reflexion storage.

    Covers every test that may call ``writeback_lessons`` or
    ``propose_confirmed_fix`` — no fabricated lesson may land in the operator
    ``var/reflexion`` corpus. Pre-set env (outer CI harness) is respected.

    CALLERS: SET BOTH NAMES OR NEITHER. The preset below resolves as
    ``OMNIAGENTOS_VAR or OMNIAGENTOS_VAR_DIR`` and then seeds ONE root, while
    production resolvers read the names independently. A caller that overrides
    only ``_VAR_DIR`` therefore leaves ``OMNIAGENTOS_VAR`` pointing wherever the
    ambient shell had it: this fixture seeds ``connectors.yaml`` into THAT root
    while the registry is read from the other, and the split is silent until
    something asks for a connector. ``merge-gate.sh``'s workers did exactly this
    on 2026-08-05 — ``tests/api/test_capability_requests.py`` went 30 passed to
    10 failed with "Unable to read connector registry", and every default-path
    var/vault write in a gate ladder landed in the operator's LIVE tree, which
    is the breach this fixture exists to prevent. A partial override is worse
    than none.
    """
    preset = os.environ.get("OMNIAGENTOS_VAR") or os.environ.get("OMNIAGENTOS_VAR_DIR")
    if preset:
        root = Path(preset).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "reflexion").mkdir(parents=True, exist_ok=True)
        registry = root / "connectors.yaml"
        if not registry.exists():
            registry.write_bytes(
                (Path(__file__).resolve().parents[1] / "configs" / "connectors.yaml").read_bytes()
            )
        yield root
        return

    root = tmp_path_factory.mktemp("session-var")
    (root / "reflexion").mkdir(parents=True, exist_ok=True)
    (root / "connectors.yaml").write_bytes(
        (Path(__file__).resolve().parents[1] / "configs" / "connectors.yaml").read_bytes()
    )
    previous = {
        "OMNIAGENTOS_VAR": os.environ.get("OMNIAGENTOS_VAR"),
        "OMNIAGENTOS_VAR_DIR": os.environ.get("OMNIAGENTOS_VAR_DIR"),
    }
    os.environ["OMNIAGENTOS_VAR"] = str(root)
    os.environ["OMNIAGENTOS_VAR_DIR"] = str(root)
    try:
        yield root
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


#: ``contracts`` env overrides for the two operator state roots.
LEDGER_DIR_ENV = "OMNIAGENTOS_LEDGER_DIR"
VAULT_DIR_ENV = "OMNIAGENTOS_VAULT_DIR"


def operator_state_dirs() -> tuple[Path, Path]:
    """``(ledger, vault)`` as ``contracts`` resolves them with NO env override.

    Asked of the production resolvers with the overrides temporarily removed,
    rather than rebuilt from ``__file__`` here, for the same reason
    ``production_knowledge_databases`` reads the frozen contract: moving the
    default has to move the guard with it, and a second hand-rolled copy of the
    path rule is a copy that can silently disagree with the one in use.
    """
    from omniagentos import contracts

    saved = {key: os.environ.pop(key, None) for key in (LEDGER_DIR_ENV, VAULT_DIR_ENV)}
    try:
        return (
            Path(contracts.default_ledger_dir()).resolve(),
            Path(contracts.default_vault_dir()).resolve(),
        )
    finally:
        for key, old in saved.items():
            if old is not None:
                os.environ[key] = old


def _rebind_across_modules(original: Any, replacement: Any) -> list[tuple[Any, str, Any]]:
    """Point every module attribute that IS *original* at *replacement*.

    Callers overwhelmingly do ``from omniagentos.contracts import
    default_vault_dir`` at import time, so patching only
    ``omniagentos.contracts`` leaves every already-imported caller bound to the
    ORIGINAL function object — the same independent-bindings trap
    ``_refuse_production_knowledge_db`` documents for ``psycopg.connect``.
    Identity is the test (not the name), so an ``import ... as`` alias is caught
    too; modules imported LATER pick the replacement up from
    ``omniagentos.contracts`` for free. Returns ``(module, name, original)``
    triples so the fixture restores exactly what it changed.
    """
    touched: list[tuple[Any, str, Any]] = []
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            members = list(vars(module).items())
        except Exception:  # noqa: BLE001 -- some module __dict__s are proxies; skip them.
            continue
        for name, value in members:
            if value is original:
                setattr(module, name, replacement)
                touched.append((module, name, original))
    return touched


@pytest.fixture(autouse=True, scope="session")
def _isolate_ledger_and_vault(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Session-wide isolated ``ledger/`` and ``vault/`` roots, in two layers.

    Layer 1 pins ``OMNIAGENTOS_LEDGER_DIR`` / ``OMNIAGENTOS_VAULT_DIR`` at
    session temp dirs. That alone covers every production caller, because they
    all resolve through ``default_ledger_dir()`` / ``default_vault_dir()`` at
    CALL time (``ledger_dir or default_ledger_dir()``), never at import time —
    and unlike the psycopg guard, an env pin crosses the subprocess boundary
    with the inherited environment.

    A preset from the host shell is deliberately NOT honoured (unlike
    ``OMNIAGENTOS_VAR``): ``scripts/launch-env.sh`` exports both, pointing at
    ``$OMNIAGENTOS_VAR_DIR/{ledger,vault}`` — the operator's LIVE state — so
    "respect the preset" would mean "write to production" on exactly the boxes
    that have one. Function-scoped ``monkeypatch.setenv`` still wins per test.

    Layer 2 is the fail-closed half. ``default_*_dir()`` is replaced by a
    wrapper that RAISES when the resolution lands on the operator's real
    directory, so an unpinned path is a loud, named error instead of a silent
    fallback to the live location.
    """
    from omniagentos import contracts

    operator_ledger, operator_vault = operator_state_dirs()

    ledger_root = tmp_path_factory.mktemp("session-ledger")
    vault_root = tmp_path_factory.mktemp("session-vault")
    previous = {
        LEDGER_DIR_ENV: os.environ.get(LEDGER_DIR_ENV),
        VAULT_DIR_ENV: os.environ.get(VAULT_DIR_ENV),
    }
    os.environ[LEDGER_DIR_ENV] = str(ledger_root)
    os.environ[VAULT_DIR_ENV] = str(vault_root)

    original_ledger = contracts.default_ledger_dir
    original_vault = contracts.default_vault_dir

    def _guard(original: Any, forbidden: Path, env_key: str, label: str) -> Any:
        def resolve() -> str:
            value = original()
            if Path(value).resolve() == forbidden:
                raise RuntimeError(
                    f"refusing to resolve the OPERATOR {label} directory {forbidden} "
                    f"from a test process: it is live state (run manifests / notes) and "
                    f"this code path WRITES to it. {env_key} is pinned session-wide by "
                    f"tests/conftest.py::_isolate_ledger_and_vault; something cleared it. "
                    f"Pass an explicit directory, or pin {env_key} with monkeypatch."
                )
            return value

        return resolve

    guarded_ledger = _guard(original_ledger, operator_ledger, LEDGER_DIR_ENV, "ledger")
    guarded_vault = _guard(original_vault, operator_vault, VAULT_DIR_ENV, "vault")
    touched = _rebind_across_modules(original_ledger, guarded_ledger)
    touched += _rebind_across_modules(original_vault, guarded_vault)
    try:
        yield
    finally:
        for module, name, original in touched:
            setattr(module, name, original)
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


#: Session-pinned agent DSN for the knowledge subsystem. It names an obviously
#: test-only database on a port nothing listens on, so a code path that falls
#: back to the default resolution fails IMMEDIATELY and loudly (connection
#: refused) instead of quietly succeeding against the operator's live knowledge
#: Postgres. Pinning it at a *reachable* database would be worse than useless:
#: the whole failure mode being closed here is a silent, plausible-looking
#: fallback.
KNOWLEDGE_GUARD_DSN = (
    "postgresql://omniagentos_test_guard@127.0.0.1:1/omniagentos_knowledge_test_guard_unset"
)


def knowledge_dsn_env_keys() -> tuple[str, str, str]:
    """``(agent, admin, migrate)`` env var names, from the frozen contract."""
    from omniagentos.knowledge.contracts import ENV_ADMIN_DSN, ENV_DSN, ENV_MIGRATE_DSN

    return (ENV_DSN, ENV_ADMIN_DSN, ENV_MIGRATE_DSN)


def scrub_production_knowledge_dsns(
    env: MutableMapping[str, str],
    secrets: MutableMapping[str, str],
) -> None:
    """Pin the agent DSN to the guard and clear the privileged DSNs, in BOTH layers.

    ``knowledge.config.dsn()`` resolves ``var/secrets/knowledge-pg.env`` BEFORE
    the environment (``_SECRETS.get(...) or os.getenv(...) or DEFAULT_DSN``), so
    pinning the environment alone is not isolation — it only holds while that
    secrets file happens to be absent. Once ``scripts/knowledge/pg-auth-setup.sh``
    has run, the file exists and carries the production agent/admin/superuser
    credentials, and an env-only pin would be silently overruled.

    ``admin``/``migrate`` are cleared rather than pinned: unset is already their
    fail-closed state (``api.deps`` raises, ``knowledge.cli`` errors out, the
    curator skips), and inventing a fake privileged DSN would only teach callers
    a shape they never see in production.
    """
    agent_key, admin_key, migrate_key = knowledge_dsn_env_keys()
    env[agent_key] = KNOWLEDGE_GUARD_DSN
    secrets.pop(agent_key, None)
    for key in (admin_key, migrate_key):
        env.pop(key, None)
        secrets.pop(key, None)


def production_knowledge_databases() -> frozenset[str]:
    """Database names no test may open a connection to.

    Derived from the frozen contract (``contracts.DEFAULT_DSN``) rather than
    hard-coded, so moving the production DSN moves the guard with it.
    """
    from psycopg.conninfo import conninfo_to_dict

    from omniagentos.knowledge.contracts import DEFAULT_DSN

    names = set()
    for candidate in (DEFAULT_DSN,):
        name = str(conninfo_to_dict(candidate).get("dbname") or "")
        if name:
            names.add(name)
    return frozenset(names)


def effective_dbname(conninfo: str, kwargs: Mapping[str, Any]) -> str:
    """The database a ``psycopg`` connect call would actually select.

    Parsed with libpq's own parser, never pattern-matched: libpq accepts
    repeated keywords and URI query parameters with last-one-wins precedence, so
    a hand-rolled scan can name a database nobody connects to (the reasoning is
    spelled out in ``tests.knowledge.db_ownership.parse_dbname_from_dsn``).
    ``psycopg.connect`` merges its keyword arguments OVER *conninfo*, so an
    explicit ``dbname=`` wins; when neither names a database, libpq falls back
    to ``PGDATABASE``, which is followed here for the same reason — the guard
    has to read the name that will actually be used, not the one that was typed.
    """
    from psycopg.conninfo import conninfo_to_dict

    params: dict[str, Any] = {}
    raw = (conninfo or "").strip()
    if raw:
        try:
            params = conninfo_to_dict(raw)
        except Exception:
            # Unreadable by libpq is unreadable by the connection too; let
            # psycopg raise its own error rather than guessing a name here.
            return ""
    if kwargs.get("dbname") is not None:
        params["dbname"] = kwargs["dbname"]
    return str(params.get("dbname") or os.environ.get("PGDATABASE") or "")


@pytest.fixture(autouse=True, scope="session")
def _isolate_knowledge_pg() -> Iterator[None]:
    """Keep knowledge disabled and off the operator's production Postgres by default.

    Same shape as ``_isolate_default_db``: pin session-wide, restore afterwards,
    and let a function-scoped ``monkeypatch.setenv`` win for the tests that
    deliberately enable knowledge or point at their own owned test database
    (``tests/knowledge/*`` via ``tests.knowledge.db_ownership``).

    A preset from the host shell is deliberately NOT honoured here — unlike
    ``OMNIAGENTOS_VAR`` — because ``scripts/launch-omniagentos.sh`` and
    ``scripts/scheduler/install-steward.sh`` both export the production DSN, so
    "respect the preset" would mean "use production" on exactly the boxes that
    have one.
    """
    from omniagentos.knowledge import config as knowledge_config

    previous_env = {key: os.environ.get(key) for key in knowledge_dsn_env_keys()}
    previous_knowledge = os.environ.get("OMNIAGENTOS_KNOWLEDGE")
    previous_secrets = dict(knowledge_config._SECRETS)
    scrub_production_knowledge_dsns(os.environ, knowledge_config._SECRETS)
    os.environ["OMNIAGENTOS_KNOWLEDGE"] = "0"
    try:
        yield
    finally:
        if previous_knowledge is None:
            os.environ.pop("OMNIAGENTOS_KNOWLEDGE", None)
        else:
            os.environ["OMNIAGENTOS_KNOWLEDGE"] = previous_knowledge
        for key, old in previous_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        knowledge_config._SECRETS.clear()
        knowledge_config._SECRETS.update(previous_secrets)


@pytest.fixture(autouse=True, scope="session")
def _refuse_production_knowledge_db() -> Iterator[None]:
    """Backstop at the driver: refuse to OPEN the production knowledge database.

    The env pin above fixes *default resolution*; this fixes *reachability*. It
    is the layer that does not care how the DSN arrived — a stale module-level
    ``from ... import dsn`` alias, a test that sets the production DSN by hand,
    a hard-coded string in a helper — because it checks the database libpq is
    about to select, at the moment of connecting, and raises instead.

    Deliberately a deny-list of production names rather than an allow-list of
    test names: the suite legitimately connects to ``postgres`` for bootstrap and
    to intentionally dead DSNs to prove degraded paths, and turning those into
    ``RuntimeError`` would change what those tests assert. The deny-list has no
    false positives and closes the only database that matters.
    """
    import psycopg

    denied = production_knowledge_databases()

    def _refuse_if_production(conninfo: str, kwargs: Mapping[str, Any]) -> None:
        name = effective_dbname(conninfo, kwargs)
        if name in denied:
            raise RuntimeError(
                f"refusing to connect to the PRODUCTION knowledge database {name!r} "
                "from a test process: it is the operator's live corpus and the "
                "knowledge_agent role holds INSERT on it. Point the test at its "
                "owned database (tests.knowledge.db_ownership) instead."
            )

    module_connect = psycopg.connect
    sync_connect = psycopg.Connection.__dict__["connect"]
    async_connect = psycopg.AsyncConnection.__dict__["connect"]

    def guarded_connect(conninfo: str = "", **kwargs: Any) -> Any:
        _refuse_if_production(conninfo, kwargs)
        return module_connect(conninfo, **kwargs)

    def guarded_sync_connect(cls: Any, conninfo: str = "", **kwargs: Any) -> Any:
        _refuse_if_production(conninfo, kwargs)
        return sync_connect.__func__(cls, conninfo, **kwargs)

    async def guarded_async_connect(cls: Any, conninfo: str = "", **kwargs: Any) -> Any:
        _refuse_if_production(conninfo, kwargs)
        return await async_connect.__func__(cls, conninfo, **kwargs)

    # All three entry points, because they are independent bindings: psycopg
    # binds ``connect = Connection.connect`` once at import, so patching the
    # class alone leaves ``psycopg.connect`` (what this repo actually calls)
    # unguarded, and patching the module alone leaves pools — which go through
    # ``connection_class.connect`` — unguarded.
    psycopg.connect = guarded_connect
    psycopg.Connection.connect = classmethod(guarded_sync_connect)
    psycopg.AsyncConnection.connect = classmethod(guarded_async_connect)
    try:
        yield
    finally:
        psycopg.connect = module_connect
        psycopg.Connection.connect = sync_connect
        psycopg.AsyncConnection.connect = async_connect


# ---------------------------------------------------------------------------
# Offline-lane guard: no live model / provider call without a live marker.
# ---------------------------------------------------------------------------

#: The markers ``pyproject``'s default ``addopts`` excludes because the test
#: behind them talks to something real, and therefore the ONLY markers that lift
#: this guard. ``perf`` and ``counterfeit_gate`` are excluded from the lane for
#: cost, not for liveness, so they are deliberately NOT here — a perf test that
#: reaches a model is as mismarked as any other.
#: ``tests/isolation/test_offline_lane_and_state_isolation.py`` pins this set
#: against the lane's own exclusion list; drift is exactly how the 9 leaked live
#: tests documented in the Makefile got back into the default lane.
LIVE_OPT_IN_MARKERS = frozenset({"live", "live_cli", "live_ollama"})


def local_provider_ports() -> dict[int, str]:
    """Loopback ports that are LIVE MODEL endpoints, derived from production.

    Loopback is otherwise allowed — a test that binds its own listener is not
    "outbound network" — but a local model daemon is a live provider call in
    every sense that matters here: it costs wall time, it is nondeterministic,
    and it is absent on CI. The port numbers are read out of the production
    defaults so moving an endpoint moves the guard with it.
    """
    import inspect
    from urllib.parse import urlsplit

    ports: dict[int, str] = {}

    def _add(url: object, label: str) -> None:
        if not isinstance(url, str):
            return
        port = urlsplit(url).port
        if port:
            ports[port] = label

    try:
        from omniagentos.knowledge.embeddings import OllamaEmbedding

        _add(inspect.signature(OllamaEmbedding.__init__).parameters["url"].default, "Ollama")
    except Exception:  # noqa: BLE001 -- an absent module cannot be reached either.
        pass
    try:
        from omniagentos.filesearch.embeddings import OLLAMA_HOST

        _add(OLLAMA_HOST, "Ollama")
    except Exception:  # noqa: BLE001
        pass
    try:
        from omniagentos.routing.api_policy import litellm_api_base

        _add(litellm_api_base(), "LiteLLM proxy")
    except Exception:  # noqa: BLE001
        pass
    return ports


def provider_cli_names() -> frozenset[str]:
    """CLI binaries the production adapters spawn, derived from the registry."""
    try:
        from omniagentos.adapters.registry import _ADAPTERS
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(
        key[len("cli-") :] for key in _ADAPTERS if key.startswith("cli-") and len(key) > 4
    )


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def refusal_for_connect(
    family: int, kind: int, address: object, provider_ports: Mapping[int, str]
) -> str | None:
    """Why this ``connect`` must be refused in an offline lane, or ``None``.

    Only AF_INET/AF_INET6 **SOCK_STREAM** is judged. A datagram ``connect`` is
    deliberately allowed: it performs a route lookup and sends no packet, which
    is how ``tests/lease/test_sandbox_escape.py::_non_loopback_ipv4`` discovers
    the host address ("Sends no packets" — its own docstring). Refusing it would
    change what that test asserts rather than close a leak.
    """
    if family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if kind != socket.SOCK_STREAM:
        return None
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = address[0], address[1]
    if not _is_loopback(host):
        return f"outbound network connection to {host}:{port}"
    label = provider_ports.get(port if isinstance(port, int) else -1)
    if label is not None:
        return f"live {label} call to {host}:{port}"
    return None


def _spawn_argv0(args: object) -> str | None:
    """The program a ``Popen`` would actually execute, or ``None`` if unreadable."""
    if isinstance(args, (str, bytes)):
        text = args.decode() if isinstance(args, bytes) else args
        parts = shlex.split(text)
        return parts[0] if parts else None
    if isinstance(args, Sequence):
        first = next(iter(args), None)
        if isinstance(first, (str, bytes, os.PathLike)):
            return os.fsdecode(first)
    return None


def refusal_for_spawn(
    args: object,
    env: Mapping[str, str] | None,
    cli_names: frozenset[str],
    test_owned_roots: Sequence[Path],
) -> str | None:
    """Why this ``Popen`` must be refused in an offline lane, or ``None``.

    Only binaries the production adapters actually spawn are judged, and only
    when the executable that PATH would select lives outside the pytest tmp root
    this session owns. That distinction matters and is not cosmetic:
    ``tests/entrypoints/conftest.py`` installs fake
    ``claude``/``codex``/``gemini``/``kimi``/``qwen``/``grok`` scripts under
    ``tmp_path`` and puts them first on ``PATH`` — those are stubs and must keep
    working. ``~/.local/bin/claude`` is not a stub, and neither is anything a
    test staged outside the session's own tmp tree.
    """
    import shutil

    argv0 = _spawn_argv0(args)
    if not argv0:
        return None
    if os.path.basename(argv0) not in cli_names:
        return None
    search_path = (env or os.environ).get("PATH")
    try:
        resolved = shutil.which(argv0, path=search_path) or argv0
        real = Path(resolved).resolve()
    except OSError:
        real = Path(argv0)
    for root in test_owned_roots:
        try:
            real.relative_to(root)
            return None
        except ValueError:
            continue
    return f"provider CLI spawn: {real}"


class OfflineLaneViolation(BaseException):
    """Raised when an unmarked test reaches a live model / provider / host.

    Derives from ``BaseException``, NOT ``Exception``, and that is the whole
    point. Every live seam in this repo is wrapped in a deliberately broad
    ``except Exception`` because it is best-effort by contract
    (``run_fable_json`` "an unavailable CLI must degrade, not crash intake";
    ``OllamaEmbedding.embed``'s per-endpoint ``except Exception: continue``;
    ``classify_chat_project``'s "LLM client unavailable"). An ``Exception``
    here would be caught by exactly those handlers and the violation would
    degrade into the same silence it exists to break — measured: the first cut
    of this guard turned ``test_e2e_real`` from a 44s live call into a SKIP and
    left ``test_route_returns_shape`` green.
    """


#: Process-global because the seams are process-global. Flipped per test by
#: ``_offline_guard_opt_in`` below.
_LIVE_ALLOWED = {"value": False}


def _offline_guard_message(reason: str) -> str:
    return (
        f"OFFLINE LANE VIOLATION — {reason}.\n"
        "The default pytest lane excludes live/live_cli/live_ollama/perf on purpose: "
        "it is an OFFLINE lane. A test that reaches a real model, provider CLI or "
        "remote host without one of those markers is MISMARKED, and the symptom is a "
        "silent multi-second block instead of a named failure.\n"
        "Fix it at the seam the production code already exposes (inject fable_runner=, "
        "knowledge_ingest=, client=, embedder=...), or mark the test @pytest.mark.live "
        "/ live_cli / live_ollama so it runs in `make test-live` instead."
    )


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_calls(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Turn a silent live call in the offline lane into an immediate named failure.

    Two seams, both process-global for the duration of the session:
    ``socket.socket.connect``/``connect_ex`` (every HTTP client in this repo —
    requests, httpx, urllib, psycopg — bottoms out there) and
    ``subprocess.Popen.__init__`` (``run``/``call``/``check_output`` all route
    through it).

    LIMITATION, stated because the merged psycopg guard has the same one and it
    was worth saying out loud: this is IN-PROCESS. A test that shells out to a
    helper which then makes the live call is not covered by the socket half —
    only by the ``Popen`` half, and only when the child is a known provider CLI.
    A child that runs pytest re-imports this conftest and is guarded again on its
    own account; a child that is an arbitrary script is not.
    """
    provider_ports = local_provider_ports()
    cli_names = provider_cli_names()
    # Deliberately ONLY the session's own pytest tmp root. The system temp dir
    # is not on this list (a stub staged there is indistinguishable from a real
    # install as far as this guard can tell), and neither is the repo — no
    # provider CLI is ever installed into the checkout, so allowing it would only
    # widen the hole.
    test_owned_roots = [Path(tmp_path_factory.getbasetemp()).resolve()]

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_popen_init = subprocess.Popen.__init__

    def _check_connect(sock: socket.socket, address: object) -> None:
        if _LIVE_ALLOWED["value"]:
            return
        reason = refusal_for_connect(sock.family, sock.type, address, provider_ports)
        if reason is not None:
            raise OfflineLaneViolation(_offline_guard_message(reason))

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        _check_connect(self, address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        _check_connect(self, address)
        return real_connect_ex(self, address)

    def guarded_popen_init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> Any:
        if not _LIVE_ALLOWED["value"]:
            reason = refusal_for_spawn(args, kwargs.get("env"), cli_names, test_owned_roots)
            if reason is not None:
                raise OfflineLaneViolation(_offline_guard_message(reason))
        return real_popen_init(self, args, *rest, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    subprocess.Popen.__init__ = guarded_popen_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        subprocess.Popen.__init__ = real_popen_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _offline_guard_opt_in(request: pytest.FixtureRequest) -> Iterator[None]:
    """Lift the offline guard for exactly the tests that carry a live marker."""
    markers = {marker.name for marker in request.node.iter_markers()}
    previous = _LIVE_ALLOWED["value"]
    _LIVE_ALLOWED["value"] = bool(markers & LIVE_OPT_IN_MARKERS)
    try:
        yield
    finally:
        _LIVE_ALLOWED["value"] = previous


@pytest.fixture(autouse=True)
def _fake_agent_view_registry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Agent View collector shells `claude agents --json` per profile — a
    provider CLI spawn the offline lane forbids. Discovery/board paths call it
    transitively (reconcile_board -> sync_external_sessions_to_board), so the
    fake seam lives HERE, centrally, not per test file. Tests that exercise
    enrollment monkeypatch their own registry over this; live tests carry a
    live marker and keep the real collector."""
    markers = {marker.name for marker in request.node.iter_markers()}
    if markers & LIVE_OPT_IN_MARKERS:
        return
    from omniagentos.sessions import agents_view

    monkeypatch.setattr(agents_view, "collect_all", lambda: {})


@pytest.fixture(autouse=True)
def _no_notification_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistence stays real; OS/ntfy delivery never fires from a test."""
    from omniagentos.notifications import service as _notifications_service

    monkeypatch.setattr(_notifications_service, "_push", lambda *_a, **_k: None)


# --- egress-env hermeticity (2026-08-10) ------------------------------------
#
# ``_no_notification_push`` above stubs ONE seam —
# ``notifications.service._push``. It does not cover
# ``omniagentos/sessions/notify.py``, which reads ``OMNI_NTFY_URL`` and
# ``OPS_ALERT_SLACK_WEBHOOK_URL`` straight out of ``os.environ`` and POSTs to
# whatever they name. ``pipeline/bridge/run-loop.sh`` exports ``OMNI_NTFY_URL``
# into every loop role, so any pytest started from a loop session — the merge
# gate included — inherits a LIVE endpoint.
#
# MEASURED, not inferred: with a counting HTTP listener standing in for the
# endpoint, one run of ``tests/notifications tests/sessions
# tests/steward/test_notify.py tests/alerts/test_monitor.py`` put 21 real POSTs
# on the wire (18 ntfy "Approval required", 3 Slack). Every test still PASSED —
# the damage is silent egress to the operator's phone on every gate run, which
# is why no red suite ever surfaced it. Against an endpoint that REFUSES the
# connection the same suites are also green, but ``pipeline/tests``'s five
# claim-repair tests are not: they answer differently depending on the ambient
# shell, which is the instrument-error-as-candidate-defect shape.
#
# This is the sibling of ``_isolate_default_db`` and the knowledge-DSN refusal
# above — "never let a test inherit a live external resource from the
# environment" — and it is deliberately spelled the same way as
# ``pipeline/tests/conftest.py``'s ``EGRESS_ENV_VARS`` so the two copies of the
# rule cannot drift apart in wording while claiming to be the same rule.
#
# FUNCTION-SCOPED AND AUTOUSE, both load-bearing. Autouse so no suite has to
# remember; function-scoped so a test that opts INTO a transport with its own
# ``monkeypatch.setenv`` still wins — this fixture's delenv runs first, the
# test's setenv runs second, and both unwind at teardown.
# ``tests/notifications/test_delivery.py`` and
# ``tests/sessions/core/test_notify_transport_guard.py`` depend on exactly that
# ordering and are the regression check for it. Production is untouched.

#: Spelled identically to ``pipeline/tests/conftest.py``'s list, so an opt-in
#: ``setenv`` in a test body overrides the same key this deletes.
EGRESS_ENV_VARS = (
    "OMNI_NTFY_URL",
    "OPS_ALERT_SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
)


@pytest.fixture(autouse=True)
def _scrub_egress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove egress-notification env vars for the duration of each test."""
    for name in EGRESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _require_testfarm_guard(config: pytest.Config) -> None:
    """A4 hermetic lane (``make test-hermetic``) — fail-loud handshake, opt-in only.

    ``TESTFARM_HERMETIC=1`` asserts "this run is supposed to be socket-guarded".
    The guard itself activates by *installation* (testfarm's ``pytest11`` entry
    point), so the failure modes worth closing here are the silent ones: the
    flag set but the plugin missing or disabled, and the flag set with
    ``--testfarm-allow-network`` slipped in — either way a run that LOOKS
    hermetic and enforces nothing. Refuse to start rather than degrade, and
    identify the active mode on stderr (``-q``-proof) plus a JUnit property.

    When the flag is unset this function returns before touching testfarm — no
    import, no hard dependency, zero behaviour change for ``make test`` /
    ``make test-fast`` (whose venv never has testfarm installed at all). Any
    future compatibility wiring against the plugin (for example composing this
    repo's own ``vcr_config`` from ``testfarm_vcr_config``) must live behind the
    same double guard: flag set AND try/except import.
    """
    if os.environ.get("TESTFARM_HERMETIC") != "1":
        return
    try:
        import testfarm.harness.plugin  # noqa: F401
    except ImportError as exc:
        raise pytest.UsageError(
            "TESTFARM_HERMETIC=1 but the testfarm harness is not importable in "
            "this venv, so the socket guard would NOT be active. Run the lane "
            "via `make test-hermetic` (it installs testfarm editable into "
            ".venv-hermetic), or unset TESTFARM_HERMETIC."
        ) from exc
    if not config.pluginmanager.hasplugin("testfarm_harness"):
        raise pytest.UsageError(
            "TESTFARM_HERMETIC=1 and testfarm is importable, but the "
            "testfarm_harness pytest plugin is not registered (disabled via "
            "-p no:testfarm_harness or PYTEST_DISABLE_PLUGIN_AUTOLOAD?). The "
            "hermetic lane refuses to run unguarded."
        )
    # --testfarm-allow-network disables the guard for the WHOLE run. Inside the
    # hermetic lane that is a consequential downgrade, so it needs a second,
    # explicit acknowledgement — otherwise a slipped-in flag would produce a
    # green run indistinguishable from a guarded one.
    allow_network = bool(config.getoption("--testfarm-allow-network", default=False))
    if allow_network and os.environ.get("TESTFARM_HERMETIC_ALLOW_NETWORK_ACK") != "1":
        raise pytest.UsageError(
            "TESTFARM_HERMETIC=1 with --testfarm-allow-network would run the "
            "hermetic lane UNGUARDED. If that is really intended, acknowledge "
            "it explicitly with TESTFARM_HERMETIC_ALLOW_NETWORK_ACK=1."
        )
    # Mode banner straight to stderr: pytest -q suppresses report headers, so
    # the plugin's own header line is not sufficient identification. This line
    # is emitted unconditionally whenever the hermetic handshake is active.
    mode = (
        "ALLOW-NETWORK — guard DISABLED via --testfarm-allow-network (acknowledged)"
        if allow_network
        else "blocked (loopback + unix sockets only)"
    )
    sys.stderr.write(f"testfarm hermetic lane: network {mode}\n")
    sys.stderr.flush()


@pytest.fixture(scope="session", autouse=True)
def _testfarm_guard_mode_junit(
    record_testsuite_property, request: pytest.FixtureRequest
) -> None:
    """Record the hermetic lane's guard mode as a JUnit testsuite property.

    Makes the mode machine-checkable in var/test-reports/test-hermetic-latest.xml
    (``testfarm_guard_mode`` = ``blocked`` | ``allow-network``), so an unguarded
    run can never present a report identical to a guarded one. Outside the
    hermetic lane (flag unset) this records nothing and does nothing.
    """
    if os.environ.get("TESTFARM_HERMETIC") != "1":
        return
    allow_network = bool(
        request.config.getoption("--testfarm-allow-network", default=False)
    )
    record_testsuite_property(
        "testfarm_guard_mode", "allow-network" if allow_network else "blocked"
    )


def pytest_configure(config: pytest.Config) -> None:
    _require_testfarm_guard(config)
    config.addinivalue_line(
        "markers",
        "real_auth: run with the real app-level session-token gate active "
        "(do not bypass require_session_token)",
    )


@pytest.fixture(autouse=True)
def _bypass_global_auth(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    if "omniagentos.api.main" not in sys.modules:
        # App-touching tests normally import omniagentos.api.main during
        # collection, including through per-directory API client fixtures.
        # The deliberate exceptions are tests/entrypoints/test_api_lifespan_indexes_vault.py,
        # tests/entrypoints/test_api_lifespan_mints_token.py,
        # tests/entrypoints/test_api_lifespan_seeds.py,
        # tests/entrypoints/test_api_startup_recovers_stale_swarms.py,
        # tests/entrypoints/test_api_startup_refuses_incoherent_sim.py,
        # tests/scripts/test_openapi_drift_check.py,
        # tests/scripts/test_reachability_gate_framework_routes.py,
        # tests/skills/test_integration.py, and tests/test_prod_imports.py.
        # They are lifespan/startup/smoke, OpenAPI-schema-drift, or skills/
        # production-import sanity checks: none exercises a route guarded by
        # require_session_token, so none needs this bypass. Keep the corpus
        # assertion in tests/meta/test_bypass_global_auth_allowlist.py green
        # before changing that set. Avoid importing the app tree for tests that
        # provably never use it.
        yield
        return
    try:
        from omniagentos.api.main import app, require_session_token
    except Exception:
        # Non-API tests never touch the app; nothing to bypass.
        yield
        return
    app.dependency_overrides[require_session_token] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_session_token, None)
