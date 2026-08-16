"""Tests for governance risk classification + config floors (§6, §5b, B2/B3).

Acceptance backbone (§13.4, §13):
  * declared-vs-actual mismatch ⇒ L4
  * symlink resolving into Tier P ⇒ L4 (realpath, built off a real git repo)
  * governance.yaml edit proposal ⇒ L4
  * Tier-S (detector.py) edit ⇒ ≥ L3
  * module shadowing a protected module ⇒ L4
  * keyword guard ⇒ L4
  * classifier only RAISES, never lowers
  * config floors reject panel < 3 distinct families (and window < 6h)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import omniagentos.policy.protected_paths as protected_paths
from omniagentos.policy.protected_paths import ProtectedPathsError
from omniagentos.reliability.governance import (
    DiffEntry,
    GovernanceConfig,
    GovernanceConfigError,
    build_diff_entries,
    classify_risk,
    load_governance_config,
    path_tier,
)
from omniagentos.reliability.taxonomy import ChangeRisk


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    # A protected file must actually exist for a symlink target to realpath into it.
    (repo / "configs").mkdir()
    (repo / "configs" / "governance.yaml").write_text("quorum: {}\n", encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


# --- Tier membership


def test_tier_p_and_s_membership():
    assert path_tier("configs/governance.yaml") == "P"
    assert path_tier("configs/reliability.yaml") == "P"
    assert path_tier("configs/protected-paths.yaml") == "P"
    assert path_tier("omniagentos/policy/shell.py") == "P"
    assert path_tier("omniagentos/policy/protected_paths.py") == "P"
    assert path_tier("omniagentos/notifications/service.py") == "P"
    assert path_tier("omniagentos/db/migrations/999_x.sql") == "P"
    assert path_tier("omniagentos/reliability/judges.py") == "P"
    # Tier P wins over the Tier-S reliability/** dir.
    assert path_tier("omniagentos/reliability/governance.py") == "P"
    # Tier S.
    assert path_tier("omniagentos/reliability/detector.py") == "S"
    assert path_tier("omniagentos/orgdims/company_org.py") == "S"
    assert path_tier("omniagentos/orgdims/company_requests.py") == "S"
    assert path_tier("docs/architecture/reliability.md") == "S"
    assert path_tier("ARCHI.md") == "S"
    # Retired company/ tombstone path is no longer Tier-S protected.
    assert path_tier("omniagentos/company/org.py") is None
    # LaunchAgents (absolute, outside repo) is Tier P.
    assert path_tier("/Users/x/Library/LaunchAgents/com.omniagentos.api.plist") == "P"
    # Separator confusion must still classify.
    assert path_tier(r"omniagentos\policy\shell.py") == "P"
    # Unprotected.
    assert path_tier("notes/foo.txt") is None


def test_governance_has_no_independent_tier_lists():
    """P0 invariant: governance must not re-declare editable tier sets."""
    import omniagentos.reliability.governance as gov

    for name in (
        "_TIER_P_FILES",
        "_TIER_P_DIRS",
        "_TIER_S_FILES",
        "_TIER_S_DIRS",
        "_PROTECTED_P_MODULES",
        "_PROTECTED_S_MODULES",
    ):
        assert not hasattr(gov, name), name


def test_registry_failure_is_fail_closed_at_governance_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-protected-paths.yaml"
    protected_paths._clear_protected_paths_cache()
    monkeypatch.setattr(protected_paths, "_default_registry_path", lambda: missing)
    try:
        with pytest.raises(ProtectedPathsError, match="missing"):
            classify_risk(
                [DiffEntry("M", "notes/ordinary.txt", content="+ harmless")],
                ["notes/ordinary.txt"],
            )
    finally:
        protected_paths._clear_protected_paths_cache()


# --- classify_risk


def test_governance_yaml_edit_is_l4():
    r = classify_risk(
        [DiffEntry("M", "configs/governance.yaml", content="+ allow: true")],
        ["configs/governance.yaml"],
    )
    assert r.level == ChangeRisk.L4.value
    assert r.tier == "P" and r.tier_p


def test_tier_s_detector_edit_is_at_least_l3():
    r = classify_risk(
        [DiffEntry("M", "omniagentos/reliability/detector.py", content="+ tune window")],
        ["omniagentos/reliability/detector.py"],
        suggested_level=1,
    )
    assert r.level >= ChangeRisk.L3.value
    assert r.tier == "S" and not r.tier_p


def test_declared_vs_actual_mismatch_is_l4():
    # Diff touches an undeclared path (not itself protected) ⇒ forced L4.
    r = classify_risk(
        [DiffEntry("M", "notes/foo.txt", content="+ hi")],
        ["notes/bar.txt"],
        suggested_level=1,
    )
    assert r.level == ChangeRisk.L4.value
    assert r.declared_mismatch


def test_module_shadow_of_protected_is_l4():
    r = classify_risk(
        [DiffEntry("A", "omniagentos/orchestrator/governance.py", content="x = 1")],
        ["omniagentos/orchestrator/governance.py"],
    )
    assert r.level == ChangeRisk.L4.value
    assert r.tier_p


def test_keyword_guard_forces_l4():
    for text in ("+ process payment", "+ update auth token", "+ delete row"):
        r = classify_risk(
            [DiffEntry("M", "notes/a.md", content=text)],
            ["notes/a.md"],
        )
        assert r.level == ChangeRisk.L4.value, text
        assert r.keyword_hit


def test_classifier_only_raises_never_lowers():
    # Benign change stays at the suggested level.
    r = classify_risk(
        [DiffEntry("M", "notes/a.md", content="+ reword log line")],
        ["notes/a.md"],
        suggested_level=1,
    )
    assert r.level == ChangeRisk.L1.value
    # A high suggestion is never lowered by a benign diff.
    r = classify_risk(
        [DiffEntry("M", "notes/a.md", content="+ reword log line")],
        ["notes/a.md"],
        suggested_level=4,
    )
    assert r.level == ChangeRisk.L4.value


def test_benign_tier_s_stays_l3_not_l4():
    r = classify_risk(
        [DiffEntry("M", "omniagentos/orgdims/company_org.py", content="+ rename var")],
        ["omniagentos/orgdims/company_org.py"],
        suggested_level=2,
    )
    assert r.level == ChangeRisk.L3.value


# --- symlink / rename resolving into Tier P (realpath, off a real repo)


def test_symlink_into_tier_p_is_l4(git_repo: Path):
    # A NEW symlink at an unprotected path whose target resolves into Tier P.
    link = git_repo / "evil_link"
    link.symlink_to(git_repo / "configs" / "governance.yaml")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "add symlink")

    entries = build_diff_entries(git_repo, "HEAD~1", "HEAD")
    link_entries = [e for e in entries if e.path == "evil_link"]
    assert link_entries and link_entries[0].is_symlink
    # Literal path is unprotected; the RESOLVED target lands in Tier P.
    assert "configs/governance.yaml" in link_entries[0].resolved_paths

    r = classify_risk(entries, ["evil_link"])
    assert r.level == ChangeRisk.L4.value
    assert r.tier_p


def test_rename_into_tier_p_is_l4(git_repo: Path):
    # Rename an unprotected file into a Tier-P dir: the NEW path is protected.
    (git_repo / "omniagentos" / "policy").mkdir(parents=True, exist_ok=True)
    _git(git_repo, "mv", "notes/seed.txt", "omniagentos/policy/seed.txt")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "rename into policy")

    entries = build_diff_entries(git_repo, "HEAD~1", "HEAD")
    declared = [e.path for e in entries] + [e.old_path for e in entries if e.old_path]
    r = classify_risk(entries, declared)
    assert r.level == ChangeRisk.L4.value
    assert r.tier_p


# --- config loader floors (B3)


def test_loader_defaults_when_missing(tmp_path: Path):
    cfg = load_governance_config(tmp_path / "nope.yaml")
    assert isinstance(cfg, GovernanceConfig)
    assert cfg.allow_majority_l1 is False
    assert len(cfg.panel_families) >= 3
    assert cfg.observation_hours(1) == 24
    assert cfg.observation_hours(2) == 48


def test_loader_reads_repo_governance_yaml():
    cfg = load_governance_config("configs/governance.yaml")
    assert cfg.allow_majority_l1 is False
    assert cfg.max_auto_risk_cap <= 2
    assert cfg.regression_fraction() <= 1.0


def test_floor_rejects_panel_below_three(tmp_path: Path):
    p = tmp_path / "gov.yaml"
    p.write_text("panel:\n  families: [anthropic, openai]\n", encoding="utf-8")
    with pytest.raises(GovernanceConfigError):
        load_governance_config(p)


def test_floor_rejects_short_observation_window(tmp_path: Path):
    p = tmp_path / "gov.yaml"
    p.write_text("observation_windows_hours:\n  L1: 3\n", encoding="utf-8")
    with pytest.raises(GovernanceConfigError):
        load_governance_config(p)


def test_floor_rejects_threshold_out_of_bounds(tmp_path: Path):
    p = tmp_path / "gov.yaml"
    p.write_text("kpi_thresholds:\n  regression_fraction: 5.0\n", encoding="utf-8")
    with pytest.raises(GovernanceConfigError):
        load_governance_config(p)


def test_floor_rejects_auto_risk_above_cap(tmp_path: Path):
    p = tmp_path / "gov.yaml"
    p.write_text("max_auto_risk_cap: 3\n", encoding="utf-8")
    with pytest.raises(GovernanceConfigError):
        load_governance_config(p)
