"""Contract tests for the standalone dev transcript uploader.

Everything here is local: harvest roots live under a fake ``$HOME``, and the
only remote a push test ever sees is a bare repository on the filesystem. No
test in this file may touch the network — the uploader's whole job is moving
bytes a dev already has into a clone they already have.

Every secret in a fixture is OBVIOUSLY FAKE (``FAKE``/``0000`` filler) and is
asserted absent from the uploaded copy, which is what makes a leak here a test
failure rather than a real leak.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from omniagentos.team import transcript_uploader as uploader

DAY = 86400.0


def _write(path: Path, text: str, *, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake $HOME the default harvest roots resolve against."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """An ai-transcripts clone with a bare file:// remote and an upstream."""
    bare = tmp_path / "remote.git"
    # -b main: without it the bare repo's HEAD points at the host's
    # init.defaultBranch (often master), so a second clone checks out nothing
    # ("remote HEAD refers to nonexistent ref"), commits to the wrong branch,
    # and the victim's pull sees none of it — the rebase test then fails on
    # any machine whose default branch is not main (CI runners included).
    subprocess.run(["git", "init", "--bare", "--quiet", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "ai-transcripts"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "uploader@example.invalid")
    _git(repo, "config", "user.name", "Uploader Test")
    (repo / "README.md").write_text("archive\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "init")
    _git(repo, "remote", "add", "origin", bare.as_uri())
    _git(repo, "push", "--quiet", "-u", "origin", "main")
    return repo


def _run(home: Path, clone: Path, *extra: str) -> int:
    return uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
            "--no-push",
            *extra,
        ]
    )


def _uploaded(clone: Path) -> list[Path]:
    return sorted(
        path
        for path in (clone / "transcripts").rglob("*")
        if path.is_file() and path.name != "NOTES.md"
    )


# --- harvest, watermark, window ------------------------------------------------


