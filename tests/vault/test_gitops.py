"""Flag-gated git auto-commit (contracts/vault-frontmatter.md "Git behavior
(p05)"): default OFF, OFF in tests except these dedicated tmp-git-repo tests;
bot author; vault/ paths ONLY; hard guard raises on anything else staged;
never push (no `git push` is ever invoked — asserted implicitly: there is no
remote configured in these tmp repos, so a push would fail loudly)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.vault import VaultGitGuardError, render_run_note, write_note
from omniagentos.vault.gitops import BOT_EMAIL, BOT_NAME
from tests.vault.helpers import commit_count, commit_log, git, sample_run


def _repo_root(vault_dir: Path) -> Path:
    return vault_dir.parent


def test_autocommit_off_by_default_writes_no_commit(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    run = sample_run(id="run_off_default")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content)  # autocommit=None, env unset -> OFF

    after = commit_count(repo)
    assert after == before
    # file IS written to disk even though nothing was committed
    assert (git_vault_dir / relpath).is_file()
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert f"vault/{relpath}" in status  # shows up as untracked/dirty, not committed
    assert not any(line.startswith(("A ", "M ")) for line in status.splitlines())  # nothing staged


def test_autocommit_explicit_false_writes_no_commit(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    run = sample_run(id="run_off_explicit")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content, autocommit=False)

    assert commit_count(repo) == before


def test_autocommit_explicit_true_commits_vault_only(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    run = sample_run(id="run_on_explicit", discipline="code-changes")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content, autocommit=True)

    after = commit_count(repo)
    # one commit for the discipline stub (first reference) + one for the run note
    assert after == before + 2

    log = commit_log(repo)
    assert log[0] == "vault: run run_on_explicit"
    assert log[1] == "vault: discipline code-changes"

    # every file touched across the new commits is under vault/
    diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~2", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    touched = [line for line in diff.splitlines() if line.strip()]
    assert touched
    assert all(path.startswith("vault/") for path in touched)

    # working tree clean afterwards (everything we touched got committed)
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_autocommit_env_flag_commits_vault_only(
    git_vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAULT_AUTOCOMMIT", "1")
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    run = sample_run(id="run_on_env", discipline="research-briefs")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content)  # autocommit=None -> reads env

    after = commit_count(repo)
    assert after == before + 2  # discipline stub + run note


def test_autocommit_uses_bot_author(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    run = sample_run(id="run_bot_author", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content, autocommit=True)

    author = subprocess.run(
        ["git", "show", "-s", "--format=%an <%ae>", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert author == f"{BOT_NAME} <{BOT_EMAIL}>"

    committer = subprocess.run(
        ["git", "show", "-s", "--format=%cn <%ce>", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert committer == f"{BOT_NAME} <{BOT_EMAIL}>"


def test_autocommit_message_format(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    run = sample_run(id="run_msg_fmt", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content, autocommit=True)

    subject = subprocess.run(
        ["git", "show", "-s", "--format=%s", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject == "vault: run run_msg_fmt"


def test_autocommit_never_pushes(git_vault_dir: Path) -> None:
    """No remote is configured in this tmp repo; if commit_note ever tried to
    push, this would raise. Its absence is the assertion."""
    repo = _repo_root(git_vault_dir)
    remotes = subprocess.run(
        ["git", "remote"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remotes == ""

    run = sample_run(id="run_no_push", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    # would raise (no remote 'origin') if write_note attempted to push
    write_note(str(git_vault_dir), relpath, content, autocommit=True)


def test_a_non_ascii_in_vault_path_is_not_reported_as_outside_the_vault(
    git_vault_dir: Path,
) -> None:
    """`core.quotePath` defaults to TRUE, so git C-quotes every path byte >= 0x80
    and wraps the whole path in literal double quotes. Joining that spelling onto
    the repo root gives a first component of `"vault`, not `vault`, so a note that
    IS inside the vault was judged outside it and the unrelated run note's commit
    was refused — with a message naming an in-vault path as outside the vault.

    The vault is the human-readable half of the system of record and carries
    human-authored notes (sections.py preserves them); accented and CJK titles are
    ordinary. The guard must read a path spelling it can actually match.
    """
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    (git_vault_dir / "café.md").write_text("human-authored note\n", encoding="utf-8")
    git(repo, "add", "--", "vault/café.md")

    run = sample_run(id="run_nonascii", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])

    write_note(str(git_vault_dir), relpath, content, autocommit=True)

    assert commit_count(repo) == before + 1


def test_a_non_ascii_path_outside_the_vault_is_still_refused(git_vault_dir: Path) -> None:
    """The fail-closed half. Reading an unquoted path spelling must not turn the
    guard into a check that cannot fail: a non-ASCII path OUTSIDE the vault is
    still an offender, and is now named in the message in a spelling an operator
    can actually grep for."""
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    (repo / "sécrets.env").write_text("KEY=1\n", encoding="utf-8")
    git(repo, "add", "--", "sécrets.env")

    run = sample_run(id="run_nonascii_outside", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])

    with pytest.raises(VaultGitGuardError) as excinfo:
        write_note(str(git_vault_dir), relpath, content, autocommit=True)

    assert "sécrets.env" in str(excinfo.value)
    assert commit_count(repo) == before


def test_outside_vault_guard_raises_and_commits_nothing(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    before = commit_count(repo)

    # simulate a dirty index with something staged OUTSIDE vault/ before we run
    (repo / "outside.txt").write_text("not part of the vault\n", encoding="utf-8")
    git(repo, "add", "outside.txt")

    run = sample_run(id="run_guard_test", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])

    with pytest.raises(VaultGitGuardError):
        write_note(str(git_vault_dir), relpath, content, autocommit=True)

    assert commit_count(repo) == before  # refused: no commit happened at all


def test_outside_vault_guard_message_names_the_offending_path(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    (repo / "sneaky.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "sneaky.txt")

    run = sample_run(id="run_guard_msg", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])

    with pytest.raises(VaultGitGuardError, match="sneaky.txt"):
        write_note(str(git_vault_dir), relpath, content, autocommit=True)


def test_regenerating_identical_content_is_a_commit_noop(git_vault_dir: Path) -> None:
    repo = _repo_root(git_vault_dir)
    run = sample_run(id="run_idempotent", discipline=None)
    del run["discipline"]
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(git_vault_dir), relpath, content, autocommit=True)
    after_first = commit_count(repo)

    # write the exact same bytes again — nothing new staged, must not error
    write_note(str(git_vault_dir), relpath, content, autocommit=True)
    after_second = commit_count(repo)
    assert after_second == after_first
