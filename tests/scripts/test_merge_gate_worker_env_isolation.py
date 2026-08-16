"""Regression tests for the worker-env-leak incidents (2026-08-04 and 2026-08-05).

Both are the same class — a "hermetic" gate worker inheriting a premise from the
operator's shell — and both are pinned here against the REAL ``merge-gate.sh``,
each with its own negative control. 2026-08-04 (below) is the SCRUBBED variable,
``OMNIAGENTOS_GATE_WORKSPACE``. 2026-08-05 is the HALF-SET runtime root: the
workers overrode ``OMNIAGENTOS_DB``/``_VAR_DIR``/``_LEDGER_DIR`` but left
``OMNIAGENTOS_VAR``/``_VAULT_DIR`` aimed at the operator's live tree, so
``tests/conftest.py::_isolate_var_and_reflexion`` (which resolves the preset as
``VAR or VAR_DIR``) seeded ``connectors.yaml`` into one root while the registry
was read from the other. A third leak of the same class — ``MERGE_GATE_PINNED``
riding into the fixture gates — is pinned at the bottom of this file.

2026-08-04 incident (FIX 1).

``scripts/launch-env.sh`` auto-exports ``OMNIAGENTOS_GATE_WORKSPACE`` from any
shell whose ``<repo>-gate`` checkout is clean (see its own comment above the
export). ``merge-gate.sh``'s ``counterfeit_worker`` and ``suite_worker``
isolate ``OMNIAGENTOS_DB``/``_VAR_DIR``/``_LEDGER_DIR`` per worker, but used to
INHERIT that variable unscrubbed — so a merge-gate invoked from any launch-env
shell let ``default_gate_workspace()`` (``gate_runner.py``) resolve it inside
the "hermetic" ladder/counterfeit run and really EXECUTE the routine's
declared gate mid-suite. That flipped premise tests in
``tests/scheduler/test_builtin_jobs.py``
(``test_no_input_cycle_is_neutral_not_accepted`` and its siblings) red, and —
because that node sits in a counterfeit's ``must_fail`` set — produced the
chronic in-gate "COUNTERFEIT GATE CONTROL FAILED" refusal plus ~20 minutes of
hidden live-gate execution per ladder run.

This drives the REAL ``merge-gate.sh`` end to end (subprocess boundary and
all), with ``OMNIAGENTOS_GATE_WORKSPACE`` exported in the PARENT environment
exactly as a launch-env shell would, and proves neither worker's child process
ever observes it. A "reverted" run — the pre-fix worker bodies with the
``unset`` removed, reproduced by string substitution against the real file, is
the negative control: it shows the SAME harness DOES record the leaked value
once the fix is gone, so this test fails loudly if the scrub is ever deleted.
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    REAL_PYTHON,
    REPO_ROOT,
    M8Repo,
    _output,
    _run_gate,
)
from tests.scripts.test_merge_gate_m8_refusals import m8_repo as m8_repo  # noqa: F401

# Every branch a real launch-env-shaped worker exercises must dump the WHOLE
# runtime-root env it sees to a marker file OUTSIDE $SCRATCH (the trial-merge
# worktree is removed by merge-gate.sh's EXIT trap before this test could
# otherwise inspect it), and must still behave like the M8 fixture's own stub
# for everything else so the rest of the gate's steps keep passing.
#
# ALL SIX KEYS, not just the scrubbed one. GATE_WORKSPACE proves the 2026-08-04
# scrub; DB/VAR/VAR_DIR/LEDGER_DIR/VAULT_DIR prove the 2026-08-05 one, where the
# workers overrode only PART of the runtime root and left OMNIAGENTOS_VAR /
# _VAULT_DIR aimed at the operator's LIVE tree. Dumping one key could not have
# caught that, and could not catch its return: a future edit dropping the
# OMNIAGENTOS_VAR line would restore the half-isolation defect silently.
# MERGE_GATE_PY added 2026-08-08. It is the FIRST candidate in merge-gate.sh's
# interpreter resolution, so an inherited value overrides the fixture's stub
# python, the nested gate runs the REAL counterfeit corpus, and each generation
# spawns more — 13+ generations live, 166 orphans measured here, and 180.44s vs
# 2.14s on a single node because pytest-timeout cuts in before the work ends.
_MARKER_KEYS = (
    "OMNIAGENTOS_GATE_WORKSPACE",
    "MERGE_GATE_PY",
    "OMNIAGENTOS_DB",
    "OMNIAGENTOS_VAR",
    "OMNIAGENTOS_VAR_DIR",
    "OMNIAGENTOS_LEDGER_DIR",
    "OMNIAGENTOS_VAULT_DIR",
)
#: The runtime-root keys every worker must aim inside its own $SCRATCH subtree.
_RUNTIME_ROOT_KEYS = tuple(
    k for k in _MARKER_KEYS if k not in ("OMNIAGENTOS_GATE_WORKSPACE", "MERGE_GATE_PY")
)

_MARKER_STUB = """#!/bin/sh
dump_env() {{
  [ -n "${{MERGE_GATE_TEST_ENV_MARKER_DIR:-}}" ] || return 0
  mkdir -p "$MERGE_GATE_TEST_ENV_MARKER_DIR"
  {{
    printf 'OMNIAGENTOS_GATE_WORKSPACE=%s\\n' "${{OMNIAGENTOS_GATE_WORKSPACE:-<unset>}}"
    printf 'MERGE_GATE_PY=%s\\n' "${{MERGE_GATE_PY:-<unset>}}"
    printf 'OMNIAGENTOS_DB=%s\\n' "${{OMNIAGENTOS_DB:-<unset>}}"
    printf 'OMNIAGENTOS_VAR=%s\\n' "${{OMNIAGENTOS_VAR:-<unset>}}"
    printf 'OMNIAGENTOS_VAR_DIR=%s\\n' "${{OMNIAGENTOS_VAR_DIR:-<unset>}}"
    printf 'OMNIAGENTOS_LEDGER_DIR=%s\\n' "${{OMNIAGENTOS_LEDGER_DIR:-<unset>}}"
    printf 'OMNIAGENTOS_VAULT_DIR=%s\\n' "${{OMNIAGENTOS_VAULT_DIR:-<unset>}}"
  }} >"$MERGE_GATE_TEST_ENV_MARKER_DIR/$1"
}}
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ]; then
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  dump_env counterfeit-worker.env
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf 'CAUGHT    cf-fixture\\n'
  printf -- '------------------------------------------------------------\\n'
  printf 'total=1  caught=1  survived=0  skipped_platform=0  other=0\\n'
  exit 0
