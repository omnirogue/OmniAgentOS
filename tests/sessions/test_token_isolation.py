"""Isolation coverage for session token path resolution under sim mode.

These tests fail against the historical import-time
``TOKEN_PATH = Path(__file__).resolve().parents[2] / "var" / "secrets" /
"sessions-token"`` default: under ``OMNIAGENTOS_SIM_MODE=1`` that path silently
pointed at the operator checkout credential file.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.scope.paths import resolve_into_realm
from omniagentos.sessions import token

_OPERATOR_CHECKOUT = Path("/Users/youruser/OmniAgentOS")
_PRODUCTION_TOKEN = _OPERATOR_CHECKOUT / "var" / "secrets" / "sessions-token"
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_WORKTREE_TOKEN = _WORKTREE_ROOT / "var" / "secrets" / "sessions-token"


def test_sim_mode_token_path_stays_inside_campaign_not_operator_checkout(
    tmp_path: Path,
) -> None:
    """Decisive: sim mode + campaign var dir, cwd at operator checkout.

    Must resolve into the campaign root, not the production path, and must not
    create anything on the production path. Subprocess so import is fresh and
    cwd is controlled. PYTHONSAFEPATH=1 keeps cwd off sys.path so the worktree
    package (via PYTHONPATH) is what actually loads.
    """
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    production_existed_before = _PRODUCTION_TOKEN.exists()
    production_mtime_before: float | None = None
    if production_existed_before:
        production_mtime_before = _PRODUCTION_TOKEN.stat().st_mtime

    env = os.environ.copy()
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_VAR_DIR"] = str(campaign_root)
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "token-isolation-test"
    env.pop("OMNIAGENTOS_VAR", None)
    env["PYTHONPATH"] = str(_WORKTREE_ROOT)
    # Python 3.11+: omit the script/cwd directory from sys.path so a cwd of
    # the operator checkout cannot shadow PYTHONPATH with that checkout's tree.
    env["PYTHONSAFEPATH"] = "1"

    if _OPERATOR_CHECKOUT.is_dir():
        cwd: Path = _OPERATOR_CHECKOUT
    else:
        cwd = tmp_path / "no-operator-checkout"
        cwd.mkdir()

    # WORKTREE is passed via argv (not string interpolation) so the subprocess
    # can assert it loaded this worktree's package, not the operator checkout.
    script = """
import sys
from pathlib import Path

import omniagentos.sessions.token as t

worktree = Path(sys.argv[1]).resolve()
assert Path(t.__file__).resolve().is_relative_to(worktree), t.__file__

path = t.token_path()
value = t.load_or_create_token()
print(path.resolve())
print(value)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(_WORKTREE_ROOT)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    lines = completed.stdout.strip().splitlines()
    assert len(lines) >= 2, f"expected path and token lines, got {completed.stdout!r}"
    printed = Path(lines[0])
    printed_token = lines[1]

    assert resolve_into_realm(str(printed), str(campaign_root)) is not None
    assert printed != _PRODUCTION_TOKEN
    # Dies when the fix is reverted to __file__-derived import-time TOKEN_PATH.
    assert printed != _WORKTREE_TOKEN
    assert printed == campaign_root / "secrets" / "sessions-token"

    campaign_token = campaign_root / "secrets" / "sessions-token"
    assert campaign_token.is_file()
    assert stat.S_IMODE(campaign_token.stat().st_mode) == 0o600
    assert campaign_token.read_text(encoding="utf-8").strip() == printed_token
    secrets_dir = campaign_root / "secrets"
    assert secrets_dir.is_dir()
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700

    assert _PRODUCTION_TOKEN.exists() is production_existed_before
    if production_existed_before:
        assert production_mtime_before is not None
        assert _PRODUCTION_TOKEN.stat().st_mtime == production_mtime_before


