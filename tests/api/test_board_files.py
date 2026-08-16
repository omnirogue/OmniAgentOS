"""Adversarial + contract tests for the P2 board-files API.

Exercises the pinned contract against the REAL app (mirrors
``tests/api/test_mounts_routes.py``'s style for this file-serving surface):

  * ``POST /api/board/{id}/files/upload``
  * ``GET  /api/board/{id}/files``
  * ``GET  /api/board/{id}/files/download``
  * ``GET  /api/board/{id}/files/archive``

Every route in :mod:`omniagentos.api.routes.board_files` declares its OWN
``Depends(_authorized)`` rather than teaching the app-level
``require_session_token`` gate a new path pattern (main.py stays append-only),
so -- unlike most of this test suite -- the session-token requirement here is
NOT bypassed by the suite-wide autouse fixture in ``tests/conftest.py`` (that
fixture only overrides ``require_session_token`` itself). Every request below
therefore carries ``auth_headers`` (from ``tests/api/conftest.py``) except the
dedicated "no token" probes at the bottom, exactly like
``tests/api/test_sessions.py`` does for its own locally-gated routes.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import UploadFile

import omniagentos
from omniagentos import simgate
from omniagentos.api import files_common
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import board_files
from omniagentos.api.routes import sessions as sessions_routes
from omniagentos.api.routes.board_files import resolve_board_workspace
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.api.services import ApiError
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.path_containment import inode_relative_parts
from omniagentos.policy import dir_grants, load_policy
from omniagentos.policy.dir_grants import DirGrantError
from omniagentos.sessions.dal import SessionsDal
from omniagentos.simgate import SimContext
from tests.support.db_template import migrated_db

_REAL_HOME = Path(os.path.realpath(os.path.expanduser("~")))
_FIRMLINK_HOME = Path(f"/System/Volumes/Data{_REAL_HOME}")
_PRODUCTION_CHECKOUT = _REAL_HOME / "OmniAgentOS"


def _set_valid_sim_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campaign: str = "campaign"
) -> Path:
    """Install a complete simgate environment and return its campaign root."""
    sim_root = tmp_path / "sim-root"
    campaign_root = sim_root / campaign
    var_dir = campaign_root / "var"
    var_dir.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", campaign)
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    return campaign_root


def _macos_home_firmlink_available() -> bool:
    try:
        return _FIRMLINK_HOME.exists() and os.path.samestat(
            os.stat(_FIRMLINK_HOME), os.stat(_REAL_HOME)
        )
    except OSError:
        return False


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class Harness:
    def __init__(
        self, client: httpx.AsyncClient, store: Any, collab: CollabStore, session_dal: SessionsDal
    ) -> None:
        self.client = client
        self.store = store
        self.collab = collab
        self.session_dal = session_dal

    def make_session_card(
        self, workspace: Path, *, session_id: str | None = None
    ) -> tuple[str, str]:
        """Create a live, session-linked board card whose workspace is ``workspace``."""
        workspace.mkdir(parents=True, exist_ok=True)
        session_id = session_id or f"ses_{new_suffix()}"
        self.session_dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "project_dir": str(workspace),
                "state": "running",
                "model": "test",
            }
        )
        card = BoardTask(title="Files card", result_ref=session_id)
        self.collab.create_board_task(card)
        return str(card.id), session_id


_COUNTER = 0


def new_suffix() -> str:
    global _COUNTER
    _COUNTER += 1
    return f"t{_COUNTER}"


# --- scratch location -------------------------------------------------------
#
# This whole module drives a FILE-SERVING SECURITY SURFACE, so its per-test
# workspaces must live where production workspaces actually live -- NOT under a
# system temp dir the surface deliberately refuses to serve.


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Per-test scratch rooted OFF ``board_files._SYSTEM_DENY`` (F-2/LS-017).

    ``board_files._deny_system`` fail-closed-403s any path resolving under
    ``/tmp`` / ``/private/tmp`` / ``/etc`` / ``~/.ssh`` -- a deliberate control:
    this unsandboxed surface must never serve a workspace out of a
    world-writable system temp dir. On Linux ``tempfile.gettempdir()`` is
    ``/tmp``, so pytest's builtin ``tmp_path`` lands under a denied root and
    every test here that builds a LEGITIMATE workspace (``OMNIAGENTOS_VAR_DIR``-
    anchored or grantable-mount-rooted, exactly as intake/project workspaces
    are) under ``tmp_path`` and then asserts a file is SERVED -- list, download,
    archive, reveal -- is denied on Linux only. macOS uses ``/var/folders/...``,
    which is not denied, which is why this suite is green there. Production
    workspaces live under ``<repo>/var`` or a grantable mount, never ``/tmp``:
    the failure is an artifact of where pytest puts scratch on Linux, so the fix
    belongs HERE (put the scratch where production workspaces live), NOT in the
    guard -- weakening ``_deny_system`` would drop a real control the deny tests
    in this same module (``test_deny_guard_denies_system_roots``,
    ``test_download_denies_system_directory_reachable_via_symlink``) require.

    Overrides the builtin ``tmp_path`` for THIS module only, so the suite-wide
    convention that ``tmp_path`` sits under ``tempfile.gettempdir()`` (relied on
    by e.g. ``tests/orchestrator/test_approvals_floors.py``) is untouched
    elsewhere. The DENY tests are unaffected -- they target explicit
    ``/private/tmp`` / ``/etc`` / secret-name / symlink-escape paths, not the
    scratch location. Only diverges from the builtin where the default temp dir
    is itself denied (Linux); ``/var/tmp`` is a world-writable, persistent temp
    dir on Linux and macOS that resolves under NONE of the deny roots.
    """
    default_base = Path(tempfile.gettempdir()).resolve()
    under_denied = any(
        inode_relative_parts(default_base, Path(root)) is not None
        for root in board_files._SYSTEM_DENY
    )
    parent = "/var/tmp" if under_denied else None
    if parent is not None and not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
        parent = None  # extraordinary; fall back to the builtin default
    path = Path(tempfile.mkdtemp(prefix="board-files-", dir=parent)).resolve()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    db = migrated_db(CollabStore, tmp_path / "board.db")
    collab = CollabStore(db)
    store = collab._store
    session_dal = SessionsDal(db)
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: session_dal)
    # The F-015 approved-root floor only allows this repo's var/ dir plus
    # currently-grantable mount roots -- every OTHER test below builds its
    # card workspace under tmp_path, which is neither, so the floor is widened
    # to tmp_path here for test convenience. The dedicated approved-root-floor
    # tests further down use ``real_floor_board`` instead, which does NOT
    # widen it, so they exercise the real floor.
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(tmp_path.resolve())])
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    harness = Harness(client, store, collab, session_dal)
    try:
        yield harness
    finally:
        _run(client.aclose())
        app.dependency_overrides.clear()