fi
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  dump_env suite-worker.env
  printf '1 passed in 0.01s\\n'
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
exec {real_python} "$@"
"""


def _marker(path: Path) -> dict[str, str]:
    """Parse one worker's dumped environment. Missing file is a hard failure."""
    assert path.is_file(), f"{path.name}: worker never ran — cannot prove isolation"
    seen: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        seen[key] = value
    missing = [k for k in _MARKER_KEYS if k not in seen]
    assert not missing, f"{path.name}: stub dumped no value for {missing}"
    return seen


def _install_marker_stub(repo: Path) -> None:
    """Overwrite the M8 fixture's fake interpreter with the marker-writing one."""
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        _MARKER_STUB.format(
            source_root=shlex.quote(str(REPO_ROOT)),
            real_python=shlex.quote(str(REAL_PYTHON)),
        ),
        encoding="utf-8",
    )
    python.chmod(0o755)


# ---------------------------------------------------------------------------
# HOW THE REVERT-CHECKS BIND TO merge-gate.sh  (rewritten 2026-08-08)
#
# Each negative control below rebuilds a PRE-FIX shape of the real
# ``merge-gate.sh`` and runs it, to prove the protected test beside it exercises
# the fix instead of being a tautology of its own stub. The hard part was never
# the mutation — it is binding to the source WITHOUT coupling to its formatting.
#
# These anchors USED TO BE multi-line verbatim quotes of the worker bodies:
# the ``cd "$SCRATCH" && \`` line, the indentation, the ``PYTHONPATH=`` line
# that happened to sit underneath, and the exact list of keys handed to
# ``env -u``. Every one of those is incidental to what is under test. So when
# bac2719e widened the scrub from the pair (OMNIAGENTOS_GATE_WORKSPACE,
# MERGE_GATE_PY) to the trio (+ MERGE_GATE_PINNED) — a change that STRENGTHENED
# the very property this module pins — the anchors stopped binding and this
# module went red on every candidate that came near it (3fce04aa and 217304c9
# among them). Re-typing the anchors to match the new text fixes the day and
# leaves the mechanism, which breaks again on the next refactor.
#
# So bind to the two things that are NOT incidental:
#   1. the shell FUNCTION each worker is — ``counterfeit_worker`` /
#      ``suite_worker``, names the gate deliberately exposes and refers to in
#      its own comments and in run_suite();
#   2. the SCRUB ITSELF — ``-u <KEY>``, ``<KEY>=`` — the single token the
#      control exists to remove.
# Everything between those two — indentation, ordering, sibling keys added or
# removed, line continuations, neighbouring assignments — is free to change.
# A negative control MUST know what defect it reintroduces, so coupling to the
# scrub token is not avoidable and not the bug; coupling to its surroundings is.
#
# DELIBERATELY NOT an env-var / injection point. Threading something like
# MERGE_GATE_WORKER_SCRUB= through merge-gate.sh so a test could switch the
# scrub off would put an ambient, inheritable switch on a hermeticity guard —
# a self-inflicted instance of the exact defect class this module exists to
# pin. The test bends around the script; the script does not grow a bypass.
#
# And a mutation that cannot be built raises MutationNotApplied, never a quiet
# pass: "I could not set up the experiment" must never be readable as "the
# guard held".
# ---------------------------------------------------------------------------


class MutationNotApplied(AssertionError):
    """A revert-check could not construct the pre-fix shape it meant to run.

    Deliberately distinct from a failed assertion ABOUT the gate's behaviour:
    this one says the experiment never happened. A negative control that cannot
    apply its patch has stopped guarding, and the one thing it must not do is
    stay green while testing nothing.
    """


#: The two ``merge-gate.sh`` functions whose child environments these controls revert.
_WORKER_FUNCTIONS = ("counterfeit_worker", "suite_worker")

#: Keys the workers must hand to ``env -u``: the 2026-08-04 leak
#: (OMNIAGENTOS_GATE_WORKSPACE), the 2026-08-08 one (MERGE_GATE_PY) and the
#: 2026-08-10 egress trio — ``pipeline/bridge/run-loop.sh`` exports
#: ``OMNI_NTFY_URL`` into every loop role, so a gate started from a loop session
#: inherits a live push endpoint and its test children POST to it for real
#: (21 POSTs measured in one run of the ladder's notify-adjacent suites).
#: MERGE_GATE_PINNED is scrubbed there too but is pinned by its own tests below,
#: so this control leaves it in place and reverts only what it is evidence for.
#: It is also what keeps the surgery valid shell: strip every flag and ``env``
#: would be left with no ``-u`` at all.
_SCRUBBED_KEYS = (
    "OMNIAGENTOS_GATE_WORKSPACE",
    "MERGE_GATE_PY",
    "OMNI_NTFY_URL",
    "OPS_ALERT_SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
)

