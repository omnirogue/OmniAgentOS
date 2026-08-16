"""Fail-closed validation for the process-wide simulation environment."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from omniagentos.connectors import ConnectorError, compute_registry_hash, load_registry
from omniagentos.path_containment import inode_relative_parts

_DEFAULT_SIM_ROOT = Path.home() / "OmniAgentOS-sims"
_REPO_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "configs" / "connectors.yaml"

LOG = logging.getLogger(__name__)


class SimGateError(RuntimeError):
    """The simulation environment cannot be admitted safely."""


@dataclass(frozen=True)
class SimContext:
    """Validated simulation state for one request or startup handoff."""

    sim_mode: bool
    campaign: str | None
    campaign_root: Path | None


def _refuse(message: str) -> SimGateError:
    return SimGateError(message)


def _selected_var_root(environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Mirror token.py's VAR_DIR-first, then VAR precedence."""
    var_dir = environ.get("OMNIAGENTOS_VAR_DIR")
    if var_dir:
        return "OMNIAGENTOS_VAR_DIR", var_dir.strip()
    var = environ.get("OMNIAGENTOS_VAR")
    if var:
        return "OMNIAGENTOS_VAR", var.strip()
    return None, None


def _parse_strict_sim_mode(values: Mapping[str, str]) -> bool:
    """Strictly parse ``OMNIAGENTOS_SIM_MODE``: fail closed on anything incoherent.

    Unset or empty means production (``False``); the exact string ``"1"`` means
    simulation (``True``); anything else (``"0"``, ``"true"``, stray whitespace,
    ...) is refused loudly rather than silently treated as production. This is
    shared with ``sessions/token.py`` (imported directly — see that module's
    ``token_path()``) so both places refuse the same incoherent values instead
    of drifting apart.
    """
    sim_mode = values.get("OMNIAGENTOS_SIM_MODE")
    if sim_mode is None or sim_mode == "":
        return False
    if sim_mode != "1":
        raise _refuse(
            "OMNIAGENTOS_SIM_MODE has invalid value "
            f"{sim_mode!r}; set OMNIAGENTOS_SIM_MODE exactly to '1' or unset it "
            "for production"
        )
    return True


def resolve_sim_context(environ: Mapping[str, str] | None = None) -> SimContext:
    """Resolve and validate simulation state without trusting inherited flags."""
    values = os.environ if environ is None else environ

    if not _parse_strict_sim_mode(values):
        return SimContext(sim_mode=False, campaign=None, campaign_root=None)

    campaign = values.get("OMNIAGENTOS_SIM_CAMPAIGN")
    if not campaign:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN is missing or empty while "
            "OMNIAGENTOS_SIM_MODE='1'; set it to a non-empty campaign name"
        )
    if "/" in campaign or ".." in campaign:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN contains a forbidden '/' or '..' in "
            f"{campaign!r}; set it to a single directory name without path syntax"
        )

    var_name, var_raw = _selected_var_root(values)
    if var_name is None or not var_raw:
        raise _refuse(
            "OMNIAGENTOS_VAR_DIR or OMNIAGENTOS_VAR is missing while "
            "OMNIAGENTOS_SIM_MODE='1'; set OMNIAGENTOS_VAR_DIR (preferred) or "
            "OMNIAGENTOS_VAR to an absolute campaign var directory"
        )
    var_dir = Path(var_raw)
    if not var_dir.is_absolute():
        raise _refuse(
            f"{var_name}={var_raw!r} is not absolute while "
            "OMNIAGENTOS_SIM_MODE='1'; set it to an absolute campaign var directory"
        )

    campaign_root_raw = values.get("OMNIAGENTOS_SIM_CAMPAIGN_ROOT")
    if not campaign_root_raw:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT is missing while "
            "OMNIAGENTOS_SIM_MODE='1'; set it to an absolute existing campaign directory"
        )
    campaign_root = Path(campaign_root_raw)
    if not campaign_root.is_absolute():
        raise _refuse(
            f"OMNIAGENTOS_SIM_CAMPAIGN_ROOT={campaign_root_raw!r} is not absolute; "
            "set OMNIAGENTOS_SIM_CAMPAIGN_ROOT to an absolute campaign directory"
        )
    try:
        root_exists = campaign_root.exists()
        root_is_dir = campaign_root.is_dir()
    except OSError as exc:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT cannot be inspected; set "
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT to an accessible existing directory "
            f"(path {campaign_root_raw!r})"
        ) from exc
    if not root_exists or not root_is_dir:
        raise _refuse(
            f"OMNIAGENTOS_SIM_CAMPAIGN_ROOT={campaign_root_raw!r} must name an "
            "existing directory; create it or set the variable to an existing campaign directory"
        )

    sim_root_raw = values.get("OMNIAGENTOS_SIM_ROOT") or str(_DEFAULT_SIM_ROOT)
    sim_root = Path(sim_root_raw)
    if not sim_root.is_absolute():
        raise _refuse(
            f"OMNIAGENTOS_SIM_ROOT={sim_root_raw!r} is not absolute; set "
            "OMNIAGENTOS_SIM_ROOT to an absolute existing simulations directory"
        )
    campaign_parts = inode_relative_parts(campaign_root, sim_root)
    if campaign_parts is None:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT is not inode-contained in "
            f"OMNIAGENTOS_SIM_ROOT={sim_root_raw!r}; set both variables so the "
            "campaign directory is physically inside the simulations root"
        )
    if campaign_parts == ():
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT resolves to the same inode as "
            f"OMNIAGENTOS_SIM_ROOT={sim_root_raw!r}; set the campaign root to a child "
            "directory, never the simulations root itself"
        )

    try:
        resolved_basename = os.path.basename(os.path.realpath(campaign_root))
    except (OSError, ValueError) as exc:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT cannot be canonicalized; set "
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT to an accessible campaign directory"
        ) from exc
    if resolved_basename != campaign:
        raise _refuse(
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT realpath basename "
            f"{resolved_basename!r} does not match OMNIAGENTOS_SIM_CAMPAIGN="
            f"{campaign!r}; set the campaign name to the directory basename"
        )

    # Validate SIM_ROOT: refuse dangerous directories (m4 — fail-closed on dangerous roots)
    home = Path.home()
    if (
        sim_root == Path("/")
        or sim_root == home
        or inode_relative_parts(home, sim_root) is not None
    ):
        raise _refuse(
            f"OMNIAGENTOS_SIM_ROOT={sim_root_raw!r} cannot be '/', '$HOME', or an "
            "ancestor of $HOME; set OMNIAGENTOS_SIM_ROOT to a dedicated simulations "
            "directory (e.g. $HOME/OmniAgentOS-sims)"
        )

    var_parts = inode_relative_parts(var_dir, campaign_root)
    if var_parts is None:
        raise _refuse(
            f"{var_name}={var_raw!r} is not inode-contained in campaign_root "
            f"{campaign_root_raw!r}; set {var_name} to a directory inside the "
            "campaign root"
        )

    return SimContext(sim_mode=True, campaign=campaign, campaign_root=campaign_root)