@pytest.fixture
def real_floor_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """Like ``board``, but does NOT widen the F-015 approved-root floor.

    Used only by the dedicated approved-root-floor tests, which need the REAL
    floor (this repo's ``var/`` dir + ``grantable_mount_roots()``) active so
    they can prove a ``$HOME``-rooted workspace is refused and a var/mount-
    rooted one is still served.
    """
    db = migrated_db(CollabStore, tmp_path / "floor-board.db")
    collab = CollabStore(db)
    store = collab._store
    session_dal = SessionsDal(db)
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: session_dal)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    harness = Harness(client, store, collab, session_dal)
    try:
        yield harness
    finally:
        _run(client.aclose())
        app.dependency_overrides.clear()


# --- resolve_board_workspace: the frozen resolution order ---------------------


def test_resolve_order_session_result_ref(board: Harness, tmp_path: Path) -> None:
    work = tmp_path / "ses-workspace"
    task_id, _ = board.make_session_card(work)
    row, workspace = resolve_board_workspace(board.store, board.collab, task_id)
    assert row is not None
    assert workspace == work


def test_resolve_order_run_id_tools_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'tools'-mode run's ``plan_json[0].params.working_dir`` resolves via run_id."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = migrated_db(CollabStore, tmp_path / "runboard.db")
    collab = CollabStore(db)
    store = collab._store
    dispatched = dispatch_spec(
        store,
        collab,
        load_policy(),
        RefinedSpec(title="Tools run", description="do work"),
        harness=HarnessType.MOCK.value,
        execute="tools",
    )
    board_id = str(dispatched["board_task"]["id"])
    row, workspace = resolve_board_workspace(store, collab, board_id)
    assert row is not None
    assert workspace is not None
    assert str(workspace) == dispatched["working_dir"]


def test_resolve_orch_prefix_missing_table_degrades_to_none(board: Harness) -> None:
    """migration 039's ``orchestrations`` table does not exist in this worktree --
    an ``orch_`` result_ref must degrade to 'no workspace', never crash."""
    card = BoardTask(title="Orch card", result_ref="orch_does_not_exist")
    board.collab.create_board_task(card)
    row, workspace = resolve_board_workspace(board.store, board.collab, card.id)
    assert row is not None
    assert workspace is None


def test_resolve_unknown_card_returns_none(board: Harness) -> None:
    row, workspace = resolve_board_workspace(board.store, board.collab, "btk_missing")
    assert row is None
    assert workspace is None


def test_resolve_card_with_no_linkage_returns_no_workspace(board: Harness) -> None:
    card = BoardTask(title="Unlinked card")
    board.collab.create_board_task(card)
    row, workspace = resolve_board_workspace(board.store, board.collab, card.id)
    assert row is not None
    assert workspace is None


# --- F-015: approved-root floor (real floor, NOT the tmp_path-widened one) ----


def test_approved_workspace_roots_sim_mode_excludes_running_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = _set_valid_sim_context(tmp_path, monkeypatch)
    monkeypatch.setattr(board_files, "grantable_mount_roots", lambda: [])

    checkout = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
    )

    assert board_files._approved_workspace_roots() == [str(campaign_root.resolve())]
    assert checkout not in board_files._approved_workspace_roots()


@pytest.mark.parametrize(
    "candidate_kind", ["etc", "home-ssh", "private-tmp", "running-checkout", "basename-mismatch"]
)
def test_sim_workspace_floor_measured_admissions_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    _set_valid_sim_context(tmp_path, monkeypatch)
    candidates = {
        "etc": Path("/etc"),
        "home-ssh": _REAL_HOME / ".ssh",
        "private-tmp": Path("/private/tmp"),
        "running-checkout": Path(
            os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
        ),
    }
    if candidate_kind == "basename-mismatch":
        mismatched_root = tmp_path / "sim-root" / "not-campaign"
        mismatched_var = mismatched_root / "var"
        mismatched_var.mkdir(parents=True)
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(mismatched_root))
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(mismatched_var))
        candidate = mismatched_root
    else:
        candidate = candidates[candidate_kind]

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(candidate.resolve(strict=False))
    assert excinfo.value.status_code == 403


def test_approved_workspace_roots_sim_mode_admits_campaign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = _set_valid_sim_context(tmp_path, monkeypatch)

    assert board_files._approved_workspace_roots() == [os.path.realpath(campaign_root)]


def test_approved_workspace_roots_rejects_sim_context_without_campaign_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimContext(sim_mode=True, campaign="x", campaign_root=None)
    monkeypatch.setattr(simgate, "resolve_sim_context", lambda environ=None: context)

    with pytest.raises(ApiError) as excinfo:
        board_files._approved_workspace_roots()
    assert excinfo.value.status_code == 403
    assert "operator error" not in excinfo.value.message

    with pytest.raises(DirGrantError, match="broken invariant"):
        dir_grants.allowed_grant_roots(sim_ctx=context)


def test_sim_context_without_campaign_root_rejected_under_optimized_python() -> None:
    script = """
from omniagentos import simgate
from omniagentos.api.routes import board_files
from omniagentos.api.services import ApiError
from omniagentos.policy import dir_grants
from omniagentos.policy.dir_grants import DirGrantError
from omniagentos.simgate import SimContext

context = SimContext(sim_mode=True, campaign="x", campaign_root=None)
simgate.resolve_sim_context = lambda: context

try:
    board_files._approved_workspace_roots()
except ApiError as exc:
    if exc.status_code != 403 or "broken invariant" not in exc.message:
        raise SystemExit("board_files refused with the wrong error")
else:
    raise SystemExit("board_files returned a root for a broken sim context")

try:
    dir_grants.allowed_grant_roots(sim_ctx=context)
except DirGrantError as exc:
    if "broken invariant" not in str(exc):
        raise SystemExit("dir_grants refused with the wrong error")
else:
    raise SystemExit("dir_grants returned a root for a broken sim context")
"""
    completed = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_approved_workspace_roots_sim_mode_canonicalizes_campaign_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = _set_valid_sim_context(tmp_path, monkeypatch)
    alias = campaign_root.parent / "campaign-alias"
    alias.symlink_to(campaign_root, target_is_directory=True)
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(alias))

    assert board_files._approved_workspace_roots() == [str(campaign_root.resolve())]


def test_approved_workspace_roots_sim_mode_excludes_mount_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = os.path.realpath(tmp_path / "production-mount")
    calls = 0

    def fake_grantable_mount_roots() -> list[str]:
        nonlocal calls
        calls += 1
        return [sentinel]

    monkeypatch.setattr(board_files, "grantable_mount_roots", fake_grantable_mount_roots)
    campaign_root = _set_valid_sim_context(tmp_path, monkeypatch)

    assert board_files._approved_workspace_roots() == [os.path.realpath(campaign_root)]
    assert calls == 0

    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE")
    assert sentinel in board_files._approved_workspace_roots()
    assert calls == 1