def test_sim_mode_missing_var_dir_raises_token_path_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    legacy_existed_before = token._LEGACY_TOKEN_PATH.exists()

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    message = str(exc_info.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "OMNIAGENTOS_VAR_DIR" in message or "OMNIAGENTOS_VAR" in message
    # Error message must not present the legacy path as a returned value.
    assert str(token._LEGACY_TOKEN_PATH) not in message
    # Failed resolution must not create the legacy token on disk.
    assert token._LEGACY_TOKEN_PATH.exists() is legacy_existed_before


@pytest.mark.parametrize(
    "sim_mode_value",
    ["0", "true", "yes", " 1", "1 "],
)
def test_sim_mode_incoherent_value_refuses_never_silently_production(
    monkeypatch: pytest.MonkeyPatch,
    sim_mode_value: str,
) -> None:
    """Fail closed: an incoherent SIM_MODE must never be silently treated as production.

    Regression for the historical ``os.environ.get(...) == "1"`` truthiness check,
    which treated ``'0'`` / ``'true'`` / stray whitespace as production
    (``sim_mode=False``) and quietly fell through instead of refusing. Mirrors
    ``simgate.resolve_sim_context``'s strict parse: only the exact string ``"1"``
    or unset/empty are coherent.
    """
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", sim_mode_value)
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    legacy_existed_before = token._LEGACY_TOKEN_PATH.exists()

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    message = str(exc_info.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert repr(sim_mode_value) in message
    # Failed resolution must not create the legacy token on disk.
    assert token._LEGACY_TOKEN_PATH.exists() is legacy_existed_before


def test_sim_mode_unset_is_production_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset SIM_MODE is plain production (sim_mode=False), never a refusal."""
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert token.token_path() == token._LEGACY_TOKEN_PATH


def test_sim_mode_exactly_one_engages_sim_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly ``'1'`` is the only value that engages sim resolution."""
    campaign = tmp_path / "campaign-var"
    campaign.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert token.token_path() == campaign / "secrets" / "sessions-token"


def test_sim_mode_relative_var_dir_raises_token_path_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", "relative/campaign/var")
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    assert "OMNIAGENTOS_SIM_MODE" in str(exc_info.value)


def test_sim_mode_var_dir_pointing_at_legacy_root_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(token._DEFAULT_VAR_ROOT))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    message = str(exc_info.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()


def _volume_is_case_insensitive(tmp_path: Path) -> bool:
    """Detect volume case-insensitivity by inode identity, not platform string."""
    probe = tmp_path / "CaseProbeTokenIsolation"
    probe.write_text("probe\n", encoding="utf-8")
    try:
        lower = tmp_path / "caseprobetokenisolation"
        return os.stat(probe).st_ino == os.stat(lower).st_ino
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "var_dir_factory,expect_raise",
    [
        (lambda: str(token._DEFAULT_VAR_ROOT), True),
        (lambda: str(token._DEFAULT_VAR_ROOT).lower(), True),
        (lambda: str(token._DEFAULT_VAR_ROOT).upper(), True),
        (
            lambda: str(token._DEFAULT_VAR_ROOT / "simulations" / "campaign-x"),
            False,
        ),
    ],
    ids=["exact", "lower", "upper", "campaign-under-var"],
)
def test_sim_mode_legacy_var_case_and_campaign_truth_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    var_dir_factory: Callable[[], str],
    expect_raise: bool,
) -> None:
    """Pin production-path identity after realpath + case-fold.

    CRITICAL SAFETY: only call token_path() here — never load_or_create_token()
    when OMNIAGENTOS_VAR_DIR points near the real repo var. token_path() is pure
    and touches no filesystem state; do not create, chmod, read, or delete
    anything under the real var/secrets/.
    """
    var_dir = var_dir_factory()
    # Case variants only matter on case-insensitive volumes; on a truly
    # case-sensitive FS the lower/upper spellings are different directories.
    if var_dir != str(token._DEFAULT_VAR_ROOT) and var_dir in (
        str(token._DEFAULT_VAR_ROOT).lower(),
        str(token._DEFAULT_VAR_ROOT).upper(),
    ):
        if not _volume_is_case_insensitive(tmp_path):
            pytest.skip("volume is case-sensitive; case variants are distinct paths")

    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", var_dir)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    if expect_raise:
        with pytest.raises(token.TokenPathError) as exc_info:
            token.token_path()
        message = str(exc_info.value)
        assert "OMNIAGENTOS_SIM_MODE" in message
        assert "production" in message.lower() or "legacy" in message.lower()
    else:
        path = token.token_path()
        assert path == Path(var_dir) / "secrets" / "sessions-token"


