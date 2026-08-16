"""Runnable faithfulness check for the M2 ecosystem-gate counterfeits.

WHY THIS FILE IS COMMITTED
--------------------------
A counterfeit test is only worth what it catches.  "The test passes" says
nothing about whether it would still pass against a *subtly weaker*
implementation — and the whole value of `tests/scheduler/test_gate_ecosystems.py`
is that it would not.  This module makes that claim reproducible instead of a
sentence in a commit message: each entry names a real weakening of the
production code and the test that must go red when it is applied.

The first time this was run by hand, one mutation SURVIVED — a "read the report
from the candidate tree" edit that resolved a path relative to the wrong
directory and therefore did not actually reintroduce the bug.  The test was
fine; the mutation was too weak to prove anything.  That is exactly the failure
mode a committed, reviewable list prevents.

RUNNING IT
----------
    PYTHONPATH=$PWD OMNIAGENTOS_DB=$PWD/var/scratch-m2.sqlite3 \\
        .venv/bin/python -m tests.scheduler.counterfeit_mutations_m2

Every mutation must be reported KILLED.  The module edits files in place and
restores them in a ``finally``; it refuses to start on a dirty worktree so a
crash can never be mistaken for uncommitted work.

``test_gate_ecosystems.py`` carries a cheap guard
(``test_every_counterfeit_mutation_anchor_still_exists``) that asserts every
anchor below is still present in the source, so this list cannot rot silently
into a no-op while still reporting success.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ECO = REPO_ROOT / "omniagentos" / "scheduler" / "gate_ecosystems.py"
RUNNER = REPO_ROOT / "omniagentos" / "scheduler" / "gate_runner.py"
EVIDENCE = REPO_ROOT / "omniagentos" / "scheduler" / "gate_evidence.py"
ROUTINES = REPO_ROOT / "omniagentos" / "scheduler" / "routines.py"
TESTS = "tests/scheduler/test_gate_ecosystems.py"


@dataclass(frozen=True)
class Mutation:
    """One weakening of production code, and the test that must catch it."""

    label: str
    target: Path
    anchor: str
    replacement: str
    test: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        label="counterfeit-a: fall back to a report in the candidate tree",
        target=RUNNER,
        anchor="            outcome = executor.count(",
        # The realistic fail-open: "the tool wrote its report into the project
        # like CI does, so pick it up from there."
        replacement="""            for stray in sorted(run_tree.glob("*.xml")):
                (artifacts / "vitest-junit.xml").write_bytes(stray.read_bytes())
            outcome = executor.count(""",
        test=f"{TESTS}::test_fabricated_junit_xml_in_the_candidate_tree_is_not_counted",
    ),
    Mutation(
        label="counterfeit-a2: follow a symlink planted at the report path",
        target=ECO,
        anchor="        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)",
        replacement="        fd = os.open(path, os.O_RDONLY)",
        test=f"{TESTS}::test_a_symlink_planted_at_the_report_path_is_not_evidence",
    ),
    Mutation(
        label="counterfeit-b: drop the zero-collected refusal",
        target=RUNNER,
        anchor="        if outcome.collected == 0:",
        replacement="        if False:",
        test=f"{TESTS}::test_zero_tests_with_exit_zero_is_refused_not_passed",
    ),
    Mutation(
        label="counterfeit-c: tolerate non-JSON go output",
        target=ECO,
        anchor="""            except json.JSONDecodeError:
                raise GateExecutionInfraError(""",
        replacement="""            except json.JSONDecodeError:
                continue
            if False:
                raise GateExecutionInfraError(""",
        test=f"{TESTS}::test_go_stub_printing_a_success_line_without_json_events_is_refused",
    ),
    Mutation(
        label="argv: drop -count=1 so a cached result can be reprinted",
        target=ECO,
        anchor='return [program, "test", "-json", "-count=1", *packages]',
        replacement='return [program, "test", "-json", *packages]',
        test=f"{TESTS}::test_go_cached_results_are_disabled_by_the_executors_own_argv",
    ),
    Mutation(
        label="argv: add a -run name filter (a silent deselect)",
        target=ECO,
        anchor='return [program, "test", "-json", "-count=1", *packages]',
        replacement='return [program, "test", "-json", "-count=1", "-run", "Test", *packages]',
        test=f"{TESTS}::test_go_cached_results_are_disabled_by_the_executors_own_argv",
    ),
    Mutation(
        label="timeout: treat exit 124 as an ordinary run",
        target=RUNNER,
        anchor="            if exit_code == 124:",
        replacement="            if False:",
        test=f"{TESTS}::test_a_hanging_toolchain_is_terminated_and_reported_inconclusive",
    ),
    Mutation(
        label="trust: unknown ecosystem falls back to python",
        target=ECO,
        anchor="""    if not isinstance(value, str) or value not in SUPPORTED_ECOSYSTEMS:
        raise GateEvidenceRefusal(""",
        replacement="""    if not isinstance(value, str) or value not in SUPPORTED_ECOSYSTEMS:
        return ECOSYSTEM_PYTHON
    if False:
        raise GateEvidenceRefusal(""",
        test=f"{TESTS}::test_unknown_ecosystem_is_refused_and_never_falls_back_to_python",
    ),
    Mutation(
        label="cargo: drop the summary/per-test cross-check",
        target=ECO,
        anchor="            if (block_passed, block_failed, block_skipped) != declared:",
        replacement="            if False:",
        test=f"{TESTS}::test_cargo_summary_inflated_above_its_test_lines_is_refused",
    ),
    Mutation(
        label="npm: drop the junit attribute cross-check",
        target=ECO,
        anchor=(
            "        if (collected, failed, skipped) != "
            "(declared_tests, declared_failures, declared_skipped):"
        ),
        replacement="        if False:",
        test=f"{TESTS}::test_junit_reports_inconsistent_with_their_own_attributes_are_refused",
    ),
    Mutation(
        # The weakening this reaches is not "a tampered report is accepted" — it
        # is "an HONEST report of an errored test becomes inconclusive", turning
        # a real failure into no-evidence. That is why the test it points at is
        # the acceptance one, not the refusal one.
        label="npm: count only failures, ignoring <error> elements",
        target=ECO,
        anchor=(
            "            declared_failures += _required_int("
            'suite, "errors", "vitest JUnit testsuite")'
        ),
        replacement="            declared_failures += 0",
        test=f"{TESTS}::test_an_errored_vitest_case_is_counted_as_failed_not_as_inconsistent",
    ),
    Mutation(
        label="decision point: drop the ecosystem/tool binding",
        target=EVIDENCE,
        anchor="            if evidence.tool != expected_tool:",
        replacement="            if False:",
        test=f"{TESTS}::test_evidence_from_one_ecosystem_is_rejected_against_another_config",
    ),
    # --- the sol review round -----------------------------------------------
    Mutation(
        label="merge gate: believe a receipt from any verifier",
        target=EVIDENCE,
        anchor="    elif evidence.tool != MERGE_GATE_TOOL:",
        replacement="    elif False:",
        test=f"{TESTS}::test_a_merge_receipt_from_another_ecosystem_is_not_believed",
    ),
    Mutation(
        label="merge gate: let a merge_candidate run on another ecosystem",
        target=RUNNER,
        anchor=(
            "        if request.gate_type == MERGE_GATE_TYPE and ecosystem != ECOSYSTEM_PYTHON:"
        ),
        replacement="        if False:",
        test=f"{TESTS}::test_merge_candidate_execution_refuses_a_non_python_ecosystem",
    ),
    Mutation(
        label="merge gate: let a merge_candidate routine be STORED with another ecosystem",
        target=ROUTINES,
        anchor=(
            '        if ecosystem is not None and ecosystem != "python" '
            'and gate_type == "merge_candidate":'
        ),
        replacement="        if False:",
        test=f"{TESTS}::test_merge_candidate_routine_cannot_declare_a_non_python_ecosystem",
    ),
    Mutation(
        label="cargo: stop reading manifests for harness = false",
        target=ECO,
        anchor='                if entry.get("harness") is False:',
        replacement="                if False:",
        test=f"{TESTS}::test_cargo_target_with_harness_false_is_refused",
    ),
    Mutation(
        label="cargo: check only the named manifest, not workspace members",
        target=ECO,
        anchor="""            for child in self._member_manifests(run_tree, resolved, data):
                pending.append((child, False))""",
        replacement="            pass",
        test=f"{TESTS}::test_cargo_harness_false_in_a_workspace_member_is_refused",
    ),
    Mutation(
        # BLOCKER A from the round-2 review: `members` is only half of Cargo's
        # membership rule. Path dependencies inside the workspace directory are
        # IMPLICIT members, so a harness=false crate that appears nowhere in
        # `members` still has its tests run.
        label="cargo: ignore path dependencies (implicit workspace members)",
        target=ECO,
        anchor="""            for child in self._path_dependency_manifests(run_tree, resolved, data):
                pending.append((child, False))""",
        replacement="            pass",
        test=f"{TESTS}::test_cargo_harness_false_in_a_path_dependency_is_refused",
    ),
    Mutation(
        label="cargo: scan only [dependencies], not dev-/build-dependencies",
        target=ECO,
        anchor="""        for key in cls._DEPENDENCY_TABLES:
            scan(data.get(key))""",
        replacement='        scan(data.get("dependencies"))',
        test=f"{TESTS}::test_a_path_dependency_is_followed_from_every_dependency_table",
    ),
    Mutation(
        label="cargo: ignore platform-specific [target.*] dependency tables",
        target=ECO,
        anchor="""        targets = data.get("target")
        if isinstance(targets, dict):""",
        replacement="""        targets = None
        if isinstance(targets, dict):""",
        test=f"{TESTS}::test_a_path_dependency_is_followed_from_a_platform_specific_table",
    ),
    Mutation(
        label="cargo: let an unverifiable path dependency pass silently",
        target=ECO,
        anchor="            resolved = cls._contained(run_tree, candidate)",
        replacement="""            try:
                resolved = cls._contained(run_tree, candidate)
            except GateEvidenceRefusal:
                continue""",
        test=f"{TESTS}::test_a_path_dependency_outside_the_run_tree_is_refused",
    ),
    Mutation(
        # Over-refusing is a defect too: the spec boundaries must be honoured,
        # so these two prove the SKIPS are load-bearing rather than incidental.
        label="cargo: ignore workspace.exclude (over-refuse an excluded crate)",
        target=ECO,
        anchor="            if self._is_excluded(resolved.parent, excluded):",
        replacement="            if False:",
        test=f"{TESTS}::test_an_excluded_directory_is_not_a_member_and_is_not_refused",
    ),
    Mutation(
        # `workspace.exclude` removes a SUBTREE. Exact-parent equality
        # over-refused a crate two levels under an excluded directory, reached
        # via a path dependency — an over-refusal is a defect too.
        label="cargo: exclude only the named directory, not the subtree under it",
        target=ECO,
        anchor="        return any(directory == entry or entry in directory.parents for entry in excluded)",
        replacement="        return any(directory == entry for entry in excluded)",
        test=f"{TESTS}::test_a_crate_beneath_an_excluded_directory_is_not_refused",
    ),
    Mutation(
        label="cargo: ignore [patch.*] path redirects",
        target=ECO,
        anchor="""        patch = data.get("patch")
        if isinstance(patch, dict):
            for source_table in patch.values():
                scan(source_table)""",
        replacement="        patch = None",
        test=f"{TESTS}::test_a_path_dependency_in_patch_is_scanned",
    ),
    Mutation(
        label="cargo: ignore [replace] path redirects",
        target=ECO,
        anchor='        scan(data.get("replace"))',
        replacement="        pass",
        test=f"{TESTS}::test_a_path_dependency_in_replace_is_scanned",
    ),
    Mutation(
        # MAJOR from the round-3 confirmation: skipping a symlinked directory is
        # a hole, not a safeguard. vitest's globbing follows symlinked dirs
        # (fast-glob `followSymbolicLinks: true`), so the config behind one runs
        # while the digest never hashes it.
        label="vitest: skip the symlink check on directories",
        target=ECO,
        anchor="""                if child.is_symlink():""",
        replacement="""                if False:""",
        test=f"{TESTS}::test_a_symlinked_directory_in_the_tree_is_refused_not_skipped",
    ),
    Mutation(
        label="vitest: prune a committed node_modules instead of refusing it",
        target=ECO,
        anchor="                if name == self._VENDOR_DIRNAME:",
        replacement="                if False:",
        test=f"{TESTS}::test_a_committed_node_modules_is_refused",
    ),
    Mutation(
        label="cargo: ignore a dependency's own [workspace] boundary",
        target=ECO,
        anchor="            if not is_root and self._declares_own_workspace(data):",
        replacement="            if False:",
        test=(
            f"{TESTS}::test_a_path_dependency_with_its_own_workspace_table_is_a_separate_workspace"
        ),
    ),
    Mutation(
        label="vitest: accept a gate with no operator config pin",
        target=ECO,
        anchor="        if not isinstance(declared, str) or not _HEX64_RE.match(declared):",
        replacement="        if False:\n            pass\n        elif False:",
        test=f"{TESTS}::test_an_npm_gate_without_a_config_pin_is_refused",
    ),
    Mutation(
        # THREE earlier attempts at this mutation survived, and every time the
        # mutation was at fault, not the test. The filename survives in the
        # material three independent ways: the present entry's name, the absent
        # entries' names, and the POSITION of the non-empty digest in an ordered
        # list. Clearing any one of them leaves the digest fully discriminating
        # — a real (and welcome) fact about the implementation, not a gap.
        #
        # So the mutation is the naive implementation someone would plausibly
        # write instead — "just hash the config files that are there" — which
        # destroys all three at once and genuinely collides `vitest.config.ts`
        # holding C with `vite.config.js` holding C. Those resolve differently,
        # so they must not digest the same.
        label="vitest: hash only the content of the configs that exist",
        target=ECO,
        anchor="""        material: list[dict[str, str]] = []
        for name in self._CONFIG_FILENAMES:""",
        replacement="""        return digest(
            json.dumps(
                sorted(
                    digest((run_tree / name).read_bytes().decode("utf-8", "surrogateescape"))
                    for name in self._CONFIG_FILENAMES
                    if (run_tree / name).is_file()
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        material: list[dict[str, str]] = []
        for name in self._CONFIG_FILENAMES:""",
        test=f"{TESTS}::test_the_config_digest_covers_absence_and_every_resolvable_filename",
    ),
    Mutation(
        label="vitest: ignore config content entirely (presence-only digest)",
        target=ECO,
        # The digest line alone matched TWICE (the _CONFIG_FILENAMES loop and the
        # _nested_config_paths loop below it), so `.replace(..., 1)` bound
        # whichever came first in the file rather than the site this mutation is
        # evidence about — and the old membership-only anchors_present() could
        # not see that. Carrying the preceding `"name": name,` pins the
        # top-level loop, which is the site replace(..., 1) already hit, so the
        # corpus result is unchanged; only the ambiguity is gone.
        # NOTE: the nested-config loop's own digest has no mutation of its own —
        # a presence-only defect landing THERE is still uncovered.
        anchor=(
            '                    "name": name,\n'
            '                    "digest": digest(path.read_bytes()'
            '.decode("utf-8", "surrogateescape")),'
        ),
        replacement='                    "name": name,\n                    "digest": "present",',
        test=f"{TESTS}::test_editing_a_pinned_config_stops_the_gate",
    ),
    Mutation(
        # BLOCKER B(a) from the round-2 review. Encoding a symlink as "absent"
        # let a digest pinned on a genuinely absent config be satisfied by
        # planting a symlink to candidate-controlled content: both trees hashed
        # the same, the pin MATCHED, and vitest followed the link.
        label="vitest: treat a symlinked config as absent instead of refusing",
        target=ECO,
        anchor="""        if path.is_symlink():
            raise GateEvidenceRefusal(""",
        replacement="""        if False:
            raise GateEvidenceRefusal(""",
        test=(f"{TESTS}::test_a_symlink_planted_where_the_pin_says_absent_is_refused_not_matched"),
    ),
    Mutation(
        # BLOCKER B(b): a resolvable filename missing from the pinned list is a
        # file the candidate may add, changing which tests run, for free.
        label="vitest: pin only the popular config filenames",
        target=ECO,
        anchor='_VITEST_WORKSPACE_BASENAMES = ("vitest.workspace", "vitest.projects")',
        replacement='_VITEST_WORKSPACE_BASENAMES = ("vitest.workspace",)',
        test=f"{TESTS}::test_the_pinned_filename_list_covers_the_vitest_v2_resolution_order",
    ),
    Mutation(
        label="vitest: pin only the popular config extensions",
        target=ECO,
        anchor='_VITEST_CONFIG_EXTENSIONS = ("ts", "mts", "cts", "js", "mjs", "cjs")',
        replacement='_VITEST_CONFIG_EXTENSIONS = ("ts", "js")',
        test=f"{TESTS}::test_a_config_added_at_any_resolvable_filename_stops_the_gate",
    ),
    Mutation(
        label="go: accept file targets (a silent partial suite)",
        target=ECO,
        anchor='            if target.endswith(".go"):',
        replacement="            if False:",
        test=f"{TESTS}::test_go_file_targets_are_refused_at_grammar_time",
    ),
    Mutation(
        label="go: stop requiring a package start event",
        target=ECO,
        anchor="            if package not in package_started:",
        replacement="            if False:",
        test=f"{TESTS}::test_bare_run_pass_pairs_without_a_package_lifecycle_are_refused",
    ),
    Mutation(
        label="go: stop requiring a terminal package result",
        target=ECO,
        anchor="            if package not in package_terminal:",
        replacement="            if False:",
        test=f"{TESTS}::test_a_package_that_never_reports_a_terminal_result_is_refused",
    ),
    Mutation(
        label="classification: call a go package failure inconclusive again",
        target=ECO,
        anchor="""        if package_failed and failed == 0:
            # A CANDIDATE fact, not an instrument fact.""",
        replacement="""        if package_failed and failed == 0:
            raise GateExecutionInfraError("outside the counted tests")
        if False:
            # A CANDIDATE fact, not an instrument fact.""",
        test=(
            f"{TESTS}::test_go_package_failure_with_no_failing_test_settles_as_a_candidate_failure"
        ),
    ),
    Mutation(
        label="classification: call a cargo compile failure inconclusive again",
        target=ECO,
        anchor=(
            "            if self._COMPILE_FAILURE_RE.search(stderr) "
            "or self._COMPILE_FAILURE_RE.search(stdout):"
        ),
        replacement="            if False:",
        test=f"{TESTS}::test_cargo_compile_failure_settles_as_a_candidate_failure",
    ),
    Mutation(
        label="process group: stop reaping descendants on the normal exit path",
        target=RUNNER,
        anchor="        _reap_process_group(pgid, grace_seconds=grace_seconds)",
        replacement="        pass",
        test=f"{TESTS}::test_no_descendant_survives_the_leader_on_the_normal_exit_path",
    ),
    Mutation(
        label="PATH: forward '.' and empty components to the child",
        target=ECO,
        anchor="    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))",
        replacement="    return os.pathsep.join(entries)",
        test=f"{TESTS}::test_path_sanitization_drops_cwd_and_relative_entries",
    ),
    Mutation(
        # N3: a pinned workspace/projects file REFERENCES its configuration by
        # glob; the referenced sub-configs used to be unpinned and freely
        # editable, which is the exclusion hole one directory further down.
        label="vitest: pin only root-level configs, not referenced sub-configs",
        target=ECO,
        anchor="        for relative, path in self._nested_config_paths(run_tree):",
        replacement="        for relative, path in []:",
        test=f"{TESTS}::test_editing_a_referenced_sub_config_stops_the_gate",
    ),
    Mutation(
        label="vitest: skip the symlink check on nested configs",
        target=ECO,
        anchor="                self._refuse_symlinked_config(run_tree, path, relative)",
        replacement="                pass",
        test=f"{TESTS}::test_a_symlinked_sub_config_is_refused_too",
    ),
    Mutation(
        # N4: a descendant that calls setsid leaves the child's process group and
        # survives the reap, then rewrites the report it learned from argv.
        label="artifact: accept a report modified after the run finished",
        target=ECO,
        anchor="""            and before.st_mtime_ns > not_modified_after_ns + _ARTIFACT_MTIME_TOLERANCE_NS""",
        replacement="            and False",
        test=f"{TESTS}::test_a_report_modified_after_the_run_finished_is_refused",
    ),
    Mutation(
        # Points at the END-TO-END test: the unit test calls
        # read_artifact_nofollow directly and cannot see whether the runner
        # actually hands it the finish time, so an unwired check would sail past
        # it as dead code.
        label="artifact: stop passing the run's finish time to the counter",
        target=RUNNER,
        anchor="            finished_at_ns = time.time_ns()",
        replacement="            finished_at_ns = None",
        test=f"{TESTS}::test_a_report_stamped_after_the_run_is_refused_end_to_end",
    ),
    Mutation(
        label="artifact: do not notice a report that changed mid-read",
        target=ECO,
        anchor="""        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (""",
        replacement="""        if False and (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (""",
        test=f"{TESTS}::test_a_report_rewritten_mid_read_is_refused",
    ),
    Mutation(
        label="artifact: accept a hard-linked report",
        target=ECO,
        anchor="        if before.st_nlink != 1:",
        replacement="        if False:",
        test=f"{TESTS}::test_a_hard_linked_report_is_refused",
    ),
)


def anchors_present() -> list[str]:
    """Every mutation whose anchor does not bind EXACTLY ONCE in its target.

    Membership was not enough (fixed 2026-08-08). ``run()`` applies each
    mutation with ``.replace(anchor, replacement, 1)``, so an anchor matching
    TWO sites mutates whichever happens to come first in the file and quietly
    stops being evidence about the site it was written for — while an
    ``anchor in source`` check goes on reporting that everything is fine. That
    is the same shape as a negative control whose patch no longer applies: a
    guard that stopped guarding without saying so, which is exactly what this
    function exists to prevent. So count, and say WHICH way it failed —
    "missing" and "ambiguous" have opposite remedies.
    """
    drifted: list[str] = []
    for mutation in MUTATIONS:
        count = mutation.target.read_text(encoding="utf-8").count(mutation.anchor)
        if count == 0:
            drifted.append(
                f"{mutation.label}: anchor MISSING from {mutation.target.name} — "
                "the mutation is a no-op that would still report KILLED"
            )
        elif count > 1:
            drifted.append(
                f"{mutation.label}: anchor AMBIGUOUS in {mutation.target.name} "
                f"({count} matches) — replace(..., 1) mutates an arbitrary one of "
                "them; widen the anchor until it binds the single site this "
                "mutation is evidence about"
            )
    return drifted


def _worktree_is_clean() -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and not proc.stdout.strip()


def run(python: str | None = None) -> int:
    """Apply every mutation in turn; return the number that SURVIVED."""
    if not _worktree_is_clean():
        print(
            "REFUSING: worktree is dirty. This module edits tracked files in place; "
            "a crash mid-run must never be confusable with your own work.",
            file=sys.stderr,
        )
        return -1

    drifted = anchors_present()
    if drifted:
        for detail in drifted:
            print(f"ANCHOR DOES NOT BIND EXACTLY ONCE: {detail}", file=sys.stderr)
        return -1

    interpreter = python or sys.executable
    env = dict(
        os.environ,
        PYTHONPATH=str(REPO_ROOT),
        OMNIAGENTOS_DB=str(REPO_ROOT / "var" / "scratch-counterfeit-m2.sqlite3"),
    )
    survivors = 0
    for mutation in MUTATIONS:
        original = mutation.target.read_text(encoding="utf-8")
        mutation.target.write_text(
            original.replace(mutation.anchor, mutation.replacement, 1), encoding="utf-8"
        )
        try:
            proc = subprocess.run(
                [
                    interpreter,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:randomly",
                    mutation.test,
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            mutation.target.write_text(original, encoding="utf-8")
        if proc.returncode == 0:
            survivors += 1
            print(f"*** SURVIVED — TEST IS NOT DECISIVE ***: {mutation.label}")
            print(proc.stdout[-2000:])
        else:
            print(f"KILLED: {mutation.label}")

    print(f"\n{len(MUTATIONS) - survivors}/{len(MUTATIONS)} mutations killed.")
    return survivors


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(1 if run() != 0 else 0)
