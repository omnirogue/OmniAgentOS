"""Adversarial coverage for literal-variable resolution + the scoped-delete proof.

Two changes land together here (2026-07-24, at the operator's direction):

  1. A delete is no longer vetoed on sight. A LEADING ``rm``/``rmdir``/``unlink``
     whose every operand provably resolves strictly beneath the primary project
     classifies INTERNAL_REVERSIBLE (auto). Granted roots widen writes only;
     everything else destructive still hard-stops.
  2. ``$NAME``/``${NAME}`` operands resolve against LITERAL assignments made in
     the same command, and any path still carrying an expansion or a glob is
     UNPROVABLE. (2) is what makes (1) usable, and it also closes a real hole:
     before this, ``$W/x`` was treated as a relative path, joined to the project
     root, and "proved" in-scope no matter what ``W`` actually held.

Every test below is written as an attack or as the exact false positive the operator hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy.shell import classify_shell


@pytest.fixture
def project(tmp_path: Path) -> str:
    root = tmp_path / "workspace"
    (root / "build").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    return str(root)


class TestVariableResolutionClosesTheWriteHole:
    """A `$VAR` path must never be "proved" in-scope by being treated as relative."""

    def test_unbound_var_write_target_hard_stops(self, project: str) -> None:
        assert classify_shell('cp a.txt "$DEST/b.txt"', project) == ActionClass.IRREVERSIBLE

    def test_var_bound_outside_scope_hard_stops(self, project: str, tmp_path: Path) -> None:
        """The hole: W=/etc then a write to "$W/passwd" used to auto-execute."""
        command = f'W={tmp_path / "outside"}\ncp a.txt "$W/passwd"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_var_bound_inside_scope_is_in_scope(self, project: str) -> None:
        assert (
            classify_shell(f'W={project}\ncp a.txt "$W/b.txt"', project)
            == ActionClass.INTERNAL_REVERSIBLE
        )

    def test_redirect_through_unbound_var_hard_stops(self, project: str) -> None:
        assert classify_shell('echo hi > "$OUT/log.txt"', project) == ActionClass.IRREVERSIBLE

    def test_braced_ref_is_resolved_not_blanket_denied(self, project: str) -> None:
        assert (
            classify_shell(f'W={project}\ncp a.txt "${{W}}/b.txt"', project)
            == ActionClass.INTERNAL_REVERSIBLE
        )

    def test_command_substitution_still_hard_stops(self, project: str) -> None:
        assert classify_shell('cp a.txt "$(pwd)/b.txt"', project) == ActionClass.IRREVERSIBLE
        assert classify_shell("cp a.txt `pwd`/b.txt", project) == ActionClass.IRREVERSIBLE

    def test_binding_whose_value_expands_is_untrusted(self, project: str) -> None:
        """A value that itself contains `$` is not a literal — refuse to chain."""
        command = f'BASE={project}\nW=$BASE/sub\ncp a.txt "$W/b.txt"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_rebound_variable_is_dropped(self, project: str, tmp_path: Path) -> None:
        """Two bindings, one in-scope and one not: which reaches the operand?"""
        command = f'W={project}\nW={tmp_path / "outside"}\ncp a.txt "$W/b.txt"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_assignment_not_leading_a_segment_is_not_a_binding(self, project: str) -> None:
        """`echo W=/tmp` assigns nothing; the later `$W` must stay unprovable."""
        command = f'echo W={project}\ncp a.txt "$W/b.txt"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_later_assignment_cannot_prove_an_earlier_operand(self, project: str) -> None:
        command = f'mv "$HOME/source" dest; HOME={project}'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_pipeline_assignment_cannot_prove_a_parent_shell_operand(self, project: str) -> None:
        command = f'HOME={project} | true; mv "$HOME/source" dest'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_failed_or_assignment_cannot_prove_the_fallback_operand(self, project: str) -> None:
        command = f'HOME={project} || mv "$HOME/source" dest'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_assignment_does_not_bind_its_own_redirect(self, project: str) -> None:
        command = f'HOME={project} > "$HOME/note.txt"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_literal_binding_survives_a_later_read_only_segment(self, project: str) -> None:
        command = f'W={project}; true; cp a.txt "$W/b.txt"'
        assert classify_shell(command, project) == ActionClass.INTERNAL_REVERSIBLE


class TestScopedDeleteProof:
    """A delete auto-approves only when EVERY operand is provably in-scope."""

    def test_the_false_positive_owner_hit(self, project: str) -> None:
        """`W=<workspace>` + `rm -f "$W/..."` — 3 of his 5 pending approvals."""
        command = f'W={project}\nrm -f "$W/.cookies_probe" "$W/.chrome-launch.log"'
        assert classify_shell(command, project) == ActionClass.INTERNAL_REVERSIBLE

    def test_plain_in_scope_delete(self, project: str) -> None:
        assert classify_shell(f"rm -rf {project}/build", project) == (
            ActionClass.INTERNAL_REVERSIBLE
        )

    def test_relative_in_scope_delete(self, project: str) -> None:
        assert classify_shell("rm -f ./build/tmp.o", project) == ActionClass.INTERNAL_REVERSIBLE

    def test_relative_escape_hard_stops(self, project: str) -> None:
        assert classify_shell("rm -rf ../../etc", project) == ActionClass.IRREVERSIBLE

    def test_home_delete_hard_stops(self, project: str) -> None:
        assert classify_shell("rm -rf ~/jambot", project) == ActionClass.IRREVERSIBLE

    def test_root_delete_hard_stops(self, project: str) -> None:
        assert classify_shell("rm -rf /", project) == ActionClass.IRREVERSIBLE

    def test_glob_operand_hard_stops(self, project: str) -> None:
        """A glob can match siblings the operand never named."""
        assert classify_shell(f"rm -rf {project}/*", project) == ActionClass.IRREVERSIBLE

    def test_no_operand_hard_stops(self, project: str) -> None:
        assert classify_shell("rm -rf", project) == ActionClass.IRREVERSIBLE

    def test_unrecognised_flag_hard_stops(self, project: str) -> None:
        """An unknown flag may change which paths are touched."""
        assert classify_shell(f"rm --one-file-system -rf {project}/build", project) == (
            ActionClass.IRREVERSIBLE
        )

    def test_mixed_operands_take_the_worst(self, project: str, tmp_path: Path) -> None:
        command = f"rm -rf {project}/build {tmp_path / 'outside'}"
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_second_segment_still_classified(self, project: str, tmp_path: Path) -> None:
        """The delete proof must not swallow another segment's hard stop."""
        command = f"rm -rf {project}/build && cp a.txt {tmp_path / 'outside'}/b.txt"
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_two_in_scope_segments_stay_auto(self, project: str) -> None:
        command = f"rm -rf {project}/build && cp a.txt {project}/b.txt"
        assert classify_shell(command, project) == ActionClass.INTERNAL_REVERSIBLE


