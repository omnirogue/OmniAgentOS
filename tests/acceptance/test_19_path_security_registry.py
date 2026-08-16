"""Codebase-wide AST gate for filesystem path security decisions.

The gate is auto-enumerating: it parses every Python module below
``omniagentos/`` and rejects path containment/identity decisions made with
string or lexical-Path operations.  Non-security uses are permitted only via
the reasoned exclusion registry.

Operator entry point: ``make path-security-gate``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.acceptance.suites.path_security_registry import (
    PACKAGE_ROOT,
    PATH_DECISION_EXCLUSIONS,
    PathDecisionSite,
    audit_path_decisions,
    counterfeit_grep_for_startswith,
    enumerate_path_decisions,
    known_dead_params,
)


def test_stale_and_blank_exclusions_are_rejected(tmp_path: Path) -> None:
    """Constructed completeness proof for stale/blank exclusion enforcement.

    The live registry may currently have zero stale keys; that must not make the
    checker appear green when the stale/blank logic is gutted.  This test injects
    both a missing-key exclusion and a blank-reason exclusion and requires the
    audit to report them.
    """
    tests_root = tmp_path / "pkg"
    tests_root.mkdir()
    (tests_root / "mod.py").write_text(
        "def contains(a, b):\n    return str(a).startswith(str(b))\n",
        encoding="utf-8",
    )
    sites = enumerate_path_decisions(tests_root)
    assert sites, "scratch module must produce at least one path-decision site"
    live_key = sites[0].key

    report = audit_path_decisions(
        sites,
        exclusions={
            live_key: "   ",  # blank reason is incomplete
            "ghost/module.py::gone::string-prefix::deadbeef0001": (
                "site removed or rewritten; must surface as stale"
            ),
        },
    )

    assert live_key in report.stale_exclusions, report.stale_exclusions
    assert any(
        key.startswith("ghost/module.py::") for key in report.stale_exclusions
    ), report.stale_exclusions
    # Blank reason does not count as a valid exclusion either.
    assert any(site.key == live_key for site in report.unregistered)


def test_spot_check_formerly_missed_path_string_sites_are_enumerated() -> None:
    """Regression: roots grant-merge and CSI worktree binding must be visible.

    These exact patterns previously slipped the AST walker (string ``==`` on
    path operands / ``Path(gitdir_text) != worktree / '.git'``).  Production
    must resolve them through ``inode_paths_equal``; if either is reverted to
    a lexical compare, this test and the main unregistered gate both fail.
    """
    live = enumerate_path_decisions(PACKAGE_ROOT)
    report = audit_path_decisions(live)

    roots_hits = [
        site
        for site in live
        if site.relative_file == "policy/roots.py"
        and site.rule == "inode-paths-equal"
        and "working_real" in site.source
    ]
    implement_hits = [
        site
        for site in live
        if site.relative_file == "csi/implement.py"
        and site.rule == "inode-paths-equal"
        and "gitdir_text" in site.source
        and "worktree" in site.source
        and ".git" in site.source
    ]
    assert roots_hits, "merge_standing_roots must use inode_paths_equal for working_dir"
    assert implement_hits, (
        "worktree .git binding via gitdir_text must use inode_paths_equal"
    )
    # Lexical reintroduction of either spot must also surface as unregistered.
    leaked = [
        site
        for site in report.unregistered
        if site.relative_file in {"policy/roots.py", "csi/implement.py"}
        and site.rule == "lexical-path-identity"
    ]
    assert not leaked, _render(tuple(leaked))


def test_promotion_path_sites_distinguish_safety_from_spelling_policy() -> None:
    """Promotion import admission is inode-backed; exact spellings are exclusions.

    The candidate-import check grants permission to execute code and therefore
    must use the shared inode primitive.  The evidence-root/lock comparisons
    only impose a stricter canonical spelling before the stable lock performs
    its descriptor/inode checks, so they remain explicit source-bound
    exclusions rather than counterfeit inode safety.
    """
    live = enumerate_path_decisions(PACKAGE_ROOT)
    report = audit_path_decisions(live)

    import_sites = [
        site
        for site in live
        if site.relative_file == "integration/promote.py"
        and site.scope == "_assert_candidate_import"
    ]
    assert {site.rule for site in import_sites} == {"inode-relative-parts-anchored"}
    assert all(site.is_inode_backed for site in import_sites), _render(tuple(import_sites))

    canonical_spelling_sites = [
        site
        for site in report.excluded_sites
        if site.relative_file == "integration/promote.py"
        and site.scope in {"_StableLock.acquire", "PromotionFinalizer.run"}
        and site.rule == "lexical-path-identity"
    ]
    assert len(canonical_spelling_sites) == 4, _render(tuple(canonical_spelling_sites))
    assert {site.source for site in canonical_spelling_sites} == {
        "request.evidence_root != canonical_root",
        "request.lock_path != canonical_lock",
    }
    assert not any(
        site.relative_file == "integration/promote.py" and site.scope == "_assert_candidate_import"
        for site in report.excluded_sites
    ), "candidate import admission must never be papered over by exclusion"


def test_archdocs_canonical_name_guard_is_explicit_fail_closed_policy() -> None:
    """The ARCHI.md spelling check is reviewed as refusal-only, not inode safety."""
    live = enumerate_path_decisions(PACKAGE_ROOT)
    report = audit_path_decisions(live)
    matches = [
        site
        for site in report.excluded_sites
        if site.relative_file == "archdocs/staleness.py"
        and site.scope == "is_stamp_stale"
        and site.source == "archi_path.resolve() != (repo_root / 'ARCHI.md').resolve()"
    ]
    assert len(matches) == 1, _render(tuple(matches))
    assert matches[0].rule == "lexical-path-identity"
    assert not matches[0].is_inode_backed


def _render(sites: tuple[PathDecisionSite, ...]) -> str:
    return "\n".join(site.render() for site in sites)


def test_all_security_path_decisions_resolve_through_inode_primitive() -> None:
    """Every discovered production site is safe or has a written non-security reason."""
    report = audit_path_decisions(enumerate_path_decisions(PACKAGE_ROOT))

    assert not report.unregistered, (
        "path decision sites bypass the shared inode primitive; migrate security "
        "decisions to omniagentos.path_containment.inode_relative_parts or add a "
        "specific non-security exclusion with a written reason:\n"
        f"{_render(report.unregistered)}"
    )
    assert not report.stale_exclusions, (
        "stale path-decision exclusions must be removed (site disappeared or changed):\n"
        + "\n".join(report.stale_exclusions)
    )
    assert report.safe_sites, "degenerate registry: no inode-backed path decisions found"
    assert any(
        site.relative_file == "sessions/token.py"
        and site.rule == "inode-anchored-component-identity"
        for site in report.safe_sites
    ), (
        "sessions/token.py must retain the nearest-existing-ancestor inode check "
        "plus exact remaining-component identity"
    )


def test_perrun_read_scope_is_inode_backed_and_never_excluded() -> None:
    """Whole-file reflection admission must stay on explicit inode decisions."""
    live = enumerate_path_decisions(PACKAGE_ROOT)
    scope_sites = [
        site
        for site in live
        if site.relative_file == "reflection/perrun.py" and site.scope == "_is_run_scoped_path"
    ]

    assert scope_sites, "per-run whole-file admission must remain visible to the gate"
    assert all(site.is_inode_backed for site in scope_sites), _render(tuple(scope_sites))
    assert {site.rule for site in scope_sites} == {
        "inode-path-is-within-anchored",
        "inode-paths-equal",
    }
    assert not any(
        key.startswith("reflection/perrun.py::_is_run_scoped_path::")
        for key in PATH_DECISION_EXCLUSIONS
    ), "security-relevant per-run read admission must never be papered over by exclusion"


def test_registry_location_containment_uses_explicit_inode_tristate() -> None:
    """The protected registry's admission call must fail closed on inode unknown."""
    live = enumerate_path_decisions(PACKAGE_ROOT)
    matches = [
        site
        for site in live
        if site.relative_file == "policy/protected_paths.py"
        and site.scope == "_assert_registry_location_safe"
        and site.rule == "inode-path-is-within-anchored"
    ]
    assert len(matches) == 1, _render(tuple(matches))
    assert matches[0].is_inode_backed
    assert not any(
        site.relative_file == "policy/protected_paths.py"
        and site.scope == "_assert_registry_location_safe"
        and site.rule == "tri-state-bare-truthiness"
        for site in live
    )