#: Every place ``merge-gate.sh`` builds a child environment with ``env -u``.
#: Two are the workers above; the other two run CANDIDATE code just as directly
#: — the openapi-drift regen imports the candidate's ``api/main.py`` (F1) and
#: the tests-own-tree probe imports its package ``__init__`` chain (F6).
#: Named here so ``test_every_gate_child_scrubs_the_same_key_set`` can prove
#: they agree without hard-coding how many there are.
_SCRUB_SITE_MARKERS = (
    'OPENAPI_OUT=$(env -u ',
    'RESOLVED=$(cd "$SCRATCH" && env -u ',
)

#: The 2026-08-05 half-isolation revert. The workers used to set only PART of
#: the runtime root (DB + _VAR_DIR + _LEDGER_DIR) and leave these two ambient,
#: i.e. aimed at the operator's live var/runtime. Removing exactly these
#: reproduces the SPLIT between OMNIAGENTOS_VAR and OMNIAGENTOS_VAR_DIR that
#: made conftest seed connectors.yaml into a root nothing read back.
_HALF_ISOLATION_KEYS = ("OMNIAGENTOS_VAR", "OMNIAGENTOS_VAULT_DIR")


def _scrub_flag(key: str) -> re.Pattern[str]:
    """The ``env -u KEY`` flag for ONE key, however it is spaced or ordered."""
    return re.compile(rf"-u[ \t]+{re.escape(key)}\b[ \t]*")


def _assignment_line(key: str) -> re.Pattern[str]:
    """A whole ``KEY=...`` line for ONE key, at any indentation.

    ``KEY=`` with the ``=`` required immediately after the name is what keeps
    ``OMNIAGENTOS_VAR`` from also eating ``OMNIAGENTOS_VAR_DIR`` — the line that
    must SURVIVE for the half-isolation split to reproduce at all.
    """
    return re.compile(rf"^[ \t]*{re.escape(key)}=[^\n]*\n", re.MULTILINE)


def _shell_function_body(source: str, name: str) -> tuple[int, int]:
    """Offsets of shell function ``name``'s body within ``source``.

    Delimited by the declaration and the closing brace at ITS OWN indentation —
    never by the text inside it, which is precisely what the callers rewrite.
    """
    opener = re.compile(
        rf"^(?P<indent>[ \t]*){re.escape(name)}\(\)[ \t]*\{{[ \t]*(?:#.*)?$",
        re.MULTILINE,
    )
    declarations = list(opener.finditer(source))
    if len(declarations) != 1:
        raise MutationNotApplied(
            f"EXPERIMENT NOT SET UP: expected exactly one `{name}()` declaration "
            f"in {MERGE_GATE.name}, found {len(declarations)}. The worker was "
            "renamed, split or duplicated — re-point this revert-check at it "
            "rather than deleting the check."
        )
    declared = declarations[0]
    closer = re.compile(rf"^{declared.group('indent')}\}}[ \t]*$", re.MULTILINE)
    closed = closer.search(source, declared.end())
    if closed is None:
        raise MutationNotApplied(
            f"EXPERIMENT NOT SET UP: `{name}()` in {MERGE_GATE.name} has no "
            "closing brace at its own indentation, so its body cannot be "
            "delimited and nothing can be reverted inside it."
        )
    return declared.end(), closed.start()


def _outside_workers(source: str) -> str:
    """``source`` with both worker bodies excised, for a byte-exact scope proof.

    ``merge-gate.sh`` scrubs the SAME keys in its openapi-drift regen (F1) and
    its tests-own-tree probe (F6). Reverting those too would change what the
    run is evidence of, so each control proves everything outside the workers
    came through untouched — an exact comparison, not a count that has to be
    re-derived every time the gate grows another scrub site.
    """
    excised = source
    for name in _WORKER_FUNCTIONS:
        start, end = _shell_function_body(excised, name)
        excised = excised[:start] + excised[end:]
    return excised


def _revert_inside_workers(
    source: str,
    keys: Sequence[str],
    pattern_for: Callable[[str], re.Pattern[str]],
    *,
    what: str,
) -> tuple[str, int]:
    """Delete each key's ``what`` from each worker body. Loud when any is missing.

    EVERY (worker, key) pair must yield at least one deletion, not merely the
    set as a whole. Requiring one hit across the key set is the incomplete-
    propagation shape all over again: with MERGE_GATE_PY's scrub already
    deleted, an "any key matched" rule would still report the mutation applied,
    the run would be evidence only about OMNIAGENTOS_GATE_WORKSPACE, and the
    lost half of the coverage would never be named. Pairwise is the only
    version that cannot half-apply.

    Returns the mutated source and the total number of deletions.
    """
    mutated, removed = source, 0
    for name in _WORKER_FUNCTIONS:
        for key in keys:
            start, end = _shell_function_body(mutated, name)
            body, hits = pattern_for(key).subn("", mutated[start:end])
            if hits < 1:
                raise MutationNotApplied(
                    f"EXPERIMENT NOT SET UP: found no {what} for {key} inside "
                    f"`{name}()` of {MERGE_GATE.name}, so there was nothing to "
                    "revert and this run observed NOTHING about that key. Either "
                    "it moved out of the worker (re-point this check) or it is "
                    "already gone — in which case the PROTECTED test beside this "
                    "one is the failure to read, not this one."
                )
            mutated = mutated[:start] + body + mutated[end:]
            removed += hits
    return mutated, removed