def test_approved_workspace_roots_non_sim_mode_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_root = tmp_path / "var-root"
    mount_roots = [
        os.path.realpath(tmp_path / "mount-a"),
        os.path.realpath(tmp_path / "mount-b"),
    ]
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setattr(board_files, "grantable_mount_roots", lambda: mount_roots)

    assert board_files._approved_workspace_roots() == [board_files._repo_var_dir()] + mount_roots


# --- F-1 (LS-018): the non-sim approved-root floor must refuse a candidate
# root that is itself a code checkout, including the live serving checkout ---


def test_approved_workspace_roots_non_sim_mode_excludes_checkout_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grantable mount root that happens to BE a code checkout must never
    reach the approved-root list -- only a plain, non-checkout mount root
    survives the filter, alongside the repo var/ dir."""
    marker_name = "." + "git"
    var_root = tmp_path / "var-root"
    checkout_mount = tmp_path / "mount-checkout"
    plain_mount = os.path.realpath(tmp_path / "mount-plain")
    (checkout_mount / marker_name).mkdir(parents=True)
    os.makedirs(plain_mount, exist_ok=True)
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setattr(
        board_files,
        "grantable_mount_roots",
        lambda: [os.path.realpath(checkout_mount), plain_mount],
    )

    roots = board_files._approved_workspace_roots()

    assert os.path.realpath(checkout_mount) not in roots
    assert plain_mount in roots
    assert board_files._repo_var_dir() in roots


def test_approved_workspace_roots_non_sim_mode_excludes_serving_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live serving checkout itself, admitted as a grantable mount by
    identity (no local marker directory required to reach it), is refused."""
    var_root = tmp_path / "var-root"
    checkout = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
    )
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setattr(board_files, "grantable_mount_roots", lambda: [checkout])

    roots = board_files._approved_workspace_roots()

    assert checkout not in roots


def test_is_code_checkout_root_ignores_a_not_yet_created_root(
    tmp_path: Path,
) -> None:
    """A root that simply does not exist yet (many approved roots, e.g. an
    intake var/ dir, are created lazily) is not itself flagged as a checkout
    -- the F-015 floor's own containment/existence checks are what refuse an
    absent workspace, not this marker probe."""
    missing = tmp_path / "never-created"
    assert board_files._is_code_checkout_root(str(missing)) is False



# --- F-2 (LS-017): _deny_guard must fail closed on OS-sensitive directories,
# a workspace-floor-independent second gate -----------------------------------


def test_deny_guard_denies_system_roots() -> None:
    # Exercise the guard against the roots the module ACTUALLY resolved on THIS
    # host (macOS collapses /etc->/private/etc, /tmp->/private/tmp; Linux keeps
    # /etc, /tmp), skipping any spelling that does not exist here. The prior form
    # hard-coded /private/etc/hosts + /private/tmp, which on Linux realpath to
    # nonexistent paths under neither /etc nor /tmp, so the required ubuntu-latest
    # gate saw the deny never fire. At least one system root (/etc) always exists.
    tested = 0
    for root in board_files._SYSTEM_DENY:
        if not Path(root).is_dir():
            continue
        tested += 1
        with pytest.raises(ApiError) as excinfo:
            board_files._deny_guard(Path(root))
        assert excinfo.value.status_code == 403
    assert tested, "no denied system root exists on this host to exercise"


def test_deny_guard_admits_ordinary_workspace_path(tmp_path: Path) -> None:
    # This asserts the ADMIT path. On Linux pytest's tmp_path is under /tmp,
    # which _SYSTEM_DENY now (correctly) covers, so admission there is a false
    # premise; skip where the harness tmpdir is itself a denied root (macOS uses
    # /var/folders, which is not denied).
    resolved_tmp = os.path.realpath(tmp_path)
    if any(
        resolved_tmp == root or resolved_tmp.startswith(root.rstrip("/") + os.sep)
        for root in board_files._SYSTEM_DENY
    ):
        pytest.skip("pytest tmp_path resolves under a denied system root on this host")
    ordinary = tmp_path / "uploads" / "notes.txt"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text("fine", encoding="utf-8")
    board_files._deny_guard(ordinary.resolve())


def test_download_denies_system_directory_reachable_via_symlink(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """A workspace-relative symlink whose target resolves under a denied
    system root (here /private/tmp) must be refused on the REALPATH, not the
    caller-supplied string -- the exact TOCTOU/symlink shape the containment
    rule exists to close."""
    work = tmp_path / "download-system-escape-ws"
    task_id, _ = board.make_session_card(work)
    real_system_target = Path(os.path.realpath("/private/tmp"))
    link = work / "escape"
    link.symlink_to(real_system_target)
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "escape"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("campaign_value", ["", None], ids=["empty", "unset"])
def test_workspace_floor_half_set_sim_env_fails_closed_without_mount_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    campaign_value: str | None,
) -> None:
    registry_workspace = tmp_path / "registry-workspace"
    registry_workspace.mkdir()
    calls = 0

    def fake_grantable_mount_roots() -> list[str]:
        nonlocal calls
        calls += 1
        return [str(registry_workspace)]

    monkeypatch.setattr(board_files, "grantable_mount_roots", fake_grantable_mount_roots)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "sim-var"))
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)
    if campaign_value is None:
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    else:
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", campaign_value)

    checkout = Path(os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__))))
    for workspace in (registry_workspace, checkout, checkout / "var"):
        with pytest.raises(ApiError) as excinfo:
            board_files._enforce_workspace_floor(workspace.resolve(strict=False))
        assert excinfo.value.status_code == 403
        assert "OMNIAGENTOS_SIM_CAMPAIGN is missing or empty" in excinfo.value.message
    assert calls == 0


def test_sim_campaign_root_filesystem_root_is_dropped_and_cannot_admit_machine_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-home"
    for relative in ("Work", "Desktop", ".ssh"):
        (fake_home / relative).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "selfopt-001")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", "/")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "campaign-var"))

    with pytest.raises(ApiError) as excinfo:
        board_files._approved_workspace_roots()
    assert excinfo.value.status_code == 403

    for candidate in (
        Path("/etc"),
        fake_home,
        fake_home / ".ssh",
        fake_home / "Work",
        fake_home / "Desktop",
        Path("/private/tmp"),
        Path("/"),
    ):
        with pytest.raises(ApiError) as excinfo:
            board_files._enforce_workspace_floor(candidate.resolve(strict=False))
        assert excinfo.value.status_code == 403, str(candidate)


@pytest.mark.parametrize("campaign_root_kind", ["home", "home-ancestor"])
def test_sim_campaign_root_home_or_ancestor_is_dropped_and_cannot_admit_home_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    campaign_root_kind: str,
) -> None:
    home_ancestor = tmp_path / "home-ancestor"
    fake_home = home_ancestor / "fake-home"
    workspace = fake_home / "Work"
    workspace.mkdir(parents=True)
    campaign_root = fake_home if campaign_root_kind == "home" else home_ancestor
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "selfopt-001")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "campaign-var"))

    with pytest.raises(ApiError) as excinfo:
        board_files._approved_workspace_roots()
    assert excinfo.value.status_code == 403

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(workspace.resolve())
    assert excinfo.value.status_code == 403