@pytest.mark.parametrize(
    "run_id",
    [
        "run_0123456789abcdefabcd",
        "run-legacy.1",
        "RUN_ABC_123",
        "run_é",
        "legacy.v2-BUILD_7",
    ],
)
def test_perrun_canonical_run_id_accepts_opaque_segments(run_id: str) -> None:
    """Current generated and legacy-safe IDs remain valid single segments."""
    from omniagentos.reflection.perrun import _canonical_run_id_segment

    assert _canonical_run_id_segment(run_id) == run_id


@pytest.mark.parametrize(
    "run_id",
    [
        None,
        7,
        "",
        " ",
        ".",
        "..",
        "run ",
        " run",
        "run id",
        "run\nid",
        "run\x00id",
        "./run",
        "run/.",
        "run/..",
        "run_a/../run_b",
        "run//other",
        r"run\other",
        "/absolute",
        r"\rooted",
        "//server/share",
        r"\\server\share",
        "C:run",
        r"C:\run",
        "run_e\u0301",
        "_leading",
        ".hidden",
        "run@host",
        "run+variant",
        "run.",
        "run:stream",
        "run.txt:stream",
        "run<peer",
        'run"peer',
        "run>peer",
        "run|peer",
        "run?peer",
        "run*peer",
        "CON",
        "con",
        "CON.txt",
        "PRN",
        "AUX.log",
        "NUL",
        "nul.txt",
        "COM1",
        "com9.log",
        "COM¹",
        "LPT1",
        "lpt9.txt",
        "LPT².log",
        "r" * 129,
    ],
)
def test_perrun_canonical_run_id_rejects_aliases(run_id: object) -> None:
    """Dot, separator, drive, whitespace, and normalization aliases fail closed."""
    from omniagentos.reflection.perrun import _canonical_run_id_segment

    assert _canonical_run_id_segment(run_id) is None