def _gate_with_half_isolated_runtime_roots(tmp_path: Path) -> Path:
    """``merge-gate.sh`` with both workers' OMNIAGENTOS_VAR/_VAULT_DIR removed."""
    source = MERGE_GATE.read_text(encoding="utf-8")
    mutated, removed = _revert_inside_workers(
        source,
        _HALF_ISOLATION_KEYS,
        _assignment_line,
        what="runtime-root assignment",
    )
    assert _outside_workers(mutated) == _outside_workers(source), (
        "the runtime-root revert escaped the worker functions — it must touch "
        "nothing else in merge-gate.sh"
    )
    for name in _WORKER_FUNCTIONS:
        start, end = _shell_function_body(mutated, name)
        body = mutated[start:end]
        for key in _HALF_ISOLATION_KEYS:
            assert not _assignment_line(key).search(body), f"{name} still sets {key}"
        # The defect is the SPLIT, not the absence of both names: VAR_DIR has to
        # survive pointing into $SCRATCH while VAR falls back to the ambient
        # value, or the control reproduces something other than the 2026-08-05 bug.
        assert _assignment_line("OMNIAGENTOS_VAR_DIR").search(body), (
            f"{name} lost OMNIAGENTOS_VAR_DIR too — that is a DIFFERENT shape "
            "from the half-isolation split this control reproduces"
        )
    path = tmp_path / "merge-gate-half-isolated-roots.sh"
    path.write_text(mutated, encoding="utf-8")
    print(
        f"MUTATION APPLIED [runtime-roots]: removed {removed} assignment(s) from "
        f"{len(_WORKER_FUNCTIONS)} worker(s)"
    )
    return path


def _gate_without_worker_env_scrub(tmp_path: Path) -> Path:
    """``merge-gate.sh`` with both workers' ``env -u`` scrub of the leak keys reverted.

    Rebuilds the pre-fix shape from the real file, anchored on the worker
    FUNCTION and the scrub FLAG rather than on the surrounding source text, so
    it tracks merge-gate.sh through reformatting instead of a snapshot of it.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    mutated, removed = _revert_inside_workers(
        source, _SCRUBBED_KEYS, _scrub_flag, what="`env -u` scrub"
    )
    assert _outside_workers(mutated) == _outside_workers(source), (
        "the env-scrub revert escaped the worker functions — the openapi-drift "
        "regen (F1) and the tests-own-tree probe (F6) scrub the same keys and "
        "must come through this mutation untouched"
    )
    for name in _WORKER_FUNCTIONS:
        start, end = _shell_function_body(mutated, name)
        for key in _SCRUBBED_KEYS:
            assert not _scrub_flag(key).search(mutated[start:end]), f"{name} still scrubs {key}"
    path = tmp_path / "merge-gate-leaky-worker-env.sh"
    path.write_text(mutated, encoding="utf-8")
    print(
        f"MUTATION APPLIED [worker-env-scrub]: removed {removed} `env -u` flag(s) "
        f"from {len(_WORKER_FUNCTIONS)} worker(s)"
    )
    return path


# ---------------------------------------------------------------------------
# META: the revert-checks' OWN binding mechanism (added 2026-08-08).
#
# The negative controls in this module are guards. Their binding to
# merge-gate.sh was, until now, the only part of them nothing guarded — which
# is why a strengthening refactor (bac2719e) could switch them off. These run
# in milliseconds on synthetic source and turn a drift into an instant, named
# failure instead of one discovered inside a three-minute gate run.
# ---------------------------------------------------------------------------

_SYNTHETIC_GATE = """#!/usr/bin/env bash
# A site OUTSIDE both workers that scrubs the SAME keys — merge-gate.sh really
# has two of these (openapi-drift regen, tests-own-tree probe) and no revert
# here is allowed to touch them.
openapi_step() {
  OUT=$(env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \\
    -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \\
    OMNIAGENTOS_VAR="$TREE/var/openapi" \\
    OMNIAGENTOS_VAULT_DIR="$TREE/var/openapi/vault" "$PY" check.py)
}

  counterfeit_worker() {
    local outfile="$1"
    (
      cd "$SCRATCH" && \\
      env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \\
        -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \\
        PYTHONPATH="$SCRATCH${PYTHONPATH:+:$PYTHONPATH}" \\
        OMNIAGENTOS_DB="$SCRATCH/var/gate-worker-counterfeit/state.sqlite3" \\
        OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-counterfeit" \\
        OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-counterfeit" \\
        OMNIAGENTOS_LEDGER_DIR="$SCRATCH/var/gate-worker-counterfeit/ledger" \\
        OMNIAGENTOS_VAULT_DIR="$SCRATCH/var/gate-worker-counterfeit/vault" \\
        "$PY" -m tests.counterfeits.harness >"$outfile" 2>&1
    )
  }

  suite_worker() {  # out-file, status-file, pytest args... (safe to background)
    local outfile="$1"
    out=$(cd "$SCRATCH" && \\
      env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \\
      -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL -u SLACK_WEBHOOK_URL \\
      OMNIAGENTOS_DB="$SCRATCH/var/gate-worker-suite/state.sqlite3" \\
      OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-suite" \\
      OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-suite" \\
      OMNIAGENTOS_LEDGER_DIR="$SCRATCH/var/gate-worker-suite/ledger" \\
      OMNIAGENTOS_VAULT_DIR="$SCRATCH/var/gate-worker-suite/vault" \\
      "$PY" -m pytest -q "$@" 2>&1)
  }