@pytest.mark.skipif(
    not _macos_home_firmlink_available(),
    reason="macOS /System/Volumes/Data home firmlink is unavailable",
)
def test_workspace_floor_refuses_firmlink_home_library_by_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "selfopt-001")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "campaign-var"))

    data_volume = Path("/System/Volumes/Data")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(data_volume))
    with pytest.raises(ApiError) as excinfo:
        board_files._approved_workspace_roots()
    assert excinfo.value.status_code == 403
    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor((_FIRMLINK_HOME / "Work").resolve())
    assert excinfo.value.status_code == 403

    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(_FIRMLINK_HOME))
    with pytest.raises(ApiError) as excinfo:
        board_files._approved_workspace_roots()
    assert excinfo.value.status_code == 403

    # Force the approved root spelling so the home-inode guard proves a
    # firmlink alias cannot split the verdict for the same inode.
    monkeypatch.setattr(
        board_files,
        "_approved_workspace_roots",
        lambda: [os.path.realpath(_FIRMLINK_HOME)],
    )
    normal_workspace = Path(os.path.realpath(_REAL_HOME / "Library" / "Messages"))
    firmlink_workspace = Path(os.path.realpath(_FIRMLINK_HOME / "Library" / "Messages"))

    for workspace in (normal_workspace, firmlink_workspace):
        with pytest.raises(ApiError) as excinfo:
            board_files._enforce_workspace_floor(workspace)
        assert excinfo.value.status_code == 403


@pytest.mark.skipif(
    not _macos_home_firmlink_available(),
    reason="macOS /System/Volumes/Data home firmlink is unavailable",
)
def test_board_floor_migration_admits_safe_firmlink_root_by_inode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old lexical containment split one safe root inode into two verdicts."""
    workspace = (_REAL_HOME / "Work").resolve()
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setattr(
        board_files,
        "_approved_workspace_roots",
        lambda: [str(_FIRMLINK_HOME)],
    )

    assert board_files._enforce_workspace_floor(workspace) is None


def test_workspace_floor_refuses_home_library_case_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-home"
    (fake_home / "Library").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(
        board_files,
        "_approved_workspace_roots",
        lambda: [os.path.realpath(fake_home)],
    )

    for spelling in ("Library", "library"):
        workspace = (fake_home / spelling / "Messages").resolve(strict=False)
        with pytest.raises(ApiError) as excinfo:
            board_files._enforce_workspace_floor(workspace)
        assert excinfo.value.status_code == 403


def test_files_common_migration_old_vs_new_uses_inode_ancestry(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real_sub = real / "sub"
    real_sub.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    candidate = real_sub.resolve()
    legacy_lexical_verdict = candidate == alias or alias in candidate.parents
    assert legacy_lexical_verdict is False
    assert files_common.is_within(candidate, alias)
    assert files_common.inode_relative_parts(candidate, alias) == ("sub",)

    prefix_collision = tmp_path / "real-evil"
    prefix_collision.mkdir()
    assert files_common.inode_relative_parts(prefix_collision, real) is None
    assert files_common.inode_relative_parts(candidate, tmp_path / "missing-root") is None


@pytest.mark.parametrize(
    "candidate_kind",
    [
        "production-checkout",
        "production-vault",
        "legacy-home-checkout",
        "home",
        "home-work",
        "home-desktop",
        "home-ssh",
        "home-library-messages",
        "etc",
        "filesystem-root",
        "private-tmp",
        "campaign-prefix-collision",
        "symlink-to-production-checkout",
    ],
)
def test_sim_workspace_floor_refusal_battery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    campaign_root = tmp_path / "sim-root" / "campaign"
    campaign_root.mkdir(parents=True)
    prefix_collision = tmp_path / "campaign-evil"
    prefix_collision.mkdir()
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(tmp_path / "sim-root"))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    var_root = campaign_root / "var"
    var_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))

    candidates = {
        "production-checkout": _PRODUCTION_CHECKOUT,
        "production-vault": _PRODUCTION_CHECKOUT / "vault",
        "legacy-home-checkout": _REAL_HOME / "OmniAgentOS",
        "home": _REAL_HOME,
        "home-work": _REAL_HOME / "Work",
        "home-desktop": _REAL_HOME / "Desktop",
        "home-ssh": _REAL_HOME / ".ssh",
        "home-library-messages": _REAL_HOME / "Library" / "Messages",
        "etc": Path("/etc"),
        "filesystem-root": Path("/"),
        "private-tmp": Path("/private/tmp"),
        "campaign-prefix-collision": prefix_collision,
    }
    if candidate_kind == "symlink-to-production-checkout":
        if not _PRODUCTION_CHECKOUT.exists():
            pytest.skip("production checkout does not exist")
        link = campaign_root / "production-link"
        link.symlink_to(_PRODUCTION_CHECKOUT, target_is_directory=True)
        candidate = link.resolve()
    else:
        candidate = candidates[candidate_kind]
        if not candidate.exists():
            pytest.skip(f"absolute refusal target does not exist: {candidate}")
        candidate = candidate.resolve()

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(candidate)
    assert excinfo.value.status_code == 403


def test_sim_workspace_floor_refusal_battery_always_checks_other_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = tmp_path / "sim-root" / "campaign"
    campaign_root.mkdir(parents=True)
    var_root = campaign_root / "var"
    var_root.mkdir()
    other_checkout = tmp_path / "other-checkout"
    (other_checkout / "omniagentos").mkdir(parents=True)
    (other_checkout / "vault").mkdir()
    other_checkout_link = campaign_root / "other-checkout-link"
    other_checkout_link.symlink_to(other_checkout, target_is_directory=True)

    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(tmp_path / "sim-root"))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))

    resolved_checkout = other_checkout.resolve()
    approved_roots = [Path(root) for root in board_files._approved_workspace_roots()]
    assert all(not files_common.is_within(resolved_checkout, root) for root in approved_roots)

    for candidate in (
        resolved_checkout,
        (other_checkout / "vault").resolve(),
        other_checkout_link.resolve(),
    ):
        with pytest.raises(ApiError) as excinfo:
            board_files._enforce_workspace_floor(candidate)
        assert excinfo.value.status_code == 403


@pytest.mark.parametrize("sim_isolated", [False, True], ids=["non-sim", "sim"])
def test_workspace_floor_refuses_out_of_root_in_all_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sim_isolated: bool
) -> None:
    approved_parent = tmp_path / "approved"
    candidate = tmp_path / "outside"
    candidate.mkdir()
    campaign_root = approved_parent / "campaign"
    campaign_root.mkdir(parents=True)
    var_root = campaign_root / "var"
    var_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setattr(
        board_files,
        "grantable_mount_roots",
        lambda: [os.path.realpath(approved_parent / "mount")],
    )
    if sim_isolated:
        monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign")
        monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(approved_parent))
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    else:
        monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(candidate.resolve())

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "workspace outside approved roots"


def test_workspace_floor_refuses_string_prefix_without_path_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "campaign"
    prefix_collision = tmp_path / "campaign-evil"
    approved.mkdir()
    prefix_collision.mkdir()
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(approved.resolve())])

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(prefix_collision.resolve())

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "workspace outside approved roots"


def test_workspace_floor_refuses_unresolved_symlink_to_out_of_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decisive: unresolved symlink into production must be 403, not admitted."""
    approved = tmp_path / "approved"
    production = tmp_path / "production"
    approved.mkdir()
    production.mkdir()
    link = approved / "link-to-production"
    link.symlink_to(production)
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(approved.resolve())])

    # Differential: the old lexical containment fork admits this spelling.
    assert link == approved or approved in link.parents
    assert inode_relative_parts(link, approved) is None
    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(link)

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "workspace path is not canonical"
    assert str(link) in str(excinfo.value.detail)


