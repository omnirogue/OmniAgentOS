"""WP4 — runtime state resolves inside the campaign, never into the checkout.

Every converted call site is pinned twice, because both halves have failed
before:

1. **Production byte-identity.** The expected value is an oracle that literally
   re-implements the pre-conversion expression (see ``_base_*`` below), asserted
   with ``OMNIAGENTOS_VAR``/``OMNIAGENTOS_VAR_DIR`` SET — the launchers always
   export them (``scripts/launch-env.sh:103-104``), so "works when the env is
   empty" proves nothing.
2. **Campaign isolation, as a filesystem counterfeit.** A canary is planted on
   the production path, the operation runs under a campaign environment built
   exactly the way ``scripts/launch-env.sh`` builds one, and afterwards the
   canary's digest and mtime are unchanged, the production directory gained no
   entries at all, and the artifact is provably under the campaign root.

Tests live here rather than in each module's suite because they assert one
cross-cutting property (``omniagentos.runtime_paths``) over eight modules, and
because tests/hygiene is where the D3 ratchet that guards the same property
already lives.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from omniagentos import contracts, secret_registry, simgate
from omniagentos.employee_transcripts import storage as storage_module
from omniagentos.reflection import adapters as adapters_module
from omniagentos.runtime_paths import (
    CANONICAL_VAR_ENV_KEYS,
    TOKEN_VAR_ENV_KEYS,
    RuntimePathError,
    resolve_sim_context_or_none,
    resolve_var_root,
)
from omniagentos.sessions import chain_read, token

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Production targets, resolved from the package anchor at import time so a
#: campaign environment can never make a canary point at the campaign itself.
_PROD_CHAIN_READ_DIR = _REPO_ROOT / "var" / "sessions" / "chain-read"
_PROD_TRANSCRIPTS_DIR = _REPO_ROOT / "var" / "transcripts"
_PROD_SWARM_DIR = _REPO_ROOT / "var" / "swarm"
_PROD_SECRETS_DIR = _REPO_ROOT / "var" / "secrets"
_PROD_DB_PATH = _REPO_ROOT / "var" / "omniagentos.db"

_SIM_ENV_KEYS = (
    "OMNIAGENTOS_SIM_MODE",
    "OMNIAGENTOS_SIM_CAMPAIGN",
    "OMNIAGENTOS_SIM_ROOT",
    "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
)
_VAR_ENV_KEYS = ("OMNIAGENTOS_VAR", "OMNIAGENTOS_VAR_DIR")


# ---------------------------------------------------------------------------
# Oracles: the resolution each call site had BEFORE the conversion, verbatim.
# ---------------------------------------------------------------------------


def _base_chain_read_dir() -> Path:
    # chain_read.py:32 @ 2b54dc4c
    root = Path(
        os.environ.get("OMNIAGENTOS_VAR") or Path(chain_read.__file__).resolve().parents[2] / "var"
    )
    return root / "sessions" / "chain-read"


def _base_transcripts_root() -> Path:
    # employee_transcripts/storage.py:48-49 @ 2b54dc4c
    return Path(storage_module.__file__).resolve().parents[2] / "var" / "transcripts"


def _base_swarm_dir() -> Path:
    # reflection/adapters.py:505-513 @ 2b54dc4c
    override = os.environ.get("OMNIAGENTOS_SWARM_DIR")
    if override:
        return Path(override)
    return Path(adapters_module.__file__).resolve().parents[2] / "var" / "swarm"


def _base_db_path() -> str:
    # contracts.py:63-64 @ 2b54dc4c
    return os.environ.get("OMNIAGENTOS_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(contracts.__file__))),
        "var",
        "omniagentos.db",
    )


def _base_secret_dirs() -> list[str]:
    # secret_registry.py:117-124 @ 2b54dc4c
    home = os.path.expanduser("~")
    dirs = [os.path.realpath(os.path.join(home, rel)) for rel in secret_registry.SECRET_DIR_RELS]
    dirs.append(
        os.path.realpath(
            str(Path(secret_registry.__file__).resolve().parents[1] / "var" / "secrets")
        )
    )
    # P0 worktree-miss fix: a linked worktree also registers the MAIN checkout's
    # store, so the golden mirrors that entry when (and only when) the suite runs
    # from a worktree. In a production main checkout this is None and the base list
    # is unchanged, keeping the production byte-identity assertion intact.
    if secret_registry._MAIN_WORKTREE_ROOT is not None:
        dirs.append(os.path.realpath(str(secret_registry._MAIN_WORKTREE_ROOT / "var" / "secrets")))
    seen: list[str] = []
    for entry in dirs:
        if entry and entry not in seen:
            seen.append(entry)
    return seen


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        _SIM_ENV_KEYS
        + _VAR_ENV_KEYS
        + (
            "OMNIAGENTOS_DB",
            "OMNIAGENTOS_SWARM_DIR",
            "OMNIAGENTOS_LEDGER_DIR",
            "OMNIAGENTOS_VAULT_DIR",
        )
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A NON-sim environment with the var variables set, as the launchers do."""
    _clear_runtime_env(monkeypatch)
    var_dir = tmp_path / "runtime"
    var_dir.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_dir))
    return var_dir