"""

#: Reformattings of the workers that must NOT disturb either revert-check.
#: "widened-key-list" is the literal 2026-08-08 drift (bac2719e added a third
#: ``-u``); the rest are the neighbouring shapes an ordinary refactor produces.
_REFACTORS: tuple[tuple[str, str, str], ...] = (
    (
        "widened-key-list",
        "-u MERGE_GATE_PINNED \\",
        "-u MERGE_GATE_PINNED -u MERGE_GATE_LADDER_WORKERS \\",
    ),
    ("narrowed-key-list", " -u MERGE_GATE_PINNED", ""),
    (
        "reordered-keys",
        "env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED",
        "env -u MERGE_GATE_PINNED -u MERGE_GATE_PY -u OMNIAGENTOS_GATE_WORKSPACE",
    ),
    ("widened-spacing", "-u OMNIAGENTOS_GATE_WORKSPACE", "-u\tOMNIAGENTOS_GATE_WORKSPACE"),
    ("reindented-body", "\n        OMNIAGENTOS_DB=", "\n            OMNIAGENTOS_DB="),
    (
        "neighbour-line-added",
        'cd "$SCRATCH" && \\\n',
        'cd "$SCRATCH" && \\\n      OMNIAGENTOS_NEW_PRESET=1 \\\n',
    ),
    (
        "reordered-runtime-roots",
        'OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-suite" \\\n      '
        'OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-suite" \\\n',
        'OMNIAGENTOS_VAR_DIR="$SCRATCH/var/gate-worker-suite" \\\n      '
        'OMNIAGENTOS_VAR="$SCRATCH/var/gate-worker-suite" \\\n',
    ),
)


def _both_reverts(source: str) -> tuple[tuple[str, int], tuple[str, int]]:
    """Run both of this module's reverts against ``source``."""
    return (
        _revert_inside_workers(source, _SCRUBBED_KEYS, _scrub_flag, what="scrub"),
        _revert_inside_workers(source, _HALF_ISOLATION_KEYS, _assignment_line, what="roots"),
    )


@pytest.mark.parametrize("label,before,after", _REFACTORS, ids=[r[0] for r in _REFACTORS])
def test_the_revert_checks_survive_reformatting_of_the_worker_bodies(
    label: str, before: str, after: str
) -> None:
    """The anchors track merge-gate.sh through a refactor instead of breaking on it.

    This is the regression pin for 2026-08-08. The old anchors quoted the worker
    bodies verbatim, so ``widened-key-list`` below — literally what bac2719e did
    — unbound them and took this whole module red on candidates that never
    touched the gate. Each shape here must still yield the SAME reverts.
    """
    refactored = _SYNTHETIC_GATE.replace(before, after)
    assert refactored != _SYNTHETIC_GATE, f"{label}: refactor fixture no longer applies"

    (scrubbed, scrub_hits), (roots, root_hits) = _both_reverts(refactored)

    # Two keys x two workers, and two runtime-root assignments x two workers.
    assert scrub_hits == len(_SCRUBBED_KEYS) * len(_WORKER_FUNCTIONS), label
    assert root_hits == len(_HALF_ISOLATION_KEYS) * len(_WORKER_FUNCTIONS), label
    for name in _WORKER_FUNCTIONS:
        start, end = _shell_function_body(scrubbed, name)
        for key in _SCRUBBED_KEYS:
            assert not _scrub_flag(key).search(scrubbed[start:end]), f"{label}/{name}/{key}"
        start, end = _shell_function_body(roots, name)
        body = roots[start:end]
        for key in _HALF_ISOLATION_KEYS:
            assert not _assignment_line(key).search(body), f"{label}/{name}/{key}"
        assert _assignment_line("OMNIAGENTOS_VAR_DIR").search(body), f"{label}/{name}"
    # The out-of-worker site keeps every key it had — reverting it would change
    # what a control run is evidence of.
    for mutated in (scrubbed, roots):
        assert _outside_workers(mutated) == _outside_workers(refactored), label


@pytest.mark.parametrize(
    "label,damage",
    [
        (
            "scrub-already-gone-everywhere",
            lambda s: s.replace("-u OMNIAGENTOS_GATE_WORKSPACE ", ""),
        ),
        (
            "scrub-gone-from-one-worker-only",
            lambda s: s.replace(
                "env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY -u MERGE_GATE_PINNED \\\n"
                "        -u OMNI_NTFY_URL -u OPS_ALERT_SLACK_WEBHOOK_URL"
                " -u SLACK_WEBHOOK_URL \\\n"
                "        PYTHONPATH=",
                "env -u MERGE_GATE_PINNED \\\n        PYTHONPATH=",
            ),
        ),
        ("worker-renamed", lambda s: s.replace("counterfeit_worker()", "cf_worker()")),
        ("worker-duplicated", lambda s: s + "\n  suite_worker() {\n    :\n  }\n"),
        ("closing-brace-lost", lambda s: s.replace("2>&1\n    )\n  }\n", "2>&1\n    )\n")),
    ],
)
def test_a_revert_that_cannot_apply_is_loud_and_says_so(label: str, damage: object) -> None:
    """FAIL LOUDLY, and say which kind of failure it is.

    The class this whole module pins is "a guard that stopped guarding without
    saying so". These are the five ways the binding can come apart; every one of
    them must raise ``MutationNotApplied`` — never mutate nothing and hand back
    a script whose green run would read as "the leak did not reproduce".
    """
    broken = damage(_SYNTHETIC_GATE)  # type: ignore[operator]
    assert broken != _SYNTHETIC_GATE, f"{label}: damage fixture no longer applies"

    with pytest.raises(MutationNotApplied) as raised:
        _revert_inside_workers(broken, _SCRUBBED_KEYS, _scrub_flag, what="scrub")

    # The message must separate "I could not set up the experiment" from "the
    # guard did not fire" — the two have opposite remedies, and reading one as
    # the other is how the coverage was lost in the first place.
    assert "EXPERIMENT NOT SET UP" in str(raised.value), label


