"""update_home (contracts/interfaces.md §p05): rewrites ONLY the
omniagentos:status block; everything else in Home.md is untouched."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.vault import VaultError, update_home
from omniagentos.vault.home import STATUS_BEGIN, STATUS_END


def _before_after(content: str) -> tuple[str, str]:
    before = content.split(STATUS_BEGIN, 1)[0]
    after = content.split(STATUS_END, 1)[1]
    return before, after


def test_update_home_touches_only_the_status_block(vault_dir: Path) -> None:
    home_path = vault_dir / "Home.md"
    original = home_path.read_text(encoding="utf-8")
    before_orig, after_orig = _before_after(original)

    update_home(str(vault_dir), {"total_runs": 5, "by_state": {"completed": 4, "failed": 1}})

    updated = home_path.read_text(encoding="utf-8")
    before_new, after_new = _before_after(updated)

    assert before_new == before_orig
    assert after_new == after_orig
    assert updated != original  # the block itself did change


def test_update_home_status_block_contains_stats(vault_dir: Path) -> None:
    update_home(str(vault_dir), {"total_runs": 5, "by_state": {"completed": 4, "failed": 1}})
    content = (vault_dir / "Home.md").read_text(encoding="utf-8")
    block = content.split(STATUS_BEGIN, 1)[1].split(STATUS_END, 1)[0]
    assert "5" in block
    assert "completed: 4" in block
    assert "failed: 1" in block


def test_update_home_empty_stats_matches_original_placeholder(vault_dir: Path) -> None:
    home_path = vault_dir / "Home.md"
    original = home_path.read_text(encoding="utf-8")

    update_home(str(vault_dir), {})

    updated = home_path.read_text(encoding="utf-8")
    assert updated == original  # empty stats -> same "_No runs recorded yet._" placeholder


def test_update_home_preserves_notes_human_section(vault_dir: Path) -> None:
    home_path = vault_dir / "Home.md"
    content = home_path.read_text(encoding="utf-8")
    content += "\nOperator wrote this note.\n"
    home_path.write_text(content, encoding="utf-8")

    update_home(str(vault_dir), {"total_runs": 1})

    final = home_path.read_text(encoding="utf-8")
    assert "Operator wrote this note." in final


def test_update_home_raises_if_home_missing(tmp_path: Path) -> None:
    empty_vault = tmp_path / "vault"
    empty_vault.mkdir()
    with pytest.raises(VaultError):
        update_home(str(empty_vault), {"total_runs": 1})


def test_update_home_raises_if_markers_missing(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    v.mkdir()
    (v / "Home.md").write_text("# Home\n\nno markers here\n", encoding="utf-8")
    with pytest.raises(VaultError):
        update_home(str(v), {"total_runs": 1})


def test_update_home_is_idempotent_shape_across_calls(vault_dir: Path) -> None:
    update_home(str(vault_dir), {"total_runs": 2})
    first = (vault_dir / "Home.md").read_text(encoding="utf-8")
    update_home(str(vault_dir), {"total_runs": 2})
    second = (vault_dir / "Home.md").read_text(encoding="utf-8")
    # content outside the block is stable; block re-renders (timestamp differs)
    # but structurally still contains the same stat
    before1, after1 = _before_after(first)
    before2, after2 = _before_after(second)
    assert before1 == before2
    assert after1 == after2
