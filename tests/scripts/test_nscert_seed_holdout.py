"""The seeder must never recreate the in-checkout protected-store leak (R1-012).

``tests/lab/eval/test_pentest_leak.py`` pins ``var/eval_protected.db`` as a
must-not-exist location; these tests pin the seeder's refusal of the whole
in-checkout class, not the one spelling.

The round-2 review killed the first repair here: it derived the guarded root
from ``omniagentos.__file__`` only, and these tests asked ``_repo_root()`` where
that was — so they agreed with whatever the implementation had picked and could
not see the case that matters, where the installed package and the executing
script live in DIFFERENT checkouts.  So the split is simulated explicitly below,
and both roots are asserted refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.northstar_cert.seed_holdout import (
    _protected_roots,
    _repo_root,
    _script_root,
    main,
    seed_holdout,
)


def test_seeding_inside_the_checkout_is_refused() -> None:
    inside = _script_root() / "var" / "eval_protected.db"
    with pytest.raises(ValueError, match="inside the checkout"):
        seed_holdout(inside)
    assert not inside.exists()


def test_any_in_repo_path_is_refused_not_just_the_old_default() -> None:
    elsewhere_inside = _script_root() / "var" / "northstar-cert" / "sneaky.db"
    with pytest.raises(ValueError, match="inside the checkout"):
        seed_holdout(elsewhere_inside)
    assert not elsewhere_inside.exists()


def test_the_script_checkout_is_guarded_even_when_the_package_lives_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production split: cadence imports the INSTALLED checkout's package
    while running this script out of a worktree.

    A guard keyed on ``omniagentos.__file__`` alone refuses the checkout nobody
    is writing to and admits the one that is being written to.  Both must refuse.
    """
    import omniagentos

    installed = (tmp_path / "installed-checkout").resolve()
    (installed / "omniagentos").mkdir(parents=True)
    monkeypatch.setattr(omniagentos, "__file__", str(installed / "omniagentos" / "__init__.py"))

    # The split is real for this test, not assumed.
    assert _repo_root().resolve() == installed
    assert _script_root().resolve() != installed
    assert set(_protected_roots()) == {installed, _script_root().resolve()}

    in_script_checkout = _script_root() / "var" / "eval_protected.db"
    with pytest.raises(ValueError, match="inside the checkout"):
        seed_holdout(in_script_checkout)
    assert not in_script_checkout.exists()

    in_installed_checkout = installed / "var" / "eval_protected.db"
    with pytest.raises(ValueError, match="inside the checkout"):
        seed_holdout(in_installed_checkout)
    assert not in_installed_checkout.exists()


def test_the_guard_narrows_rather_than_opens_when_the_package_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unimportable package must not disable the guard.

    Absence is never favourable: with no package root to name, the script's own
    checkout is still guarded.
    """
    import scripts.northstar_cert.seed_holdout as module

    def _unimportable() -> Path:
        raise ImportError("no omniagentos on this interpreter")

    monkeypatch.setattr(module, "_repo_root", _unimportable)
    assert _protected_roots() == (_script_root().resolve(),)
    with pytest.raises(ValueError, match="inside the checkout"):
        seed_holdout(_script_root() / "var" / "eval_protected.db")


def test_out_of_checkout_seeding_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "protected.db"
    resolved = seed_holdout(target)
    assert resolved == target.resolve()
    assert target.exists()


def test_cli_refuses_in_checkout_path_with_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    import sys
    from unittest import mock

    argv = ["seed_holdout", "--path", str(_script_root() / "var" / "eval_protected.db")]
    with mock.patch.object(sys, "argv", argv):
        assert main() == 2
    assert "refused" in capsys.readouterr().err