def assert_registry_truth(environ: Mapping[str, str] | None = None) -> None:
    """Refuse a runtime connector registry that is missing or differs from truth.

    Three outcomes, each distinguishable in the refusal text because they need
    different operator actions:

    * **verified** — the runtime copy is byte-identical to ``configs/connectors.yaml``;
    * **missing_runtime_copy** — ``$OMNIAGENTOS_VAR_DIR/connectors.yaml`` does not
      exist. ``_default_registry_path()`` resolves the registry under the same var
      dir, so this is not a harmless absence: every capability lookup would fail at
      request time. The fix is to source ``scripts/launch-env.sh``, which mints the
      copy;
    * **drift** — the runtime copy parses but differs from repository truth. Both
      digests are named so the divergence can be diffed.

    Every failure mode raises :class:`SimGateError`, including an unreadable or
    malformed runtime copy, so ``assert_startup_coherence``'s "REFUSING TO START:"
    prefix contract holds for all of them rather than only for drift.
    """
    values = os.environ if environ is None else environ
    configured = values.get("OMNIAGENTOS_VAR_DIR")
    runtime_registry = (
        Path(configured).expanduser() / "connectors.yaml"
        if configured and configured.strip()
        else _REPO_REGISTRY_PATH
    )

    if not runtime_registry.is_file():
        raise _refuse(
            "connector registry runtime copy is missing: "
            f"{runtime_registry} does not exist. It is refreshed from "
            "configs/connectors.yaml by scripts/launch-env.sh; source that file "
            "(or launch via scripts/launch-omniagentos.sh) before starting the API."
        )

    # Parse and validate before comparing hashes so a malformed runtime copy is
    # reported as itself rather than disguised as drift. The registry's native
    # ConnectorError is wrapped, not propagated, so the refusal is uniform.
    try:
        load_registry(str(runtime_registry))
    except ConnectorError as exc:
        raise _refuse(f"connector registry runtime copy is unusable: {exc}") from exc
    try:
        loaded_hash = compute_registry_hash(runtime_registry)
        expected_hash = compute_registry_hash(_REPO_REGISTRY_PATH)
    except OSError as exc:
        raise _refuse(f"connector registry truth cannot be read: {exc}") from exc

    if loaded_hash != expected_hash:
        raise _refuse(
            "connector registry hash mismatch: "
            f"loaded_sha256={loaded_hash} expected_sha256={expected_hash}"
        )

    LOG.info(
        "Registry truth verified loaded_sha256=%s expected_sha256=%s",
        loaded_hash,
        expected_hash,
    )


def assert_startup_coherence(environ: Mapping[str, str] | None = None) -> None:
    """Refuse startup when simulation state or registry truth is incoherent."""
    try:
        resolve_sim_context(environ)
        assert_registry_truth(environ)
    except SimGateError as exc:
        raise SimGateError(f"REFUSING TO START: {exc}") from exc