class TestStillHardStopping:
    """Everything destructive that is NOT the narrow provable shape."""

    @pytest.mark.parametrize(
        "template",
        [
            "find {p} -name '*.o' -delete",
            "find {p} -name '*.o' -exec rm {{}} ;",
            "git -C {p} rm -r build",
            "rsync -a --delete {p}/a/ {p}/b/",
            "dd if=/dev/zero of={p}/disk",
            "truncate -s 0 {p}/log",
            "shred -u {p}/secret.txt",
            "mkfs.ext4 {p}/dev",
            "echo x | xargs rm -f",
            "bash -c 'rm -rf {p}/build'",
            "python3 -c \"import shutil; shutil.rmtree('{p}')\"",
            "sudo rm -rf {p}/build",
        ],
    )
    def test_out_of_shape_delete_hard_stops(self, project: str, template: str) -> None:
        assert classify_shell(template.format(p=project), project) == ActionClass.IRREVERSIBLE

    def test_buried_rm_hard_stops(self, project: str) -> None:
        """`rm` that is not the leading word is not a shape we prove."""
        assert classify_shell(f"nohup rm -rf {project}/build", project) == (
            ActionClass.IRREVERSIBLE
        )

    def test_secret_delete_hard_stops(self, project: str) -> None:
        """The credential check runs regardless of the delete proof."""
        assert classify_shell(f"rm -f {project}/id_rsa", project) == ActionClass.IRREVERSIBLE

    def test_argv_list_delete_is_proven_on_literals_only(self, project: str) -> None:
        assert classify_shell(["rm", "-rf", f"{project}/build"], project) == (
            ActionClass.INTERNAL_REVERSIBLE
        )
        assert classify_shell(["rm", "-rf", "$W/build"], project) == ActionClass.IRREVERSIBLE
        assert classify_shell(["rm", "-rf", "/etc"], project) == ActionClass.IRREVERSIBLE

    def test_fork_bomb_still_hard_stops(self, project: str) -> None:
        assert classify_shell(":(){ :|:& };:", project) == ActionClass.IRREVERSIBLE


class TestEnvPrefixIsNotStripped:
    """An assignment PREFIX changes what the command does — it is not a binding."""

    def test_env_prefixed_command_still_hard_stops(self, project: str) -> None:
        assert classify_shell("PATH=/tmp/evil ls", project) == ActionClass.IRREVERSIBLE

    def test_env_prefixed_delete_is_not_proven(self, project: str) -> None:
        assert classify_shell(f"PATH=/tmp/evil rm -rf {project}/build", project) == (
            ActionClass.IRREVERSIBLE
        )

    def test_env_prefix_does_not_bind_for_a_later_segment(self, project: str) -> None:
        """`W=<ws> ls` runs a command, so `$W` later must stay unprovable."""
        command = f'W={project} ls\nrm -rf "$W/build"'
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE

    def test_ld_preload_prefix_hard_stops(self, project: str) -> None:
        assert classify_shell("LD_PRELOAD=/tmp/x.so cat a.txt", project) == (
            ActionClass.IRREVERSIBLE
        )