def _make_campaign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, var_dir_leaf: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """Build a campaign env the way ``scripts/launch-env.sh`` builds one.

    ``var_dir_leaf`` covers the legal variant where the var root is a
    subdirectory of the campaign root rather than the campaign root itself
    (``simgate`` only requires inode containment).
    """
    _clear_runtime_env(monkeypatch)
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / "camp-wp4"
    var_dir = campaign_root.joinpath(*var_dir_leaf)
    var_dir.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "camp-wp4")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_dir))
    return campaign_root, var_dir


@pytest.fixture
def campaign(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The launcher's layout: ``OMNIAGENTOS_VAR_DIR == campaign root``."""
    campaign_root, _ = _make_campaign(monkeypatch, tmp_path)
    return campaign_root


@contextlib.contextmanager
def production_canary(directory: Path) -> Iterator[Path]:
    """Assert a production directory is untouched across the block.

    Content hash AND mtime of a planted canary, plus the directory's full entry
    list — the last one is what catches "the sim wrote a NEW file here".
    """
    created_root: Path | None = None
    if not directory.exists():
        probe = directory
        while not probe.exists():
            created_root = probe
            probe = probe.parent
        directory.mkdir(parents=True)
    canary = directory / f".wp4-canary-{uuid4().hex}"
    canary.write_bytes(b"canary")
    digest = hashlib.sha256(canary.read_bytes()).hexdigest()
    mtime_ns = canary.stat().st_mtime_ns
    entries = sorted(path.name for path in directory.iterdir())
    try:
        yield canary
        assert canary.is_file(), f"production canary was deleted: {canary}"
        assert hashlib.sha256(canary.read_bytes()).hexdigest() == digest, (
            f"production canary content changed: {canary}"
        )
        assert canary.stat().st_mtime_ns == mtime_ns, f"production canary mtime changed: {canary}"
        assert sorted(path.name for path in directory.iterdir()) == entries, (
            f"production directory gained or lost entries: {directory}"
        )
    finally:
        canary.unlink(missing_ok=True)
        if created_root is not None:
            with contextlib.suppress(OSError):
                for path in sorted(created_root.rglob("*"), reverse=True):
                    path.rmdir()
                created_root.rmdir()


@contextlib.contextmanager
def unchanged_file(path: Path) -> Iterator[None]:
    """Assert a production FILE is neither created nor modified in the block."""
    existed = path.exists()
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if existed else None
    yield
    assert path.exists() is existed, f"production file existence changed: {path}"
    if existed:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
            f"production file content changed: {path}"
        )


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------


def test_resolver_package_anchor_matches_the_legacy_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_env(monkeypatch)
    assert resolve_var_root(leaf=("sessions", "chain-read")) == _base_chain_read_dir()
    assert resolve_var_root() == _REPO_ROOT / "var"