def _scrub_keys_at(source: str, start: int, end: int) -> frozenset[str]:
    """Every ``-u KEY`` handed to the FIRST ``env`` in ``source[start:end]``.

    Bounded to the one command so a second ``env`` further down the region
    cannot contribute keys the first one never scrubbed.
    """
    region = source[start:end]
    opened = region.find("env -u ")
    if opened < 0:
        raise MutationNotApplied(
            "EXPERIMENT NOT SET UP: no `env -u` command in the region — the "
            "scrub site moved, so this parity check observed NOTHING."
        )
    # The command ends at the first newline not preceded by a `\` continuation.
    tail, cursor = region[opened:], 0
    while True:
        nl = tail.find("\n", cursor)
        if nl < 0:
            break
        if not tail[:nl].rstrip(" \t").endswith("\\"):
            tail = tail[:nl]
            break
        cursor = nl + 1
    return frozenset(re.findall(r"-u[ \t]+([A-Za-z_][A-Za-z0-9_]*)", tail))


def test_every_gate_child_scrubs_the_same_key_set() -> None:
    """All FOUR ``env -u`` sites agree, or this says which one was forgotten.

    ``merge-gate.sh`` builds a scrubbed child environment in four places: the
    two workers, the openapi-drift regen (F1) and the tests-own-tree probe (F6).
    Every one of them executes CANDIDATE code, so a key that matters at one
    matters at all four — and "the fix landed at three of four" is this repo's
    named defect shape, not a hypothetical: 518 clone families mean one change
    is structurally several.

    A shared shell array would express this in the script instead of in a test,
    and was rejected on purpose: the negative controls in this module revert the
    scrub INSIDE the worker bodies and prove, byte for byte, that the other two
    sites came through untouched. Hoisting the flags out of the workers would
    leave those controls with nothing to remove — trading a live falsifiable
    guard for a refactor. This assertion buys the same propagation property
    without spending that guard, and it fails LOUDLY and by name.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")

    sites: dict[str, frozenset[str]] = {}
    for name in _WORKER_FUNCTIONS:
        start, end = _shell_function_body(source, name)
        sites[name] = _scrub_keys_at(source, start, end)
    for marker in _SCRUB_SITE_MARKERS:
        at = source.find(marker)
        if at < 0:
            raise MutationNotApplied(
                f"EXPERIMENT NOT SET UP: {marker!r} is no longer in "
                f"{MERGE_GATE.name}. The non-worker scrub site was renamed or "
                "removed — re-point this check at it rather than deleting it, "
                "or the site stops being compared to the others."
            )
        sites[marker.strip()] = _scrub_keys_at(source, at, len(source))

    assert len(sites) == 4, f"expected four scrub sites, found {sorted(sites)}"

    reference = sites[_WORKER_FUNCTIONS[0]]
    for name, keys in sites.items():
        missing, extra = reference - keys, keys - reference
        assert not missing and not extra, (
            f"gate-child scrub sites disagree at {name}: missing {sorted(missing)}, "
            f"unexpected {sorted(extra)}. Every site builds a child environment "
            "for candidate code; a key scrubbed at one and not another is the "
            "incomplete-propagation defect, and the unscrubbed site is the one "
            "that leaks."
        )

    # And the set is not vacuously equal — it must still contain what this
    # module is evidence for, plus the pinned-mode key its own tests cover.
    assert reference >= set(_SCRUBBED_KEYS) | {"MERGE_GATE_PINNED"}, sorted(reference)


def test_both_revert_checks_still_bind_to_todays_merge_gate(tmp_path: Path) -> None:
    """FAST CANARY: both mutations build against the real script, and parse as bash.

    The two negative controls below each cost a full fixture-gate run, so a
    drifted anchor used to surface minutes in, buried in gate output. This says
    it in milliseconds, and additionally proves the surgery leaves valid shell —
    stripping ``-u`` flags must not strand an ``env`` with nothing after it.
    """
    import subprocess

    for build in (_gate_without_worker_env_scrub, _gate_with_half_isolated_runtime_roots):
        mutated = build(tmp_path)
        parsed = subprocess.run(  # noqa: S603
            ["bash", "-n", str(mutated)], capture_output=True, text=True, check=False
        )
        assert parsed.returncode == 0, (
            f"{build.__name__} produced a script bash cannot parse — the revert "
            f"is malformed, so any run of it is evidence of nothing:\n{parsed.stderr}"
        )


def test_worker_processes_never_see_gate_workspace_even_when_parent_exports_it(
    m8_repo: M8Repo, tmp_path: Path
) -> None:
    """PROTECTED: a launch-env-shaped parent env leaks nothing into either worker.

    Exports ``OMNIAGENTOS_GATE_WORKSPACE`` in the env ``merge-gate.sh`` itself
    runs under — exactly what a shell that sourced ``scripts/launch-env.sh``
    does — and proves the interpreter ``counterfeit_worker``/``suite_worker``
    actually exec never receives it.
    """
    _install_marker_stub(m8_repo.path)
    marker_dir = tmp_path / "markers-protected"
    leaked_value = str(tmp_path / "would-be-live-gate-workspace")
    Path(leaked_value).mkdir()

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={
            "OMNIAGENTOS_GATE_WORKSPACE": leaked_value,
            "MERGE_GATE_TEST_ENV_MARKER_DIR": str(marker_dir),
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    print(f"PROTECTED [worker-env] rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)
    assert "MERGE GATE: PASS" in _output(result)

    suite = _marker(marker_dir / "suite-worker.env")
    counterfeit = _marker(marker_dir / "counterfeit-worker.env")
    assert suite["OMNIAGENTOS_GATE_WORKSPACE"] == "<unset>", (
        "suite_worker's pytest subprocess saw OMNIAGENTOS_GATE_WORKSPACE even "
        f"though the parent export was {leaked_value!r}"
    )
    assert counterfeit["OMNIAGENTOS_GATE_WORKSPACE"] == "<unset>", (
        "counterfeit_worker's harness subprocess saw OMNIAGENTOS_GATE_WORKSPACE "
        f"even though the parent export was {leaked_value!r}"
    )
    # THE INTERPRETER IS THE EXPENSIVE ONE (2026-08-08). MERGE_GATE_PY is the
    # FIRST candidate in merge-gate.sh's resolution, ahead of every workspace
    # .venv, and the production gate command exports one. Leaked into pytest, it
    # overrode the fixture stub whose entire job is to answer instantly, so the
    # nested gate ran the REAL 96-entry corpus and each generation spawned more.
    # Measured: 2.14s scrubbed vs 180.44s inherited on ONE node — and 180s is
    # pytest-timeout, not completion. 166 orphans at PPID 1, load 62 on 24 cores.
    for name, seen in (("suite_worker", suite), ("counterfeit_worker", counterfeit)):
        assert seen["MERGE_GATE_PY"] == "<unset>", (
            f"{name}'s subprocess inherited MERGE_GATE_PY={seen['MERGE_GATE_PY']!r}; "
            "a nested gate will resolve the real interpreter, re-enter the real "
            "counterfeit corpus, and reproduce until something reaps it"
        )


def test_reverted_workers_do_leak_gate_workspace_into_the_child_env(
    m8_repo: M8Repo, tmp_path: Path
) -> None:
    """NEGATIVE CONTROL: without the ``unset``, the leak reproduces exactly.

    Proves the protected test above actually exercises the fix, not a
    tautology of its own stub: the same marker-stub harness, against the
    pre-fix worker bodies, DOES observe the parent's exported value in both
    children.
    """
    _install_marker_stub(m8_repo.path)
    marker_dir = tmp_path / "markers-reverted"
    leaked_value = str(tmp_path / "would-be-live-gate-workspace")
    Path(leaked_value).mkdir()
    mutated_gate = _gate_without_worker_env_scrub(tmp_path)

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        gate_script=mutated_gate,
        env_extra={
            "OMNIAGENTOS_GATE_WORKSPACE": leaked_value,
            "MERGE_GATE_TEST_ENV_MARKER_DIR": str(marker_dir),
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    print(f"REVERTED [worker-env] rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)

    suite = _marker(marker_dir / "suite-worker.env")
    counterfeit = _marker(marker_dir / "counterfeit-worker.env")
    assert suite["OMNIAGENTOS_GATE_WORKSPACE"] == leaked_value, (
        "revert-check did not reproduce the leak in suite_worker — the "
        f"mutation anchor may have drifted:\n{_output(result)}"
    )
    assert counterfeit["OMNIAGENTOS_GATE_WORKSPACE"] == leaked_value, (
        "revert-check did not reproduce the leak in counterfeit_worker — the "
        f"mutation anchor may have drifted:\n{_output(result)}"
    )


def test_the_fixture_gate_runs_unpinned_even_when_the_outer_gate_armed_pinned_mode(
    m8_repo: M8Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION (2026-08-05, layer 3): ``MERGE_GATE_PINNED`` must not be inherited.

    ``_run_gate`` builds its child environment from ``os.environ`` and pins
    ``REPO``/``MERGE_GATE_EVIDENCE_ROOT`` at the fixture. It did NOT pin the
    gate's MODE, and the operator arms pinned mode with a command prefix
    (``MERGE_GATE_PINNED=1 bash scripts/merge-gate.sh ...``), which exports it —
    so the value rode through the counterfeit worker, into the corpus's control
    pytest, and into every fixture gate this module starts. In pinned mode
    ``GATE_WS`` derives from the SCRIPT's location, which under the corpus is
    the throwaway trial-merge worktree, so the fixture gate refused
    ``gate-workspace-missing — .../var/swarm/gate-<pid>-gate`` before scoring a
    single step. Ten nodes in this file went red inside the merge gate while
    passing everywhere else, and several sit in the counterfeit corpus's
    must_fail union — so the gate refused candidates for a defect in its own
    instrument.

    These fixtures exercise the DEFAULT, un-armed gate; the modules that want
    pinned mode (``test_merge_gate_openapi_drift``, ``test_gate_venv_resolution``)
    set it explicitly in their own env dicts. Stating the mode is therefore free,
    and inheriting it is never right.
    """
    monkeypatch.setenv("MERGE_GATE_PINNED", "1")

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={"MERGE_GATE_STEP_RECEIPTS": "0"},
    )
    output = _output(result)
    print(f"AMBIENT-PINNED [m8] rc={result.returncode}\n{output}")

    assert "gate-workspace-missing" not in output, (
        "the fixture gate inherited the outer invocation's pinned mode and "
        f"refused before scoring anything:\n{output}"
    )
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output


