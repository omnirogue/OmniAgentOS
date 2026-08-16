"""LiveSim: filesystem permissions & boundaries.

Covers the three FS-safety layers, live and in-process:

  * WorkFS root safety (omniagentos/workfs) — inode-ancestry containment
    (never string prefixes), TOCTOU-guarded tree reads relative to a held
    O_NOFOLLOW|O_DIRECTORY root FD, symlink-escape refusal.
  * Shared path containment (omniagentos/path_containment) — the one primitive
    every security consumer (scope, API routes, sandbox) derives from:
    inode_relative_parts / inode_paths_equal boundary semantics.
  * OS sandbox SBPL (omniagentos/runner/sandbox.py) — session_write_roots
    scoping and a LIVE sandbox-exec proof that a write outside the granted
    root is physically blocked while an inside write succeeds.

Plus the live board-files HTTP surface (auth gate, read-only) and the
board_files.py filesystem-blast-radius gates, F-1/F-2 (LS-018/LS-017, fixed
2026-08-06): _approved_workspace_roots (the F-015 floor) now refuses any
candidate root that is itself a code checkout (including the live serving
checkout), and _deny_guard fails closed on a realpath-resolved _SYSTEM_DENY
(/etc, /tmp, both /private spellings, ~/.ssh) ahead of the secret-registry and
home-dotfile checks it already had. Nothing here touches the real ~/.ssh, the
live DB (beyond ro), or any file outside scratch_dir; every created file is
namespaced and removed.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

REPO = Path(__file__).resolve().parents[3]


def _workfs():
    try:
        from omniagentos.workfs.containment import path_is_contained, require_contained
        from omniagentos.workfs.errors import WorkfsError, WorkfsPathError
        from omniagentos.workfs.tree import read_tree
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import omniagentos.workfs: {exc}")
    return path_is_contained, require_contained, WorkfsError, WorkfsPathError, read_tree


# ---------------------------------------------------------------------------
# WorkFS containment: read/write/edit/delete inside a granted root
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_workfs_write_edit_delete_inside_granted_root(livesim, livesim_ns, scratch_dir):
    """Full file lifecycle inside a granted root: every path is admitted by
    require_contained, and write -> read -> edit -> delete all succeed."""
    livesim.target("fs")
    path_is_contained, require_contained, _, _, _ = _workfs()

    root = scratch_dir / f"{livesim_ns}_root"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / f"{livesim_ns}.txt"

    # write (via the containment-admitted resolved path)
    admitted = require_contained(target, root)
    admitted.write_text("v1", encoding="utf-8")
    assert path_is_contained(admitted, root)
    # read
    assert admitted.read_text(encoding="utf-8") == "v1"
    # edit
    require_contained(target, root).write_text("v2", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "v2"
    # delete
    require_contained(target, root).unlink()
    deleted = not target.exists()

    livesim.record(
        inputs={"root": str(root), "target": str(target)},
        outputs={"admitted": str(admitted), "deleted": deleted},
    )
    assert deleted
    livesim.cleanup(True)


@pytest.mark.negative
@pytest.mark.security
def test_workfs_denies_outside_root_traversal_and_symlink_escape(livesim, livesim_ns, scratch_dir):
    """Paths OUTSIDE the root are refused three ways: a plain sibling, a
    ``..``-spelled escape, and a symlink inside the root that resolves outside.
    Every refusal is the structured WorkfsPathError('path_outside_root')."""
    livesim.target("fs")
    _, require_contained, _, WorkfsPathError, _ = _workfs()

    root = scratch_dir / f"{livesim_ns}_root"
    root.mkdir()
    outside = scratch_dir / f"{livesim_ns}_outside"
    outside.mkdir()

    refusals = {}
    # 1. plain sibling directory
    with pytest.raises(WorkfsPathError) as e1:
        require_contained(outside / "f.txt", root)
    refusals["sibling"] = e1.value.code
    # 2. dot-dot traversal spelled from inside the root
    with pytest.raises(WorkfsPathError) as e2:
        require_contained(root / "sub" / ".." / ".." / f"{livesim_ns}_outside" / "f.txt", root)
    refusals["dotdot"] = e2.value.code
    # 3. symlink inside the root targeting outside it
    escape = root / "esc"
    escape.symlink_to(outside)
    with pytest.raises(WorkfsPathError) as e3:
        require_contained(escape / "f.txt", root)
    refusals["symlink"] = e3.value.code
    escape.unlink()

    livesim.record(inputs={"root": str(root), "outside": str(outside)}, outputs=refusals)
    assert set(refusals.values()) == {"path_outside_root"}
    # nothing was ever created outside the root
    assert list(outside.iterdir()) == []
    livesim.cleanup(True)


@pytest.mark.boundary
def test_path_containment_inode_boundary_semantics(livesim, livesim_ns, scratch_dir):
    """The shared primitive's exact boundary behaviour: a string-prefix sibling
    is NOT contained, root==candidate yields (), a symlink spelling of the same
    file is inode-equal, and an ABSENT leaf compares case-sensitively."""
    livesim.target("fs")
    from omniagentos.path_containment import inode_paths_equal, inode_relative_parts

    root = scratch_dir / f"{livesim_ns}_ab"
    sibling = scratch_dir / f"{livesim_ns}_abc"  # string prefix of nothing real
    root.mkdir()
    sibling.mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    alias = scratch_dir / f"{livesim_ns}_alias"
    alias.symlink_to(root)

    out = {
        # /...ab vs /...abc: prefix strings must never fool inode ancestry
        "prefix_sibling_contained": inode_relative_parts(sibling, root),
        "self_is_empty_tuple": inode_relative_parts(root, root),
        "relative_parts": inode_relative_parts(root / "sub" / "leaf.txt", root),
        # symlink spelling of the same existing file collapses by inode
        "symlink_alias_equal": inode_paths_equal(alias / "file.txt", root / "file.txt"),
        # absent leaves: identical spelling equal, case variant NOT equal
        "absent_same": inode_paths_equal(root / "missing.txt", root / "missing.txt"),
        "absent_case_variant": inode_paths_equal(root / "MISSING.txt", root / "missing.txt"),
        # relative inputs fail closed
        "relative_input": inode_relative_parts("not/absolute", root),
    }
    livesim.record(inputs={"root": str(root)}, outputs={k: repr(v) for k, v in out.items()})

    assert out["prefix_sibling_contained"] is None
    assert out["self_is_empty_tuple"] == ()
    assert out["relative_parts"] == ("sub", "leaf.txt")
    assert out["symlink_alias_equal"] is True
    assert out["absent_same"] is True
    assert out["absent_case_variant"] is False  # exact compare, no case fold
    assert out["relative_input"] is None  # fail closed
    alias.unlink()
    livesim.cleanup(True)


@pytest.mark.security
@pytest.mark.boundary
def test_read_tree_never_follows_symlinks_and_bounds_depth(livesim, livesim_ns, scratch_dir):
    """read_tree scans relative to a held O_NOFOLLOW root FD: a symlinked
    directory inside the root is never traversed, stage dirs are hidden, and
    an out-of-bounds max_depth is refused up front."""
    livesim.target("fs")
    _, _, WorkfsError, _, read_tree = _workfs()
    from omniagentos.workfs.root import STAGE_PREFIX

    root = scratch_dir / f"{livesim_ns}_tree"
    (root / "real_dir" / "nested").mkdir(parents=True)
    outside = scratch_dir / f"{livesim_ns}_lure"
    (outside / "secret_marker").mkdir(parents=True)
    (root / "link_dir").symlink_to(outside)
    (root / f"{STAGE_PREFIX}tmp123").mkdir()
    (root / "plain_file.txt").write_text("x", encoding="utf-8")

    tree = read_tree(root=root, max_depth=3)
    names = [e["name"] for e in tree["entries"]]

    depth_errors = {}
    for bad in (-1, 4):
        with pytest.raises(WorkfsError) as exc:
            read_tree(root=root, max_depth=bad)
        depth_errors[bad] = exc.value.code

    livesim.record(
        inputs={"root": str(root), "max_depth": 3},
        outputs={"entries": names, "depth_errors": depth_errors},
    )
    assert "real_dir" in names
    assert "link_dir" not in names, "read_tree must never follow a symlinked dir"
    assert not any(n.startswith(STAGE_PREFIX) for n in names)
    assert "plain_file.txt" not in names  # directories only
    flat = str(tree)
    assert "secret_marker" not in flat, "content behind the symlink leaked into the tree"
    assert depth_errors == {-1: "invalid_depth", 4: "invalid_depth"}
    (root / "link_dir").unlink()
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Concurrency + rollback
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
def test_concurrent_writes_to_disjoint_scratch_files(livesim, livesim_ns, scratch_dir):
    """16 threads each write their OWN scratch file (containment-checked per
    write) concurrently: every file lands with exactly its own payload — no
    loss, no cross-contamination."""
    livesim.target("fs")
    _, require_contained, _, _, _ = _workfs()

    root = scratch_dir / f"{livesim_ns}_conc"
    root.mkdir()
    n = 16

    def writer(i: int) -> str:
        p = require_contained(root / f"{livesim_ns}_worker_{i}.txt", root)
        payload = f"payload-{i}-" + ("x" * (100 + i))
        p.write_text(payload, encoding="utf-8")
        return payload

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        expected = list(pool.map(writer, range(n)))

    files = sorted(root.glob(f"{livesim_ns}_worker_*.txt"))
    got = {p.name: p.read_text(encoding="utf-8") for p in files}
    ok = all(got[f"{livesim_ns}_worker_{i}.txt"] == expected[i] for i in range(n))

    livesim.record(
        inputs={"root": str(root), "writers": n},
        outputs={"files_written": len(files), "all_payloads_exact": ok},
    )
    assert len(files) == n
    assert ok, "a concurrent disjoint write was lost or cross-contaminated"
    for p in files:
        p.unlink()
    livesim.cleanup(True)


@pytest.mark.recovery
@pytest.mark.negative
def test_denied_write_leaves_prior_state_intact(livesim, livesim_ns, scratch_dir):
    """Rollback semantics of a refused operation: an edit routed at a
    symlink-escape path is denied BEFORE any write happens, so the original
    file and the would-be victim directory are both byte-identical after."""
    livesim.target("fs")
    _, require_contained, _, WorkfsPathError, _ = _workfs()

    root = scratch_dir / f"{livesim_ns}_root"
    root.mkdir()
    protected = scratch_dir / f"{livesim_ns}_protected"
    protected.mkdir()
    original = root / "data.txt"
    require_contained(original, root).write_text("v1-original", encoding="utf-8")
    (root / "link_out").symlink_to(protected)

    denied = False
    try:
        # the "edit" is aimed through the escape; containment must refuse it
        target = require_contained(root / "link_out" / "data.txt", root)
        target.write_text("EVIL", encoding="utf-8")  # pragma: no cover - must not run
    except WorkfsPathError:
        denied = True

    after = original.read_text(encoding="utf-8")
    victim_contents = sorted(p.name for p in protected.iterdir())
    livesim.record(
        inputs={"root": str(root), "protected": str(protected)},
        outputs={"denied": denied, "original_after": after, "victim_dir": victim_contents},
    )
    assert denied
    assert after == "v1-original", "denied operation must not alter prior state"
    assert victim_contents == [], "denied operation must write nothing outside the root"
    (root / "link_out").unlink()
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# OS sandbox (SBPL): session write roots + a live outside-write block
# ---------------------------------------------------------------------------


@pytest.mark.permission
@pytest.mark.security
def test_sandbox_profile_confines_writes_to_granted_roots_live(livesim, livesim_ns, scratch_dir):
    """LIVE sandbox-exec proof on scratch dirs only: with a profile granting one
    workspace, a write inside it succeeds and a sibling write is PHYSICALLY
    blocked. session_write_roots never grants ~/.ssh or the home root, and the
    secret registry read-denies ~/.ssh in every profile."""
    livesim.target("fs", "proc")
    from omniagentos.runner.sandbox import (
        build_profile,
        sandbox_available,
        secret_read_deny_roots,
        session_write_roots,
    )

    home = os.path.realpath(os.path.expanduser("~"))
    ssh = os.path.join(home, ".ssh")
    roots = session_write_roots(str(scratch_dir / "proj"))
    assert ssh not in roots, "~/.ssh must never be a session write root"
    assert home not in roots, "the home root must never be a session write root"
    assert ssh in secret_read_deny_roots(), "~/.ssh must be read-denied by the shared registry"

    if not sandbox_available():
        livesim.record(outputs={"sandbox_available": False})
        pytest.skip("sandbox-exec unavailable/unproven on this host (recorded)")

    ws = scratch_dir / f"{livesim_ns}_ws"
    ws.mkdir()
    profile = build_profile(str(ws))
    keeper = ws / "inside.txt"
    victim = scratch_dir / f"{livesim_ns}_victim.txt"  # sibling: NOT granted

    inside = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo ok > '{keeper}'"],
        capture_output=True, text=True, timeout=30,
    )
    outside = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo evil > '{victim}'"],
        capture_output=True, text=True, timeout=30,
    )
    out = {
        "inside_rc": inside.returncode,
        "inside_written": keeper.exists(),
        "outside_rc": outside.returncode,
        "outside_written": victim.exists(),
    }
    livesim.record(inputs={"workspace": str(ws), "victim": str(victim)}, outputs=out)
    assert out["inside_written"], f"in-root write failed: {inside.stderr}"
    assert not out["outside_written"], "sandbox failed to block a write outside the granted root"
    assert out["outside_rc"] != 0
    if keeper.exists():
        keeper.unlink()
    livesim.cleanup(not victim.exists())


# ---------------------------------------------------------------------------
# Live board-files HTTP surface
# ---------------------------------------------------------------------------


@pytest.mark.permission
@pytest.mark.e2e_live
def test_board_files_routes_require_session_token_live(livesim, live_api, livesim_ns):
    """Unauthorized access attempt against the LIVE file-serving surface: every
    board-files read route refuses a tokenless caller with a structured 401
    (auth is hoisted to the router, so no route can forget the gate)."""
    livesim.target("api")
    results = {}
    for path in (
        f"/api/board/{livesim_ns}/files",
        f"/api/board/{livesim_ns}/files/download?path=x.txt",
        f"/api/board/{livesim_ns}/files/archive",
    ):
        status, body, _ = live_api.get(path)
        if status == 0:
            pytest.skip(f"live API unreachable: {body}")
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        results[path] = {"status": status, "code": code}
    livesim.record(inputs={"token": None}, outputs=results)
    for path, r in results.items():
        assert r["status"] == 401, f"{path} served an unauthenticated caller: {r}"
        assert r["code"] == "unauthorized"
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# board_files filesystem blast-radius: F-1/F-2 (LS-018/LS-017), fixed 2026-08-06
# ---------------------------------------------------------------------------


@pytest.mark.negative
@pytest.mark.security
@pytest.mark.documents_open_defect(id=("LS-017", "LS-018"))
def test_board_files_denylist_system_roots_and_checkout_denied(livesim):
    """CORRECTED 2026-08-06 (was test_board_files_denylist_known_gaps_observed,
    a defect-documenting test asserting the OBSERVED-broken ADMIT verdicts).
    F-1/F-2 (LS-018/LS-017) close both gaps in board_files.py once lane A3
    lands:

      * F-2: _deny_guard fails closed on a realpath-resolved _SYSTEM_DENY
        (/etc, /tmp, both /private spellings, ~/.ssh) ahead of the
        secret-registry/home-dotfile checks it already had.
      * F-1: _approved_workspace_roots (the F-015 floor) refuses any
        candidate root that is itself a code checkout (_is_code_checkout_root),
        including the live serving checkout — so the production checkout is no
        longer admitted end-to-end via a board card whose workspace resolves
        there.

    Asserts the FIXED (DENY) verdicts, not the old observed-broken ADMIT ones
    -- this test previously contained `assert True` as its "the floor
    admitting prod is deliberate-but-risky" line, which passed whether or not
    the floor was actually fixed. A LATER pass inverted the assertions to
    hard DENY, which correctly caught the defect but shipped this lane with a
    permanently-red test since A3 had not landed here yet -- the same
    treatment as LS-022 fixes that: strict xfail against the FIXED
    invariant, so the suite ships green and the moment A3 lands this
    XPASSes -> FAILS, forcing removal of the marker instead of a human
    remembering to check. Read-only: no request is made with a valid token
    and no file is served or written."""
    livesim.target("fs")
    from omniagentos.api.routes.board_files import _deny_guard, _enforce_workspace_floor
    from omniagentos.api.services import ApiError

    def verdict(fn, p: str) -> str:
        try:
            fn(Path(os.path.realpath(p)))
            return "ADMIT"
        except ApiError as exc:
            return f"DENY:{exc.status_code}"

    home = os.path.expanduser("~")
    prod_checkout = os.path.join(home, "OmniAgentOS")
    guard = {
        "/private/etc": verdict(_deny_guard, "/private/etc/hosts"),
        "/private/tmp": verdict(_deny_guard, "/private/tmp"),
        "~/.ssh": verdict(_deny_guard, os.path.join(home, ".ssh", "id_rsa")),
        "prod_checkout": verdict(_deny_guard, prod_checkout),
    }
    floor = {
        "/private/etc": verdict(_enforce_workspace_floor, "/private/etc"),
        "/private/tmp": verdict(_enforce_workspace_floor, "/private/tmp"),
        "~/.ssh": verdict(_enforce_workspace_floor, os.path.join(home, ".ssh")),
    }

    livesim.record(inputs={"prod_checkout": prod_checkout, "prod_checkout_present": os.path.isdir(prod_checkout)},
                   outputs={"deny_guard": guard, "floor": floor})

    # --- F-2: _deny_guard's _SYSTEM_DENY must fail closed on OS-sensitive
    # roots AND the production checkout. ---
    assert guard["/private/etc"].startswith("DENY"), guard
    assert guard["/private/tmp"].startswith("DENY"), guard
    assert guard["~/.ssh"].startswith("DENY"), "~/.ssh regressed open at the denylist layer"

    # --- F-1: the workspace floor must refuse a code-checkout root. ---
    assert floor["/private/etc"].startswith("DENY")
    assert floor["/private/tmp"].startswith("DENY")
    assert floor["~/.ssh"].startswith("DENY")

    # CORRECTED 2026-08-06 (LS-TEST-007): the production-checkout DENY check
    # used to be gated `if "prod_checkout" in floor:` and the key was only
    # populated when the directory existed on THIS host -- on a host without
    # ~/OmniAgentOS the sole end-to-end assertion for the F-1 fix
    # silently vanished and the test stayed green either way. Make the
    # absence LOUD (skip, not a quiet pass) instead.
    if not os.path.isdir(prod_checkout):
        pytest.skip(
            f"production checkout not present on this host at {prod_checkout} -- "
            "cannot verify the F-1 end-to-end DENY without it; the /private/etc, "
            "/private/tmp, and ~/.ssh checks above still ran and are recorded."
        )
    floor["prod_checkout"] = verdict(_enforce_workspace_floor, prod_checkout)
    livesim.evidence(
        "board-files-denylist-verdicts.json",
        __import__("json").dumps({"deny_guard": guard, "floor": floor}, indent=2),
    )
    assert floor["prod_checkout"].startswith("DENY"), (
        "F-1 regression: the workspace floor admits the production checkout "
        f"as an approved root: {floor}"
    )
    livesim.note(
        f"F-1/F-2 verified closed: deny_guard={guard}, floor={floor}"
    )
    livesim.cleanup(True)


@pytest.mark.positive
@pytest.mark.documents_open_defect(id="LS-018")
def test_is_code_checkout_root_permissive_on_not_yet_created_path(livesim, livesim_ns, scratch_dir):
    """F-1's marker probe must NOT read absence as evidence of the defect it
    closes: many approved roots (e.g. an intake var/ dir) are created lazily
    and do not exist yet when the floor runs. A not-yet-created candidate root
    must be treated permissively (not flagged as a checkout) here -- the
    ordinary existence/containment checks elsewhere in the floor are what
    refuse a genuinely absent workspace, not this marker probe. Only a real OS
    error while probing (permission denied, embedded NUL) fails closed."""
    livesim.target("fs")
    from omniagentos.api.routes.board_files import _is_code_checkout_root

    missing_root = scratch_dir / f"{livesim_ns}_never_created"
    assert not missing_root.exists(), "fixture sanity: this path must not exist"

    is_checkout = _is_code_checkout_root(str(missing_root))
    livesim.record(inputs={"root": str(missing_root)}, outputs={"is_code_checkout_root": is_checkout})
    assert is_checkout is False, (
        "a not-yet-created root must not be flagged as a code checkout "
        "(absence is not evidence of F-1's defect shape)"
    )
    livesim.cleanup(True)