def test_resolver_honours_the_callers_own_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAR-first and VAR_DIR-first callers must both keep their own answer."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_VAR", "/tmp/wp4-var")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", "/tmp/wp4-var-dir")
    assert resolve_var_root(CANONICAL_VAR_ENV_KEYS) == Path("/tmp/wp4-var")
    assert resolve_var_root(TOKEN_VAR_ENV_KEYS) == Path("/tmp/wp4-var-dir")


def test_resolver_uses_the_environment_value_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """No expanduser/resolve: normalising changes relative, ~ and symlink roots."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_VAR", "relative-var")
    assert resolve_var_root(CANONICAL_VAR_ENV_KEYS, ("x",)) == Path("relative-var/x")
    monkeypatch.setenv("OMNIAGENTOS_VAR", "~/tilde-var")
    assert resolve_var_root(CANONICAL_VAR_ENV_KEYS, ("x",)) == Path("~/tilde-var/x")


def test_resolver_refuses_the_package_anchor_under_a_campaign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_campaign(monkeypatch, tmp_path)
    with pytest.raises(RuntimePathError) as excinfo:
        resolve_var_root(env_keys=("OMNIAGENTOS_SWARM_DIR",), leaf=("swarm",))
    message = str(excinfo.value)
    assert "OMNIAGENTOS_SWARM_DIR" in message, "the refusal must name the env fix"
    assert str(_REPO_ROOT / "var") in message


def test_resolver_refuses_an_incoherent_simulation_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SimGateError propagates — an unvalidatable env is never 'assume production'."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "yes")
    with pytest.raises(simgate.SimGateError):
        resolve_var_root()
    with pytest.raises(simgate.SimGateError):
        resolve_sim_context_or_none()


def test_resolver_reports_no_context_outside_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    assert resolve_sim_context_or_none() is None


# ---------------------------------------------------------------------------
# secret_registry (spec 2a): a campaign's own secrets must be protected
# ---------------------------------------------------------------------------


def test_secret_dirs_production_is_byte_identical(production_env: Path) -> None:
    assert secret_registry.secret_dirs() == _base_secret_dirs()


@pytest.mark.parametrize("var_dir_leaf", [(), ("var",)])
def test_campaign_secrets_are_protected_by_both_layers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var_dir_leaf: tuple[str, ...]
) -> None:
    """The campaign's token directory joins the classifier AND the sandbox deny-list.

    Parametrised over both legal layouts (var root == campaign root, and var root
    inside it) because the guarded directory must be derived from the token's own
    spelling, not from a guessed one.
    """
    from omniagentos.runner.sandbox import secret_read_deny_roots

    _campaign_root, var_dir = _make_campaign(monkeypatch, tmp_path, var_dir_leaf=var_dir_leaf)
    token_dir = token.token_path().parent
    assert token_dir == var_dir / "secrets"

    dirs = secret_registry.secret_dirs()
    assert os.path.realpath(str(token_dir)) in dirs, "campaign secrets left unprotected"
    assert os.path.realpath(str(token_dir)) in secret_read_deny_roots()
    # The production tree stays guarded too — this ADDS a root, never swaps one.
    assert os.path.realpath(str(_PROD_SECRETS_DIR)) in dirs


def test_campaign_secret_dir_is_protected_before_it_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``is_dir()`` guard: a secrets dir created later must not be unprotected."""
    _campaign_root, var_dir = _make_campaign(monkeypatch, tmp_path)
    assert not (var_dir / "secrets").exists()
    assert os.path.realpath(str(var_dir / "secrets")) in secret_registry.secret_dirs()


def test_secret_dirs_fails_closed_on_an_incoherent_sim_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned on purpose: a deny-list must refuse, never silently narrow."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")  # no campaign -> incoherent
    with pytest.raises(simgate.SimGateError):
        secret_registry.secret_dirs()


# ---------------------------------------------------------------------------
# contracts.default_db_path (spec 2b): O-16 way 2
# ---------------------------------------------------------------------------


