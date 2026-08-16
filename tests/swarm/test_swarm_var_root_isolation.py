"""Regression coverage for campaign-local swarm state placement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos import simgate
from omniagentos.runtime_paths import RuntimePathError
from omniagentos.swarm import spawn
from omniagentos.swarm.worktrees import (
    SubprocessSwarmWorktrees,
    _ensure_coral_root,
    default_coral_shared_root,
)


def _repo_root() -> Path:
    return Path(spawn.__file__).resolve().parent.parent.parent


def _repo_swarm_root() -> Path:
    return _repo_root() / "var" / "swarm"


def _listing_bytes(path: Path) -> tuple[bytes, ...] | None:
    if not path.exists():
        return None
    return tuple(sorted(os.fsencode(entry.name) for entry in path.iterdir()))


def test_coral_root_never_leaks_into_operator_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect: CORAL was created at ``<checkout>/var/swarm/coral``.

    Deliberately ordered so the LEAK is what fails first. A path-equality
    assertion placed above would short-circuit this test before it ever
    provisioned anything, leaving the only claim that matters -- the operator's
    var is untouched -- unproven by a revert test.
    """
    campaign = tmp_path / "campaign-var"
    repo_swarm = _repo_swarm_root()
    repo_listing_before = _listing_bytes(repo_swarm)
    assert not campaign.exists(), "a fresh campaign root must not pre-exist"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))

    # The production path that produced the leak: __init__ mkdirs the CORAL root.
    worktrees = SubprocessSwarmWorktrees(
        coral_mode="shadow",
        dep_link_dirs=(),
        lock_retry_sleep=0,
    )

    assert _listing_bytes(repo_swarm) == repo_listing_before, (
        f"swarm state escaped into the operator var at {repo_swarm}: "
        f"{repo_listing_before} -> {_listing_bytes(repo_swarm)}"
    )
    # State-independent twin of the check above: the listing comparison goes
    # vacuous on a checkout that already carries a leaked ``coral`` from before
    # this fix, so pin the resolved root itself as well.
    assert _repo_root() not in worktrees._coral_shared_root.parents, (
        f"CORAL shared root {worktrees._coral_shared_root} is inside the "
        f"executing checkout {_repo_root()} instead of the campaign {campaign}"
    )

    expected_coral = Path(os.path.realpath(campaign / "swarm" / "coral"))
    assert not expected_coral.exists(), "CORAL must be lazy until explicit first use"
    _ensure_coral_root(worktrees._coral_shared_root)
    assert expected_coral.is_dir(), "CORAL root was not created inside the campaign"
    assert worktrees._coral_shared_root == expected_coral


def test_campaign_root_resolves_swarm_and_coral_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign-var"
    assert not campaign.exists()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))

    assert spawn.default_swarm_var_root() == campaign / "swarm"
    assert default_coral_shared_root() == campaign / "swarm" / "coral"


def test_sim_gate_rejects_configured_var_outside_campaign_before_swarm_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / "campaign-a"
    campaign_root.mkdir(parents=True)
    outside_var = tmp_path / "production-var"
    outside_var.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign-a")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(outside_var))

    with pytest.raises(simgate.SimGateError, match="not inode-contained in campaign_root"):
        spawn.default_swarm_var_root()

    assert not (outside_var / "swarm").exists()
    assert not (_repo_root() / "var" / "swarm" / "coral").exists()


def test_sim_gate_rejects_invalid_mode_before_swarm_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "invalid")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "anything"))

    with pytest.raises(simgate.SimGateError, match="OMNIAGENTOS_SIM_MODE"):
        spawn.default_swarm_var_root()


def test_sim_swarm_root_requires_a_configured_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "campaign-a")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", "/missing/campaign-a")
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(simgate.SimGateError):
        spawn.default_swarm_var_root()


def test_sim_swarm_root_defensive_missing_root_error_names_both_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the local defensive branch explicit if the gate contract changes."""
    monkeypatch.setattr(
        spawn,
        "resolve_sim_context_or_none",
        lambda: simgate.SimContext(True, "campaign-a", Path("/tmp/campaign-a")),
    )
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(RuntimePathError, match="OMNIAGENTOS_VAR_DIR or OMNIAGENTOS_VAR"):
        spawn.default_swarm_var_root()


def test_unset_var_uses_repo_swarm_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)

    assert spawn.default_swarm_var_root() == _repo_swarm_root()


@pytest.mark.parametrize("configured", ["", "   "])
def test_blank_var_uses_repo_swarm_root(configured: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", configured)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)

    assert spawn.default_swarm_var_root() == _repo_swarm_root()


def test_var_root_is_read_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_a = tmp_path / "campaign-a"
    root_b = tmp_path / "campaign-b"

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(root_a))
    assert spawn.default_swarm_var_root() == root_a / "swarm"

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(root_b))
    assert spawn.default_swarm_var_root() == root_b / "swarm"


def test_var_is_fallback_when_var_dir_is_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", "   ")
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "fallback"))

    assert spawn.default_swarm_var_root() == tmp_path / "fallback" / "swarm"


def test_sim_indicators_refuse_checkout_fallback_and_preserve_decoy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_var = _repo_root() / "var"
    marker = repo_var / "wp5-sim-guard-marker"
    marker.write_text("preserve", encoding="utf-8")
    try:
        for name in (
            "OMNIAGENTOS_VAR_DIR",
            "OMNIAGENTOS_VAR",
            "OMNIAGENTOS_SIM_CAMPAIGN",
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")

        with pytest.raises(simgate.SimGateError, match="is missing"):
            spawn.default_swarm_var_root()

        assert marker.read_text(encoding="utf-8") == "preserve"
    finally:
        marker.unlink(missing_ok=True)


def test_nonexistent_configured_root_never_falls_back_to_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "never-created"
    assert not configured.exists()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(configured))

    result = spawn.default_swarm_var_root()

    assert result == configured / "swarm"
    assert _repo_root() not in result.parents
    assert not configured.exists()


def test_tilde_in_configured_root_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", "~/campaign-var")

    assert spawn.default_swarm_var_root() == home / "campaign-var" / "swarm"


def test_downstream_workbook_and_worktrees_use_campaign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "downstream-campaign"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))

    assert spawn.swarm_workbook_path("run", "task") == (
        campaign / "swarm" / "run" / "task" / "WORKBOOK.md"
    )

    worktrees = SubprocessSwarmWorktrees(
        coral_mode="off",
        dep_link_dirs=(),
        lock_retry_sleep=0,
    )
    assert worktrees.worktree_path("run", "task") == (
        campaign / "swarm" / "worktrees" / "run" / "task"
    )
