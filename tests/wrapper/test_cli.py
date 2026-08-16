from __future__ import annotations

from pathlib import Path
from typing import Any

import omniagentos.ledger as ledger
import omniagentos.vault as vault
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.wrapper.__main__ import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_dry_run_prints_without_touching_durable_spine(tmp_path: Path, capsys: Any) -> None:
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "runs.db"

    result = main(
        [
            "--fusion",
            str(FIXTURES / "fusion_run"),
            "--dry-run",
            "--ledger-dir",
            str(ledger_dir),
            "--vault-dir",
            str(vault_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert result == 0
    assert '"harness":{"harness":"fusion"' in capsys.readouterr().out
    assert not ledger_dir.exists()
    assert not vault_dir.exists()
    assert not db_path.exists()


def test_real_improve_import_writes_manifest_store_row_and_vault_note(tmp_path: Path) -> None:
    """This calls the frozen ledger/vault APIs at their real arity."""
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "runs.db"
    migrate(str(db_path))

    result = main(
        [
            "--improve",
            str(FIXTURES / "results.tsv"),
            "--write-vault",
            "--ledger-dir",
            str(ledger_dir),
            "--vault-dir",
            str(vault_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert result == 0
    manifests = ledger.read_manifests(str(ledger_dir))
    assert len(manifests) == 2
    store = SqliteStore(str(db_path))
    for manifest in manifests:
        row = store.get_run(manifest.run_id)
        assert row is not None
        assert row["task_id"] == manifest.task_id
        assert row["manifest_path"]
        assert row["vault_note_path"]
        note_path = Path(str(row["vault_note_path"]))
        assert note_path.is_file()
        assert vault.parse_frontmatter(note_path.read_text(encoding="utf-8")).id == manifest.run_id


def test_no_store_keeps_ledger_and_vault_writes_available(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "runs.db"

    result = main(
        [
            "--improve",
            str(FIXTURES / "results.tsv"),
            "--write-vault",
            "--no-store",
            "--ledger-dir",
            str(ledger_dir),
            "--vault-dir",
            str(vault_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert result == 0
    assert len(ledger.read_manifests(str(ledger_dir))) == 2
    notes = list((vault_dir / "runs").rglob("*.md"))
    assert len(notes) == 2
    for note in notes:
        content = note.read_text(encoding="utf-8")
        frontmatter = vault.parse_frontmatter(content)
        assert frontmatter.id.startswith("improve:")
        assert "## Input" in content
        assert '"imported":true' in content
        assert "## Provenance" in content
    assert not db_path.exists()