def test_an_explicit_pinned_request_still_reaches_the_gate(
    m8_repo: M8Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is a DEFAULT, not a ceiling — ``env_extra`` still wins.

    Without this, "stop inheriting the mode" could silently become "pinned mode
    is untestable through this harness", and the two modules that DO drive it
    would be asserting against the un-armed path instead.

    Both inputs the pinned path reads are stated here, not inherited — the mode
    AND ``OMNIAGENTOS_GATE_WORKSPACE``. Asserting one exact refusal slug while
    leaving the workspace pointer ambient would make this test's own answer
    depend on whether the operator's shell had a clean ``<repo>-gate`` (it
    refuses ``gate-workspace-missing`` without one and ``unpinned-workspace``
    with one) — which is the defect class this whole module exists to pin.

    2026-08-10: DELETING the variable is not STATING it. With the variable
    unset, merge-gate.sh falls back to ``${SHARED_ROOT}-gate``, and SHARED_ROOT
    is derived from the SCRIPT's own location — so the refusal this test asserts
    was decided by whether a sibling directory of the checkout running the suite
    happened to exist. It does exist for the pinned gate workspace itself
    (``~/OmniAgentOS-gate``), and any concurrent ``gate-workspace.sh`` can
    create one beside any other checkout. PROVEN red by creating that directory:
    the gate then refuses ``gate-workspace-not-a-checkout`` and this node fails
    while nothing about the pinned path has changed. The pointer is now an
    absent path INSIDE the fixture, which no other process can create.
    """
    monkeypatch.delenv("MERGE_GATE_PINNED", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    absent_workspace = m8_repo.path.parent / f"absent-gate-workspace-{uuid.uuid4().hex}"
    assert not absent_workspace.exists()

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={
            "MERGE_GATE_PINNED": "1",
            "MERGE_GATE_STEP_RECEIPTS": "0",
            "OMNIAGENTOS_GATE_WORKSPACE": str(absent_workspace),
        },
    )
    output = _output(result)

    assert "gate-workspace-missing" in output, (
        "an explicit MERGE_GATE_PINNED=1 no longer arms the gate — the pinned "
        f"path is now unreachable through this harness:\n{output}"
    )


def _assert_roots_isolated(seen: dict[str, str], *, scratch_prefix: str, live_root: str) -> None:
    """Every runtime-root key aims inside this worker's own $SCRATCH subtree."""
    for key in _RUNTIME_ROOT_KEYS:
        value = seen[key]
        assert value != "<unset>", f"{key} was not set at all — the worker has no isolated root"
        assert value.startswith(scratch_prefix), (
            f"{key}={value!r} escapes the gate scratch tree ({scratch_prefix!r}); "
            "a worker that writes outside $SCRATCH is not isolated"
        )
        assert not value.startswith(live_root), f"{key} still points at the operator's live tree"
    assert seen["OMNIAGENTOS_VAR"] == seen["OMNIAGENTOS_VAR_DIR"], (
        "OMNIAGENTOS_VAR and OMNIAGENTOS_VAR_DIR disagree "
        f"({seen['OMNIAGENTOS_VAR']!r} vs {seen['OMNIAGENTOS_VAR_DIR']!r}). "
        "tests/conftest.py::_isolate_var_and_reflexion resolves the preset as "
        "`VAR or VAR_DIR` and seeds ONE of them, so a split root makes it write "
        "connectors.yaml where nothing reads it — the 2026-08-05 defect"
    )


