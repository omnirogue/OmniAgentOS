"""The hard-stop gate must guard the path that is actually written.

`validate_proposal` read only ``target["file"]`` while `write_document_change` wrote
``target["doc"] or target["file"]``. One policy, two judgment sites, different keys — so a
proposal targeting ``{"doc": "scripts/merge-gate.sh"}`` left ``file`` absent, the hard-stop
branch (``if file_path:``) never ran, validation passed, and the writer appended arbitrary
text to the merge gate the next run executes.

That is favourable absence: a missing key rendering as "touches no restricted resource".

Every test here fails against the pre-fix tree.
"""

from __future__ import annotations

import pytest

from omniagentos.reflection.validate import is_hard_stop, validate_proposal


def _resolver():
    """Imported lazily so the rest of the suite yields real assertion failures,
    not a collection error, when run against the pre-fix tree."""
    from omniagentos.reflection.validate import resolve_target_path

    return resolve_target_path


LIMITS: dict = {}


def _proposal(kind, target):
    """A schema-complete proposal — validate_proposal checks shape before targets."""
    return {
        "id": "p-1",
        "kind": kind,
        "target": target,
        "current": "",
        "proposed": "text",
        "rationale": "r",
    }


def _lesson(target):
    return _proposal("lesson", target)


def _validate(proposal):
    return validate_proposal(proposal, LIMITS)


# --------------------------------------------------------------------------
# The resolver: validate and apply must agree on WHICH path is at stake.
# --------------------------------------------------------------------------

class TestResolveTargetPath:
    def test_doc_key_resolves(self):
        assert _resolver()("lesson", {"doc": "docs/x.md"}) == "docs/x.md"

    def test_file_key_resolves(self):
        assert _resolver()("lesson", {"file": "docs/x.md"}) == "docs/x.md"

    def test_doc_wins_over_file_matching_the_writer(self):
        # write_document_change is `target.get("doc") or target.get("file")`
        assert _resolver()("lesson", {"doc": "a.md", "file": "b.md"}) == "a.md"

    def test_bare_string_target_resolves(self):
        assert _resolver()("lesson", "docs/x.md") == "docs/x.md"

    def test_absent_target_resolves_to_the_writers_fallback(self):
        # No target still WRITES something — the check must see it.
        #
        # CHANGED 2026-08-07, deliberately and not quietly: this assertion used
        # to read `_resolver()("brief_template", {}) == "AGENTS.md"`, i.e. it
        # PINNED the permissive fallback that made the escalation possible. The
        # writer no longer defaults an untargeted kind to AGENTS.md at all
        # (see omniagentos/reflection/kinds.DOCUMENT_FALLBACKS), so mirroring
        # the writer now means resolving to None and refusing. `lesson` keeps
        # its dated fallback, which is a real per-kind route, not a default doc.
        assert _resolver()("brief_template", {}) is None
        assert _resolver()("lesson", {}).startswith("docs/lessons/")


# --------------------------------------------------------------------------
# The defect itself.
# --------------------------------------------------------------------------

class TestDocTargetReachesHardStop:
    def test_doc_target_at_the_merge_gate_is_refused(self):
        ok, err = _validate(_lesson({"doc": "scripts/merge-gate.sh"}))
        assert ok is False, "a doc target at the merge gate must be refused"
        assert "hard-stop" in err.lower()

    def test_doc_target_at_policy_dir_is_refused(self):
        ok, err = _validate(_lesson({"doc": "omniagentos/policy/protected_paths.py"}))
        assert ok is False
        assert "hard-stop" in err.lower()

    def test_doc_target_at_governance_yaml_is_refused(self):
        ok, _ = _validate(_lesson({"doc": "configs/governance.yaml"}))
        assert ok is False

    def test_bare_string_target_is_also_checked(self):
        ok, _ = _validate(_lesson("scripts/merge-gate.sh"))
        assert ok is False

    def test_absent_target_defaulting_to_agents_md_is_refused(self):
        # brief_template with no target writes AGENTS.md — the agents' own instructions.
        ok, _ = _validate(_proposal("brief_template", {}))
        assert ok is False

    def test_an_ordinary_lesson_still_passes(self):
        # Negative control: the fix must not refuse everything.
        ok, err = _validate(_lesson({"doc": "docs/lessons/2026-01-01-note.md"}))
        assert ok is True, f"ordinary lesson should validate, got {err!r}"


# --------------------------------------------------------------------------
# The hard-stop set must cover surfaces that EXECUTE.
# --------------------------------------------------------------------------

class TestExecutableSurfacesAreHardStopped:
    @pytest.mark.parametrize(
        "path",
        [
            "scripts/merge-gate.sh",
            "scripts/gate-workspace.sh",
            "scripts/land-lane.sh",
            "omniagentos/policy/protected_paths.py",
            "omniagentos/gates/engine.py",
            ".github/workflows/ci.yml",
            "AGENTS.md",
            "CLAUDE.md",
            "Makefile",
        ],
    )
    def test_surface_is_hard_stopped(self, path):
        assert is_hard_stop(path) is True, f"{path} must be hard-stopped"

    @pytest.mark.parametrize(
        "path",
        ["docs/lessons/2026-01-01-note.md", "docs/notes/x.md", "README.md"],
    )
    def test_ordinary_docs_are_not_hard_stopped(self, path):
        assert is_hard_stop(path) is False, f"{path} must remain writable"


# --------------------------------------------------------------------------
# Traversal: the writer anchors to the repo, but the check must not be fooled first.
# --------------------------------------------------------------------------

class TestTraversal:
    @pytest.mark.parametrize(
        "path",
        [
            "docs/../scripts/merge-gate.sh",
            "./scripts/merge-gate.sh",
            "scripts//merge-gate.sh",
            "SCRIPTS/MERGE-GATE.SH",
        ],
    )
    def test_normalised_traversal_is_refused(self, path):
        assert is_hard_stop(path) is True, f"{path} must normalise to a hard stop"

    def test_absolute_path_outside_repo_is_refused(self):
        ok, _ = _validate(_lesson({"doc": "/etc/crontab"}))
        assert ok is False