@pytest.mark.parametrize(
    "run_id",
    [
        "run.",
        "run:stream",
        "run.txt:stream",
        "CON",
        "con.txt",
        "NUL",
        "nul.log",
        "COM1",
        "LPT9.txt",
    ],
)
def test_perrun_windows_reinterpretable_ids_reject_before_file_access(
    run_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows device, stream, and trailing-dot aliases never reach file I/O."""
    from omniagentos.reflection import perrun

    called = {"read_related": False}

    def _unexpected_read(*_args, **_kwargs):
        called["read_related"] = True
        raise AssertionError("invalid cross-platform run ID reached file access")

    monkeypatch.setattr(perrun, "_read_related_files", _unexpected_read)
    result = perrun.analyze_run(run_id, db_path=str(tmp_path / "missing.db"))

    assert result["ok"] is False
    assert result["error"] == "invalid run_id"
    assert result["sources_read"] == []
    assert called["read_related"] is False


def test_perrun_noncanonical_id_cannot_read_another_runs_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-tree alias cannot enumerate or sample a peer run's whole log."""
    from omniagentos.reflection import perrun

    root = tmp_path / "repo"
    peer_id = "run_b"
    peer_dir = root / "var" / "logs" / peer_id
    peer_dir.mkdir(parents=True)
    peer_log = peer_dir / "peer-secret.log"
    peer_log.write_text("PEER_RUN_SECRET_CONTENT\n", encoding="utf-8")

    # Positive control: the canonical peer ID can read its own run-scoped log.
    sources, errors, _provider_hints, content = perrun._read_related_files(peer_id, root)
    assert errors == []
    assert any(source.endswith("peer-secret.log") for source in sources)
    assert "PEER_RUN_SECRET_CONTENT" in content

    # Before the fix this normalized to var/logs/run_b and sampled the peer.
    alias = "run_a/../run_b"
    sources, errors, provider_hints, content = perrun._read_related_files(alias, root)
    assert sources == []
    assert errors == ["invalid_run_id"]
    assert provider_hints == []
    assert content == ""

    # The top-level caller must reject before candidate enumeration as well.
    called = {"read_related": False}

    def _unexpected_read(*_args, **_kwargs):
        called["read_related"] = True
        return [], [], [], "PEER_RUN_SECRET_CONTENT"

    monkeypatch.setattr(perrun, "_read_related_files", _unexpected_read)
    monkeypatch.setenv("OMNIAGENTOS_HOME", str(root))
    result = perrun.analyze_run(alias, db_path=str(tmp_path / "missing.db"))
    assert result["ok"] is False
    assert result["error"] == "invalid run_id"
    assert result["sources_read"] == []
    assert called["read_related"] is False
    assert not (root / "var" / "retro" / "run-retros.jsonl").exists()


def test_perrun_symlinked_sibling_run_directory_is_not_enumerated(
    tmp_path: Path,
) -> None:
    """A run-name symlink to a sibling run cannot bind or sample that sibling."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    peer_dir = logs_root / "run_peer"
    peer_dir.mkdir(parents=True)
    peer_log = peer_dir / "peer-secret.log"
    peer_log.write_text("SIBLING_RUN_SECRET\n", encoding="utf-8")
    (logs_root / "run_current").symlink_to(peer_dir, target_is_directory=True)

    assert _is_run_scoped_path(peer_log, "run_current", root) is False
    sources, errors, provider_hints, content = _read_related_files("run_current", root)
    assert sources == []
    assert errors == ["invalid_run_directory"]
    assert provider_hints == []
    assert "SIBLING_RUN_SECRET" not in content


def test_perrun_case_alias_cannot_bind_to_stored_peer_run(tmp_path: Path) -> None:
    """Exact stored spelling is required even on case-insensitive filesystems."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    peer_dir = root / "var" / "logs" / "run_peer"
    peer_dir.mkdir(parents=True)
    peer_log = peer_dir / "peer-secret.log"
    peer_log.write_text("CASE_ALIAS_SECRET\n", encoding="utf-8")

    assert _is_run_scoped_path(peer_log, "RUN_PEER", root) is False
    sources, errors, provider_hints, content = _read_related_files("RUN_PEER", root)
    assert sources == []
    # Case-sensitive filesystems report an absent run; case-insensitive ones
    # positively detect the noncanonical binding.
    assert errors in ([], ["invalid_run_directory"])
    assert provider_hints == []
    assert "CASE_ALIAS_SECRET" not in content


def test_perrun_logs_root_internal_symlink_cannot_redirect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed ``var/logs`` parent itself must be an exact non-link entry."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    redirected = root / "alternate-logs" / "run_alias"
    redirected.mkdir(parents=True)
    peer_log = redirected / "peer.log"
    peer_log.write_text("PARENT_SYMLINK_SECRET\n", encoding="utf-8")
    var_dir = root / "var"
    var_dir.mkdir()
    (var_dir / "logs").symlink_to(root / "alternate-logs", target_is_directory=True)

    called = {"sample": False}

    def _unexpected_sample(*_args, **_kwargs):
        called["sample"] = True
        raise AssertionError("aliased logs root reached whole-file sampling")

    monkeypatch.setattr(
        "omniagentos.reflection.adapters.read_and_sample_file",
        _unexpected_sample,
    )
    assert _is_run_scoped_path(peer_log, "run_alias", root) is False
    sources, errors, provider_hints, content = _read_related_files("run_alias", root)
    assert sources == []
    assert errors == ["invalid_logs_root"]
    assert provider_hints == []
    assert "PARENT_SYMLINK_SECRET" not in content
    assert called["sample"] is False


def test_perrun_flat_log_rejects_peer_hardlink_before_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer hard link is rejected before the whole-file reader is called."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    logs_root.mkdir(parents=True)
    peer = logs_root / "run_peer.log"
    peer.write_text("HARDLINK_SECRET\n", encoding="utf-8")
    alias = logs_root / "run_alias.log"
    alias.hardlink_to(peer)
    assert peer.stat().st_nlink >= 2

    called = {"sample": False}

    def _unexpected_sample(*_args, **_kwargs):
        called["sample"] = True
        raise AssertionError("multiply linked flat log reached whole-file sampling")

    monkeypatch.setattr(
        "omniagentos.reflection.adapters.read_and_sample_file",
        _unexpected_sample,
    )
    assert _is_run_scoped_path(alias, "run_alias", root) is False
    sources, errors, provider_hints, content = _read_related_files("run_alias", root)
    assert sources == []
    assert errors == ["invalid_run_log"]
    assert provider_hints == []
    assert "HARDLINK_SECRET" not in content
    assert called["sample"] is False


def test_perrun_flat_log_accepts_unique_file(tmp_path: Path) -> None:
    """An exact, nonlinked flat log remains a valid positive control."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    logs_root.mkdir(parents=True)
    unique = logs_root / "run_unique.log"
    unique.write_text("UNIQUE_FLAT_CONTENT\n", encoding="utf-8")
    assert unique.stat().st_nlink == 1
    assert _is_run_scoped_path(unique, "run_unique", root) is True
    sources, errors, provider_hints, content = _read_related_files("run_unique", root)
    assert sources == ["var/logs/run_unique.log"]
    assert errors == []
    assert provider_hints == ["run_unique.log"]
    assert content == "UNIQUE_FLAT_CONTENT\n"


def test_perrun_directory_log_rejects_peer_hardlink_before_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-run directory name cannot legitimize a peer-owned hard link."""
    from omniagentos.reflection.perrun import _read_related_files

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    run_dir = logs_root / "run_alias"
    run_dir.mkdir(parents=True)
    peer = logs_root / "run_peer.log"
    peer.write_text("DIRECTORY_HARDLINK_SECRET\n", encoding="utf-8")
    alias = run_dir / "peer.log"
    alias.hardlink_to(peer)
    assert alias.stat().st_nlink >= 2

    called = {"sample": False}

    def _unexpected_sample(*_args, **_kwargs):
        called["sample"] = True
        raise AssertionError("multiply linked directory log reached sampling")

    monkeypatch.setattr(
        "omniagentos.reflection.adapters.read_and_sample_file",
        _unexpected_sample,
    )
    sources, errors, provider_hints, content = _read_related_files("run_alias", root)
    assert sources == []
    assert errors == ["file_not_uniquely_linked:var/logs/run_alias/peer.log"]
    assert provider_hints == []
    assert "DIRECTORY_HARDLINK_SECRET" not in content
    assert called["sample"] is False


def test_perrun_reader_leaf_swap_and_restore_stays_on_admitted_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient leaf replacement cannot supply bytes through an ABA path."""
    from omniagentos.reflection import adapters, perrun

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    run_dir = logs_root / "run_victim"
    run_dir.mkdir(parents=True)
    safe = run_dir / "safe.log"
    safe.write_text("ADMITTED_SAFE_CONTENT\n", encoding="utf-8")
    admitted_stat = safe.stat()
    held = run_dir / "safe-held.log"
    peer = logs_root / "run_peer.log"
    peer.write_text("TRANSIENT_FILE_SWAP_SECRET\n", encoding="utf-8")
    real_reader = adapters.read_and_sample_file
    called = {"reader": False}

    def _swap_inside_reader(opened_file, byte_cap: int):
        called["reader"] = True
        safe.rename(held)
        peer.rename(safe)
        try:
            assert os.path.samestat(os.fstat(opened_file.fileno()), admitted_stat)
            assert not os.path.samestat(safe.stat(), admitted_stat)
            return real_reader(opened_file, byte_cap)
        finally:
            safe.rename(peer)
            held.rename(safe)

    monkeypatch.setattr(adapters, "read_and_sample_file", _swap_inside_reader)
    sources, errors, provider_hints, content = perrun._read_related_files(
        "run_victim",
        root,
    )

    assert called["reader"] is True
    assert sources == ["var/logs/run_victim/safe.log"]
    assert errors == []
    assert provider_hints == ["safe.log"]
    assert content == "ADMITTED_SAFE_CONTENT\n"
    assert "TRANSIENT_FILE_SWAP_SECRET" not in content
    assert all("run_peer" not in source for source in sources)


def test_perrun_reader_peer_hardlink_and_restore_stays_on_admitted_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient peer hard link cannot become the descriptor's byte source."""
    from omniagentos.reflection import adapters, perrun

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    run_dir = logs_root / "run_victim"
    run_dir.mkdir(parents=True)
    safe = run_dir / "safe.log"
    safe.write_text("ADMITTED_SAFE_CONTENT\n", encoding="utf-8")
    admitted_stat = safe.stat()
    held = run_dir / "safe-held.log"
    peer = logs_root / "run_peer.log"
    peer.write_text("TRANSIENT_HARDLINK_SECRET\n", encoding="utf-8")
    real_reader = adapters.read_and_sample_file
    called = {"reader": False}

    def _link_inside_reader(opened_file, byte_cap: int):
        called["reader"] = True
        safe.rename(held)
        safe.hardlink_to(peer)
        try:
            assert peer.stat().st_nlink == 2
            assert os.path.samestat(os.fstat(opened_file.fileno()), admitted_stat)
            assert not os.path.samestat(safe.stat(), admitted_stat)
            return real_reader(opened_file, byte_cap)
        finally:
            safe.unlink()
            held.rename(safe)

    monkeypatch.setattr(adapters, "read_and_sample_file", _link_inside_reader)
    sources, errors, provider_hints, content = perrun._read_related_files(
        "run_victim",
        root,
    )

    assert called["reader"] is True
    assert sources == ["var/logs/run_victim/safe.log"]
    assert errors == []
    assert provider_hints == ["safe.log"]
    assert content == "ADMITTED_SAFE_CONTENT\n"
    assert "TRANSIENT_HARDLINK_SECRET" not in content
    assert all("run_peer" not in source for source in sources)


def test_perrun_reader_logs_redirect_and_restore_stays_on_admitted_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ``var/logs`` symlink cannot redirect an opened file read."""
    from omniagentos.reflection import adapters, perrun

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    run_dir = logs_root / "run_victim"
    run_dir.mkdir(parents=True)
    safe = run_dir / "safe.log"
    safe.write_text("ADMITTED_SAFE_CONTENT\n", encoding="utf-8")
    admitted_stat = safe.stat()
    held_logs = root / "var" / "logs-held"
    alternate_logs = root / "alternate-logs"
    alternate_run = alternate_logs / "run_victim"
    alternate_run.mkdir(parents=True)
    alternate_safe = alternate_run / "safe.log"
    alternate_safe.write_text("TRANSIENT_ROOT_SWAP_SECRET\n", encoding="utf-8")
    real_reader = adapters.read_and_sample_file
    called = {"reader": False}

    def _redirect_inside_reader(opened_file, byte_cap: int):
        called["reader"] = True
        logs_root.rename(held_logs)
        logs_root.symlink_to(alternate_logs, target_is_directory=True)
        try:
            assert logs_root.is_symlink()
            assert os.path.samestat(os.fstat(opened_file.fileno()), admitted_stat)
            assert not os.path.samestat(
                (logs_root / "run_victim" / "safe.log").stat(), admitted_stat
            )
            return real_reader(opened_file, byte_cap)
        finally:
            logs_root.unlink()
            held_logs.rename(logs_root)

    monkeypatch.setattr(adapters, "read_and_sample_file", _redirect_inside_reader)
    sources, errors, provider_hints, content = perrun._read_related_files(
        "run_victim",
        root,
    )

    assert called["reader"] is True
    assert sources == ["var/logs/run_victim/safe.log"]
    assert errors == []
    assert provider_hints == ["safe.log"]
    assert content == "ADMITTED_SAFE_CONTENT\n"
    assert "TRANSIENT_ROOT_SWAP_SECRET" not in content
    assert all("alternate-logs" not in source for source in sources)


def test_perrun_descriptor_open_unavailable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platforms without safe open-at primitives never fall back to a path read."""
    from omniagentos.reflection import adapters, perrun

    root = tmp_path / "repo"
    run_dir = root / "var" / "logs" / "run_portable"
    run_dir.mkdir(parents=True)
    safe = run_dir / "safe.log"
    safe.write_text("MUST_NOT_BE_PATH_READ\n", encoding="utf-8")
    called = {"reader": False}

    def _unexpected_reader(*_args, **_kwargs):
        called["reader"] = True
        raise AssertionError("unsupported secure-open platform fell back to pathname I/O")

    monkeypatch.setattr(perrun, "_descriptor_relative_open_supported", lambda: False)
    monkeypatch.setattr(adapters, "read_and_sample_file", _unexpected_reader)
    sources, errors, provider_hints, content = perrun._read_related_files(
        "run_portable",
        root,
    )

    assert sources == []
    assert errors == ["secure_open_unavailable:var/logs/run_portable/safe.log"]
    assert provider_hints == []
    assert content == ""
    assert called["reader"] is False


def test_perrun_whole_file_scope_rejects_symlink_escape(tmp_path: Path) -> None:
    """A lexical in-tree symlink must not expose an outside file to reflection."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    run_id = "run_symlink_escape"
    logs_dir = root / "var" / "logs" / run_id
    logs_dir.mkdir(parents=True)
    safe_log = logs_dir / "safe.log"
    safe_log.write_text("SAFE_RUN_CONTENT\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.log"
    outside.write_text("OUTSIDE_SECRET_CONTENT\n", encoding="utf-8")
    escape = logs_dir / "escape.log"
    escape.symlink_to(outside)

    assert _is_run_scoped_path(safe_log, run_id, root) is True
    assert _is_run_scoped_path(escape, run_id, root) is False

    sources, errors, _provider_hints, content = _read_related_files(run_id, root)
    assert "SAFE_RUN_CONTENT" in content
    assert "OUTSIDE_SECRET_CONTENT" not in content
    assert any(source.endswith("safe.log") for source in sources)
    assert not any(source.endswith("escape.log") for source in sources)
    assert any(
        error.startswith("file_not_run_scoped:") and error.endswith("escape.log")
        for error in errors
    )


def test_perrun_scope_rejects_symlinked_run_dir_and_traversal_id(tmp_path: Path) -> None:
    """The scope root itself must remain below logs, not merely its candidate."""
    from omniagentos.reflection.perrun import (
        _is_run_scoped_path,
        _read_related_files,
    )

    root = tmp_path / "repo"
    logs_root = root / "var" / "logs"
    logs_root.mkdir(parents=True)
    outside_dir = tmp_path / "outside-run"
    outside_dir.mkdir()
    outside_log = outside_dir / "secret.log"
    outside_log.write_text("SYMLINKED_RUN_DIR_SECRET\n", encoding="utf-8")

    run_id = "run_dir_escape"
    (logs_root / run_id).symlink_to(outside_dir, target_is_directory=True)

    assert _is_run_scoped_path(outside_log, run_id, root) is False
    sources, errors, _provider_hints, content = _read_related_files(run_id, root)
    assert sources == []
    assert "SYMLINKED_RUN_DIR_SECRET" not in content
    assert errors == ["invalid_run_directory"]

    traversal_target = root / "var" / "outside.log"
    traversal_target.write_text("TRAVERSAL_SECRET\n", encoding="utf-8")
    assert _is_run_scoped_path(traversal_target, "../outside", root) is False

    flat_run_id = "run_flat"
    flat = logs_root / f"{flat_run_id}.log"
    flat.write_text("FLAT_RUN_CONTENT\n", encoding="utf-8")
    assert _is_run_scoped_path(flat, flat_run_id, root) is True


def test_decisive_scratch_catches_path_equality_and_worktree_gitdir(tmp_path: Path) -> None:
    """String path identity / worktree gitdir compare shapes must be auto-flagged."""
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    scratch = tests_root / "_scratch_path_equality.py"
    scratch.write_text(
        "from pathlib import Path\n\n"
        "def blocked_by_never(path: str, never_set: set[str]) -> bool:\n"
        "    for blocked in never_set:\n"
        "        if path == blocked:\n"
        "            return True\n"
        "    return False\n\n"
        "def check_gitdir(gitdir_text, registration):\n"
        "    return Path(gitdir_text) != registration.worktree / '.git'\n",
        encoding="utf-8",
    )
    report = audit_path_decisions(enumerate_path_decisions(tests_root), exclusions={})
    rules = {site.rule for site in report.unregistered}
    sources = {site.source for site in report.unregistered}
    assert "lexical-path-identity" in rules
    assert "path == blocked" in sources
    assert any("worktree" in src and ".git" in src for src in sources)
    scratch.unlink()


def test_unparseable_module_never_renders_clean(tmp_path: Path) -> None:
    """Unparseable production modules must not vanish from a CLEAN audit.

    Failing-on-revert: if ``enumerate_path_decisions`` silently ``continue``s on
    SyntaxError/UnicodeError/OSError, a package with one good inode call and one
    broken module reports unregistered=0 — the CLEAN lie the review rejected.
    """
    tests_root = tmp_path / "pkg"
    tests_root.mkdir()
    (tests_root / "good.py").write_text(
        "from omniagentos.path_containment import inode_paths_equal\n\n"
        "def ok(candidate, allowed):\n"
        "    return inode_paths_equal(candidate, allowed) is True\n",
        encoding="utf-8",
    )
    (tests_root / "broken.py").write_text(
        "def not_even_valid(\n",
        encoding="utf-8",
    )

    sites = enumerate_path_decisions(tests_root)
    unparseable = [site for site in sites if site.rule == "unparseable-module"]
    assert unparseable, (
        "unparseable module must be enumerated, not silently skipped:\n"
        f"{_render(sites)}"
    )
    assert all(site.relative_file == "broken.py" for site in unparseable)
    assert all(not site.is_inode_backed for site in unparseable)

    report = audit_path_decisions(sites, exclusions={})
    assert any(site.rule == "unparseable-module" for site in report.unregistered), (
        "unparseable must surface as unregistered so CLEAN is impossible:\n"
        f"safe={len(report.safe_sites)} unregistered={_render(report.unregistered)}"
    )
    # The good explicit call remains safe — only the broken module poisons CLEAN.
    assert any(site.rule == "inode-paths-equal" for site in report.safe_sites)
    assert report.unregistered  # not clean


def test_bare_truthiness_on_tri_state_not_inode_safe(tmp_path: Path) -> None:
    """Bare truthiness on three-valued inode helpers must not be labeled safe.

    ``inode_paths_equal`` / ``inode_path_is_within_anchored`` return True/False/None.
    ``if inode_paths_equal(...):`` treats None as false — the over-correction the
    review contract forbids.  Only ``is True`` / ``is not False`` (etc.) is safe.
    """
    tests_root = tmp_path / "pkg"
    tests_root.mkdir()
    (tests_root / "bare.py").write_text(
        "from omniagentos.path_containment import (\n"
        "    inode_path_is_within_anchored,\n"
        "    inode_paths_equal,\n"
        ")\n\n"
        "def bare_equal(candidate, allowed):\n"
        "    if inode_paths_equal(candidate, allowed):\n"
        "        return True\n"
        "    return False\n\n"
        "def bare_within(candidate, root):\n"
        "    if inode_path_is_within_anchored(candidate, root):\n"
        "        return True\n"
        "    return False\n\n"
        "def explicit_equal(candidate, allowed):\n"
        "    if inode_paths_equal(candidate, allowed) is True:\n"
        "        return True\n"
        "    return False\n\n"
        "def explicit_within(candidate, root):\n"
        "    return inode_path_is_within_anchored(candidate, root) is not False\n",
        encoding="utf-8",
    )

    sites = enumerate_path_decisions(tests_root)
    bare = [site for site in sites if site.rule == "tri-state-bare-truthiness"]
    explicit = [
        site
        for site in sites
        if site.rule in {"inode-paths-equal", "inode-path-is-within-anchored"}
    ]
    assert len(bare) == 2, f"expected both bare call sites:\n{_render(sites)}"
    assert all(not site.is_inode_backed for site in bare), (
        "bare truthiness must not be inode-backed:\n" f"{_render(tuple(bare))}"
    )
    assert len(explicit) == 2, f"expected both explicit call sites:\n{_render(sites)}"
    assert all(site.is_inode_backed for site in explicit), (
        "explicit is/is-not True/False must remain inode-backed:\n"
        f"{_render(tuple(explicit))}"
    )

    report = audit_path_decisions(sites, exclusions={})
    bare_keys = {site.key for site in bare}
    assert bare_keys <= {site.key for site in report.unregistered}, (
        "bare tri-state truthiness must be unregistered:\n"
        f"{_render(report.unregistered)}"
    )
    explicit_keys = {site.key for site in explicit}
    assert explicit_keys <= {site.key for site in report.safe_sites}, (
        "explicit tri-state checks must stay safe:\n" f"{_render(report.safe_sites)}"
    )


def test_ast_walker_taints_through_boolop_and_ifexp(tmp_path: Path) -> None:
    """Path taint must propagate through ``or`` / ternary so lexical compares are seen.

    Failing-on-revert: if ``_is_path_expr`` drops ``ast.BoolOp`` / ``ast.IfExp``,
    the assignment targets below stay untainted and neither comparison is
    enumerated.  That is exactly the gap the cross-lineage review rejected.
    """
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    scratch = tests_root / "_scratch_boolop_ifexp_taint.py"
    scratch.write_text(
        "def via_boolop(candidate_path, root_path, other):\n"
        "    chosen = candidate_path or other\n"
        "    return chosen == root_path\n\n"
        "def via_ifexp(candidate_path, root_path, other):\n"
        "    chosen = candidate_path if True else other\n"
        "    return chosen == root_path\n",
        encoding="utf-8",
    )
    report = audit_path_decisions(enumerate_path_decisions(tests_root), exclusions={})
    sources = {site.source for site in report.unregistered}
    assert "chosen == root_path" in sources, (
        "BoolOp/IfExp taint must surface the lexical path identity:\n"
        f"{_render(report.unregistered)}"
    )
    # Both functions must produce the comparison site (two identical sources).
    assert sum(1 for site in report.unregistered if site.source == "chosen == root_path") == 2, (
        _render(report.unregistered)
    )
    scratch.unlink()


def test_decisive_scratch_string_prefix_is_auto_enumerated(tmp_path: Path) -> None:
    """A new module needs no registry edit before its string decision is flagged."""
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    scratch = tests_root / "_scratch_path_string_prefix.py"
    scratch.write_text(
        "import os\n\n"
        "def contains(a, b):\n"
        "    return str(a).startswith(str(b))\n\n"
        "def contains_typed(target: str, root: str):\n"
        "    return target == root or target.startswith(root + os.sep)\n",
        encoding="utf-8",
    )

    report = audit_path_decisions(
        enumerate_path_decisions(tests_root),
        exclusions={},
    )

    assert len(report.unregistered) == 3, _render(report.unregistered)
    assert [site.rule for site in report.unregistered].count("string-prefix") == 2
    assert [site.rule for site in report.unregistered].count("lexical-path-identity") == 1
    assert all(
        site.relative_file == "_scratch_path_string_prefix.py" for site in report.unregistered
    )
    scratch.unlink()


def test_ast_gate_catches_non_startswith_counterfeit(tmp_path: Path) -> None:
    """A grep-for-startswith checker passes while structural equivalents fail."""
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    scratch = tests_root / "_scratch_spelled_differently.py"
    scratch.write_text(
        "import os\n"
        "from pathlib import Path, PurePath\n\n"
        "def sliced_prefix(a, b):\n"
        "    return a[:len(b)] == b\n\n"
        "def commonpath_prefix(a, b):\n"
        "    return os.path.commonpath((a, b)) == b\n\n"
        "def purepath_identity(a, b):\n"
        "    return PurePath(a) == PurePath(b)\n\n"
        "def relative_to_check(a, b):\n"
        "    return Path(a).is_relative_to(b)\n",
        encoding="utf-8",
    )

    assert counterfeit_grep_for_startswith(tests_root) == ()

    report = audit_path_decisions(
        enumerate_path_decisions(tests_root),
        exclusions={},
    )
    rules = {site.rule for site in report.unregistered}
    sources = {site.source for site in report.unregistered}
    assert "prefix-slice-equality" in rules, _render(report.unregistered)
    assert "commonpath-comparison" in rules, _render(report.unregistered)
    assert "lexical-path-identity" in rules, _render(report.unregistered)
    assert "lexical-is_relative_to" in rules, _render(report.unregistered)
    assert "a[:len(b)] == b" in sources
    assert "PurePath(a) == PurePath(b)" in sources
    assert any("is_relative_to" in src for src in sources)
    scratch.unlink()


def test_registry_rejects_case_folded_token_component_identity(tmp_path: Path) -> None:
    """Case-folded remaining components must not be reported as inode-backed.

    The contract requires nearest-existing-ancestor by samestat *plus exact*
    remaining components.  A registry rule that labels ``_fold`` / ``normcase``
    / ``.lower()`` comparisons as safe is a CLEAN lie.
    """
    tests_root = tmp_path / "sessions"
    tests_root.mkdir()
    # Mimic the production scope name + relative path the special-case keys on.
    token_mod = tests_root / "token.py"
    token_mod.write_text(
        "from omniagentos.path_containment import inode_relative_parts\n\n"
        "def _fold(name):\n"
        "    return name.lower()\n\n"
        "def _is_legacy_token_path(candidate):\n"
        "    anchor = '/tmp/anchor'\n"
        "    legacy_parts = ('secrets', 'sessions-token')\n"
        "    parts = inode_relative_parts(candidate, anchor)\n"
        "    if parts is None:\n"
        "        return False\n"
        "    return tuple(map(_fold, parts)) == tuple(map(_fold, legacy_parts))\n",
        encoding="utf-8",
    )
    # Package root is tmp_path so relative_file is sessions/token.py (as production).
    sites = enumerate_path_decisions(tmp_path)
    folded = [
        site
        for site in sites
        if site.relative_file == "sessions/token.py"
        and site.scope == "_is_legacy_token_path"
        and "legacy_parts" in site.source
    ]
    assert folded, f"expected the component-identity site, got:\n{_render(sites)}"
    assert all(not site.is_inode_backed for site in folded), (
        "case-folded token component compare must not be inode-backed:\n"
        f"{_render(tuple(folded))}"
    )
    assert all(site.rule == "case-folded-component-identity" for site in folded), (
        _render(tuple(folded))
    )
    report = audit_path_decisions(sites, exclusions={})
    assert any(site.key == folded[0].key for site in report.unregistered), (
        "case-folded token identity must surface as unregistered:\n"
        f"{_render(report.unregistered)}"
    )
    token_mod.unlink()


def test_union_find_parent_pointer_is_not_a_path_decision(tmp_path: Path) -> None:
    """Name-shaped ``_parent`` int tables must not look like filesystem decisions.

    Union-find / tree parent-pointer loops compare integers.  A checker that
    treats the identifier ``_parent`` as path-bearing (because ``parent`` is a
    path vocabulary word) taints the index via assignment and false-positives
    ``self._parent[i] != i``.  Discrimination must use operands, not names.
    """
    tests_root = tmp_path / "memlife"
    tests_root.mkdir()
    scratch = tests_root / "cluster.py"
    scratch.write_text(
        "class _UnionFind:\n"
        "    def __init__(self, n: int) -> None:\n"
        "        self._parent = list(range(n))\n"
        "\n"
        "    def find(self, i: int) -> int:\n"
        "        while self._parent[i] != i:\n"
        "            self._parent[i] = self._parent[self._parent[i]]\n"
        "            i = self._parent[i]\n"
        "        return i\n",
        encoding="utf-8",
    )

    sites = enumerate_path_decisions(tests_root)
    assert sites == (), (
        "union-find integer parent pointers must not be path-decision sites:\n"
        f"{_render(sites)}"
    )

    # Must-keep: genuine string-path containment still auto-enumerated (same run).
    keep = tests_root / "_scratch_string_path_prefix.py"
    keep.write_text(
        "def contains(a, b):\n"
        "    return str(a).startswith(str(b))\n",
        encoding="utf-8",
    )
    report = audit_path_decisions(enumerate_path_decisions(tests_root), exclusions={})
    assert any(
        site.rule == "string-prefix" and "startswith" in site.source
        for site in report.unregistered
    ), _render(report.unregistered)
    assert not any(
        "self._parent" in site.source for site in report.unregistered
    ), _render(report.unregistered)
    keep.unlink()
    scratch.unlink()


@pytest.mark.parametrize("entry", known_dead_params())
def test_known_dead_security_path_entry(entry) -> None:
    """Known-dead entries are strict xfails; an inode migration must XPASS loudly."""
    sites = enumerate_path_decisions(PACKAGE_ROOT)
    matching = tuple(site for site in sites if site.key == entry.key)
    assert matching and all(site.is_inode_backed for site in matching)
