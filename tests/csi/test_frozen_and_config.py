"""CSI foundation: frozen surfaces + kill switch."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.csi.config import clear_csi_config_cache, load_csi_config
from omniagentos.csi.frozen import assert_writable, is_frozen_path, reject_frozen_paths


def test_pipeline_config_is_runnable_when_enabled() -> None:
    """Safe human pipeline is enabled in configs/self_improvement.yaml."""
    clear_csi_config_cache()
    cfg = load_csi_config()
    # Production config may flip enabled; code defaults remain safe if file missing.
    if cfg.enabled and not cfg.global_halt:
        ok, reason = cfg.is_runnable()
        assert ok is True
        assert reason == "ok"
        assert cfg.use_live_planners is False
        sl = cfg.routine("self_learning")
        assert sl is not None and sl.enabled is True
    else:
        ok, reason = cfg.is_runnable()
        assert ok is False


def test_frozen_surfaces_block_selfimprove_and_csi() -> None:
    assert is_frozen_path("omniagentos/selfimprove/gate.py")
    assert is_frozen_path("omniagentos/csi/engine.py")
    assert is_frozen_path("configs/policy.yaml")
    assert is_frozen_path("configs/roots.yaml")
    assert is_frozen_path("omniagentos/gates/service.py")
    assert not is_frozen_path("vault/skills/foo/SKILL.md")
    assert not is_frozen_path("omniagentos/swarm/planner.py")


def test_reject_frozen_paths_filters() -> None:
    hits = reject_frozen_paths(
        [
            "omniagentos/swarm/planner.py",
            "omniagentos/csi/engine.py",
            "vault/skills/x/SKILL.md",
        ]
    )
    assert hits == ["omniagentos/csi/engine.py"]


def test_assert_writable_raises() -> None:
    with pytest.raises(PermissionError, match="frozen"):
        assert_writable(["omniagentos/selfimprove/skills.py"])


def test_absolute_path_under_repo_is_frozen() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    abs_csi = str(root / "omniagentos" / "csi" / "engine.py")
    assert is_frozen_path(abs_csi)


def test_outside_repo_path_is_frozen() -> None:
    assert is_frozen_path("/tmp/evil/omniagentos/csi/engine.py")


def test_min_panel_size_clamped_to_two(tmp_path: Path) -> None:
    import yaml

    from omniagentos.csi.config import clear_csi_config_cache, load_csi_config

    p = tmp_path / "si.yaml"
    p.write_text(
        yaml.dump({"enabled": False, "min_panel_size": 1, "routines": {}}),
        encoding="utf-8",
    )
    clear_csi_config_cache()
    cfg = load_csi_config(str(p))
    assert cfg.min_panel_size >= 2
