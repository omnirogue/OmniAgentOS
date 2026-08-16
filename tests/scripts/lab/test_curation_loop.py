"""N10 — the curation loop must observe and exit, and must never ship exit 126."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.lab.curation_loop import (
    LAB_CURATION_MODE_ENV,
    LABEL,
    campaign_fingerprint,
    main,
    plist_problems,
    preflight_problems,
    render_default_plist,
    render_plist,
    repo_root,
    runner_path,
    template_path,
    wrapper_path,
)


@pytest.fixture(autouse=True)
def _arm_lab_curation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests exercise the observe pass under shadow (production default is off)."""
    monkeypatch.setenv(LAB_CURATION_MODE_ENV, "shadow")


def _seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real LabStore on disk with the demo discipline seeded, outside the checkout."""
    from omniagentos.lab import surfaces
    from omniagentos.lab.db import LabStore
    from omniagentos.lab.eval.grader import ProtectedGrader
    from omniagentos.lab.seed import ensure_demo_seeded

    surface_root = tmp_path / "surface-root"
    surface_root.mkdir()
    monkeypatch.setattr(surfaces, "_repository_root", lambda: surface_root)

    db_path = tmp_path / "lab.db"
    store = LabStore(str(db_path))
    with ProtectedGrader(":memory:") as grader:
        ensure_demo_seeded(store, grader)
    return db_path


# --- N4r guard: mode 0755 + an absolute, existing, executable program -----------


def test_runner_and_wrapper_are_mode_0755() -> None:
    for path in (runner_path(), wrapper_path()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755, path
        assert os.access(path, os.X_OK), path
    assert preflight_problems() == []


def test_rendered_plist_lints_and_points_at_an_executable_program(tmp_path: Path) -> None:
    target = render_default_plist(tmp_path / f"{LABEL}.plist")
    data: dict[str, Any] = plistlib.loads(target.read_bytes())

    assert data["Label"] == LABEL
    assert data["RunAtLoad"] is False
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 20}

    program = Path(data["ProgramArguments"][0])
    assert program.is_absolute()
    assert program.exists(), program
    assert os.access(program, os.X_OK), program
    assert stat.S_IMODE(program.stat().st_mode) == 0o755
    assert program == wrapper_path()

    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(data[key]).is_absolute()
        assert Path(data[key]).parent.is_dir()

    assert "{{" not in target.read_text(encoding="utf-8")
    assert plist_problems(target) == []

    plutil = shutil.which("plutil")
    if plutil is not None:  # macOS only; the plist is still parsed above elsewhere
        assert subprocess.run([plutil, "-lint", str(target)], check=False).returncode == 0


def test_plist_problems_flags_a_non_executable_program(tmp_path: Path) -> None:
    program = tmp_path / "not-executable.sh"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o644)
    log = tmp_path / "lab-curation.log"
    rendered = tmp_path / "bad.plist"
    rendered.write_text(
        render_plist(
            template_path().read_text(encoding="utf-8"),
            label=LABEL,
            program_args=[str(program)],
            working_dir=str(tmp_path),
            hour=3,
            minute=20,
            stdout_path=str(log),
            stderr_path=str(log),
        ),
        encoding="utf-8",
    )
    problems = plist_problems(rendered)
    assert any("not executable" in problem for problem in problems), problems


def test_plist_problems_flags_a_missing_and_relative_program(tmp_path: Path) -> None:
    log = tmp_path / "lab-curation.log"
    for program, expected in ((str(tmp_path / "gone.sh"), "does not exist"), ("run.sh", "absolute")):
        rendered = tmp_path / "case.plist"
        rendered.write_text(
            render_plist(
                template_path().read_text(encoding="utf-8"),
                label=LABEL,
                program_args=[program],
                working_dir=str(tmp_path),
                hour=3,
                minute=20,
                stdout_path=str(log),
                stderr_path=str(log),
            ),
            encoding="utf-8",
        )
        assert any(expected in problem for problem in plist_problems(rendered))


def test_self_test_cli_accepts_the_rendered_plist(tmp_path: Path) -> None:
    target = render_default_plist(tmp_path / f"{LABEL}.plist")
    assert main(["self-test", "--plist", str(target)]) == 0


def test_self_test_cli_rejects_a_broken_plist(tmp_path: Path) -> None:
    broken = tmp_path / "broken.plist"
    broken.write_text("not a plist", encoding="utf-8")
    assert main(["self-test", "--plist", str(broken)]) == 3


# --- observe-only -------------------------------------------------------------


def test_observe_pass_proposes_without_mutating_campaign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.lab.db import LabStore

    db_path = _seeded_db(tmp_path, monkeypatch)
    before_fingerprint = campaign_fingerprint(db_path)
    before_store = LabStore(str(db_path))
    assert before_store.list_experiments() == []
    before_surfaces = before_store.list_surfaces("general")

    out_dir = tmp_path / "artifacts"
    assert (
        main(
            [
                "run",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
                "--log-path",
                str(tmp_path / "lab-curation.log"),
                "--quiet",
            ]
        )
        == 0
    )

    # Campaign state is identical and holds no new experiments: proposals were
    # made against a sandbox copy, so nothing was promoted, run, or persisted.
    assert campaign_fingerprint(db_path) == before_fingerprint
    after_store = LabStore(str(db_path))
    assert after_store.list_experiments() == []
    assert after_store.list_surfaces("general") == before_surfaces

    artifacts = sorted(out_dir.glob("proposals-*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["observe_only"] is True
    assert payload["promoted"] == []
    assert payload["executed"] == []
    assert (
        payload["campaign_fingerprint_before"]
        == payload["campaign_fingerprint_after"]
        == before_fingerprint
    )
    assert payload["errors"] == []
    assert payload["proposal_count"] == len(payload["proposals"]) > 0
    assert {item["status"] for item in payload["proposals"]} == {"proposed"}
    assert {item["discipline"] for item in payload["proposals"]} == {"general"}
    for item in payload["proposals"]:
        assert item["challenger_surface_id"]
        assert item["eval_suite_id"]


def test_observe_pass_leaves_no_challenger_files_in_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """version_prompt writes a file per challenger; none of them may land in vault/."""
    db_path = _seeded_db(tmp_path, monkeypatch)
    vault_prompts = repo_root() / "vault" / "prompts"
    before = sorted(p.name for p in vault_prompts.rglob("*")) if vault_prompts.exists() else []

    assert (
        main(
            [
                "run",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(tmp_path / "artifacts"),
                "--log-path",
                str(tmp_path / "lab-curation.log"),
                "--quiet",
            ]
        )
        == 0
    )

    after = sorted(p.name for p in vault_prompts.rglob("*")) if vault_prompts.exists() else []
    assert after == before


def test_run_writes_an_artifact_when_the_database_is_absent(tmp_path: Path) -> None:
    out_dir = tmp_path / "artifacts"
    assert (
        main(
            [
                "run",
                "--db-path",
                str(tmp_path / "missing.db"),
                "--out-dir",
                str(out_dir),
                "--log-path",
                str(tmp_path / "lab-curation.log"),
                "--quiet",
            ]
        )
        == 0
    )
    payload = json.loads(next(out_dir.glob("proposals-*.json")).read_text(encoding="utf-8"))
    assert payload["db_exists"] is False
    assert payload["proposals"] == []


def test_off_mode_skips_observe_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LAB_CURATION_MODE_ENV, "off")
    out_dir = tmp_path / "artifacts"
    assert (
        main(
            [
                "run",
                "--db-path",
                str(tmp_path / "missing.db"),
                "--out-dir",
                str(out_dir),
                "--log-path",
                str(tmp_path / "lab-curation.log"),
                "--quiet",
            ]
        )
        == 0
    )
    assert list(out_dir.glob("proposals-*.json")) == []