def test_sim_mode_symlink_to_repo_var_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink into the real repo var must be rejected (realpath identity).

    CRITICAL SAFETY: only call token_path() — never load_or_create_token() when
    the var dir resolves near the real repo var. Do not create, chmod, read, or
    delete anything under the real var/secrets/.
    """
    link = tmp_path / "link-to-repo-var"
    link.symlink_to(token._DEFAULT_VAR_ROOT)

    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(link))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    message = str(exc_info.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()


def _firmlink_spelling(path: Path) -> Path:
    """The macOS data-volume spelling of an absolute path (same inode, other string)."""
    return Path("/System/Volumes/Data" + os.path.realpath(path))


def test_sim_mode_firmlink_spelling_of_legacy_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decisive: a different STRING naming the SAME inode must still be refused.

    A canonical-spelling comparison returns False here (the firmlink path is a
    distinct string that ``realpath`` does not rewrite) while ``samestat`` says
    it is the very same directory — a false negative that admits production.

    CRITICAL SAFETY: only token_path() is called; nothing is created or read.
    """
    # Anchor the firmlink on the repo root (which exists); var/ itself may not.
    real_root = Path(os.path.realpath(token._REPO_ROOT))
    firmlink_root = _firmlink_spelling(real_root)
    if not firmlink_root.exists():
        pytest.skip(f"no firmlink spelling on this machine: {firmlink_root}")
    # Assert, don't silently pass: if it exists it MUST be the same inode.
    assert os.path.samestat(os.stat(firmlink_root), os.stat(real_root))

    real_var = Path(os.path.realpath(token._DEFAULT_VAR_ROOT))
    firmlink_var = firmlink_root / real_var.name
    assert str(firmlink_var) != str(real_var)
    # The whole point: canonical spelling calls these two different paths.
    assert os.path.realpath(firmlink_var) != os.path.realpath(real_var)

    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(firmlink_var))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(token.TokenPathError) as exc_info:
        token.token_path()

    message = str(exc_info.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()


def test_sim_mode_absent_token_file_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must-keep: the token leaf commonly does not exist yet.

    Identity must be decided without stat-ing the leaf, so a legitimate sim var
    dir with no secrets/ directory at all resolves normally and never raises.
    """
    campaign = tmp_path / "campaign-var"
    campaign.mkdir()
    assert not (campaign / "secrets").exists()

    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert token.token_path() == campaign / "secrets" / "sessions-token"

    # Also with the var root itself absent: still no stat of the leaf required.
    absent = tmp_path / "not-created-yet"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(absent))
    assert token.token_path() == absent / "secrets" / "sessions-token"


def test_legacy_identity_requires_leaf_name_not_just_parent_inode() -> None:
    """Counterfeit catcher: comparing only the parent directory by inode.

    That weaker rule would treat EVERY file in the production secrets/ dir as
    the token. Identity is (ancestor inode AND the exact remaining components),
    so a different leaf in the same directory is not the production token.

    CRITICAL SAFETY: pure identity queries; nothing is created or read.
    """
    legacy = token._LEGACY_TOKEN_PATH
    assert token._is_legacy_token_path(legacy) is True
    # Same parent directory, different leaf — parent-inode-only would say True.
    assert token._is_legacy_token_path(legacy.parent / "other-secret") is False
    assert token._is_legacy_token_path(legacy.parent / "sessions-token.bak") is False
    # The secrets directory itself is not the token file either.
    assert token._is_legacy_token_path(legacy.parent) is False
    # ...and the firmlink spelling of the token IS still the token.
    firmlink_root = _firmlink_spelling(token._REPO_ROOT)
    if firmlink_root.exists():
        firmlink_legacy = firmlink_root / "var" / "secrets" / legacy.name
        assert token._is_legacy_token_path(firmlink_legacy) is True
        assert token._is_legacy_token_path(firmlink_legacy.parent / "other") is False


def test_legacy_identity_absent_suffix_is_exact_not_case_folded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent remaining components must match exactly — no case-fold / lower.

    Failing-on-revert: if ``_is_legacy_token_path`` reintroduces ``_fold`` /
    ``normcase`` / ``.lower()`` on the unstatable suffix, a different-case
    absent leaf is treated as the production token.
    CRITICAL SAFETY: pure identity query; nothing is created or read.
    """
    # Point the legacy anchor at an existing tmp root so the leaf can be absent.
    anchor = tmp_path / "var"
    secrets = anchor / "secrets"
    secrets.mkdir(parents=True)
    legacy = secrets / "sessions-token"
    # Leaf intentionally absent.
    assert not legacy.exists()

    monkeypatch.setattr(token, "_LEGACY_TOKEN_PATH", legacy)
    monkeypatch.setattr(token, "_DEFAULT_VAR_ROOT", anchor)
    monkeypatch.setattr(token, "_REPO_ROOT", tmp_path)

    assert token._is_legacy_token_path(legacy) is True
    # Same components, exact spelling.
    assert token._is_legacy_token_path(secrets / "sessions-token") is True
    # Case-folded *absent* leaf must NOT match (contract: exact remaining
    # components). Existing ancestors still collapse through samestat.
    assert token._is_legacy_token_path(secrets / "Sessions-Token") is False
    assert token._is_legacy_token_path(secrets / "SESSIONS-TOKEN") is False
    assert token._is_legacy_token_path(secrets / "sessions-token.bak") is False


def test_non_sim_honours_omniagentos_var_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert token.token_path() == tmp_path / "secrets" / "sessions-token"


def test_non_sim_empty_env_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert token.token_path() == token._LEGACY_TOKEN_PATH
    assert token.token_path() == token._DEFAULT_VAR_ROOT / "secrets" / "sessions-token"


def test_token_path_override_wins_for_load_or_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "sessions-token"
    monkeypatch.setattr(token, "TOKEN_PATH", override)
    legacy_existed_before = token._LEGACY_TOKEN_PATH.exists()

    value = token.load_or_create_token()

    assert override.is_file()
    assert override.read_text(encoding="utf-8").strip() == value
    mode = stat.S_IMODE(override.stat().st_mode)
    assert mode == 0o600
    # Override write must not create (or remove) the legacy token path.
    assert token._LEGACY_TOKEN_PATH.exists() is legacy_existed_before
    assert resolve_into_realm(str(override), str(tmp_path)) is not None


def test_token_path_override_wins_under_sim_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override-token"
    campaign = tmp_path / "campaign-var"
    campaign.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)
    monkeypatch.setattr(token, "TOKEN_PATH", override)

    assert token.token_path() == override
    value = token.load_or_create_token()
    assert override.is_file()
    assert override.read_text(encoding="utf-8").strip() == value
    mode = stat.S_IMODE(override.stat().st_mode)
    assert mode == 0o600
    assert not (campaign / "secrets" / "sessions-token").exists()