def test_workspace_floor_refuses_unresolved_dotdot_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    production = tmp_path / "production"
    approved.mkdir()
    production.mkdir()
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(approved.resolve())])
    candidate = approved / ".." / "production"

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(candidate)

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "workspace path is not canonical"


def test_workspace_floor_refuses_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(approved.resolve())])

    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(Path("approved"))

    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "workspace path is not canonical"


def test_workspace_floor_admits_canonical_approved_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a realpath'd, genuinely-approved workspace still clears the floor."""
    approved = tmp_path / "approved"
    workspace = approved / "ws"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(approved.resolve())])

    board_files._enforce_workspace_floor(workspace.resolve())


def test_workspace_floor_admits_when_root_is_symlink_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides: symlink-spelled approved root must still admit a canonical workspace."""
    real_root = tmp_path / "real-approved"
    link_root = tmp_path / "link-approved"
    real_root.mkdir()
    link_root.symlink_to(real_root)
    workspace = real_root / "ws"
    workspace.mkdir()
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(link_root)])

    board_files._enforce_workspace_floor(workspace.resolve())


def test_home_rooted_workspace_refused_by_approved_root_floor(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's live probe: a card whose workspace IS $HOME must be
    refused before anything under it (e.g. Library/Messages/chat.db) can ever
    be reached, not merely denied per-file after the fact."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    task_id, _ = real_floor_board.make_session_card(fake_home)

    listing = _run(real_floor_board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert listing.status_code == 403
    assert listing.json()["error"]["code"] == "forbidden"

    download = _run(
        real_floor_board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "Library/Messages/chat.db"},
            headers=auth_headers,
        )
    )
    assert download.status_code == 403

    archive = _run(
        real_floor_board.client.get(f"/api/board/{task_id}/files/archive", headers=auth_headers)
    )
    assert archive.status_code == 403

    upload = _run(
        real_floor_board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("x.txt", b"data", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert upload.status_code == 403


def test_refused_upload_creates_no_directory_outside_approved_roots(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
) -> None:
    """A 403 from the F-015 floor must also mean nothing was created.

    The refusal above asserts the VERDICT only, and cannot observe the side
    effect: its workspace already exists, so ``mkdir(exist_ok=True)`` is a
    no-op there. With a workspace that does NOT exist, the upload route used
    to ``mkdir(parents=True)`` the row-supplied path and refuse it afterwards,
    leaving a tree outside every approved root. The read routes never did this
    -- they resolve strictly and 404 -- so this pins the write path to the same
    "decide before you create" order ``projects.py`` already uses.
    """
    escape_root = tmp_path / "escape"
    workspace = escape_root / "deep" / "tree"
    task_id, _ = real_floor_board.make_session_card(workspace)
    # The card now names a workspace outside every approved root. Remove it so
    # the ONLY thing that could create it is the server.
    shutil.rmtree(escape_root)
    assert not escape_root.exists()

    upload = _run(
        real_floor_board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("x.txt", b"data", "text/plain"))],
            headers=auth_headers,
        )
    )

    assert upload.status_code == 403
    assert upload.json()["error"]["message"] == "workspace outside approved roots"
    assert not escape_root.exists(), (
        f"refused upload created a directory tree outside every approved root: {workspace}"
    )


def test_refused_upload_creates_nothing_for_a_client_supplied_cwd(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
) -> None:
    """The same refusal, driven end to end through the real HTTP routes.

    ``POST /api/sessions/ingest`` stores ``project_dir`` straight from the
    client's ``cwd`` (``sessions.py``, "never gates"), so the path the upload
    route creates is client-supplied rather than harness-injected. This is the
    reachability half of the finding and would not be covered by the fixture
    form above.
    """
    escape_root = tmp_path / "client-named"
    workspace = escape_root / "a" / "b" / "c"
    assert not escape_root.exists()

    ingest = _run(
        real_floor_board.client.post(
            "/api/sessions/ingest",
            json={"session_id": "ses_boardfloor1", "cwd": str(workspace)},
            headers=auth_headers,
        )
    )
    assert ingest.status_code == 200

    card = BoardTask(title="client-named workspace", result_ref="ses_boardfloor1")
    real_floor_board.collab.create_board_task(card)

    upload = _run(
        real_floor_board.client.post(
            f"/api/board/{card.id}/files/upload",
            files=[("files", ("x.txt", b"data", "text/plain"))],
            headers=auth_headers,
        )
    )

    assert upload.status_code == 403
    assert not escape_root.exists(), (
        f"refused upload created a client-named tree outside every approved root: {workspace}"
    )


def test_var_intake_workspace_still_served_by_approved_root_floor(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace under <var>/intake-workspace/... -- the OMNIAGENTOS_VAR_DIR
    knob every real intake/project workspace is anchored to -- clears the floor."""
    var_dir = tmp_path / "var"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    workspace = var_dir / "intake-workspace" / "task123"
    task_id, _ = real_floor_board.make_session_card(workspace)
    (workspace / "uploads").mkdir()
    (workspace / "uploads" / "input.csv").write_text("a,b\n", encoding="utf-8")

    resp = _run(real_floor_board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 200
    assert [row["rel"] for row in resp.json()["files"]] == ["uploads/input.csv"]


def test_grantable_mount_rooted_workspace_still_served_by_approved_root_floor(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace under a currently-grantable mount root (the SAME registry
    :func:`omniagentos.mounts.grantable_mount_roots` exposes) clears the floor."""
    mount_root = tmp_path / "fake-mount"
    mount_root.mkdir()
    monkeypatch.setattr(board_files, "grantable_mount_roots", lambda: [str(mount_root)])
    workspace = mount_root / "some-project"
    task_id, _ = real_floor_board.make_session_card(workspace)
    (workspace / "outputs").mkdir()
    (workspace / "outputs" / "result.txt").write_text("ok", encoding="utf-8")

    resp = _run(real_floor_board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 200
    assert [row["rel"] for row in resp.json()["files"]] == ["outputs/result.txt"]


def test_home_rooted_grantable_mount_stays_admitted_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home-rooted grantable mount root stays ADMITTED; gate 2 refuses only
    the skip-set top levels, never all of $HOME."""
    fake_home = tmp_path / "fake-home"
    mount_root = fake_home / "Work"
    workspace = mount_root / "project-a"
    workspace.mkdir(parents=True)
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(fake_home, target_is_directory=True)

    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(board_files, "grantable_mount_roots", lambda: [str(mount_root)])

    assert board_files._enforce_workspace_floor(workspace.resolve()) is None
    alias_workspace = home_alias / "Work" / "project-a"
    assert alias_workspace.resolve() == workspace.resolve()
    assert board_files._enforce_workspace_floor(alias_workspace.resolve()) is None


def test_approved_root_under_home_library_still_refused_belt_and_braces(
    real_floor_board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: a workspace that technically clears the approved-root
    floor (it sits under the OMNIAGENTOS_VAR_DIR override) is STILL refused
    when it resolves under $HOME/Library -- the same top-level name mounts.py's
    ``_HOME_TOP_LEVEL_SKIP`` refuses for direct home-mount access."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    var_dir = fake_home / "Library" / "var"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    workspace = var_dir / "intake-workspace" / "task456"
    task_id, _ = real_floor_board.make_session_card(workspace)

    resp = _run(real_floor_board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# --- unit coverage for the shared upload cap logic (files_common.py) ----------


def test_collect_safe_uploads_enforces_per_file_cap() -> None:
    upload = UploadFile(filename="big.bin", file=io.BytesIO(b"x" * 20))
    with pytest.raises(ApiError) as excinfo:
        files_common.collect_safe_uploads(
            [upload], validate_name=lambda n: n or "x", max_file_bytes=10
        )
    assert excinfo.value.status_code == 413
    assert excinfo.value.code == "file_too_large"


def test_collect_safe_uploads_enforces_cumulative_request_cap() -> None:
    uploads = [
        UploadFile(filename="a.bin", file=io.BytesIO(b"x" * 8)),
        UploadFile(filename="b.bin", file=io.BytesIO(b"y" * 8)),
    ]
    with pytest.raises(ApiError) as excinfo:
        files_common.collect_safe_uploads(
            uploads, validate_name=lambda n: n or "x", max_file_bytes=100, max_request_bytes=10
        )
    assert excinfo.value.status_code == 413
    assert excinfo.value.code == "request_too_large"


# --- POST /api/board/{id}/files/upload -----------------------------------------


def test_upload_saves_under_uploads_and_matches_frozen_contract(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "upload-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("report.txt", b"hello world", "text/plain"))],
            data={"instructions": "please review"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["workspace"] == str(work.resolve())
    assert body["saved"] == [
        {"name": "report.txt", "path": "uploads/report.txt", "size": len(b"hello world")}
    ]
    saved_file = work / "uploads" / "report.txt"
    assert saved_file.read_bytes() == b"hello world"
    assert "please review" in (work / "uploads" / "_instructions.md").read_text(encoding="utf-8")


def test_upload_unknown_card_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    resp = _run(
        board.client.post(
            "/api/board/btk_missing/files/upload",
            files=[("files", ("x.txt", b"data", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 404


def test_upload_card_with_no_workspace_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    card = BoardTask(title="No workspace")
    board.collab.create_board_task(card)
    resp = _run(
        board.client.post(
            f"/api/board/{card.id}/files/upload",
            files=[("files", ("x.txt", b"data", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 404


def test_upload_strips_path_components_from_filename(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """A crafted filename carrying directory components collapses to its
    basename -- it can never write outside uploads/ or shadow a nested path."""
    work = tmp_path / "upload-traversal-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("../../etc/passwd", b"pwned", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 201
    saved = resp.json()["saved"]
    assert saved == [{"name": "passwd", "path": "uploads/passwd", "size": 5}]
    assert (work / "uploads" / "passwd").exists()
    # Never escaped uploads/ -- nothing was written outside the workspace.
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize("hostile_name", ["", ".env", ".", ".."])
def test_upload_rejects_empty_and_dotfile_only_names(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], hostile_name: str
) -> None:
    work = tmp_path / f"upload-hostile-ws-{hostile_name or 'empty'}"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", (hostile_name, b"data", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 422, (
        f"{hostile_name!r} should have been rejected, got {resp.status_code}"
    )


@pytest.mark.parametrize("secret_name", ["id_rsa", "connections.env", "credentials", "server.pem"])
def test_upload_rejects_secret_registry_named_files(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], secret_name: str
) -> None:
    """F-017: an upload named after a secret-registry entry 422s at write time,
    via the SAME oracle (``references_secret``) the read path's ``_deny_secret``
    uses -- never silently written, then only refused on later download."""
    work = tmp_path / f"upload-secret-ws-{secret_name}"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", (secret_name, b"secret material", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 422, (
        f"{secret_name!r} should have been rejected, got {resp.status_code}"
    )
    assert not (work / "uploads" / secret_name).exists()


def test_upload_rejects_reserved_instructions_log_name(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """F-017: an upload literally named ``_instructions.md`` is refused -- it
    would otherwise splice forged lines into the append-only operator log."""
    work = tmp_path / "upload-reserved-name-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("_instructions.md", b"forged log line", "text/plain"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 422
    assert not (work / "uploads" / "_instructions.md").exists()


def test_upload_oversize_file_is_413(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_common, "MAX_UPLOAD_FILE_BYTES", 10)
    work = tmp_path / "upload-oversize-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[("files", ("big.bin", b"x" * 50, "application/octet-stream"))],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 413


def test_upload_oversize_request_is_413(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_common, "MAX_UPLOAD_FILE_BYTES", 1000)
    monkeypatch.setattr(files_common, "MAX_UPLOAD_REQUEST_BYTES", 10)
    work = tmp_path / "upload-oversize-req-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/upload",
            files=[
                ("files", ("a.bin", b"x" * 8, "application/octet-stream")),
                ("files", ("b.bin", b"y" * 8, "application/octet-stream")),
            ],
            headers=auth_headers,
        )
    )
    assert resp.status_code == 413


# --- GET /api/board/{id}/files -------------------------------------------------


def test_list_merges_uploads_outputs_and_session_files_deduped(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "list-ws"
    task_id, session_id = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "input.csv").write_text("a,b\n", encoding="utf-8")
    (work / "outputs").mkdir()
    (work / "outputs" / "result.md").write_text("# result\n", encoding="utf-8")
    # A session-reported file that duplicates the upload (by resolved path) must
    # be deduped in favor of the upload/output scan, plus a genuinely new one.
    (work / "notes.txt").write_text("live progress note\n", encoding="utf-8")
    board.session_dal.set_session_files(
        session_id,
        json.dumps([str(work / "uploads" / "input.csv"), str(work / "notes.txt")]),
    )

    resp = _run(board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace"] == str(work.resolve())
    rels = {row["rel"]: row["kind"] for row in body["files"]}
    assert rels == {
        "uploads/input.csv": "upload",
        "outputs/result.md": "output",
        "notes.txt": "session_file",
    }
    for row in body["files"]:
        assert set(row.keys()) == {"rel", "size", "mtime", "kind"}


def test_list_unknown_card_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    resp = _run(board.client.get("/api/board/btk_missing/files", headers=auth_headers))
    assert resp.status_code == 404


def test_list_card_with_no_workspace_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    card = BoardTask(title="No workspace")
    board.collab.create_board_task(card)
    resp = _run(board.client.get(f"/api/board/{card.id}/files", headers=auth_headers))
    assert resp.status_code == 404


def test_list_excludes_secret_named_file_in_workspace(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "list-secret-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "connections.env").write_text("SECRET=x", encoding="utf-8")
    (work / "uploads" / "safe.txt").write_text("ok", encoding="utf-8")
    resp = _run(board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 200
    rels = [row["rel"] for row in resp.json()["files"]]
    assert "uploads/connections.env" not in rels
    assert "uploads/safe.txt" in rels


def test_list_ignores_session_reported_file_outside_workspace(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "list-outside-ws"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never be listed", encoding="utf-8")
    task_id, session_id = board.make_session_card(work)
    board.session_dal.set_session_files(session_id, json.dumps([str(outside)]))
    resp = _run(board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
    assert resp.status_code == 200
    assert resp.json()["files"] == []


# --- GET /api/board/{id}/files/download ----------------------------------------


def test_download_happy_path(board: Harness, tmp_path: Path, auth_headers: dict[str, str]) -> None:
    work = tmp_path / "download-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "report.txt").write_text("the report", encoding="utf-8")
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "uploads/report.txt"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    assert resp.text == "the report"


def test_download_unknown_card_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    resp = _run(
        board.client.get(
            "/api/board/btk_missing/files/download", params={"path": "x.txt"}, headers=auth_headers
        )
    )
    assert resp.status_code == 404


def test_download_missing_file_is_404(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    task_id, _ = board.make_session_card(tmp_path / "download-missing-ws")
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "nope.txt"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "hostile_path",
    ["../outside.txt", "uploads/../../outside.txt", "/etc/passwd", "a\x00b"],
)
def test_download_rejects_traversal_and_absolute_paths(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], hostile_path: str
) -> None:
    work = tmp_path / f"download-traversal-ws-{abs(hash(hostile_path))}"
    outside = tmp_path / "outside.txt"
    outside.write_text("must never be reachable", encoding="utf-8")
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": hostile_path},
            headers=auth_headers,
        )
    )
    assert resp.status_code in (403, 404), f"{hostile_path!r} got {resp.status_code}"
    assert resp.status_code != 500


def test_download_rejects_symlink_escape(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "download-symlink-ws"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never be reachable via symlink", encoding="utf-8")
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "escape.txt").symlink_to(outside)
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "uploads/escape.txt"},
            headers=auth_headers,
        )
    )
    assert resp.status_code in (403, 404)


def test_download_denies_secret_registry_name(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "download-secret-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "connections.env").write_text("SECRET=x", encoding="utf-8")
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "uploads/connections.env"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403


def test_download_denies_home_dotfile_parity(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home-dotfile deny (``home_dotfile_read_deny_pattern``, reused verbatim
    from ``mounts.py``) matches a dot-prefixed component directly under $HOME --
    a card workspace living under one (e.g. a hidden per-agent scratch dir) has
    every file it contains denied, mirroring mounts.py's own
    ``.config/omni/connections.env`` nested-dotfile-ancestor case, even though
    ``notes.txt`` itself carries no secret-registry basename."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = home / ".claude_workspace" / "cardwork"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "notes.txt").write_text("not a secret name", encoding="utf-8")
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "uploads/notes.txt"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403


def test_download_oversize_file_is_413(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.board_files as board_files_routes

    monkeypatch.setattr(board_files_routes, "_MAX_DOWNLOAD_BYTES", 8)
    work = tmp_path / "download-oversize-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "big.bin").write_bytes(b"x" * 50)
    resp = _run(
        board.client.get(
            f"/api/board/{task_id}/files/download",
            params={"path": "uploads/big.bin"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 413


# --- GET /api/board/{id}/files/archive -----------------------------------------


def test_archive_zips_uploads_and_outputs(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "archive-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "a.txt").write_text("A", encoding="utf-8")
    (work / "outputs").mkdir()
    (work / "outputs" / "b.txt").write_text("B", encoding="utf-8")
    resp = _run(board.client.get(f"/api/board/{task_id}/files/archive", headers=auth_headers))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert f"{task_id}.zip" in resp.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(archive.namelist()) == {"uploads/a.txt", "outputs/b.txt"}
    assert archive.read("uploads/a.txt") == b"A"
    assert resp.headers["x-archive-file-count"] == "2"
    assert resp.headers["x-archive-truncated"] == "false"


def test_archive_unknown_card_is_404(board: Harness, auth_headers: dict[str, str]) -> None:
    resp = _run(board.client.get("/api/board/btk_missing/files/archive", headers=auth_headers))
    assert resp.status_code == 404


def test_archive_excludes_symlink_escape_and_denied_names(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    work = tmp_path / "archive-deny-ws"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never be zipped", encoding="utf-8")
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "escape.txt").symlink_to(outside)
    (work / "uploads" / "connections.env").write_text("SECRET=x", encoding="utf-8")
    (work / "uploads" / "keep.txt").write_text("keep me", encoding="utf-8")
    resp = _run(board.client.get(f"/api/board/{task_id}/files/archive", headers=auth_headers))
    assert resp.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(archive.namelist()) == {"uploads/keep.txt"}


def test_archive_bounded_by_file_count(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.board_files as board_files_routes

    monkeypatch.setattr(board_files_routes, "_MAX_ARCHIVE_FILES", 2)
    work = tmp_path / "archive-count-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    for i in range(5):
        (work / "uploads" / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    resp = _run(board.client.get(f"/api/board/{task_id}/files/archive", headers=auth_headers))
    assert resp.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert len(archive.namelist()) == 2
    assert resp.headers["x-archive-truncated"] == "true"
    assert resp.headers["x-archive-file-count"] == "2"


def test_archive_bounded_by_total_bytes(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.board_files as board_files_routes

    monkeypatch.setattr(board_files_routes, "_MAX_ARCHIVE_BYTES", 10)
    work = tmp_path / "archive-bytes-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "small.txt").write_bytes(b"x" * 5)
    (work / "uploads" / "big.txt").write_bytes(b"y" * 20)
    resp = _run(board.client.get(f"/api/board/{task_id}/files/archive", headers=auth_headers))
    assert resp.status_code == 200
    assert resp.headers["x-archive-truncated"] == "true"


# --- POST /api/board/{id}/files/reveal (C0) ------------------------------------


@pytest.fixture
def fake_open(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the reveal argv WITHOUT actually launching Finder / an app.

    Also pins ``platform.system()`` to Darwin so the happy paths clear the
    501 non-darwin gate; the dedicated 501 test re-overrides it to Linux.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(board_files.subprocess, "run", fake_run)
    monkeypatch.setattr(board_files.platform, "system", lambda: "Darwin")
    return calls


def test_reveal_happy_path_finder(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    work = tmp_path / "reveal-ws"
    task_id, _ = board.make_session_card(work)
    (work / "outputs").mkdir()
    (work / "outputs" / "report.md").write_text("# report", encoding="utf-8")
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal",
            json={"path": "outputs/report.md"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revealed"] is True and body["app"] == "finder"
    assert body["path"] == "outputs/report.md"
    assert fake_open[-1][:2] == ["/usr/bin/open", "-R"]
    assert fake_open[-1][-1] == str((work / "outputs" / "report.md").resolve())


def test_reveal_defaults_to_finder_and_workspace_root(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    work = tmp_path / "reveal-root-ws"
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(f"/api/board/{task_id}/files/reveal", json={}, headers=auth_headers)
    )
    assert resp.status_code == 200
    assert fake_open[-1] == ["/usr/bin/open", "-R", str(work.resolve())]


def test_reveal_open_in_vscode_uses_bundle_and_directory(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    work = tmp_path / "reveal-vscode-ws"
    task_id, _ = board.make_session_card(work)
    (work / "outputs").mkdir()
    (work / "outputs" / "a.txt").write_text("a", encoding="utf-8")
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal",
            json={"path": "outputs/a.txt", "app": "vscode"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    argv = fake_open[-1]
    assert argv[:3] == ["/usr/bin/open", "-b", "com.microsoft.VSCode"]
    # A file target opens its CONTAINING directory, not the file itself.
    assert argv[-1] == str((work / "outputs").resolve())


def test_reveal_unknown_app_is_422(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    task_id, _ = board.make_session_card(tmp_path / "reveal-badapp-ws")
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal", json={"app": "emacs"}, headers=auth_headers
        )
    )
    assert resp.status_code == 422
    assert fake_open == []  # never reached the launcher


@pytest.mark.parametrize(
    "hostile_path", ["../outside.txt", "uploads/../../outside.txt", "/etc/passwd", "a\x00b"]
)
def test_reveal_rejects_traversal_and_absolute_paths(
    board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    fake_open: list[list[str]],
    hostile_path: str,
) -> None:
    work = tmp_path / f"reveal-traversal-ws-{abs(hash(hostile_path))}"
    (tmp_path / "outside.txt").write_text("must never be reachable", encoding="utf-8")
    task_id, _ = board.make_session_card(work)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal",
            json={"path": hostile_path},
            headers=auth_headers,
        )
    )
    assert resp.status_code in (403, 404), f"{hostile_path!r} got {resp.status_code}"
    assert resp.status_code != 500
    assert fake_open == []


def test_reveal_rejects_symlink_escape(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    work = tmp_path / "reveal-symlink-ws"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never be revealed via symlink", encoding="utf-8")
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "escape.txt").symlink_to(outside)
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal",
            json={"path": "uploads/escape.txt"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403
    assert fake_open == []  # the escaping symlink is never opened


def test_reveal_denies_secret_registry_name(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    work = tmp_path / "reveal-secret-ws"
    task_id, _ = board.make_session_card(work)
    (work / "uploads").mkdir()
    (work / "uploads" / "connections.env").write_text("SECRET=x", encoding="utf-8")
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal",
            json={"path": "uploads/connections.env"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403
    assert fake_open == []


def test_reveal_missing_file_is_404(
    board: Harness, tmp_path: Path, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    task_id, _ = board.make_session_card(tmp_path / "reveal-missing-ws")
    resp = _run(
        board.client.post(
            f"/api/board/{task_id}/files/reveal", json={"path": "nope.txt"}, headers=auth_headers
        )
    )
    assert resp.status_code == 404
    assert fake_open == []


def test_reveal_unknown_card_is_404(
    board: Harness, auth_headers: dict[str, str], fake_open: list[list[str]]
) -> None:
    resp = _run(
        board.client.post("/api/board/btk_missing/files/reveal", json={}, headers=auth_headers)
    )
    assert resp.status_code == 404
    assert fake_open == []


def test_reveal_kill_switch_is_403(
    board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_open: list[list[str]],
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DISABLE_LOCAL_REVEAL", "1")
    task_id, _ = board.make_session_card(tmp_path / "reveal-killed-ws")
    resp = _run(
        board.client.post(f"/api/board/{task_id}/files/reveal", json={}, headers=auth_headers)
    )
    assert resp.status_code == 403
    assert fake_open == []


def test_reveal_non_darwin_is_501(
    board: Harness,
    tmp_path: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_open: list[list[str]],
) -> None:
    monkeypatch.setattr(board_files.platform, "system", lambda: "Linux")
    task_id, _ = board.make_session_card(tmp_path / "reveal-linux-ws")
    resp = _run(
        board.client.post(f"/api/board/{task_id}/files/reveal", json={}, headers=auth_headers)
    )
    assert resp.status_code == 501
    assert fake_open == []


# --- app-level session-token gate (every route here is always gated) ----------


class TestBoardFilesRoutesAreGated:
    def test_upload_401_without_token(self, board: Harness, tmp_path: Path) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-upload-ws")
        resp = _run(
            board.client.post(
                f"/api/board/{task_id}/files/upload",
                files=[("files", ("x.txt", b"data", "text/plain"))],
            )
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_list_401_without_token(self, board: Harness, tmp_path: Path) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-list-ws")
        resp = _run(board.client.get(f"/api/board/{task_id}/files"))
        assert resp.status_code == 401

    def test_download_401_without_token(self, board: Harness, tmp_path: Path) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-download-ws")
        resp = _run(
            board.client.get(f"/api/board/{task_id}/files/download", params={"path": "x.txt"})
        )
        assert resp.status_code == 401

    def test_archive_401_without_token(self, board: Harness, tmp_path: Path) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-archive-ws")
        resp = _run(board.client.get(f"/api/board/{task_id}/files/archive"))
        assert resp.status_code == 401

    def test_reveal_401_without_token(self, board: Harness, tmp_path: Path) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-reveal-ws")
        resp = _run(board.client.post(f"/api/board/{task_id}/files/reveal", json={}))
        assert resp.status_code == 401

    def test_list_reachable_with_token(
        self, board: Harness, tmp_path: Path, auth_headers: dict[str, str]
    ) -> None:
        task_id, _ = board.make_session_card(tmp_path / "gate-ok-ws")
        resp = _run(board.client.get(f"/api/board/{task_id}/files", headers=auth_headers))
        assert resp.status_code == 200