def test_default_db_path_production_is_byte_identical(production_env: Path) -> None:
    assert contracts.default_db_path() == _base_db_path()
    assert contracts.default_db_path() == str(_PROD_DB_PATH)


def test_default_db_path_still_lets_the_environment_win(
    campaign: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher exports OMNIAGENTOS_DB; ignoring it invents a second DB."""
    explicit = campaign / "chosen.sqlite3"
    monkeypatch.setenv("OMNIAGENTOS_DB", str(explicit))
    assert contracts.default_db_path() == str(explicit)


def test_default_db_path_under_a_campaign_is_the_campaign_db(campaign: Path) -> None:
    with unchanged_file(_PROD_DB_PATH):
        resolved = Path(contracts.default_db_path())
        # The launcher's own spelling: launch-env.sh sets
        # OMNIAGENTOS_DB="$campaign_root/state.sqlite3".
        assert resolved == campaign / "state.sqlite3"
        connection = sqlite3.connect(resolved)
        connection.execute("CREATE TABLE probe (id TEXT)")
        connection.commit()
        connection.close()
    assert resolved.is_file()


@pytest.mark.parametrize(
    ("resolver", "env_name", "legacy_name"),
    [
        (contracts.default_ledger_dir, "OMNIAGENTOS_LEDGER_DIR", "ledger"),
        (contracts.default_vault_dir, "OMNIAGENTOS_VAULT_DIR", "vault"),
    ],
)
def test_default_ledger_and_vault_production_paths_are_byte_identical(
    production_env: Path, resolver, env_name: str, legacy_name: str
) -> None:
    assert resolver() == str(_REPO_ROOT / legacy_name)


@pytest.mark.parametrize(
    ("resolver", "env_name"),
    [
        (contracts.default_ledger_dir, "OMNIAGENTOS_LEDGER_DIR"),
        (contracts.default_vault_dir, "OMNIAGENTOS_VAULT_DIR"),
    ],
)
def test_default_ledger_and_vault_environment_overrides_win_in_sim(
    campaign: Path, monkeypatch: pytest.MonkeyPatch, resolver, env_name: str
) -> None:
    explicit = campaign.parent / "custom-runtime-state"
    monkeypatch.setenv(env_name, str(explicit))
    assert resolver() == str(explicit)


@pytest.mark.parametrize(
    ("resolver", "leaf"),
    [
        (contracts.default_ledger_dir, "ledger"),
        (contracts.default_vault_dir, "vault"),
    ],
)
def test_default_ledger_and_vault_follow_campaign_when_unset(
    campaign: Path, monkeypatch: pytest.MonkeyPatch, resolver, leaf: str
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_LEDGER_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAULT_DIR", raising=False)
    assert resolver() == str(campaign / leaf)


@pytest.mark.parametrize(
    "resolver",
    [contracts.default_ledger_dir, contracts.default_vault_dir],
)
def test_default_ledger_and_vault_propagate_bad_simgate(
    monkeypatch: pytest.MonkeyPatch, resolver
) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "invalid")
    with pytest.raises(simgate.SimGateError):
        resolver()


# ---------------------------------------------------------------------------
# employee_transcripts/storage (spec 2c)
# ---------------------------------------------------------------------------


def test_transcript_root_production_is_byte_identical(production_env: Path) -> None:
    """VAR/VAR_DIR are set — this call site never honoured them and must not now."""
    assert storage_module._default_root() == _base_transcripts_root()


def test_transcripts_land_in_the_campaign(
    campaign: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omniagentos.contracts import utc_now_iso
    from omniagentos.db.store import SqliteStore
    from omniagentos.employee_transcripts.store import TranscriptStore

    monkeypatch.delenv("OMNIAGENTOS_TRANSCRIPTS_ROOT", raising=False)
    assert storage_module._default_root() == campaign / "transcripts"

    database = SqliteStore(str(tmp_path / "storage.db"))
    database._connection.execute(
        "INSERT INTO employees (id, name, status, created_at) VALUES (?,?,?,?)",
        ("emp_wp4", "WP4", "active", utc_now_iso()),
    )
    with production_canary(_PROD_TRANSCRIPTS_DIR):
        service = storage_module.TranscriptStorageService(TranscriptStore(database))
        row = service.write("emp_wp4", "notes.txt", b"campaign bytes")
    written = service.root / row["storage_path"]
    assert written.is_file()
    assert written.read_bytes() == b"campaign bytes"
    assert campaign in written.parents


# ---------------------------------------------------------------------------
# sessions/chain_read (spec 2d)
# ---------------------------------------------------------------------------


def test_chain_read_production_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMNIAGENTOS_VAR keeps winning; the package anchor is unchanged."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var-dir"))
    assert chain_read._state_path("s1") == _base_chain_read_dir() / "s1.json"

    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR")
    assert chain_read._state_path("s1") == _base_chain_read_dir() / "s1.json"

    monkeypatch.delenv("OMNIAGENTOS_VAR")
    with production_canary(_PROD_CHAIN_READ_DIR):
        assert chain_read._state_path("s1") == _base_chain_read_dir() / "s1.json"


def test_chain_read_now_also_honours_var_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented, intentional divergence: VAR_DIR joins, but only second."""
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "vardir"))
    assert (
        chain_read._state_path("s1") == tmp_path / "vardir" / "sessions" / "chain-read" / "s1.json"
    )