def test_both_workers_get_a_WHOLE_isolated_runtime_root(m8_repo: M8Repo, tmp_path: Path) -> None:
    """PROTECTED: DB, VAR, VAR_DIR, LEDGER_DIR and VAULT_DIR all live in $SCRATCH.

    The 2026-08-04 fix scrubbed one variable; this pins the other half of the
    same class. Exports the operator's LIVE var/vault pointers in the parent —
    exactly what ``scripts/launch-env.sh`` does — and proves neither worker
    inherits any of them.
    """
    _install_marker_stub(m8_repo.path)
    marker_dir = tmp_path / "markers-roots"
    live_root = str(tmp_path / "live-runtime")
    Path(live_root).mkdir()

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={
            "OMNIAGENTOS_VAR": live_root,
            "OMNIAGENTOS_VAR_DIR": live_root,
            "OMNIAGENTOS_VAULT_DIR": f"{live_root}/vault",
            "OMNIAGENTOS_LEDGER_DIR": f"{live_root}/ledger",
            "OMNIAGENTOS_DB": f"{live_root}/state.sqlite3",
            "MERGE_GATE_TEST_ENV_MARKER_DIR": str(marker_dir),
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    print(f"PROTECTED [runtime-roots] rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)

    scratch_prefix = str(m8_repo.path / "var" / "swarm")
    for name in ("suite-worker.env", "counterfeit-worker.env"):
        _assert_roots_isolated(
            _marker(marker_dir / name), scratch_prefix=scratch_prefix, live_root=live_root
        )


def test_half_isolated_workers_do_leak_the_live_var_root(m8_repo: M8Repo, tmp_path: Path) -> None:
    """NEGATIVE CONTROL: drop the OMNIAGENTOS_VAR lines and the leak returns.

    Proves the protected test is not a tautology of its own stub: against the
    pre-fix worker bodies the SAME harness observes the parent's live root in
    OMNIAGENTOS_VAR while OMNIAGENTOS_VAR_DIR still points into $SCRATCH — the
    split that made conftest seed connectors.yaml where nothing reads it.
    """
    _install_marker_stub(m8_repo.path)
    marker_dir = tmp_path / "markers-roots-reverted"
    live_root = str(tmp_path / "live-runtime")
    Path(live_root).mkdir()
    mutated_gate = _gate_with_half_isolated_runtime_roots(tmp_path)

    result = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        gate_script=mutated_gate,
        env_extra={
            "OMNIAGENTOS_VAR": live_root,
            "OMNIAGENTOS_VAULT_DIR": f"{live_root}/vault",
            "MERGE_GATE_TEST_ENV_MARKER_DIR": str(marker_dir),
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    print(f"REVERTED [runtime-roots] rc={result.returncode}\n{_output(result)}")

    for name in ("suite-worker.env", "counterfeit-worker.env"):
        seen = _marker(marker_dir / name)
        assert seen["OMNIAGENTOS_VAR"] == live_root, (
            f"revert-check did not reproduce the leak in {name} — the mutation "
            f"anchor may have drifted:\n{_output(result)}"
        )
        assert seen["OMNIAGENTOS_VAR"] != seen["OMNIAGENTOS_VAR_DIR"], (
            "the pre-fix split between VAR and VAR_DIR did not reproduce"
        )
