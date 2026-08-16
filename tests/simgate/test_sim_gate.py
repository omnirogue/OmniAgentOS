from __future__ import annotations

from pathlib import Path

import pytest

import omniagentos.simgate as simgate
from omniagentos.path_containment import inode_relative_parts
from omniagentos.simgate import (
    SimContext,
    SimGateError,
    resolve_sim_context,
)


def test_default_sim_root_is_outside_checkout() -> None:
    assert simgate._DEFAULT_SIM_ROOT == Path.home() / "OmniAgentOS-sims"
    assert simgate._DEFAULT_SIM_ROOT != Path(__file__).resolve().parents[2] / "var" / "simulations"


def _coherent_environment(
    tmp_path: Path,
    *,
    campaign: str = "campaign-a",
    var_key: str = "OMNIAGENTOS_VAR_DIR",
) -> tuple[dict[str, str], Path, Path]:
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / campaign
    var_root = campaign_root / "var"
    (var_root / "secrets").mkdir(parents=True)
    environment = {
        "OMNIAGENTOS_SIM_MODE": "1",
        "OMNIAGENTOS_SIM_CAMPAIGN": campaign,
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": str(campaign_root),
        "OMNIAGENTOS_SIM_ROOT": str(sim_root),
        var_key: str(var_root),
    }
    return environment, campaign_root, var_root


def test_coherent_simulation_returns_context(tmp_path: Path) -> None:
    environment, campaign_root, _var_root = _coherent_environment(tmp_path)

    result = resolve_sim_context(environment)

    assert result == SimContext(True, "campaign-a", campaign_root)


@pytest.mark.parametrize("spelling", ["true", "0", "yes", " 1", "1 "])
def test_invalid_sim_mode_spelling_is_quoted(spelling: str) -> None:
    with pytest.raises(SimGateError) as error:
        resolve_sim_context({"OMNIAGENTOS_SIM_MODE": spelling})

    assert "OMNIAGENTOS_SIM_MODE" in str(error.value)
    assert repr(spelling) in str(error.value)


@pytest.mark.parametrize(
    ("change", "offending_var"),
    [
        (lambda env: env.update({"OMNIAGENTOS_SIM_CAMPAIGN": ""}), "OMNIAGENTOS_SIM_CAMPAIGN"),
        (
            lambda env: env.pop("OMNIAGENTOS_VAR_DIR"),
            "OMNIAGENTOS_VAR_DIR",
        ),
        (
            lambda env: env.pop("OMNIAGENTOS_SIM_CAMPAIGN_ROOT"),
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        ),
        (
            lambda env: env.update({"OMNIAGENTOS_SIM_CAMPAIGN_ROOT": "relative/campaign"}),
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        ),
        (
            lambda env: env.update({"OMNIAGENTOS_SIM_CAMPAIGN_ROOT": "/missing/campaign"}),
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        ),
    ],
)
def test_each_required_member_missing_or_invalid_names_variable(
    tmp_path: Path, change, offending_var: str
) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)
    change(environment)

    with pytest.raises(SimGateError) as error:
        resolve_sim_context(environment)

    assert offending_var in str(error.value)


def test_var_fallback_is_accepted_and_var_dir_has_precedence(tmp_path: Path) -> None:
    environment, campaign_root, var_root = _coherent_environment(
        tmp_path, var_key="OMNIAGENTOS_VAR"
    )
    result = resolve_sim_context(environment)
    assert result.campaign_root == campaign_root

    environment["OMNIAGENTOS_VAR"] = str(tmp_path / "outside")
    environment["OMNIAGENTOS_VAR_DIR"] = str(var_root)
    assert resolve_sim_context(environment).campaign_root == campaign_root


def test_sim_env_loaded_alone_is_production() -> None:
    result = resolve_sim_context({"OMNIAGENTOS_SIM_ENV_LOADED": "1"})

    assert result == SimContext(False, None, None)