def test_monkeypatch_teardown_restores_lazy_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outer env for post-teardown lazy check.
    outer_var = tmp_path / "outer-var"
    outer_var.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(outer_var))
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    override = tmp_path / "mp-override-token"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(token, "TOKEN_PATH", override)
        assert token.TOKEN_PATH == override
        assert token.token_path() == override

    assert token.TOKEN_PATH == token._LEGACY_TOKEN_PATH
    assert token.token_path() == outer_var / "secrets" / "sessions-token"


def test_omniagentos_var_fallback_and_var_dir_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    var_only = tmp_path / "var-only"
    var_dir = tmp_path / "var-dir"
    var_only.mkdir()
    var_dir.mkdir()

    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_only))
    assert token.token_path() == var_only / "secrets" / "sessions-token"

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_only))
    assert token.token_path() == var_dir / "secrets" / "sessions-token"


def test_env_set_after_import_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Import-time resolution was half the bug; env must apply at call time."""
    # Module is already imported; clear env then set after "import".
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)
    assert token.token_path() == token._LEGACY_TOKEN_PATH

    late = tmp_path / "late-var"
    late.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(late))
    assert token.token_path() == late / "secrets" / "sessions-token"


def test_verify_token_honours_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_token must read the active TOKEN_PATH, not a hard-coded legacy file."""
    known = "known-verify-token-value-for-isolation"
    override = tmp_path / "verify-token"
    override.write_text(f"{known}\n", encoding="utf-8")
    monkeypatch.setattr(token, "TOKEN_PATH", override)

    assert token.verify_token(known) is True
    assert token.verify_token("wrong") is False
    assert token.verify_token(None) is False
    assert token.verify_token("") is False
