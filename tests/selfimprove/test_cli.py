"""python -m omniagentos.selfimprove {capture-skill,append-constraint}
(omniagentos/selfimprove/cli.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omniagentos.selfimprove.cli import main

from .helpers import sample_metadata, write_status_json


def _write_metadata_json(path: Path) -> Path:
    metadata = sample_metadata()
    path.write_text(metadata.model_dump_json(), encoding="utf-8")
    return path


def test_capture_skill_cli_writes_note(vault_dir: Path, tmp_path: Path, capsys: Any) -> None:
    run_dir = tmp_path / "session-1"
    write_status_json(run_dir, state="done")
    metadata_path = _write_metadata_json(tmp_path / "metadata.json")

    exit_code = main(
        [
            "capture-skill",
            "--run-dir",
            str(run_dir),
            "--metadata",
            str(metadata_path),
            "--vault-dir",
            str(vault_dir),
        ]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["note_path"]).is_file()


def test_capture_skill_cli_refuses_unverified_run_and_exits_nonzero(
    vault_dir: Path, tmp_path: Path, capsys: Any
) -> None:
    run_dir = tmp_path / "session-2"
    write_status_json(run_dir, state="partial")
    metadata_path = _write_metadata_json(tmp_path / "metadata.json")

    exit_code = main(
        [
            "capture-skill",
            "--run-dir",
            str(run_dir),
            "--metadata",
            str(metadata_path),
            "--vault-dir",
            str(vault_dir),
        ]
    )

    assert exit_code == 1
    assert "refused" in capsys.readouterr().err
    assert not (vault_dir / "playbook").exists()


def test_append_constraint_cli_writes_file(tmp_path: Path, capsys: Any) -> None:
    run_dir = tmp_path / "session-3"
    write_status_json(run_dir, state="done")
    constraints_dir = tmp_path / "constraints"

    exit_code = main(
        [
            "append-constraint",
            "--run-dir",
            str(run_dir),
            "--project",
            "demo-project",
            "--rule",
            "Always dry-run destructive scripts first.",
            "--constraints-dir",
            str(constraints_dir),
        ]
    )

    assert exit_code == 0
    written = Path(capsys.readouterr().out.strip())
    assert written.is_file()
    assert "Always dry-run destructive scripts first." in written.read_text(encoding="utf-8")


def test_append_constraint_cli_refuses_unverified_run(tmp_path: Path, capsys: Any) -> None:
    run_dir = tmp_path / "session-4"
    write_status_json(run_dir, state="failed")
    constraints_dir = tmp_path / "constraints"

    exit_code = main(
        [
            "append-constraint",
            "--run-dir",
            str(run_dir),
            "--project",
            "demo-project",
            "--rule",
            "Should not land.",
            "--constraints-dir",
            str(constraints_dir),
        ]
    )

    assert exit_code == 1
    assert "refused" in capsys.readouterr().err
    assert not constraints_dir.exists()
