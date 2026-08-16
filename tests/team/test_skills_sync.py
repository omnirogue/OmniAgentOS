"""Per-machine skill installer (`scripts/team/skills_sync.py`).

The script is stdlib-only and standalone by design (it is copied to dev
laptops), so it is loaded from its path rather than imported as a package —
the same shape ``tests/comms/test_verify_mailboxes.py`` uses.

NO NETWORK and NO GIT REMOTE anywhere here: every run passes ``--no-pull``, and
the webhook transport is exercised through a stub.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SYNC_PATH = Path(__file__).resolve().parents[2] / "scripts" / "team" / "skills_sync.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("skills_sync_under_test", _SYNC_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass lookups need this registered first
    spec.loader.exec_module(module)
    return module


sync = _load()


def _write_skill(root: Path, slug: str, body: str, *, version: str | None = None) -> Path:
    path = root / slug
    path.mkdir(parents=True, exist_ok=True)
    lines = ["---", f'slug: "{slug}"']
    if version is not None:
        lines.append(f"version: {version}")
    lines.extend(["---", "", body, ""])
    (path / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return path


class Bench:
    """A repo + claude-dir + state file, and a runner for the CLI."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "OmniAgentOS"
        self.handwritten = self.repo / "skills"
        self.exported = self.repo / "skills-lib"
        self.handwritten.mkdir(parents=True)
        self.exported.mkdir(parents=True)
        self.claude_dir = tmp_path / "home" / ".claude" / "skills"
        self.state = tmp_path / "home" / ".skills-sync-state.json"

    def run(self, *extra: str) -> int:
        return sync.main(
            [
                "--repo",
                str(self.repo),
                "--claude-dir",
                str(self.claude_dir),
                "--state",
                str(self.state),
                "--no-pull",
                *extra,
            ]
        )

    def manifest(self) -> dict[str, Any]:
        if not self.state.exists():
            return {}
        loaded = json.loads(self.state.read_text(encoding="utf-8"))
        skills = loaded.get("skills")
        assert isinstance(skills, dict)
        return skills

    def installed(self, slug: str) -> str:
        return (self.claude_dir / slug / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    fixture = Bench(tmp_path)
    _write_skill(fixture.handwritten, "operator-runbook", "Hand-written body.")
    _write_skill(fixture.exported, "learned-skill", "Exported body.", version="3")
    return fixture


def test_installs_from_both_source_dirs(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.run() == 0

    out = capsys.readouterr().out
    assert "installed" in out
    assert "learned-skill@3" in out
    assert "operator-runbook@-" in out  # no version in the frontmatter: reported honestly
    assert "Hand-written body." in bench.installed("operator-runbook")
    assert "Exported body." in bench.installed("learned-skill")
    assert set(bench.manifest()) == {"operator-runbook", "learned-skill"}
    assert bench.manifest()["learned-skill"]["source"] == "skills-lib"


def test_second_run_is_a_no_op(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    bench.run()
    before = bench.state.read_text(encoding="utf-8")
    capsys.readouterr()

    assert bench.run() == 0

    assert "up to date" in capsys.readouterr().out
    assert bench.state.read_text(encoding="utf-8") == before


def test_content_change_updates_the_installed_copy(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run()
    capsys.readouterr()
    _write_skill(bench.exported, "learned-skill", "Exported body, revised.", version="4")

    assert bench.run() == 0

    assert "updated         learned-skill@4" in capsys.readouterr().out
    assert "revised" in bench.installed("learned-skill")
    assert bench.manifest()["learned-skill"]["version"] == "4"


def test_extra_file_in_a_skill_dir_counts_as_a_change(bench: Bench) -> None:
    bench.run()
    (bench.exported / "learned-skill" / "reference.md").write_text("more\n", encoding="utf-8")

    assert bench.run() == 0

    assert (bench.claude_dir / "learned-skill" / "reference.md").is_file()


def test_vanished_skill_is_removed(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    bench.run()
    capsys.readouterr()
    (bench.exported / "learned-skill" / "SKILL.md").unlink()
    (bench.exported / "learned-skill").rmdir()

    assert bench.run() == 0

    assert "removed         learned-skill@3" in capsys.readouterr().out
    assert not (bench.claude_dir / "learned-skill").exists()
    assert set(bench.manifest()) == {"operator-runbook"}
    assert (bench.claude_dir / "operator-runbook").is_dir()


def test_a_dev_personal_skill_is_never_touched(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    personal = bench.claude_dir / "my-own-thing"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text("mine, hands off\n", encoding="utf-8")

    assert bench.run() == 0

    assert (personal / "SKILL.md").read_text(encoding="utf-8") == "mine, hands off\n"
    assert "my-own-thing" not in bench.manifest()


def test_a_personal_skill_shadowing_a_repo_slug_survives(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    personal = bench.claude_dir / "learned-skill"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text("my own version\n", encoding="utf-8")

    assert bench.run() == 0

    # Same slug, but the manifest does not own it: refuse rather than clobber.
    assert (personal / "SKILL.md").read_text(encoding="utf-8") == "my own version\n"
    assert "learned-skill" not in bench.manifest()
    assert "not owned by this tool" in capsys.readouterr().err
    assert (bench.claude_dir / "operator-runbook").is_dir()


def test_handwritten_skill_wins_a_slug_collision(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_skill(bench.handwritten, "learned-skill", "The human version.", version="9")

    assert bench.run() == 0

    assert "The human version." in bench.installed("learned-skill")
    assert bench.manifest()["learned-skill"]["source"] == "skills"
    assert "shadowed by skills/learned-skill" in capsys.readouterr().err


def test_reinstalls_what_the_manifest_owns_but_disk_lost(bench: Bench) -> None:
    bench.run()
    import shutil

    shutil.rmtree(bench.claude_dir / "learned-skill")

    assert bench.run() == 0

    assert (bench.claude_dir / "learned-skill" / "SKILL.md").is_file()


def test_print_touches_nothing(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.run("--print") == 0

    out = capsys.readouterr().out
    assert "would installed" in out
    assert "learned-skill@3" in out
    assert not bench.claude_dir.exists()
    assert not bench.state.exists()


def test_print_after_install_reports_no_changes(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run()
    capsys.readouterr()

    assert bench.run("--print") == 0

    assert "(no changes)" in capsys.readouterr().out


def test_unreadable_state_owns_nothing_and_never_deletes(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run()
    bench.state.write_text("{not json", encoding="utf-8")

    assert bench.run() == 0

    # It owns nothing, so it must not delete the copies it made last time; it
    # reports them as foreign and leaves them alone.
    assert (bench.claude_dir / "learned-skill" / "SKILL.md").is_file()
    assert "not owned by this tool" in capsys.readouterr().err


def test_slack_summary_is_one_line_and_only_on_change(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(sync, "post_webhook", lambda url, text: posted.append((url, text)) or True)

    bench.run("--slack-webhook", "https://hooks.invalid/x")
    assert len(posted) == 1
    url, text = posted[0]
    assert url == "https://hooks.invalid/x"
    assert text.startswith("skills-sync ")
    assert "learned-skill@3" in text and "operator-runbook@-" in text
    assert "\n" not in text

    bench.run("--slack-webhook", "https://hooks.invalid/x")
    assert len(posted) == 1  # nothing changed: nothing posted


def test_summary_message_names_removals(bench: Bench) -> None:
    actions = [
        sync.Action(sync.INSTALL, "alpha", "1"),
        sync.Action(sync.REMOVE, "beta", "2"),
    ]
    assert sync.summary_message(actions, "laptop") == (
        "skills-sync laptop: updated alpha@1, beta (removed)"
    )


def test_digest_ignores_mtime_but_sees_content(tmp_path: Path) -> None:
    first = _write_skill(tmp_path / "a", "s", "body")
    second = _write_skill(tmp_path / "b", "s", "body")
    import os

    os.utime(second / "SKILL.md", (0, 0))
    assert sync.digest_dir(first) == sync.digest_dir(second)

    (second / "SKILL.md").write_text("---\nslug: s\n---\nother\n", encoding="utf-8")
    assert sync.digest_dir(first) != sync.digest_dir(second)


def test_digest_sees_a_rename(tmp_path: Path) -> None:
    first = _write_skill(tmp_path / "a", "s", "body")
    second = _write_skill(tmp_path / "b", "s", "body")
    (first / "notes.md").write_text("same bytes\n", encoding="utf-8")
    (second / "other.md").write_text("same bytes\n", encoding="utf-8")
    assert sync.digest_dir(first) != sync.digest_dir(second)


def test_bad_invocation_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nowhere"
    assert sync.main(["--repo", str(missing), "--no-pull", "--print"]) == 2

    empty = tmp_path / "clone"
    empty.mkdir()
    assert sync.main(["--repo", str(empty), "--no-pull", "--print"]) == 2
    assert "nothing to sync" in capsys.readouterr().err


def test_frontmatter_reader_handles_real_repo_skills() -> None:
    """The repo's own hand-written skills parse (block scalars and all)."""
    skill = Path(__file__).resolve().parents[2] / "skills" / "long-horizon-delivery" / "SKILL.md"
    assert sync._frontmatter_value(skill, "slug") == "long-horizon-delivery"
    assert sync._frontmatter_value(skill, "status") == "active"
    # `summary:` is a multi-line block scalar; its continuation lines must not
    # be mistaken for top-level keys.
    assert sync._frontmatter_value(skill, "version") == ""


def test_unsafe_manifest_slug_is_never_a_removal_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupted manifest key with path separators must never reach rmtree."""
    bench = Bench(tmp_path)
    _write_skill(bench.handwritten, "good", "body")
    victim = bench.claude_dir.parent / "pwn"
    victim.mkdir(parents=True)
    (victim / "precious.txt").write_text("keep me\n", encoding="utf-8")
    bench.claude_dir.mkdir(parents=True)
    bench.state.parent.mkdir(parents=True, exist_ok=True)
    bench.state.write_text(
        json.dumps({"schema": sync.SCHEMA, "skills": {"../pwn": {"digest": "x", "version": "1"}}}),
        encoding="utf-8",
    )

    assert bench.run() == 0

    assert (victim / "precious.txt").exists()
    assert "unsafe slug" in capsys.readouterr().err


def test_symlinked_skill_dir_is_not_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repo skill carrying a symlink is refused rather than materialized."""
    bench = Bench(tmp_path)
    _write_skill(bench.handwritten, "good", "body")
    skill = _write_skill(bench.handwritten, "sneaky", "body")
    secret = tmp_path / "outside.txt"
    secret.write_text("private\n", encoding="utf-8")
    (skill / "link.txt").symlink_to(secret)

    assert bench.run() == 0

    assert (bench.claude_dir / "good").exists()
    assert not (bench.claude_dir / "sneaky").exists()
    assert "symlink" in capsys.readouterr().err