def test_chain_read_state_lands_in_the_campaign(campaign: Path) -> None:
    state = chain_read.ChainReadState()
    state.relevant.append("/some/file.py")
    with production_canary(_PROD_CHAIN_READ_DIR):
        chain_read.save_state("sess-wp4", state)
        path = chain_read._state_path("sess-wp4")
    assert path == campaign / "sessions" / "chain-read" / "sess-wp4.json"
    assert path.is_file()
    assert chain_read.load_state("sess-wp4").relevant == ["/some/file.py"]


# ---------------------------------------------------------------------------
# reflection/adapters (spec 2e)
# ---------------------------------------------------------------------------


def test_swarm_dir_production_is_byte_identical(production_env: Path) -> None:
    """The regression this pins: <repo>/var/swarm must not become <VAR_DIR>/swarm.

    Every producer of swarm verdicts (swarm/planner.py, land-approved-lanes.py)
    is repo-anchored, so relocating discovery finds zero verdicts.
    """
    adapter = adapters_module.SwarmVerdictSourceAdapter()
    assert adapter._swarm_dir() == _base_swarm_dir()
    assert adapter._swarm_dir() == _PROD_SWARM_DIR


def test_swarm_dir_honours_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_SWARM_DIR", str(tmp_path / "explicit"))
    adapter = adapters_module.SwarmVerdictSourceAdapter()
    assert adapter._swarm_dir() == _base_swarm_dir() == tmp_path / "explicit"


def test_swarm_dir_isolates_under_a_campaign_without_raising(campaign: Path) -> None:
    """Nothing in the repo SETS OMNIAGENTOS_SWARM_DIR, and harvest.py calls
    discover() unguarded — so this must resolve, not raise."""
    adapter = adapters_module.SwarmVerdictSourceAdapter()
    with production_canary(_PROD_SWARM_DIR):
        resolved = adapter._swarm_dir()
        assert adapter.discover(0.0) == []
    assert resolved == campaign / "swarm"


# ---------------------------------------------------------------------------
# sessions/token (spec 3): verification reads, minting is explicit
# ---------------------------------------------------------------------------


def test_verifying_a_missing_token_fails_auth_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "secrets" / "sessions-token"
    monkeypatch.setattr(token, "TOKEN_PATH", override)

    assert token.verify_token("anything") is False
    assert token.verify_token(None) is False
    assert token.verify_token("") is False
    assert token._load_token() is None

    assert not override.exists(), "verification MINTED the credential"
    assert not override.parent.exists(), "verification created var/secrets"