def test_second_run_uploads_nothing_new(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", '{"type":"user"}\n')

    assert _run(home, clone) == 0
    first = _uploaded(clone)
    assert len(first) == 1

    assert _run(home, clone) == 0
    assert _uploaded(clone) == first
    notes = (clone / "transcripts/bob/NOTES.md").read_text(encoding="utf-8").splitlines()
    assert len(notes) == 1, "a run with nothing new must not append a NOTES line"


def test_a_grown_file_is_uploaded_again(home: Path, clone: Path) -> None:
    source = _write(home / ".claude/projects/repo/session-a.jsonl", '{"type":"user"}\n')
    assert _run(home, clone) == 0

    source.write_text('{"type":"user"}\n{"type":"assistant"}\n', encoding="utf-8")
    assert _run(home, clone) == 0
    target = _uploaded(clone)[0]
    assert target.read_text(encoding="utf-8").count("\n") == 2


def test_files_outside_the_window_are_never_harvested(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/fresh.jsonl", '{"type":"user"}\n', age_days=1)
    _write(home / ".claude/projects/repo/stale.jsonl", '{"type":"user"}\n', age_days=9)

    assert _run(home, clone) == 0
    assert [path.name.split("__", 2)[-1] for path in _uploaded(clone)] == ["fresh.jsonl"]

    assert _run(home, clone, "--window-days", "30") == 0
    assert sorted(path.name.split("__", 2)[-1] for path in _uploaded(clone)) == [
        "fresh.jsonl",
        "stale.jsonl",
    ]


def test_every_default_root_family_is_harvested(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/claude.jsonl", "{}\n")
    _write(home / ".claude-account-1/projects/repo/claude-alt.jsonl", "{}\n")
    _write(home / ".codex/sessions/2026/rollout.jsonl", "{}\n")
    _write(home / ".codex-twin/sessions/2026/rollout-twin.jsonl", "{}\n")
    _write(home / ".kimi-code/sessions/sess-7/wire.jsonl", "{}\n")
    _write(home / ".gemini/tmp/abc/chat.json", "{}\n")
    _write(home / ".grok/sessions/grok-1.jsonl", "{}\n")
    _write(home / ".ai-transcripts-spool/aider-run.log", "hello\n")

    assert _run(home, clone) == 0
    names = [path.name for path in _uploaded(clone)]
    assert len(names) == 8
    clis = sorted({name.split("__")[1] for name in names})
    assert clis == ["claude", "codex", "gemini", "grok", "kimi", "spool"]
    # Kimi names every transcript wire.jsonl; the session directory disambiguates.
    assert any(name.endswith("__kimi__sess-7.jsonl") for name in names)


def test_roots_override_replaces_the_defaults(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/ignored.jsonl", "{}\n")
    _write(home / "custom/logs/kept.txt", "hello\n")

    roots = json.dumps([{"cli": "spool", "glob": "custom/**/*.txt"}])
    assert _run(home, clone, "--roots", roots) == 0
    assert [path.name.split("__")[-1] for path in _uploaded(clone)] == ["kept.txt"]


# --- redaction -----------------------------------------------------------------

FAKE_SECRETS: tuple[tuple[str, str], ...] = (
    ("openai-key", "sk-FAKEFAKEFAKEFAKE0000"),
    ("github-pat", "ghp_FAKE0000FAKE0000FAKE0"),
    ("github-fine-grained-pat", "github_pat_FAKE0000FAKE0000FAKE0"),
    ("slack-token", "xoxb-0000FAKE0000FAKE"),
    ("aws-access-key-id", "AKIAFAKEFAKEFAKE0000"),
    ("google-api-key", "AIzaFAKE0000FAKE0000FAKE0000FAKE00"),
    ("facebook-token", "EAAFAKE0000FAKE0000FAKE0000FAKE000"),
    ("atlassian-token", "pit-0000aaaa0000bbbb"),
    ("slack-webhook", "hooks.slack.com/services/T00FAKE/B00FAKE/FAKEFAKEFAKE"),
)


@pytest.mark.parametrize(("shape", "secret"), FAKE_SECRETS, ids=[s for s, _ in FAKE_SECRETS])
def test_each_key_shape_is_redacted_in_the_copy(
    shape: str, secret: str, home: Path, clone: Path
) -> None:
    source = _write(
        home / ".claude/projects/repo/leak.jsonl",
        f'{{"text":"export TOKEN={secret} rest"}}\n',
    )

    assert _run(home, clone) == 0
    uploaded = _uploaded(clone)[0].read_text(encoding="utf-8")
    assert secret not in uploaded
    assert f"[REDACTED:{shape}]" in uploaded
    assert uploaded.endswith(' rest"}\n'), "only the matched span is replaced"
    # The original is never rewritten — this tool reads work, it does not edit it.
    assert secret in source.read_text(encoding="utf-8")


def test_private_key_block_redacts_through_its_end_line() -> None:
    text = (
        "keep me\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vFAKEFAKE\n"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAKE\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "keep me too\n"
    )
    redacted, count = uploader.redact_text(text)
    assert count == 1
    assert "b3BlbnNzaC1rZXktdjEA" not in redacted
    assert redacted == "keep me\n[REDACTED:private-key]\nkeep me too\n"


def test_private_key_inlined_on_one_json_line_keeps_the_rest_of_the_line() -> None:
    line = (
        '{"key":"-----BEGIN RSA PRIVATE KEY-----MIIFAKEFAKE'
        '-----END RSA PRIVATE KEY-----","note":"kept"}\n'
    )
    redacted, count = uploader.redact_text(line)
    assert count == 1
    assert "MIIFAKEFAKE" not in redacted
    assert redacted == '{"key":"[REDACTED:private-key]","note":"kept"}\n'


def test_unterminated_private_key_redacts_to_end_of_file() -> None:
    redacted, count = uploader.redact_text(
        "-----BEGIN PRIVATE KEY-----\nMIIFAKEFAKEFAKE\nstill key material\n"
    )
    assert count == 1
    assert redacted == "[REDACTED:private-key]\n"


def test_redactions_are_counted_per_file(home: Path, clone: Path, capsys) -> None:
    _write(
        home / ".claude/projects/repo/two.jsonl",
        "sk-FAKEFAKEFAKEFAKE0000 and AKIAFAKEFAKEFAKE0000\n",
    )
    _write(home / ".claude/projects/repo/clean.jsonl", "nothing secret here\n")

    result = uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
            "--print",
        ]
    )
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["redactions"] == 2
    by_file = {Path(item["source"]).name: item["redactions"] for item in plan["would_upload"]}
    assert by_file == {"two.jsonl": 2, "clean.jsonl": 0}


# --- skips ---------------------------------------------------------------------


def test_binary_and_oversize_files_are_skipped_not_uploaded(home: Path, clone: Path) -> None:
    binary = home / ".ai-transcripts-spool/core.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"MZ\x00\x00binary payload")
    big = home / ".ai-transcripts-spool/huge.log"
    big.write_text("x", encoding="utf-8")
    os.truncate(big, uploader.MAX_UPLOAD_BYTES + 1)
    _write(home / ".ai-transcripts-spool/fine.log", "hello\n")

    assert _run(home, clone) == 0
    assert [path.name.split("__")[-1] for path in _uploaded(clone)] == ["fine.log"]
    notes = (clone / "transcripts/bob/NOTES.md").read_text(encoding="utf-8")
    assert "uploaded 1 files (0 redactions, 2 skipped)" in notes


# --- layout, notes, state ------------------------------------------------------


def test_layout_is_dev_date_host_cli_basename(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n", age_days=1)
    stamp = (home / ".claude/projects/repo/session-a.jsonl").stat().st_mtime
    day = time.strftime("%Y-%m-%d", time.gmtime(stamp))

    assert _run(home, clone) == 0
    relative = _uploaded(clone)[0].relative_to(clone)
    assert relative.parts[:3] == ("transcripts", "bob", day)
    host, cli, basename = relative.name.split("__", 2)
    assert (cli, basename) == ("claude", "session-a.jsonl")
    assert host and "/" not in host


def test_notes_line_carries_counts_and_host(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "sk-FAKEFAKEFAKEFAKE0000\n")

    assert _run(home, clone) == 0
    line = (clone / "transcripts/bob/NOTES.md").read_text(encoding="utf-8")
    assert line.startswith("- 20")
    assert "uploaded 1 files (1 redactions, 0 skipped) via transcript_uploader" in line


def test_symlinked_dev_dir_in_the_clone_is_refused(
    home: Path, clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hostile committed symlink at transcripts/<dev> must not redirect the
    write outside the clone; the escape target stays empty and the run refuses
    that file rather than following it."""
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clone / "transcripts").mkdir(exist_ok=True)
    (clone / "transcripts" / "bob").symlink_to(outside, target_is_directory=True)

    assert _run(home, clone) == 0
    assert list(outside.iterdir()) == [], "the write escaped through the symlink"
    assert "refusing unsafe target" in capsys.readouterr().err


def test_symlinked_notes_file_is_refused(home: Path, clone: Path, tmp_path: Path) -> None:
    """NOTES.md committed as a symlink must not be appended through."""
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    outside = tmp_path / "outside.md"
    outside.write_text("original\n", encoding="utf-8")
    (clone / "transcripts" / "bob").mkdir(parents=True)
    (clone / "transcripts" / "bob" / "NOTES.md").symlink_to(outside)

    assert _run(home, clone) == 0
    # The transcript itself still lands (its own date-dir is clean); only the
    # symlinked NOTES is refused, leaving the outside file untouched.
    assert outside.read_text(encoding="utf-8") == "original\n"


def test_state_file_records_mtime_and_size(home: Path, clone: Path) -> None:
    source = _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    assert _run(home, clone) == 0
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    entry = state["files"][str(source)]
    assert entry["size"] == source.stat().st_size
    assert entry["mtime"] == pytest.approx(source.stat().st_mtime)


def test_state_forgets_files_that_no_longer_exist(home: Path, clone: Path) -> None:
    source = _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    assert _run(home, clone) == 0
    source.unlink()
    _write(home / ".claude/projects/repo/session-b.jsonl", "{}\n")

    assert _run(home, clone) == 0
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert list(state["files"]) == [str(home / ".claude/projects/repo/session-b.jsonl")]


# --- dry run and git -----------------------------------------------------------


def test_print_mode_writes_nothing_anywhere(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "sk-FAKEFAKEFAKEFAKE0000\n")

    result = uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
            "--print",
        ]
    )
    assert result == 0
    assert not (clone / "transcripts").exists()
    assert not (home / "state.json").exists()


def test_print_mode_needs_no_clone(home: Path, tmp_path: Path, capsys) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "sk-FAKEFAKEFAKEFAKE0000\n")

    result = uploader.main(
        [
            "--employee",
            "emp_alice",
            "--dest",
            str(tmp_path / "nowhere"),
            "--state",
            str(home / "state.json"),
            "--print",
        ]
    )
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dev"] == "alice"
    assert plan["redactions"] == 1
    assert plan["would_upload"][0]["relative_target"].startswith("transcripts/alice/")
    assert "_text" not in plan["would_upload"][0], "the dry run must not print file contents"


def test_no_push_commits_locally_and_leaves_the_remote_untouched(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")

    assert _run(home, clone) == 0
    local = _git(clone, "log", "--oneline", "-1", "--format=%s").stdout.strip()
    assert local.startswith("transcripts: bob ")
    assert local.endswith(" files from " + socket.gethostname().split(".")[0])
    unpushed = _git(clone, "rev-list", "--count", "origin/main..HEAD").stdout.strip()
    assert unpushed == "1"


def test_push_publishes_to_the_file_remote(home: Path, clone: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")

    result = uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
        ]
    )
    assert result == 0
    assert _git(clone, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"


def test_another_devs_upload_is_rebased_onto_before_pushing(
    home: Path, clone: Path, tmp_path: Path
) -> None:
    """The pull must happen while the tree is still clean, or it never happens.

    ``git pull --rebase`` refuses on a dirty working tree, so a pull ordered
    after the copies land silently never runs and every later push is rejected.
    """
    remote = _git(clone, "remote", "get-url", "origin").stdout.strip()
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "--quiet", remote, str(other)], check=True)
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "config", "user.name", "Other Dev")
    (other / "transcripts/alice").mkdir(parents=True)
    (other / "transcripts/alice/NOTES.md").write_text("- another dev was here\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "transcripts: alice")
    _git(other, "push", "--quiet")

    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    result = uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
        ]
    )
    assert result == 0
    assert _git(clone, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"
    assert (clone / "transcripts/alice/NOTES.md").exists(), "the pull never happened"


def test_push_failure_exits_3_without_retrying(home: Path, clone: Path, tmp_path: Path) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    result = uploader.main(
        [
            "--employee",
            "emp_bob",
            "--dest",
            str(clone),
            "--state",
            str(home / "state.json"),
        ]
    )
    assert result == 3
    # The work is banked locally: a failed push must never lose the commit.
    assert _git(clone, "log", "--oneline", "-1", "--format=%s").stdout.startswith("transcripts:")


def test_missing_clone_exits_2_with_the_exact_clone_command(
    home: Path, tmp_path: Path, capsys
) -> None:
    _write(home / ".claude/projects/repo/session-a.jsonl", "{}\n")
    destination = tmp_path / "not-cloned-yet"

    result = uploader.main(
        ["--employee", "emp_owner", "--dest", str(destination), "--state", str(home / "state.json")]
    )
    assert result == 2
    assert f"gh repo clone Globex/ai-transcripts {destination}" in capsys.readouterr().err
    assert not destination.exists(), "the uploader must never clone on its own"


def test_unusable_employee_id_exits_2(home: Path, clone: Path) -> None:
    assert uploader.main(["--employee", "../../etc", "--dest", str(clone), "--no-push"]) == 2
    assert uploader.main(["--employee", "bob", "--dest", str(clone), "--no-push"]) == 2


def test_unparseable_roots_exits_2(home: Path, clone: Path) -> None:
    assert _run(home, clone, "--roots", "[]") == 2
    assert _run(home, clone, "--roots", '[{"cli":"claude"}]') == 2
