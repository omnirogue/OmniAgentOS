"""Reliability regressions for fail-closed shell classification.

These cases are intentionally behavioral: each command is classified through the
public entry point, with real scope paths, and asserts the approval class that the
session bridge and runner consume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import roots
from omniagentos.policy.shell import classify_shell
from omniagentos.runner.core import Runner
from omniagentos.sessions.policy_map import classify_tool


@pytest.fixture
def scope_dirs(tmp_path: Path) -> tuple[str, str, str]:
    project = tmp_path / "project"
    granted = tmp_path / "granted"
    outside = tmp_path / "outside"
    for directory in (project, granted, outside):
        directory.mkdir()
    return str(project), str(granted), str(outside)


@pytest.mark.parametrize(
    "command",
    [
        "echo $(pwd)",
        'echo "$(wc -c < README.md)"',
        'echo "$(printf %s "$(pwd)")"',
        'echo "$(  pwd  )"',
        'echo "$(\n pwd\n)"',
        'echo "$(wc -c < ~/.config/omni/connections.env)"',
        'echo "$(pwd)" > {project}/result.txt',
        'echo x > "$(pwd)/result.txt"',
        "echo `cat /etc/passwd` > {project}/result.txt",
        "echo `printf '%s %s' multi word`",
        "echo `echo \\`pwd\\``",
        "cat <(printf data)",
        "echo $((1 + 1)) > {project}/result.txt",
        "echo data > >(tee {project}/result.txt)",
        "cat > {project}/result.txt <<EOF\n$(pwd)\nEOF",
        r"echo $'x\''$(printf marker) #'",
        r"echo $'x\''`printf marker` #'",
        r"echo $'x\''$(printf marker) > {project}/result.txt #'",
        "echo $\\\n(printf marker)",
        'echo "$\\\n(printf marker)"',
        "echo $\\\n(printf marker) > {project}/result.txt",
        "cat <\\\n(printf data)",
        "echo data >\\\n(tee {project}/result.txt)",
        "cat <<EOF\n$\\\n(printf marker)\nEOF",
    ],
)
def test_command_substitution_families_require_strongest_approval(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    rendered = command.format(project=project)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


def test_quoted_heredoc_treats_substitution_syntax_as_literal_data(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"cat > {project}/page.html <<'EOF'\nconst label = `$(literal template data)`;\nEOF"
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


def test_tab_stripping_quoted_heredoc_remains_literal_data(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"cat > {project}/page.html <<-'EOF'\n\tit's `$(literal)` data\n\tEOF"
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


def test_redirect_after_heredoc_delimiter_is_still_scope_checked(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    safe = f"cat <<'EOF' > {project}/out\nliteral data\nEOF"
    escaping = f"cat <<'EOF' > {outside}/out\nliteral data\nEOF"
    assert classify_shell(safe, project) is ActionClass.INTERNAL_REVERSIBLE
    assert classify_shell(escaping, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md",
        "rg -n TODO .",
        "git status --short",
        "git branch --list",
        "git branch -vv",
        "git remote -v",
        "git remote get-url origin",
        "git config --get user.name",
        "git config --global --get user.name",
        "printf '%s\\n' '$(literal)'",
        "printf '%s\\n' '`literal`'",
        "printf '%s\\n' '$\\\n(literal)'",
        "printf foo\\\nbar",
    ],
)
def test_safe_read_only_commands_remain_read_only(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell(command, project) is ActionClass.READ_ONLY


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sed -n 1,40p README.md", ActionClass.READ_ONLY),
        ("awk '{{print $5, $NF}}'", ActionClass.READ_ONLY),
        (r"find . -type f -exec ls -l {{}} \;", ActionClass.READ_ONLY),
        (
            "php -d opcache.enable=0 {project}/ok.php",
            ActionClass.INTERNAL_REVERSIBLE,
        ),
        ("php -S 127.0.0.1:8080", ActionClass.INTERNAL_REVERSIBLE),
        ("curl -s https://example.test/", ActionClass.EXTERNAL_REVERSIBLE),
    ],
)
def test_positive_safe_grammars_preserve_automatic_classification(
    command: str,
    expected: ActionClass,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell(command.format(project=project), project) is expected


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' README.md",
        "printf '%s\\n' $'literal'",
        "sed -n 1p -i README.md",
        "sed -n '1w {outside}/written' README.md",
        "awk 'BEGIN {{ system(\"id\") }}'",
        'awk "{{print $NF}}"',
        "find . -exec sh -c 'printf unsafe' \\;",
        "find . -exec {outside}/ls {{}} \\;",
        "find . -execdir ls {{}} \\;",
        "php -d auto_prepend_file={outside}/evil.php {project}/ok.php",
        "php -S 0.0.0.0:8080",
        "curl -s -X POST https://example.test/",
        "curl http://example.test/",
        "curl -s file:///etc/passwd",
        "curl -s -w '%output{{{outside}/written}}' https://example.test/",
    ],
)
def test_side_effecting_or_ambiguous_grammars_fail_closed(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    rendered = command.format(project=project, outside=outside)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        'echo "$(pwd)" > result.txt',
        "git branch -D stale",
        r"echo $'x\''$(printf marker) #'",
        r"echo $'x\''`printf marker` #'",
        r"echo $'x\''$(printf marker) > {project}/result.txt #'",
        "git branch -M old new",
        "git branch -C old new",
        "git switch -C main HEAD~1",
        "git checkout -B main HEAD~1",
        "mv {outside}/source {project}/dest",
        "mv --exchange {outside}/source {project}/dest",
        "mv ../out\\\nside/source {project}/dest",
        "install -s --strip-program={outside}/evil {project}/source {project}/dest",
        "python -i {project}/ok.py",
        "python -X pycache_prefix={outside}/cache {project}/ok.py",
        "bash --login {project}/ok.sh",
        "bun run {outside}/evil.ts",
        "bun i",
        "bun audit",
        "tsx watch {outside}/evil.ts",
        "tsx watch",
        "scala run {outside}/evil.sc",
        "scala config",
        "PYTHONINSPECT=1 python {project}/ok.py",
        "PHPRC={outside} php {project}/ok.php",
        "PHP_INI_SCAN_DIR={outside} php {project}/ok.php",
        "cd missing || rm -rf ../outside/victim",
        "git branch -D stale; cat > {project}/out <<'EOF'\nit's data\nEOF",
        "echo $\\\n(printf marker)",
        "true || cd {project}/subdir && rm -rf ../victim",
        "( cd {project}/subdir ) && mv ../outside/source dest",
        "cd missing || mv ../outside/source dest",
        "mv {project} {granted}/moved",
        'mv "$HOME/source" dest; HOME={project}',
        'HOME={project} | true; mv "$HOME/source" dest',
        'HOME={project} || mv "$HOME/source" dest',
    ],
)
def test_bridge_and_runner_share_the_fail_closed_verdict(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, outside = scope_dirs
    (Path(project) / "subdir").mkdir(exist_ok=True)
    rendered = command.format(project=project, granted=granted, outside=outside)
    assert classify_tool("Bash", {"command": rendered}, project) is ActionClass.IRREVERSIBLE
    assert Runner._command_action_class(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "git config --global user.name attacker",
        'git config --global user.name ""',
        "git config --system core.pager attacker",
        "git remote set-url origin https://example.test/repo.git",
        "git branch -D stale",
        "git branch -Df stale",
        "git branch --delete stale",
        "git branch -f main HEAD~1",
        "git checkout --force main",
        "git checkout -B main HEAD~1",
        "git checkout -Bmain HEAD~1",
        "git switch --force-create main HEAD~1",
        "git switch -C main HEAD~1",
        "git switch -Cmain HEAD~1",
        "git tag --force v1 HEAD~1",
        "git branch -M old new",
        "git branch -C old new",
    ],
)
def test_destructive_git_mutations_are_not_read_only(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "git branch topic",
        "git config user.name local-author",
    ],
)
def test_non_destructive_local_git_mutations_are_internal_reversible(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


def test_read_only_root_does_not_widen_write_proof(
    monkeypatch: pytest.MonkeyPatch,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    monkeypatch.setattr(roots, "standing_read_only_roots", lambda: [outside])
    command = f"cp source.txt {outside}/written.txt"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


def test_unknown_previous_directory_cannot_rebind_write_proof(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell("cd - && cp source.txt dest.txt", project) is ActionClass.IRREVERSIBLE


def test_failed_cd_cannot_launder_relative_delete(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert not (Path(project) / "missing").exists()
    command = "cd missing || rm -rf ../outside/victim"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "true || cd {project}/subdir && rm -rf ../victim",
        "( cd {project}/subdir ) && rm -rf ../outside/source",
        "( true && cd {project}/subdir ) && rm -rf ../outside/source",
        "( cd {project}/subdir ) && mv ../outside/source dest",
        "( true && cd {project}/subdir ) && mv ../outside/source dest",
        "cd missing || mv ../outside/source dest",
    ],
)
def test_conditional_or_subshell_cd_cannot_launder_relative_paths(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    (Path(project) / "subdir").mkdir()
    rendered = command.format(project=project, outside=outside)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "mv {project} {granted}/moved",
        "mv -t {granted} {project}",
        "mv . {granted}/moved",
        "cd {project}/subdir && mv .. {granted}/moved",
    ],
)
def test_mv_cannot_remove_the_primary_project_root(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    (Path(project) / "subdir").mkdir()
    rendered = command.format(project=project, granted=granted)
    assert classify_shell(rendered, project, extra_roots=[granted]) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        'mv "$HOME/source" dest; HOME={project}',
        'rm -rf "$HOME/source"; HOME={project}',
        'HOME={project} | true; mv "$HOME/source" dest',
        'HOME={project} || mv "$HOME/source" dest',
        'HOME={project} > "$HOME/note.txt"',
        'HOME={project}; true; HOME={outside}; mv "$HOME/source" dest',
    ],
)
def test_nonpersistent_or_later_bindings_cannot_prove_earlier_paths(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    rendered = command.format(project=project, outside=outside)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize("control", ["||", ";", "\n", "|", "&"])
def test_non_guaranteeing_cd_controls_cannot_launder_relative_delete(
    control: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"cd missing {control} rm -rf ../outside/victim"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


def test_destructive_git_before_literal_heredoc_is_not_discarded(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"git branch -D stale; cat > {project}/out <<'EOF'\nit's data\nEOF"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


def test_destructive_git_after_literal_heredoc_is_not_discarded(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"cat > {project}/out <<'EOF'\nit's data\nEOF\ngit branch -D stale"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


def test_destructive_git_after_tab_stripping_heredoc_is_not_discarded(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    command = f"cat > {project}/out <<-'EOF'\n\tit's data\n\tEOF\ngit branch -D stale"
    assert classify_shell(command, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "cp --target-directory {outside} source.txt",
        "cp -t {outside} source.txt",
        "mv --target-directory={outside} source.txt",
        "mv -t {outside} source.txt",
        "install --target-directory {outside} source.txt",
        "install -t{outside} source.txt",
        "install -d {outside}/one {project}/two",
        "install -s --strip-program={outside}/evil {project}/source {project}/dest",
        "mv {outside}/source {project}/dest",
        "mv --exchange {outside}/source {project}/dest",
        "mv ../out\\\nside/source {project}/dest",
        "mv {project}/source /dev/null",
        "cp --unknown-option value source.txt {project}/dest.txt",
    ],
)
def test_copy_move_install_destinations_fail_closed(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    rendered = command.format(project=project, outside=outside)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "cp source.txt {project}/dest.txt",
        "cp -t {project}/dest-dir source.txt",
        "mv --target-directory={project}/dest-dir source.txt",
        "install -m 755 source.txt {project}/dest",
        "install -d {project}/one {project}/two",
    ],
)
def test_proven_copy_move_install_destinations_remain_internal(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    rendered = command.format(project=project)
    assert classify_shell(rendered, project) is ActionClass.INTERNAL_REVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "python -W ignore {outside}/evil.py",
        "python -m project_module",
        'node -p "process.exit(0)"',
        "node --require helper {project}/ok.js",
        "bash -O extglob {outside}/evil.sh",
        "python -i {project}/ok.py",
        "python -X pycache_prefix={outside}/cache {project}/ok.py",
        "bash --login {project}/ok.sh",
        "bun run {outside}/evil.ts",
    ],
)
def test_interpreter_program_and_option_operands_fail_closed(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    rendered = command.format(project=project, outside=outside)
    assert classify_shell(rendered, project) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "python -W ignore {project}/ok.py",
        "node --check {project}/ok.js",
        "bash -O extglob {project}/ok.sh",
        "bun {project}/ok.ts",
        "tsx {project}/ok.ts",
        "scala {project}/ok.sc",
    ],
)
def test_proven_interpreter_script_paths_remain_internal(
    command: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    rendered = command.format(project=project)
    assert classify_shell(rendered, project) is ActionClass.INTERNAL_REVERSIBLE


def test_argv_commands_use_the_same_fail_closed_operand_proof(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, outside = scope_dirs
    assert classify_shell(["cp", "-t", outside, "source.txt"], project) is ActionClass.IRREVERSIBLE
    assert (
        classify_shell(["python", "-W", "ignore", f"{outside}/evil.py"], project)
        is ActionClass.IRREVERSIBLE
    )
    assert classify_shell(["git", "branch", "-D", "stale"], project) is ActionClass.IRREVERSIBLE


def test_granted_write_root_does_not_widen_delete_proof(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    command = f"rm -rf {granted}/child"
    assert classify_shell(command, project, extra_roots=[granted]) is ActionClass.IRREVERSIBLE


def test_granted_write_root_does_not_widen_mv_source_deletion(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    assert (
        classify_shell(
            f"mv {granted}/source {project}/dest",
            project,
            extra_roots=[granted],
        )
        is ActionClass.IRREVERSIBLE
    )
    assert (
        classify_shell(
            f"cd {granted} && mv source {project}/dest",
            project,
            extra_roots=[granted],
        )
        is ActionClass.IRREVERSIBLE
    )
    assert (
        classify_shell(
            f"mv {project}/source {granted}/dest",
            project,
            extra_roots=[granted],
        )
        is ActionClass.INTERNAL_REVERSIBLE
    )


def test_cd_into_granted_root_does_not_launder_relative_delete(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    command = f"cd {granted} && rm -rf child"
    assert classify_shell(command, project, extra_roots=[granted]) is ActionClass.IRREVERSIBLE


def test_structural_keyword_cd_cannot_launder_granted_root_delete(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    command = f"for item in one; do cd {granted} && rm -rf child; done"
    assert (
        classify_shell(
            command,
            project,
            extra_roots=[granted],
        )
        is ActionClass.IRREVERSIBLE
    )


def test_wrapped_cd_cannot_launder_granted_root_delete(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, granted, _outside = scope_dirs
    command = f"command cd {granted} && rm -rf child"
    assert (
        classify_shell(
            command,
            project,
            extra_roots=[granted],
        )
        is ActionClass.IRREVERSIBLE
    )


def test_relative_delete_after_in_project_cd_uses_effective_cwd(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    (Path(project) / "subdir").mkdir()
    command = f"cd {project}/subdir && rm -rf child"
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


def test_unconditional_top_level_cd_then_guarded_move_remains_internal(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    (Path(project) / "subdir").mkdir()
    command = f"true; cd {project}/subdir && mv source dest"
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


@pytest.mark.parametrize("verb", ["mv source dest", "rm -rf child"])
def test_guarded_cd_and_operation_in_same_subshell_remain_internal(
    verb: str,
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    (Path(project) / "subdir").mkdir()
    command = f"( cd {project}/subdir && {verb} )"
    assert classify_shell(command, project) is ActionClass.INTERNAL_REVERSIBLE


def test_delete_inside_primary_scope_remains_internal(
    scope_dirs: tuple[str, str, str],
) -> None:
    project, _granted, _outside = scope_dirs
    assert classify_shell(f"rm -rf {project}/build", project) is ActionClass.INTERNAL_REVERSIBLE
