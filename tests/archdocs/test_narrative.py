from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omniagentos.archdocs import narrative as narrative_mod
from omniagentos.archdocs.narrative import run_narrative_update
from tests.archdocs.test_archdocs import _build_fake_launchd, _build_fake_repo


def test_narrative_validator_rejects_missing_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fake_repo(tmp_path)
    _build_fake_launchd(tmp_path)

    # Create valid ARCHI.md
    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(
        "<!-- archdocs:stamp git_head=abc max_migration=2 route_count=2 generated_at=2026-07-25T09:30:05Z -->\n"
        "## Subsystems\n"
        "<!-- generated:begin:migrations -->\n"
        "- `001_init.sql` (v1)\n"
        "<!-- generated:end:migrations -->\n"
        "<!-- generated:begin:launchd -->\n"
        "<!-- generated:end:launchd -->\n"
        "<!-- generated:begin:packages -->\n"
        "<!-- generated:end:packages -->\n"
        "<!-- generated:begin:routes -->\n"
        "<!-- generated:end:routes -->\n"
        "## Notes (human)\n",
        encoding="utf-8",
    )

    # Mock subprocess.run for git log and gemini CLI
    mock_run = MagicMock()
    log_res = MagicMock()
    log_res.returncode = 0
    log_res.stdout = "abc..HEAD commit logs\n"

    cli_res = MagicMock()
    cli_res.returncode = 0
    # response missing 'migrations' block
    response_missing_marker = (
        "<!-- archdocs:stamp git_head=abc max_migration=2 route_count=2 generated_at=2026-07-25T09:30:05Z -->\n"
        "## Subsystems\n"
        "<!-- generated:begin:launchd -->\n"
        "<!-- generated:end:launchd -->\n"
        "<!-- generated:begin:packages -->\n"
        "<!-- generated:end:packages -->\n"
        "<!-- generated:begin:routes -->\n"
        "<!-- generated:end:routes -->\n"
        "## Notes (human)\n"
    )
    cli_res.stdout = json.dumps({"session_id": "test-session", "response": response_missing_marker})

    def side_effect(cmd, *args, **kwargs):
        if "git" in cmd:
            return log_res
        return MagicMock(returncode=1)

    mock_run.side_effect = side_effect
    monkeypatch.setattr("subprocess.run", mock_run)

    # The gemini call goes through Popen (process-group kill path).
    gemini_proc = MagicMock()
    gemini_proc.communicate.return_value = (cli_res.stdout, "")
    gemini_proc.returncode = 0
    monkeypatch.setattr("subprocess.Popen", MagicMock(return_value=gemini_proc))

    # Run narrative update, should return False because validation fails (missing migrations block)
    success = run_narrative_update(tmp_path)
    assert success is False


def test_narrative_validator_accepts_and_applies_valid_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fake_repo(tmp_path)
    _build_fake_launchd(tmp_path)

    # Create valid ARCHI.md
    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(
        "<!-- archdocs:stamp git_head=abc max_migration=2 route_count=2 generated_at=2026-07-25T09:30:05Z -->\n"
        "## Subsystems\n"
        "<!-- generated:begin:migrations -->\n"
        "- `001_init.sql` (v1)\n"
        "<!-- generated:end:migrations -->\n"
        "<!-- generated:begin:launchd -->\n"
        "<!-- generated:end:launchd -->\n"
        "<!-- generated:begin:packages -->\n"
        "<!-- generated:end:packages -->\n"
        "<!-- generated:begin:routes -->\n"
        "<!-- generated:end:routes -->\n"
        "## Notes (human)\n",
        encoding="utf-8",
    )

    # Mock subprocess.run for git log and gemini CLI
    mock_run = MagicMock()
    log_res = MagicMock()
    log_res.returncode = 0
    log_res.stdout = "abc..HEAD commit logs\n"

    cli_res = MagicMock()
    cli_res.returncode = 0
    valid_response = (
        "<!-- archdocs:stamp git_head=abc max_migration=2 route_count=2 generated_at=2026-07-25T09:30:05Z -->\n"
        "## Subsystems\n"
        "- **Execution** — updated narrative\n"
        "<!-- generated:begin:migrations -->\n"
        "- `001_init.sql` (v1)\n"
        "<!-- generated:end:migrations -->\n"
        "<!-- generated:begin:launchd -->\n"
        "<!-- generated:end:launchd -->\n"
        "<!-- generated:begin:packages -->\n"
        "<!-- generated:end:packages -->\n"
        "<!-- generated:begin:routes -->\n"
        "<!-- generated:end:routes -->\n"
        "## Notes (human)\n"
    )
    cli_res.stdout = json.dumps({"session_id": "test-session", "response": valid_response})

    def side_effect(cmd, *args, **kwargs):
        if "git" in cmd:
            return log_res
        return MagicMock(returncode=1)

    mock_run.side_effect = side_effect
    monkeypatch.setattr("subprocess.run", mock_run)

    # The gemini call goes through Popen (process-group kill path).
    gemini_proc = MagicMock()
    gemini_proc.communicate.return_value = (cli_res.stdout, "")
    gemini_proc.returncode = 0
    monkeypatch.setattr("subprocess.Popen", MagicMock(return_value=gemini_proc))

    # Run narrative update, should succeed and apply the change
    success = run_narrative_update(tmp_path)
    assert success is True

    updated_content = archi_path.read_text(encoding="utf-8")
    assert "Execution" in updated_content
    assert "updated narrative" in updated_content


def test_narrative_cli_exits_nonzero_when_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI must not map a failed narrative pass to shell success (exit 0).

    Counterfeit: ``sys.exit(0)`` unconditionally after ``run_narrative_update``,
    or exit based on "process started" rather than the boolean return value —
    launchd/cron then treat denial/empty/error as COMPLETED.
    """
    monkeypatch.setattr(narrative_mod, "run_narrative_update", lambda _root: False)
    monkeypatch.setattr(narrative_mod, "_repo_root_default", lambda: tmp_path)
    assert narrative_mod.main() == 1


def test_narrative_cli_exits_zero_when_update_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(narrative_mod, "run_narrative_update", lambda _root: True)
    monkeypatch.setattr(narrative_mod, "_repo_root_default", lambda: tmp_path)
    assert narrative_mod.main() == 0