def test_campaign_root_outside_sim_root_is_refused(tmp_path: Path) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)
    outside_root = tmp_path / "outside" / "campaign-a"
    outside_var_root = outside_root / "var"
    (outside_var_root / "secrets").mkdir(parents=True)
    environment["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"] = str(outside_root)
    environment["OMNIAGENTOS_VAR_DIR"] = str(outside_var_root)

    with pytest.raises(
        SimGateError,
        match="OMNIAGENTOS_SIM_CAMPAIGN_ROOT is not inode-contained in OMNIAGENTOS_SIM_ROOT",
    ):
        resolve_sim_context(environment)


def test_campaign_root_equal_to_sim_root_is_refused(tmp_path: Path) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)
    environment["OMNIAGENTOS_SIM_CAMPAIGN"] = "simulations"
    environment["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"] = environment["OMNIAGENTOS_SIM_ROOT"]

    with pytest.raises(SimGateError, match="same inode"):
        resolve_sim_context(environment)


@pytest.mark.parametrize("sim_root_kind", ["slash", "home", "home-ancestor"])
def test_sim_root_cannot_be_home_or_an_inode_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sim_root_kind: str
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    if sim_root_kind == "slash":
        sim_root = Path("/")
        # The gate checks campaign-root existence before SIM_ROOT sanity, so
        # use a writable existing campaign outside the root's own spelling.
        campaign_root = tmp_path / "unsafe-root-test"
        var_root = campaign_root / "var"
        (var_root / "secrets").mkdir(parents=True)
    elif sim_root_kind == "home":
        sim_root = fake_home
        campaign_root = fake_home / "unsafe-root-test"
        var_root = campaign_root / "var"
        (var_root / "secrets").mkdir(parents=True)
    else:  # home-ancestor
        sim_root = tmp_path
        campaign_root = tmp_path / "unsafe-root-test"
        var_root = campaign_root / "var"
        (var_root / "secrets").mkdir(parents=True)

    campaign = "unsafe-root-test"
    environment = {
        "OMNIAGENTOS_SIM_MODE": "1",
        "OMNIAGENTOS_SIM_CAMPAIGN": campaign,
        "OMNIAGENTOS_SIM_ROOT": str(sim_root),
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": str(campaign_root),
        "OMNIAGENTOS_VAR_DIR": str(var_root),
    }

    with pytest.raises(
        SimGateError,
        match=r"cannot be '/', '\$HOME', or an ancestor of \$HOME",
    ):
        resolve_sim_context(environment)


def test_default_sim_root_under_home_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    sim_root = fake_home / "OmniAgentOS-sims"
    campaign_root = sim_root / "accepted-root-test"
    var_root = campaign_root / "var"
    (var_root / "secrets").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    environment = {
        "OMNIAGENTOS_SIM_MODE": "1",
        "OMNIAGENTOS_SIM_CAMPAIGN": "accepted-root-test",
        "OMNIAGENTOS_SIM_ROOT": str(sim_root),
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": str(campaign_root),
        "OMNIAGENTOS_VAR_DIR": str(var_root),
    }

    assert resolve_sim_context(environment).campaign_root == campaign_root


def test_campaign_basename_must_match_name(tmp_path: Path) -> None:
    environment, campaign_root, _var_root = _coherent_environment(tmp_path)
    renamed_root = campaign_root.parent / "different-name"
    campaign_root.rename(renamed_root)
    environment["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"] = str(renamed_root)

    with pytest.raises(SimGateError, match="OMNIAGENTOS_SIM_CAMPAIGN"):
        resolve_sim_context(environment)


def test_var_dir_outside_campaign_root_is_refused(tmp_path: Path) -> None:
    environment, campaign_root, _var_root = _coherent_environment(tmp_path)
    outside_var_root = tmp_path / "production-var"
    outside_var_root.mkdir()
    (outside_var_root / "secrets").symlink_to(
        campaign_root / "var" / "secrets", target_is_directory=True
    )
    environment["OMNIAGENTOS_VAR_DIR"] = str(outside_var_root)

    with pytest.raises(
        SimGateError, match=r"OMNIAGENTOS_VAR_DIR.*not inode-contained in campaign_root"
    ):
        resolve_sim_context(environment)