def test_the_mint_path_creates_exactly_once_at_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "secrets" / "sessions-token"
    monkeypatch.setattr(token, "TOKEN_PATH", override)

    minted = token.load_or_create_token()
    assert override.is_file()
    assert stat.S_IMODE(override.stat().st_mode) == 0o600
    assert stat.S_IMODE(override.parent.stat().st_mode) == 0o700
    first = override.stat()

    again = token.load_or_create_token()
    assert again == minted, "the token was re-minted on the second call"
    assert override.stat().st_ino == first.st_ino
    assert override.stat().st_mtime_ns == first.st_mtime_ns
    assert token.verify_token(minted) is True
    assert token.verify_token("wrong") is False


def test_api_token_mint_is_confined_to_startup_lifespan() -> None:
    """Only trusted startup may mint; request handlers and auth checks never may."""

    def callers_of(callee: str) -> list[str]:
        callers: list[str] = []
        for path in sorted((_REPO_ROOT / "omniagentos" / "api").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            call_names = {callee}
            call_names.update(
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name == callee
            )
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if called not in call_names:
                    continue
                parent = parents.get(node)
                while parent is not None and not isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    parent = parents.get(parent)
                symbol = parent.name if parent is not None else "<module>"
                callers.append(f"{path.relative_to(_REPO_ROOT)}:{symbol}")
        return callers

    assert callers_of("load_or_create_token") == [
        "omniagentos/api/main.py:_mint_session_token_on_first_boot"
    ]
    assert callers_of("_mint_session_token_on_first_boot") == [
        "omniagentos/api/main.py:lifespan"
    ]
    main_tree = ast.parse(
        (_REPO_ROOT / "omniagentos" / "api" / "main.py").read_text(encoding="utf-8")
    )
    mint_definition = next(
        node
        for node in ast.walk(main_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_mint_session_token_on_first_boot"
    )
    assert mint_definition.decorator_list == [], "the startup minter must never become a route"


# ---------------------------------------------------------------------------
# Out-of-package re-derivations (spec 4)
# ---------------------------------------------------------------------------


def _load_script(relative: str, name: str):
    """Load a hyphenated script tree by path, exactly as its launchd job does."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: dataclass field resolution looks the module up.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("relative", "name"),
    [
        ("scripts/golden-suite/run_golden.py", "wp4_run_golden"),
        ("scripts/backlog-executor/executor.py", "wp4_executor"),
    ],
)
def test_script_var_roots_are_production_stable_and_campaign_isolated(
    relative: str,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Production stays ``<repo>/var`` (its readers are repo-anchored); a campaign
    moves the whole tree — improvement log, history, backlog, token — inside it."""
    module = _load_script(relative, name)

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "runtime"))
    assert module.var_root() == _REPO_ROOT / "var"

    campaign_root, _ = _make_campaign(monkeypatch, tmp_path)
    assert module.var_root() == campaign_root


def test_backlog_executor_paths_follow_the_campaign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_script("scripts/backlog-executor/executor.py", "wp4_executor_paths")
    campaign_root, _ = _make_campaign(monkeypatch, tmp_path)
    assert module.backlog_dir() == campaign_root / "backlog"
    assert module.improvement_log_path() == campaign_root / "improvement-log.jsonl"
    assert module.log_path() == campaign_root / "log" / "backlog-executor.log"
    assert module.default_token_path() == campaign_root / "secrets" / "sessions-token"


def test_golden_suite_defaults_follow_the_campaign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_script("scripts/golden-suite/run_golden.py", "wp4_run_golden_paths")
    campaign_root, _ = _make_campaign(monkeypatch, tmp_path)
    assert module.default_history_path() == campaign_root / "golden" / "history.jsonl"
    assert module.default_runs_dir() == campaign_root / "golden" / "runs"
    assert module.default_improvement_log_path() == campaign_root / "improvement-log.jsonl"
    assert module.default_token_path() == campaign_root / "secrets" / "sessions-token"


def test_hook_client_remains_an_explicit_runtime_minter() -> None:
    """Spawned hook clients still obtain the established runtime credential."""
    source = (_REPO_ROOT / "omniagentos" / "sessions" / "hook_client.py").read_text(
        encoding="utf-8"
    )
    assert source.count("load_or_create_token") >= 2
