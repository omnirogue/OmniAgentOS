"""Every private-worktree brief must forbid re-initialising `.git`.

Provenance: coder agents destroyed their own worktrees five times in one day by
treating a failing git command as a broken repository and running `git init` to
"repair" it. In a worktree `.git` is a FILE holding a `gitdir:` pointer; replacing
it with a directory severs the link, every tracked file reads as deleted, and the
branch's commits are stranded where the coordinator cannot fetch them.

The rule is emitted from two independent builders. They were hand-mirrored, which
is the same drift class that let `dal._ACTIVE_STATUSES` and the scheduler's reap
filter disagree about `planning`. These tests pin them to one definition.
"""

from __future__ import annotations

from omniagentos.swarm.contracts import WORKTREE_GITDIR_RULE_LINES
from omniagentos.swarm.scheduler import build_worker_brief
from omniagentos.swarm.spawn import UnifiedSpawner

RULE_TEXT = "\n".join(WORKTREE_GITDIR_RULE_LINES)
WORKTREE_JSON = {"owned_paths": ["src/a.py"], "worktree_branch": "swarm/swr_x/taskA"}
SHARED_DIR_JSON = {"owned_paths": ["src/a.py"]}


def _worker_brief(swarm_json: dict[str, object]) -> str:
    return build_worker_brief({}, {"title": "T", "description": "D"}, swarm_json, {})


class TestWorktreeGitdirRule:
    def test_worker_brief_forbids_reinitialising_the_gitdir(self) -> None:
        brief = _worker_brief(WORKTREE_JSON)
        assert "NEVER run `git init`" in brief
        # The whole rule, not a fragment: a brief that mentions git init while
        # omitting the "report, don't repair" instruction leaves the agent with a
        # prohibition and no alternative, which is what produced the behaviour.
        assert RULE_TEXT in brief

    def test_relay_rules_forbid_reinitialising_the_gitdir(self) -> None:
        rules = UnifiedSpawner._swarm_rules(WORKTREE_JSON)
        assert "NEVER run `git init`" in rules
        assert RULE_TEXT in rules

    def test_both_builders_emit_byte_identical_rule_text(self) -> None:
        """The drift guard.

        Re-wrapping one copy by hand would leave both readable and both wrong in
        the way that matters: they would no longer be the same contract. Deriving
        each from ``WORKTREE_GITDIR_RULE_LINES`` is what this asserts, without
        requiring either builder to expose its internals.
        """
        # Non-empty FIRST. `"" in anything` is True, so without this the whole
        # assertion below passes vacuously the moment the constant is emptied --
        # the empty-set favourable-default class, written into its own guard.
        # Caught by revert-testing this file: 3 of 5 tests failed on an emptied
        # constant and this one did not.
        assert RULE_TEXT.strip(), "rule text is empty — the assertion below cannot bind"
        brief = _worker_brief(WORKTREE_JSON)
        rules = UnifiedSpawner._swarm_rules(WORKTREE_JSON)
        assert RULE_TEXT in brief and RULE_TEXT in rules

    def test_shared_directory_brief_does_not_carry_the_rule(self) -> None:
        """Counterfeit guard: satisfying the assertions above by pasting the rule
        into every brief unconditionally.

        A Phase-1 shared-directory worker is not in a worktree — its `.git` is a
        real directory — so the warning would be false there, and the shared brief
        is separately pinned byte-identical to its pre-worktree form.
        """
        assert "NEVER run `git init`" not in _worker_brief(SHARED_DIR_JSON)

    def test_rule_forbids_routing_around_a_block_from_outside_the_worktree(self) -> None:
        """A sandbox refusal is a boundary, not an obstacle.

        A lane blocked from writing its gitdir escalated to GUI automation and sent
        a keystroke to the operator's Notes app. The blast radius of routing around
        a refusal lands on applications no worker has any business touching, so the
        rule names the specific escape hatches and makes "blocked" a valid outcome.
        """
        brief = _worker_brief(WORKTREE_JSON)
        for forbidden in ("GUI automation", "keystrokes", "osascript"):
            assert forbidden in brief, f"escape hatch not named: {forbidden}"
        assert "report it and stop" in brief

    def test_rule_names_the_consequence_not_just_the_prohibition(self) -> None:
        """A bare "don't do X" is the version agents already ignored.

        The rule has to say what breaks, because the agent reaches for `git init`
        precisely when git is already failing — the moment it is most motivated to
        override a prohibition it does not understand the cost of.
        """
        assert "DETACHES" in RULE_TEXT
        assert "stranded" in RULE_TEXT
        assert "REPORT the error" in RULE_TEXT