@pytest.mark.parametrize("var_key", ["OMNIAGENTOS_VAR_DIR", "OMNIAGENTOS_VAR"])
def test_var_root_equal_campaign_root_is_accepted(tmp_path: Path, var_key: str) -> None:
    environment, campaign_root, _var_root = _coherent_environment(tmp_path, var_key=var_key)
    environment[var_key] = str(campaign_root)

    result = resolve_sim_context(environment)

    assert result == SimContext(True, "campaign-a", campaign_root)


def test_symlinked_campaign_root_escaping_sim_root_is_refused(tmp_path: Path) -> None:
    environment, campaign_root, _var_root = _coherent_environment(tmp_path)
    escaped_target = tmp_path / "escaped" / "campaign-a"
    escaped_var_root = escaped_target / "var"
    (escaped_var_root / "secrets").mkdir(parents=True)
    symlinked_root = campaign_root.parent / "linked-campaign"
    symlinked_root.symlink_to(escaped_target, target_is_directory=True)
    environment["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"] = str(symlinked_root)
    environment["OMNIAGENTOS_VAR_DIR"] = str(escaped_var_root)

    with pytest.raises(
        SimGateError,
        match="OMNIAGENTOS_SIM_CAMPAIGN_ROOT is not inode-contained in OMNIAGENTOS_SIM_ROOT",
    ):
        resolve_sim_context(environment)


@pytest.mark.parametrize("campaign", ["bad/name", "bad..name", ".."])
def test_campaign_name_cannot_contain_path_syntax(tmp_path: Path, campaign: str) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)
    environment["OMNIAGENTOS_SIM_CAMPAIGN"] = campaign

    with pytest.raises(SimGateError, match="OMNIAGENTOS_SIM_CAMPAIGN"):
        resolve_sim_context(environment)


@pytest.mark.parametrize("var_key", ["OMNIAGENTOS_VAR_DIR", "OMNIAGENTOS_VAR"])
@pytest.mark.parametrize("campaign", ["campaign-a", "campaign-b"])
@pytest.mark.parametrize(
    "matrix_case",
    ["coherent", "campaign-outside-sim-root", "var-outside-campaign-root"],
)
def test_coherent_and_incoherent_matrix_enforces_simulation_containment(
    tmp_path: Path, var_key: str, campaign: str, matrix_case: str
) -> None:
    environment, campaign_root, var_root = _coherent_environment(
        tmp_path / var_key.lower().replace("_", "-"), campaign=campaign, var_key=var_key
    )
    token_path = var_root / "secrets" / "sessions-token"
    expected_error = None

    if matrix_case == "campaign-outside-sim-root":
        escaped_campaign_root = tmp_path / "escaped" / campaign
        escaped_var_root = escaped_campaign_root / "var"
        (escaped_var_root / "secrets").mkdir(parents=True)
        environment["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"] = str(escaped_campaign_root)
        environment[var_key] = str(escaped_var_root)
        expected_error = (
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT is not inode-contained in OMNIAGENTOS_SIM_ROOT"
        )
    elif matrix_case == "var-outside-campaign-root":
        outside_var_root = tmp_path / "outside-var"
        (outside_var_root / "secrets").mkdir(parents=True)
        environment[var_key] = str(outside_var_root)
        expected_error = "not inode-contained in campaign_root"

    if matrix_case == "coherent":
        context = resolve_sim_context(environment)

        assert context == SimContext(True, campaign, campaign_root)
        assert inode_relative_parts(var_root, campaign_root) is not None
        assert inode_relative_parts(token_path, campaign_root) is not None
    else:
        assert expected_error is not None
        with pytest.raises(SimGateError, match=expected_error):
            resolve_sim_context(environment)
