"""S4 guard: destructive chrome-profile GC must never guess its root in a sim.

``_gc_chrome_profiles`` resolves its var root from ``OMNIAGENTOS_VAR``, then
``OMNIAGENTOS_VAR_DIR``, then the pre-existing fallbacks: cwd when both knobs
are unset (``Path("")`` is ``"."``) or the package anchor when a knob is
set-but-dangling. Any fallback is refused when a sim indicator is set; the
non-sim fallback behavior is pinned unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.maintenance import cache_gc
from omniagentos.maintenance.cache_gc import _gc_chrome_profiles, collect_garbage

_ENV_KEYS = (
    "OMNIAGENTOS_VAR",
    "OMNIAGENTOS_VAR_DIR",
    "OMNIAGENTOS_SIM_MODE",
    "OMNIAGENTOS_SIM_CAMPAIGN",
    "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
)

_SIM_CASES = (
    ("OMNIAGENTOS_SIM_MODE", "1"),
    ("OMNIAGENTOS_SIM_CAMPAIGN", "camp-2026"),
    ("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", "/tmp/camp-2026"),
)


@pytest.fixture(autouse=True)
def _clean_env_and_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Scrub every knob the resolver reads and pin cwd to a fresh tmp dir."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


def _plant_decoy(var_root: Path) -> Path:
    """Create a chrome-profile-shaped dir the sweep would delete if it ran here."""
    decoy = var_root / "projects" / "proj" / "workspace" / ".jambot-chrome"
    decoy.mkdir(parents=True)
    (decoy / "Cookies").write_text("session-state")
    return decoy


def _fake_anchor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the module's package-anchor fallback at a throwaway tree."""
    pkg = tmp_path / "anchor" / "omniagentos" / "maintenance"
    pkg.mkdir(parents=True)
    fake = pkg / "cache_gc.py"
    fake.touch()
    monkeypatch.setattr(cache_gc, "__file__", str(fake))
    return tmp_path / "anchor" / "var"


@pytest.mark.parametrize(("sim_key", "sim_value"), _SIM_CASES)
def test_sim_without_var_refuses_and_decoys_survive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sim_key: str, sim_value: str
) -> None:
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    cwd_decoy = _plant_decoy(Path.cwd())
    monkeypatch.setenv(sim_key, sim_value)

    with pytest.raises(RuntimeError) as excinfo:
        _gc_chrome_profiles(apply=True)

    message = str(excinfo.value)
    assert "OMNIAGENTOS_VAR" in message
    assert "OMNIAGENTOS_VAR_DIR" in message
    assert sim_key in message
    assert anchor_decoy.is_dir()
    assert cwd_decoy.is_dir()


def test_sim_refusal_propagates_through_collect_garbage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    db_path = str(tmp_path / "gc.db")
    migrate(db_path)

    with pytest.raises(RuntimeError, match="OMNIAGENTOS_VAR"):
        collect_garbage(db_path)
    # The guard is about root resolution, so a dry run refuses too rather than
    # reporting counts read from outside the sandbox.
    with pytest.raises(RuntimeError, match="OMNIAGENTOS_VAR"):
        collect_garbage(db_path, dry_run=True)
    assert anchor_decoy.is_dir()


def test_sim_with_dangling_knobs_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "missing"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "also-missing"))

    with pytest.raises(RuntimeError, match="OMNIAGENTOS_VAR"):
        _gc_chrome_profiles(apply=True)
    assert anchor_decoy.is_dir()


def test_sim_with_valid_var_still_collects_its_own_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    campaign_var = tmp_path / "campaign" / "var"
    decoy = _plant_decoy(campaign_var)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(campaign_var))

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not decoy.exists()


def test_var_dir_honored_when_var_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    var_dir = tmp_path / "var-dir"
    decoy = _plant_decoy(var_dir)
    keeper = decoy.parent / "src"
    keeper.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not decoy.exists()
    assert keeper.is_dir()


def test_var_dir_honored_when_var_dangling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    var_dir = tmp_path / "var-dir"
    decoy = _plant_decoy(var_dir)
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "missing"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not decoy.exists()
    # VAR_DIR pre-empts the package-anchor fallback.
    assert anchor_decoy.is_dir()


def test_var_precedence_over_var_dir_when_both_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    var_decoy = _plant_decoy(tmp_path / "var-a")
    var_dir_decoy = _plant_decoy(tmp_path / "var-b")
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var-a"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var-b"))

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not var_decoy.exists()
    assert var_dir_decoy.is_dir()


def test_non_sim_dangling_var_still_uses_package_anchor_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behavior pin: outside a sim the pre-existing fallback stays allowed."""
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "missing"))

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not anchor_decoy.exists()


def test_non_sim_both_unset_resolves_to_cwd_as_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behavior pin: with both knobs unset, ``Path("")`` is ``"."``, so the
    sweep runs against ``<cwd>/projects`` — NOT the package anchor. This is the
    pre-existing (accidental, but production) resolution, kept byte-identical;
    the sim guard is what closes the hazard for sim runs.
    """
    anchor_decoy = _plant_decoy(_fake_anchor(monkeypatch, tmp_path))
    cwd_decoy = _plant_decoy(Path.cwd())

    report = _gc_chrome_profiles(apply=True)

    assert report == {"chrome_profile_dirs": 1}
    assert not cwd_decoy.exists()
    assert anchor_decoy.is_dir()
