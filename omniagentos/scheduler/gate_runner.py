"""The only sanctioned producer of :class:`GateEvidence`.

Nothing else may mint gate evidence.  A routine's own run output is the thing
being graded, so it can never also be the grader; this executor re-runs the
routine's declared verifier itself, in an isolated workspace, and reports what
actually happened.

Safety properties this module is responsible for:

* **Preflight, not trust.**  The command must pass the same
  :func:`omniagentos.scheduler.routines._is_non_vacuous_gate_command` allowlist
  the write paths enforce, AND every declared target must resolve inside the
  workspace and exist on disk.  A gate naming a target that isn't there cannot
  "pass" — that is the exact fail-open this chain exists to close.
* **Process group containment.**  Pytest is executed as a process-group leader
  with a SIGTERM -> GRACE -> SIGKILL state machine. Descendants holding pipes
  are terminated and reaped.
* **Counted, not inferred.**  Outcomes come from the pre-filter pytest inventory
  (and cross-checked with JUnit XML), so ``checks_collected``/``passed``/``skipped``/``failed``
  and ``deselected_count`` are exact.
* **Opt-in.**  :func:`default_gate_runner` returns ``None`` unless
  ``OMNIAGENTOS_GATE_WORKSPACE`` names an existing directory, so importing or
  ticking the scheduler never executes anything on its own.
* **The right interpreter, derived from the target path.**  A gate whose targets
  live under ``loops/`` executes on the loops venv; everything else executes on
  the production venv.  See :func:`interpreter_class_for_targets` for why this is
  derived from the resolved, containment-checked target paths and from nothing a
  routine row can reach.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from omniagentos.contracts import _repo_root, digest, utc_now_iso
from omniagentos.harnesses.release_gate import normalize_executable
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scheduler.gate_ecosystems import (
    ECOSYSTEM_PYTHON,
    CountedOutcome,
    EcosystemExecutor,
    ecosystem_of_gate_config,
    executor_for,
    resolve_program,
    sanitize_path_env,
)
from omniagentos.scheduler.gate_evidence import (
    MERGE_GATE_TOOL,
    MERGE_GATE_TYPE,
    SCHEMA,
    GateEvidence,
    GateEvidenceError,
    GateEvidenceExists,
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateExecutionInfraError,
    GateWorkspaceUnusable,
    binding_digest,
    normalize_gate_command,
    workspace_digest_for,
)
from omniagentos.scheduler.routines import (
    LOOP_TASK_MODULE,
    _is_non_vacuous_gate_command,
    loop_gate_target_verdict,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 900

#: Identifying a toolchain must not itself be able to hang the gate.
_TOOL_VERSION_TIMEOUT_SECONDS = 20.0

_SUPPORTED_TOOL = "pytest"

#: The one repo subtree whose tests the production venv cannot run. ``loops/``
#: needs LangGraph, which ``loops/requirements.txt`` installs into a SEPARATE
#: venv on purpose (the loop runtime is a different dependency closure from the
#: API/runner, and merging them was rejected). ``loops/bin/loop-tests`` says so
#: in its own header: "The production venv has no LangGraph ... this is the only
#: supported way the loops suite is executed."
LOOPS_SUBTREE = "loops"

#: Interpreter families. A gate belongs to exactly one.
INTERPRETER_CLASS_REPO = "repo"
INTERPRETER_CLASS_LOOPS = "loops"


@dataclass(frozen=True, slots=True)
class GateRunRequest:
    """Everything the executor needs, all of it from trusted configuration."""

    routine_id: str
    run_id: str
    iteration: int
    gate_type: str
    gate_config: dict[str, Any]
    workspace: Path
    candidate_sha: str = ""
    merge_base_sha: str = ""
    #: ``task_template.input.module`` of the routine this gate judges — the ONE
    #: thing that tells the executor a gate belongs to a LOOP, which decides
    #: whether the machinery/blanket policy applies (a routine whose work IS the
    #: scheduler is properly gated by the scheduler's suite). Defaulted empty so
    #: every existing construction stays valid, and deliberately NOT part of
    #: ``binding_digest``: it selects a policy, it does not describe the run.
    task_module: str = ""


@dataclass(frozen=True, slots=True)
class GateEvidenceOutcome:
    """Result of attempting gate evidence production."""

    status: Literal["evidence", "refused", "unavailable", "in_progress"]
    evidence: GateEvidence | None
    detail: str


class TrustedGateRunner(Protocol):
    """Produces durable evidence for one routine run, or raises."""

    def run(self, request: GateRunRequest) -> GateEvidence: ...


def parse_gate_command(command: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(tool, targets)`` for an allowlisted verifier command."""
    normalized = normalize_gate_command(command)
    if not _is_non_vacuous_gate_command(normalized):
        raise GateEvidenceRefusal("gate command is not a recognized objective verifier")
    parts = shlex.split(normalized, posix=True)
    if parts[0] == "pytest":
        return "pytest", tuple(parts[1:])
    if len(parts) >= 3 and parts[1] == "-m" and parts[2] == "pytest":
        return "pytest", tuple(parts[3:])
    raise GateEvidenceRefusal(f"no trusted executor for gate command: {normalized}")


#: One gate target's real location inside a workspace.
#:
#: ``status`` is what the caller's policy keys on, and the three values are
#: deliberately distinguishable: the executor refuses all but ``inside`` (it is
#: about to run the thing), while the write-side validator can only refuse what
#: it can PROVE, and a target absent from the validating checkout is not proof
#: of anything.
TARGET_INSIDE = "inside"
TARGET_ESCAPES = "escapes"
TARGET_MISSING = "missing"


@dataclass(frozen=True, slots=True)
class GateTargetLocation:
    """Where one gate target really is, decided by inode and never by spelling."""

    target: str
    status: str
    parts: tuple[str, ...] | None


def locate_gate_target(workspace: Path, target: str) -> GateTargetLocation:
    """Resolve one gate target against *workspace*: the ONE such definition.

    Two callers with two policies, never two resolutions. The executor
    (:func:`_anchored_target_parts`) refuses anything that is not
    :data:`TARGET_INSIDE`, because it is about to execute it. The routine write
    path (``routines.loop_gate_errors``) asks the same question much earlier, to
    decide whether a loop's declared gate names the loop machinery — and it must
    get the same answer, or validation and execution disagree about what a path
    IS, which is this repo's settled-definition-divergence defect class wearing
    a filesystem hat.

    ``parts`` come from :func:`inode_relative_parts_anchored` on the *resolved*
    path, so they describe where the file actually is, not what the command
    spelled: a target spelled ``loops/x`` that is a symlink into ``tests/``
    reports ``("tests", ...)``, and vice versa. ``()`` means the target IS the
    workspace root.
    """
    path_part = target.split("::", 1)[0]
    pure = PurePosixPath(path_part)
    if pure.is_absolute() or ".." in pure.parts:
        return GateTargetLocation(target=target, status=TARGET_ESCAPES, parts=None)
    resolved = (workspace / path_part).resolve()
    anchored = inode_relative_parts_anchored(resolved, workspace)
    if anchored is None:
        return GateTargetLocation(target=target, status=TARGET_ESCAPES, parts=None)
    if not resolved.exists():
        return GateTargetLocation(target=target, status=TARGET_MISSING, parts=tuple(anchored))
    return GateTargetLocation(target=target, status=TARGET_INSIDE, parts=tuple(anchored))


def _anchored_target_parts(
    workspace: Path, targets: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """Containment-check every target and return its real workspace-relative parts."""
    if not targets:
        raise GateEvidenceRefusal("gate command declares no targets")
    try:
        root = workspace.resolve(strict=True)
    except FileNotFoundError:
        raise GateWorkspaceUnusable(
            f"gate workspace directory does not exist: {workspace}"
        ) from None

    parts: list[tuple[str, ...]] = []
    for target in targets:
        located = locate_gate_target(root, target)
        if located.status == TARGET_ESCAPES:
            raise GateEvidenceRefusal(f"gate target escapes the workspace: {target}")
        if located.status == TARGET_MISSING:
            raise GateEvidenceRefusal(f"gate target does not exist: {target}")
        assert located.parts is not None
        parts.append(located.parts)
    return tuple(parts)


def resolve_targets(workspace: Path, targets: tuple[str, ...]) -> tuple[str, ...]:
    """Verify every target exists inside *workspace*; return them unchanged."""
    _anchored_target_parts(workspace, targets)
    return targets


def refuse_loop_gate_on_the_machinery(
    tree: Path, targets: tuple[str, ...], request: GateRunRequest
) -> None:
    """The BINDING half of the loop-gate target policy: judged where it executes.

    ``routines.loop_gate_errors`` applies the same policy at write time, against
    the checkout the API happens to be running from. That is early feedback and
    it is all it can be: the gate executes later, against a SEPARATELY configured
    workspace pinned at a DIFFERENT commit, and nothing binds those two states.
    The concrete bypass the reviewer built from that gap: commit A's API checkout
    has no ``aliases/loop_gate.py``, so validation classifies it on its spelling
    and admits it; commit B's clean gate workspace carries that same tracked path
    as a symlink to ``tests/scheduler/test_loop_jobs.py``, and the executor —
    which resolves inode-anchored — happily runs the machinery. "Clean and
    committed" never meant "the same commit as validation", and a target that is
    honest today can be turned into that symlink by any later commit.

    So the rule is applied HERE, in the run tree that is actually about to be
    executed, using the SAME verdict function
    (:func:`~omniagentos.scheduler.routines.loop_gate_target_verdict`) and the
    same resolver the interpreter class is derived from. Validation stays as
    feedback; this is the check that binds.

    WHY ``GateEvidenceRefusal`` — the CONDEMNING class, chosen from the taxonomy
    in ``routines_settle``'s module docstring, which maps outcomes as:

        outcome "refused": False (gate actively rejected evidence — a fact about
        the CANDIDATE: an unrecognised verifier, a target that does not exist, a
        manipulated execution)

        outcome "unavailable": None (absence of evidence ... or the WORKSPACE was
        unusable — missing, not a checkout, or dirty)

    A gate whose targets resolve onto the loop machinery is a fact about the
    CANDIDATE — the routine's declared command, in the tree it named — and it is
    the same kind of fact as "an unrecognised verifier". Nothing is absent and
    nothing about the workspace is wrong: the workspace answered the question
    perfectly, and the answer condemns the gate. Settling it NULL
    (``GateWorkspaceUnusable``) would take it out of the acceptance-floor
    denominator and let the alias tick forever, uncounted — which is the
    self-grading loop back again, wearing the mask of an infrastructure problem.
    ``gate_passed=0`` with ``stop_reason='gate_refused'`` is the honest record,
    and the auto-pause floor then does what it exists to do.

    A non-loop routine is untouched: ``task_module`` only names the loop runtime
    for loop rows, and a routine whose work IS the scheduler is properly gated by
    the scheduler's own suite.
    """
    if request.task_module != LOOP_TASK_MODULE:
        return
    for parts, target in zip(_anchored_target_parts(tree, targets), targets, strict=True):
        verdict = loop_gate_target_verdict(parts)
        if verdict is None:
            continue
        where = "/".join(parts) or "."
        raise GateEvidenceRefusal(
            f"loop gate target {target!r} resolves to {where!r} in the run tree, which "
            + (
                "IS the scheduler→worker machinery — identical for every loop instance, "
                "so it passes whatever this instance produced"
                if verdict == "machinery"
                else "CONTAINS the scheduler→worker machinery — a blanket gate whose "
                "verdict is uncorrelated with what this instance produced"
            )
            + ". A loop's gate must be able to go red on its own work; refusing here "
            "because the spelling and the resolved path can differ between the "
            "validating checkout and this pinned workspace."
        )


def interpreter_class_for_targets(workspace: Path, targets: tuple[str, ...]) -> str:
    """Which interpreter family this gate's targets require.

    WHY THE INTERPRETER IS A FUNCTION OF THE TARGET PATH AND OF NOTHING ELSE
    ------------------------------------------------------------------------
    Running every gate on ``sys.executable`` was a live false-adverse generator:
    the loops suite cannot import LangGraph in the production venv, so a loop
    routine whose declared gate named its own tests failed that gate on every
    tick (``ModuleNotFoundError: No module named 'langgraph'`` at
    ``loops/omniagentos_loops/runtime.py``), settled ``outcome_class=adverse``
    against a *favourable* self-report, and auto-paused after three ticks —
    manufacturing exactly the unearned failures this whole chain exists to
    prevent. Refusing ``loops/`` targets instead would have been the same defect
    wearing a different hat: a real, passing, 61-test suite declared unusable as
    evidence forever.

    The selection input is therefore the resolved, containment-checked target
    path — data this module already validates and already trusts enough to
    execute — and never anything a routine row supplies directly. A row's only
    influence is *which files* it names, and every one of them has already been
    proven to exist inside the workspace; it cannot name an interpreter, cannot
    reach the parent environment, and cannot reach this decision by any other
    route. The hermetic child environment is unchanged: :func:`_sanitized_env`
    keeps the same keys and never learns the loops venv exists. Only ``argv[0]``
    moves.

    A gate that mixes the two families is REFUSED rather than resolved to a
    winner. No single interpreter can run both (the loops venv deliberately does
    not install the production dependency closure, and vice versa), so any
    "winner" rule would silently half-run the gate; and a refusal is a fact about
    the candidate command, which is the correct place for it to land.
    """
    classes = {
        INTERPRETER_CLASS_LOOPS if parts[:1] == (LOOPS_SUBTREE,) else INTERPRETER_CLASS_REPO
        for parts in _anchored_target_parts(workspace, targets)
    }
    if len(classes) > 1:
        raise GateEvidenceRefusal(
            f"gate command mixes {LOOPS_SUBTREE}/ targets with production targets; "
            "they run on different interpreters and cannot be one gate"
        )
    return classes.pop()


def default_loops_interpreter() -> Path:
    """The interpreter a ``loops/`` gate executes on.

    Resolved by exactly the rule ``loops/bin/loop-worker``,
    ``loops/bin/loop-tests`` and ``omniagentos_loops.paths.venv_python`` use, and
    for the same reason each of them uses it: the gate must grade a loop
    instance's tests on the RUNTIME THAT INSTANCE ACTUALLY RUNS ON. Two rules
    would mean the worker running on venv X while its evidence came from venv Y —
    a gate that certifies something other than the code under test.

    The two overrides are read from THIS process's environment, which is the
    operator's launch profile (``scripts/launch-env.sh`` / the launchd plist) and
    the identical trust tier as ``OMNIAGENTOS_GATE_WORKSPACE``, which already
    decides *which tree* the gate executes. Deciding which interpreter is
    strictly less powerful than that. A routine row is data read out of SQLite by
    the scheduler; it has no path to the scheduler's own environment.

    Deliberately NOT normalized here: this function is the precedence rule and
    nothing else, so it stays literally comparable to the three shell/Python
    copies of it. :meth:`PytestGateRunner._interpreter_for` normalizes and
    proves the result exists.
    """
    override = os.environ.get("OMNIAGENTOS_LOOPS_VENV")
    if override:
        return Path(override).expanduser() / "bin" / "python"
    root = os.environ.get("OMNIAGENTOS_LOOPS_ROOT")
    base = Path(root).expanduser() if root else Path(_repo_root()) / "var" / "loops"
    return base / "venv" / "bin" / "python"


def _sanitized_env(workspace: Path) -> dict[str, str]:
    """A minimal environment with every pytest selection knob removed."""
    if not workspace.is_absolute():
        raise GateEvidenceRefusal(f"gate workspace must be absolute: {workspace}")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    }
    # An empty PATH component and a literal "." both mean "the current working
    # directory", and the child's cwd IS the candidate tree — so an unsanitized
    # PATH lets a file the candidate commits at ./python or ./git be executed as
    # a trusted tool. Absolute entries only.
    env["PATH"] = sanitize_path_env(env.get("PATH"))
    env["PYTHONHASHSEED"] = "0"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["OMNIAGENTOS_VAR_DIR"] = str(workspace / "var")
    env["PYTHONPATH"] = str(_repo_root())
    return env


def _reap_process_group(pgid: int, *, grace_seconds: float) -> int:
    """TERM -> grace -> KILL every survivor of *pgid*; return how many were found.

    Called once the leader has already been reaped, so an empty group is the
    expected case and costs one ``killpg(pgid, 0)``.  Raises
    :class:`GateExecutionInfraError` if the group is still alive after
    SIGKILL, which means the measurement cannot be trusted to have ended.

    A ``PermissionError`` from ANY of the probes/signals below means "cannot
    tell", never "alive" and never "gone". It surfaces when the pgid has been
    recycled into a process we no longer own, or — seen in the wild inside a
    sandboxed merge-gate run — when the sandbox denies us a signal to our own
    just-spawned descendant. Either way we cannot confirm the group is dead,
    and a descendant we merely *couldn't check on* could still be holding
    write access to this run's artifacts. Silently treating that as "no
    survivors" (the previous behaviour, for the very first probe only) is
    exactly the fail-open this executor exists to close, so it is refused
    the same way an unkillable survivor is: the run settles INCONCLUSIVE
    (``GateExecutionInfraError`` -> "unavailable" at the seam), never a quiet
    pass and never a false claim that something is still running.
    """

    def _probe(sig: int) -> bool:
        """True once *pgid* is confirmed gone; raises if that can't be told."""
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise GateExecutionInfraError(
                f"process group {pgid} could not be signalled ({exc}); its "
                "liveness cannot be confirmed after the leader exited, so the "
                "run cannot be trusted"
            ) from exc
        return False

    if _probe(0):
        return 0

    if _probe(signal.SIGTERM):
        return 0

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if _probe(0):
            return 1
        time.sleep(0.05)

    if _probe(signal.SIGKILL):
        return 1

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if _probe(0):
            return 1
        time.sleep(0.05)

    raise GateExecutionInfraError(
        f"process group {pgid} survived SIGKILL after the leader exited; "
        "descendants could still be writing this run's artifacts"
    )


def _run_process_group(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: float,
    grace_seconds: float = 5.0,
) -> tuple[int, str, str]:
    """Execute argv as process group leader with TERM -> GRACE -> KILL -> VERIFY state machine."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        # THE LEADER EXITING IS NOT THE RUN ENDING.
        #
        # `start_new_session=True` makes the child a session leader, so anything
        # it spawns — a vitest worker pool, a `cargo test` binary that forks, a
        # deliberately backgrounded process — survives its parent and keeps
        # running with write access to everything this run touched, including
        # the report artifact this executor is about to read. That is a live
        # TOCTOU window: the leader exits 0, we read the report, and a
        # descendant rewrites it in between (or rewrites it and THEN we read).
        #
        # So the group is torn down and confirmed dead on the ordinary exit path
        # too, not just on timeout. Surviving descendants are killed rather than
        # treated as a verdict: test suites that leak a helper process are
        # common and that is not evidence about the candidate's tests — but they
        # do not get to outlive the measurement.
        _reap_process_group(pgid, grace_seconds=grace_seconds)
        return int(proc.returncode), stdout, stderr
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
        time.sleep(0.1)

    leader_alive = proc.poll() is None
    group_alive = True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        group_alive = False

    if leader_alive or group_alive:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    proc.wait()
    try:
        stdout, stderr = proc.communicate(timeout=1.0)
    except Exception:
        stdout, stderr = "", ""

    try:
        os.killpg(pgid, 0)
        raise GateExecutionInfraError(f"process group {pgid} survived SIGKILL")
    except ProcessLookupError:
        pass

    return 124, stdout, stderr


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    """Exact ``(collected, passed, skipped, failed)`` from pytest JUnit XML."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, 0, 0, 0
    collected = passed = skipped = failed = 0
    for testcase in root.iter("testcase"):
        collected += 1
        if testcase.find("skipped") is not None:
            skipped += 1
        elif testcase.find("failure") is not None or testcase.find("error") is not None:
            failed += 1
        else:
            passed += 1
    return collected, passed, skipped, failed


def _check_inventory_credibility(inventory: dict[str, Any], workspace: Path) -> None:
    """Validate that inventory artifact was produced cleanly and unmanipulated."""
    selection = inventory.get("selection")
    if not isinstance(selection, dict):
        raise GateEvidenceRefusal("inventory has no selection block")
    if selection.get("keyword") or selection.get("markexpr"):
        raise GateEvidenceRefusal("inventory shows keyword or markexpr selector used")
    if selection.get("deselect") or selection.get("ignore") or selection.get("ignore_glob"):
        raise GateEvidenceRefusal("inventory shows deselect or ignore selector used")
    if selection.get("maxfail") or selection.get("last_failed") or selection.get("failed_first"):
        raise GateEvidenceRefusal("inventory shows partial or cached selector used")

    environment = inventory.get("environment")
    if not isinstance(environment, dict):
        raise GateEvidenceRefusal("inventory has no environment block")
    if environment.get("plugin_autoload_disabled") is not True:
        raise GateEvidenceRefusal("inventory allowed plugin autoloading")
    if environment.get("pytest_addopts") != "":
        raise GateEvidenceRefusal("inventory inherited PYTEST_ADDOPTS")
    if environment.get("pytest_plugins") != "":
        raise GateEvidenceRefusal("inventory inherited PYTEST_PLUGINS")
    if environment.get("plugin_dists") != []:
        raise GateEvidenceRefusal("inventory loaded external plugin distributions")

    producer = inventory.get("producer")
    if not isinstance(producer, dict):
        raise GateEvidenceRefusal("inventory has no producer block")
    plugin_file = producer.get("plugin_file")
    if not isinstance(plugin_file, str) or not plugin_file:
        raise GateEvidenceRefusal("inventory does not name its producing plugin")
    try:
        plugin_path = Path(plugin_file).resolve()
        repo = _repo_root()
        if not (
            inode_relative_parts_anchored(plugin_path, repo) is not None
            or inode_relative_parts_anchored(plugin_path, workspace) is not None
        ):
            raise GateEvidenceRefusal("inventory plugin is outside certified tree and workspace")
    except Exception as exc:
        raise GateEvidenceRefusal(f"inventory plugin path check failed: {exc}") from exc

    invocation_dir = producer.get("invocation_dir")
    if (
        not isinstance(invocation_dir, str)
        or inode_relative_parts_anchored(Path(invocation_dir).resolve(), workspace) != ()
    ):
        raise GateEvidenceRefusal(f"inventory invoked outside workspace: {invocation_dir}")


def _resolve_pinned_workspace(request: GateRunRequest) -> Path:
    """The configured workspace, resolved, or an UNAVAILABLE refusal."""
    try:
        return request.workspace.expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise GateWorkspaceUnusable(
            f"gate workspace directory does not exist: {request.workspace}"
        ) from None


def _pin_and_verify_git(workspace: Path, request: GateRunRequest) -> tuple[str, str, str]:
    """Return ``(workspace_sha, candidate_sha, merge_base_sha)`` for a clean pin.

    Shared by every ecosystem executor deliberately.  These five checks — the
    workspace is a git repo, the candidate SHA is the workspace tip, the
    merge-base is an ancestor of the candidate, and the tree is clean at the
    moment of execution — are the entire reason evidence can be bound to a
    commit at all.  A second copy of them for non-Python ecosystems would be a
    second place for that binding to rot, so there is exactly one.
    """
    git_sha_proc = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    if git_sha_proc.returncode != 0 or not re.match(
        r"\A[0-9a-f]{40}\Z", git_sha_proc.stdout.strip()
    ):
        raise GateWorkspaceUnusable(f"workspace {workspace} is not a valid git repository")
    workspace_sha = git_sha_proc.stdout.strip()
    candidate_sha = request.candidate_sha or workspace_sha
    if not re.match(r"\A[0-9a-f]{40}\Z", candidate_sha):
        raise GateEvidenceRefusal("candidate SHA is invalid")
    if candidate_sha != workspace_sha:
        raise GateEvidenceRefusal(
            f"candidate SHA {candidate_sha} is not workspace tip {workspace_sha}"
        )
    merge_base_sha = request.merge_base_sha
    if merge_base_sha:
        if not re.match(r"\A[0-9a-f]{40}\Z", merge_base_sha):
            raise GateEvidenceRefusal("merge-base SHA is invalid")
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "merge-base",
                "--is-ancestor",
                merge_base_sha,
                candidate_sha,
            ],
            capture_output=True,
            text=True,
        )
        if ancestor.returncode != 0:
            raise GateEvidenceRefusal(
                f"merge-base SHA {merge_base_sha} is not an ancestor of candidate {candidate_sha}"
            )

    git_status_proc = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    if git_status_proc.returncode != 0 or git_status_proc.stdout.strip():
        # The TOCTOU seat: configuration proved this workspace clean at job
        # spawn, and it can go dirty at any moment afterwards (a concurrent
        # merge, a checkout, an editor). That is a fact about the workspace,
        # never about the run being graded, so it must not settle as a gate
        # failure. Note this fires on a FAILED `git status` too — an empty
        # stdout from a broken git is not a clean tree.
        raise GateWorkspaceUnusable("workspace has uncommitted changes or untracked files")
    return workspace_sha, candidate_sha, merge_base_sha


def _post_run_tree_clean(run_tree: Path, expected_sha: str) -> bool:
    """Whether the verifier left the tree it ran in exactly as it found it.

    Evaluated against the tree the verifier actually ran in, and BEFORE that
    tree is destroyed. A verifier that wrote into its own tree misbehaved, and
    that stays CONDEMNING: ``workspace_tree_clean=False`` reaches
    ``evidence_rejections``, which settles the run ``gate_passed=0``. It is a
    fact about the run, not about the environment, so it is deliberately not a
    ``GateWorkspaceUnusable``. What ephemeral trees change is only that the
    damage cannot reach the NEXT run.
    """
    post_sha = subprocess.run(
        ["git", "-C", str(run_tree), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    post_status = subprocess.run(
        ["git", "-C", str(run_tree), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    return (
        post_sha.returncode == 0
        and post_sha.stdout.strip() == expected_sha
        and post_status.returncode == 0
        and post_status.stdout.strip() == ""
    )


@contextlib.contextmanager
def _ephemeral_run_tree(source: Path, sha: str) -> Iterator[Path]:
    """Materialize a throwaway checkout of *sha*, and destroy it afterwards.

    WHY THE GATE NEVER EXECUTES IN A PERSISTENT CHECKOUT
    ----------------------------------------------------
    A verifier is planner-authored code that this module runs. If it executes in
    a checkout that survives between runs, one side-effecting verifier poisons
    every later run:

      1. the verifier writes into the tree it runs in;
      2. its own run is correctly condemned (``workspace_tree_clean=False``);
      3. every SUBSEQUENT run then trips the pre-execution dirty check, which is
         a ``GateWorkspaceUnusable`` — settled NULL, excluded from the floor;
      4. auto-pause needs settled FAILURES, so a routine that misbehaved exactly
         once buys permanent immunity from ever being paused, silently.

    Isolating the execution surface is what removes step 3. The configured
    workspace becomes only the SOURCE OF THE PIN — it is where the SHA and the
    cleanliness of the pin come from — and every run gets its own tree at that
    SHA. Nothing a verifier writes can outlive its own run, so a poisoned run
    condemns itself and the next run is judged on its own merits.

    The evidence is deliberately unchanged: ``workspace_digest`` and
    ``workspace_sha`` still name the CONFIGURED workspace and the commit it was
    pinned to, because that is the thing a decision point verifies against. Only
    the working directory moved.

    Cleanup is layered, because an orphaned worktree is a real cost in a repo
    that already carries ~30 named ones: ``git worktree remove --force`` in a
    ``finally``, wrapped in a ``TemporaryDirectory`` that deletes the files even
    if that git call fails. A crash therefore leaves at worst a *prunable*
    registration and no directory. This deliberately does NOT run ``git worktree
    prune``: the pin source shares its object store with every other worktree on
    the box, and pruning would reap peers whose directories are merely
    temporarily absent.
    """
    # TMPDIR, never the repo's parent directory — a tree that appears next to the
    # named worktrees is one somebody eventually mistakes for a real lane.
    with tempfile.TemporaryDirectory(prefix="gate-tree-") as parent:
        tree = Path(parent) / "workspace"
        add = subprocess.run(
            ["git", "-C", str(source), "worktree", "add", "--detach", str(tree), sha],
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            # The pin source could not produce a tree: an environment fact about
            # the workspace, exactly like the dirty-tree case, never a verdict on
            # the run. This is now the ONLY way a routine reaches perpetual NULL.
            detail = (add.stderr or add.stdout or "").strip()[-500:]
            raise GateWorkspaceUnusable(
                f"could not materialize a run tree from {source} at {sha}: {detail}"
            )
        try:
            yield tree
        finally:
            subprocess.run(
                ["git", "-C", str(source), "worktree", "remove", "--force", str(tree)],
                capture_output=True,
                text=True,
            )


def build_gate_evidence(
    *,
    request: GateRunRequest,
    workspace: Path,
    workspace_sha: str,
    candidate_sha: str,
    merge_base_sha: str,
    command: str,
    targets: tuple[str, ...],
    tool: str,
    tool_version: str,
    interpreter: str,
    interpreter_version: str,
    exit_code: int,
    checks_collected: int,
    checks_passed: int,
    checks_skipped: int,
    checks_failed: int,
    deselected_count: int,
    node_inventory_digest: str,
    workspace_tree_clean: bool,
    started_at: str,
) -> GateEvidence:
    """Assemble one unsigned :class:`GateEvidence` from a completed execution.

    There is exactly ONE place that computes ``binding_digest`` for produced
    evidence, and this is it.  Every ecosystem executor funnels through here so
    that the identity a decision point re-derives cannot drift per-ecosystem —
    a second, subtly different binding computation would be a silent
    accept-anything for whichever ecosystem got it wrong.
    """
    # The CONFIGURED workspace, not the throwaway tree: a decision point
    # verifies evidence against the workspace it configured, and the run tree is
    # an implementation detail with a different path every run.
    ws_digest = workspace_digest_for(workspace)
    return GateEvidence(
        schema=SCHEMA,
        routine_id=request.routine_id,
        run_id=request.run_id,
        iteration=int(request.iteration),
        gate_type=request.gate_type,
        command=normalize_gate_command(command),
        targets=targets,
        workspace_digest=ws_digest,
        binding_digest=binding_digest(
            routine_id=request.routine_id,
            run_id=request.run_id,
            iteration=int(request.iteration),
            gate_type=request.gate_type,
            command=command,
            targets=targets,
            workspace_digest=ws_digest,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
        ),
        tool=tool,
        tool_version=tool_version,
        exit_code=exit_code,
        checks_collected=checks_collected,
        checks_passed=checks_passed,
        checks_skipped=checks_skipped,
        checks_failed=checks_failed,
        started_at=started_at,
        finished_at=utc_now_iso(),
        nonce=secrets.token_hex(16),
        workspace_sha=workspace_sha,
        workspace_tree_clean=workspace_tree_clean,
        interpreter=interpreter,
        interpreter_version=interpreter_version,
        node_inventory_digest=node_inventory_digest,
        deselected_count=deselected_count,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
    )


class PytestGateRunner:
    """Executes a routine's pytest gate and records signed evidence."""

    def __init__(
        self,
        store: GateEvidenceStore,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        python_executable: str | None = None,
        loops_python_executable: str | Path | None = None,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable or sys.executable
        self.loops_python_executable = str(
            loops_python_executable
            if loops_python_executable is not None
            else default_loops_interpreter()
        )
        self._cached_interpreter_versions: dict[str, str] = {}

    def _get_interpreter_version(self, interpreter: str) -> str:
        cached = self._cached_interpreter_versions.get(interpreter)
        if cached is not None:
            return cached
        try:
            res = subprocess.run(
                [interpreter, "-c", "import sys; print(sys.version.split()[0])"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version = res.stdout.strip()
        except Exception:
            version = ""
        self._cached_interpreter_versions[interpreter] = version
        return version

    def _interpreter_for(self, interpreter_class: str) -> str:
        """The executable for *interpreter_class*, or raise.

        A missing loops venv is an ABSENCE, not a verdict. It is a fact about
        this host — the same class as a workspace that is gone or has gone dirty
        — so it raises :class:`GateWorkspaceUnusable`, settles NULL/NULL and stays
        out of the acceptance floor. Refusing (``gate_passed=0``) would recreate
        the false-adverse-at-scale defect in a new place: an operator who has not
        built ``var/loops/venv`` on this box would auto-pause every loop routine
        on evidence nobody produced.
        """
        if interpreter_class != INTERPRETER_CLASS_LOOPS:
            return self.python_executable
        candidate = normalize_executable(Path(self.loops_python_executable).expanduser())
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise GateWorkspaceUnusable(
                f"loops interpreter is missing or not executable: {candidate} — "
                f"a gate whose targets live under {LOOPS_SUBTREE}/ cannot run on the "
                "production venv (no LangGraph); build it with loops/requirements.txt"
            )
        return str(candidate)

    def run(self, request: GateRunRequest) -> GateEvidence:
        command = str(request.gate_config.get("command") or "")
        tool, targets = parse_gate_command(command)
        if tool != _SUPPORTED_TOOL:
            raise GateEvidenceRefusal(f"no trusted executor for tool: {tool}")

        workspace = _resolve_pinned_workspace(request)

        resolved_targets = resolve_targets(workspace, targets)
        # Before any git work: a mixed-family gate is refused here rather than
        # halfway through, and a host with no loops venv is `unavailable` without
        # having materialized a worktree to discover it.
        interpreter_class = interpreter_class_for_targets(workspace, resolved_targets)
        interpreter = self._interpreter_for(interpreter_class)

        workspace_sha, candidate_sha, merge_base_sha = _pin_and_verify_git(workspace, request)

        started_at = utc_now_iso()
        with _ephemeral_run_tree(workspace, workspace_sha) as run_tree:
            # In the tree that is about to execute, at the pinned SHA — see
            # refuse_loop_gate_on_the_machinery for why the write-side check
            # cannot bind this and this one can.
            refuse_loop_gate_on_the_machinery(run_tree, resolved_targets, request)
            (
                exit_code,
                collected,
                passed,
                skipped,
                failed,
                deselected_count,
                node_inventory_digest,
                workspace_tree_clean,
            ) = self._execute(
                run_tree,
                resolved_targets,
                workspace_sha,
                interpreter=interpreter,
                interpreter_class=interpreter_class,
            )

        # NEVER `Path.resolve()` an interpreter path — see
        # `harnesses.release_gate.normalize_executable`. Both venvs' `bin/python`
        # are symlinks to the SAME uv base interpreter, so resolving them would
        # record one identical string for two different dependency closures and
        # delete the only field in the receipt that says which one ran.
        interpreter_abs = str(normalize_executable(interpreter))

        evidence = build_gate_evidence(
            request=request,
            workspace=workspace,
            workspace_sha=workspace_sha,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
            command=command,
            targets=tuple(resolved_targets),
            tool=_SUPPORTED_TOOL,
            tool_version=_pytest_version(),
            interpreter=interpreter_abs,
            interpreter_version=self._get_interpreter_version(interpreter),
            exit_code=exit_code,
            checks_collected=collected,
            checks_passed=passed,
            checks_skipped=skipped,
            checks_failed=failed,
            deselected_count=deselected_count,
            node_inventory_digest=node_inventory_digest,
            workspace_tree_clean=workspace_tree_clean,
            started_at=started_at,
        )
        return self.store.record(evidence)

    def _execute(
        self,
        run_tree: Path,
        resolved_targets: tuple[str, ...],
        expected_sha: str,
        *,
        interpreter: str,
        interpreter_class: str,
    ) -> tuple[int, int, int, int, int, int, str, bool]:
        """Run the verifier in *run_tree* and report exactly what happened."""
        # Re-resolved against the tree that will actually be executed. A target
        # can exist in the pin source and not here: `git status` reports a tree
        # with ignored files as CLEAN, and a fresh checkout has none of them.
        resolve_targets(run_tree, resolved_targets)
        # ...and re-classified against it for the same reason. The pin source and
        # the run tree hold the same SHA, so tracked content agrees by
        # construction — but an untracked-or-ignored symlink exists only in the
        # pin source, and it must not be able to pick the interpreter for a file
        # that is somewhere else in the tree actually executed.
        tree_class = interpreter_class_for_targets(run_tree, resolved_targets)
        if tree_class != interpreter_class:
            raise GateEvidenceRefusal(
                f"gate targets resolve to the {interpreter_class} interpreter in the pin "
                f"source and the {tree_class} interpreter in the run tree"
            )

        with tempfile.TemporaryDirectory(prefix="gate-junit-") as scratch:
            scratch_path = Path(scratch)
            junit = scratch_path / "gate.xml"
            inventory_path = scratch_path / "inventory.json"
            argv = [
                interpreter,
                "-m",
                "pytest",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "-p",
                "scripts.certification_pytest_plugin",
                "--certification-inventory",
                str(inventory_path),
                "--certification-inventory-mode",
                "execution",
                "--junitxml",
                str(junit),
                *resolved_targets,
            ]
            exit_code, _stdout, _stderr = _run_process_group(
                argv,
                cwd=str(run_tree),
                env=_sanitized_env(run_tree),
                timeout_seconds=self.timeout_seconds,
            )

            if inventory_path.exists():
                try:
                    inventory_data = json.loads(inventory_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise GateExecutionInfraError(f"unparseable inventory artifact: {exc}") from exc
                if not isinstance(inventory_data, dict):
                    raise GateExecutionInfraError("invalid inventory artifact payload")

                _check_inventory_credibility(inventory_data, run_tree)

                collected_nodeids = list(inventory_data.get("collected_nodeids") or [])
                selected_nodeids = list(inventory_data.get("selected_nodeids") or [])
                deselected_nodeids = list(inventory_data.get("deselected_nodeids") or [])
                outcomes = dict(inventory_data.get("outcomes") or {})

                node_inventory_digest = digest(json.dumps(sorted(collected_nodeids)))
                deselected_set = (set(collected_nodeids) - set(selected_nodeids)) | set(
                    deselected_nodeids
                )
                deselected_count = len(deselected_set)
                collected = len(collected_nodeids)
                passed = sum(1 for v in outcomes.values() if v == "passed")
                failed = sum(1 for v in outcomes.values() if v == "failed")
                skipped = sum(1 for v in outcomes.values() if v == "skipped")

                if exit_code != 124:
                    junit_coll, junit_pass, junit_skip, junit_fail = _junit_counts(junit)
                    if (len(outcomes), passed, skipped, failed) != (
                        junit_coll,
                        junit_pass,
                        junit_skip,
                        junit_fail,
                    ):
                        raise GateExecutionInfraError(
                            f"inventory counts {(len(outcomes), passed, skipped, failed)} "
                            f"mismatch junit counts {(junit_coll, junit_pass, junit_skip, junit_fail)}"
                        )
            else:
                if exit_code != 124:
                    raise GateExecutionInfraError("pytest execution produced no inventory artifact")
                collected = passed = skipped = failed = deselected_count = 0
                node_inventory_digest = digest(json.dumps([]))

        workspace_tree_clean = _post_run_tree_clean(run_tree, expected_sha)
        return (
            exit_code,
            collected,
            passed,
            skipped,
            failed,
            deselected_count,
            node_inventory_digest,
            workspace_tree_clean,
        )


def _pytest_version() -> str:
    try:
        import pytest
    except ImportError:
        return ""
    return str(pytest.__version__)


def _ecosystem_env(workspace: Path, executor: EcosystemExecutor) -> dict[str, str]:
    """A minimal child environment for a non-Python toolchain.

    The same allowlist ``_sanitized_env`` uses, minus the pytest-specific keys
    and plus the executor's own hermeticity overrides.  It is an ALLOWLIST, so
    every selection knob this seam has never heard of (``NODE_OPTIONS``,
    ``RUSTFLAGS``, ``GOFLAGS``, ``CARGO_BUILD_TARGET``, …) is dropped rather
    than denied one at a time.
    """
    if not workspace.is_absolute():
        raise GateEvidenceRefusal(f"gate workspace must be absolute: {workspace}")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    }
    env["PATH"] = sanitize_path_env(env.get("PATH"))
    env.update(executor.env_overrides)
    return env


class EcosystemGateRunner:
    """The single :class:`TrustedGateRunner`, dispatching on declared ecosystem.

    A gate config with no ``ecosystem`` key — every routine that existed before
    M2 — is delegated verbatim to :class:`PytestGateRunner`.  That delegation is
    the whole compatibility argument: the Python path is not re-implemented
    here, so it cannot drift, and this class can only ADD ecosystems, never
    change what a Python gate means.
    """

    def __init__(
        self,
        store: GateEvidenceStore,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        pytest_runner: TrustedGateRunner | None = None,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.pytest_runner = pytest_runner or PytestGateRunner(
            store, timeout_seconds=timeout_seconds
        )

    def run(self, request: GateRunRequest) -> GateEvidence:
        ecosystem = ecosystem_of_gate_config(request.gate_config)
        if request.gate_type == MERGE_GATE_TYPE and ecosystem != ECOSYSTEM_PYTHON:
            # THE OTHER HALF OF THE MERGE-GATE PIN, and the load-bearing one.
            #
            # Merge-candidate evidence is written under the FIXED merge-gate
            # identity (routine_id `merge-gate`, run_id = candidate SHA), which
            # is precisely the record `merge-gate.sh` reads as authorization to
            # merge this repository. `routines._validate_gate` refuses to STORE
            # a merge_candidate routine naming another ecosystem — but stored
            # rows are never re-validated, so validation alone would leave every
            # row written before it as a live path to a forged receipt. This
            # refuses at execution, where the receipt would actually be minted.
            raise GateEvidenceRefusal(
                f"a {MERGE_GATE_TYPE} gate may not declare ecosystem {ecosystem!r}: its "
                f"receipt authorizes merging this repository and is accepted only as "
                f"{MERGE_GATE_TOOL!r} evidence"
            )
        if ecosystem == ECOSYSTEM_PYTHON:
            return self.pytest_runner.run(request)

        executor = executor_for(ecosystem)
        command = str(request.gate_config.get("command") or "")
        targets = executor.parse_command(normalize_gate_command(command))

        workspace = _resolve_pinned_workspace(request)
        # Containment is proven against the on-disk paths the targets imply —
        # for a Go pattern like `./pkg/...` that is `pkg`, not the pattern — so a
        # pattern can never be a hole in the containment check. The evidence
        # still records what the gate DECLARED, which is what actually ran.
        resolve_targets(workspace, executor.containment_paths(targets))
        # Resolved before any git work, for the same reason the pytest runner
        # resolves its interpreter first: a host without the toolchain is
        # `unavailable` without having materialized a worktree to discover it.
        program = resolve_program(executor, path=os.environ.get("PATH"))
        tool_version = self._tool_version(program, executor)

        workspace_sha, candidate_sha, merge_base_sha = _pin_and_verify_git(workspace, request)

        started_at = utc_now_iso()
        with _ephemeral_run_tree(workspace, workspace_sha) as run_tree:
            # Same policy, same function, in the non-python executor's run tree.
            # A loops-module row cannot legitimately get here today (the write
            # side admits only python-grammar verifiers for loops), so this is
            # the belt for a row that predates the rule or a future ecosystem
            # loop gate — and it costs one call.
            refuse_loop_gate_on_the_machinery(
                run_tree, tuple(executor.containment_paths(targets)), request
            )
            exit_code, outcome, tree_clean = self._execute(
                run_tree,
                targets,
                workspace_sha,
                executor=executor,
                program=program,
                gate_config=request.gate_config,
            )

        if outcome.collected == 0:
            # A candidate fact, not a host fact: a project whose declared gate
            # runs and finds nothing to check is not gate-valid, and calling it
            # `unavailable` would let a repo with no tests sit forever in the
            # excluded-from-the-floor bucket instead of being told it has no
            # gate. `evidence_rejections` refuses a zero-check record too; this
            # is the earlier of the two, so no vacuous record is ever minted.
            raise GateEvidenceRefusal(
                f"{executor.ecosystem} gate collected zero checks (vacuous pass): "
                f"{normalize_gate_command(command)}"
            )

        evidence = build_gate_evidence(
            request=request,
            workspace=workspace,
            workspace_sha=workspace_sha,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
            command=command,
            targets=tuple(targets),
            tool=executor.tool,
            tool_version=tool_version,
            interpreter=str(program),
            interpreter_version=tool_version,
            exit_code=exit_code,
            checks_collected=outcome.collected,
            checks_passed=outcome.passed,
            checks_skipped=outcome.skipped,
            checks_failed=outcome.failed,
            deselected_count=outcome.deselected,
            node_inventory_digest=digest(json.dumps(sorted(outcome.node_ids))),
            workspace_tree_clean=tree_clean,
            started_at=started_at,
        )
        return self.store.record(evidence)

    def _tool_version(self, program: Path, executor: EcosystemExecutor) -> str:
        """Identify the toolchain, or declare the host unusable.

        ``evidence_rejections`` refuses a record that cannot name its executing
        tool, so a version probe that fails is not a detail to shrug at: it
        means this binary could not be identified and must not be trusted to
        grade anything.
        """
        try:
            res = subprocess.run(
                [str(program), *executor.version_argv],
                capture_output=True,
                text=True,
                timeout=_TOOL_VERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GateWorkspaceUnusable(
                f"{executor.ecosystem} toolchain at {program} could not be identified: {exc}"
            ) from exc
        version = (res.stdout or res.stderr or "").strip().splitlines()
        if res.returncode != 0 or not version or not version[0].strip():
            raise GateWorkspaceUnusable(
                f"{executor.ecosystem} toolchain at {program} printed no version"
            )
        return version[0].strip()

    def _execute(
        self,
        run_tree: Path,
        resolved_targets: tuple[str, ...],
        expected_sha: str,
        *,
        executor: EcosystemExecutor,
        program: Path,
        gate_config: dict[str, Any],
    ) -> tuple[int, CountedOutcome, bool]:
        """Run the toolchain in *run_tree* and count only what it produced here."""
        # Re-resolved against the tree that will actually be executed. A target
        # can exist in the pin source and not here: `git status` reports a tree
        # with ignored files as CLEAN, and a fresh checkout has none of them.
        resolve_targets(run_tree, executor.containment_paths(resolved_targets))
        # Everything the candidate could do to make itself ungradeable is caught
        # HERE, against the exact content about to run and before the toolchain
        # starts: a Cargo target that replaces libtest with its own main(), a Go
        # target that is a file rather than a package, a vitest config that is
        # not the one the operator pinned.
        executor.preflight(run_tree, resolved_targets)
        executor.check_config_pin(run_tree, gate_config)

        with tempfile.TemporaryDirectory(prefix="gate-eco-") as scratch:
            # OUTSIDE the run tree, minted for this one invocation, and proved
            # empty before the child starts. This is what makes a report count
            # as evidence *of this run*: a machine-readable file committed into
            # the candidate tree lives at a path no executor ever reads.
            artifacts = Path(scratch).resolve()
            if inode_relative_parts_anchored(artifacts, run_tree.resolve()) is not None:
                raise GateExecutionInfraError(
                    "gate artifact directory is inside the candidate run tree; the report "
                    "would be indistinguishable from a file the candidate committed"
                )
            if any(artifacts.iterdir()):
                raise GateExecutionInfraError("gate artifact directory was not empty at start")

            argv = executor.build_argv(str(program), resolved_targets, artifacts)
            exit_code, stdout, stderr = _run_process_group(
                argv,
                cwd=str(run_tree),
                env=_ecosystem_env(run_tree, executor),
                timeout_seconds=self.timeout_seconds,
            )
            # Sampled the instant the measured process group was observed dead,
            # and handed to the counter so a report whose mtime postdates it can
            # be recognised as written by something we were not measuring — a
            # descendant that escaped the group by calling setsid for itself.
            finished_at_ns = time.time_ns()
            if exit_code == 124:
                # INCONCLUSIVE, never a pass and never a hang: the process group
                # has already been TERM/KILLed and reaped by _run_process_group,
                # and no counted evidence exists for a run that did not finish.
                raise GateExecutionInfraError(
                    f"{executor.ecosystem} gate exceeded {self.timeout_seconds}s and was "
                    "terminated; no counted evidence exists"
                )
            outcome = executor.count(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                artifacts_dir=artifacts,
                finished_at_ns=finished_at_ns,
            )

        return exit_code, outcome, _post_run_tree_clean(run_tree, expected_sha)


def produce_gate_evidence(
    runner: TrustedGateRunner | None,
    store: GateEvidenceStore,
    request: GateRunRequest,
) -> GateEvidenceOutcome:
    """Return trusted evidence outcome for *request*, executing at most once under lease."""
    try:
        existing = store.load(request.routine_id, request.run_id)
        if existing is not None:
            return GateEvidenceOutcome(
                status="evidence", evidence=existing, detail="loaded existing evidence"
            )
    except GateEvidenceError as exc:
        logger.warning(
            "failed to load gate evidence for routine %s run %s: %s",
            request.routine_id,
            request.run_id,
            exc,
        )
        return GateEvidenceOutcome(status="unavailable", evidence=None, detail=str(exc))
    except Exception as exc:
        logger.exception(
            "unexpected failure loading gate evidence for routine %s run %s",
            request.routine_id,
            request.run_id,
        )
        return GateEvidenceOutcome(
            status="unavailable", evidence=None, detail=f"{type(exc).__name__}: {exc}"
        )

    if runner is None:
        return GateEvidenceOutcome(
            status="unavailable", evidence=None, detail="no trusted gate runner configured"
        )

    ttl = (
        getattr(runner, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS
    ) + 60.0
    token = store.claim(request.routine_id, request.run_id, ttl_seconds=ttl)
    if token is None:
        return GateEvidenceOutcome(
            status="in_progress", evidence=None, detail="live execution claim exists"
        )

    try:
        evidence = runner.run(request)
        return GateEvidenceOutcome(
            status="evidence", evidence=evidence, detail="recorded new evidence"
        )
    except GateEvidenceExists:
        try:
            loaded = store.load(request.routine_id, request.run_id)
            if loaded is not None:
                return GateEvidenceOutcome(
                    status="evidence", evidence=loaded, detail="loaded winner evidence"
                )
            return GateEvidenceOutcome(
                status="unavailable",
                evidence=None,
                detail="evidence exists but could not be loaded",
            )
        except GateEvidenceError as exc:
            logger.warning(
                "failed to load winner gate evidence for routine %s run %s: %s",
                request.routine_id,
                request.run_id,
                exc,
            )
            return GateEvidenceOutcome(status="unavailable", evidence=None, detail=str(exc))
    # MUST precede the GateEvidenceRefusal clause it is a subclass of. The
    # workspace was unusable, so nothing about this run was judged: that is
    # `unavailable` (the NULL/NULL, excluded-from-the-acceptance-floor bucket),
    # not `refused` (which settles gate_passed=0 and counts toward auto-pause).
    # It is the settlement-side half of the workspace configuration: a probe can
    # only prove the workspace was clean when the job spawned, and the tree can
    # go dirty at any point before the gate runs. Classifying the cause is what
    # makes that race harmless; narrowing the window never could.
    except GateWorkspaceUnusable as exc:
        logger.warning(
            "gate workspace unusable for routine %s run %s: %s",
            request.routine_id,
            request.run_id,
            exc,
        )
        return GateEvidenceOutcome(status="unavailable", evidence=None, detail=str(exc))
    except GateEvidenceRefusal as exc:
        logger.warning(
            "gate execution refused for routine %s run %s: %s",
            request.routine_id,
            request.run_id,
            exc,
        )
        return GateEvidenceOutcome(status="refused", evidence=None, detail=str(exc))
    except GateExecutionInfraError as exc:
        logger.warning(
            "gate execution infra error for routine %s run %s: %s",
            request.routine_id,
            request.run_id,
            exc,
        )
        return GateEvidenceOutcome(status="unavailable", evidence=None, detail=str(exc))
    except GateEvidenceError as exc:
        logger.warning(
            "gate execution error for routine %s run %s: %s",
            request.routine_id,
            request.run_id,
            exc,
        )
        return GateEvidenceOutcome(status="unavailable", evidence=None, detail=str(exc))
    except Exception as exc:
        logger.exception(
            "unexpected gate execution failure for routine %s run %s",
            request.routine_id,
            request.run_id,
        )
        return GateEvidenceOutcome(
            status="unavailable", evidence=None, detail=f"{type(exc).__name__}: {exc}"
        )
    finally:
        store.release_claim(token)


def default_gate_runner(store: GateEvidenceStore) -> TrustedGateRunner | None:
    """Return a trusted runner when a gate workspace is resolvable.

    The dispatcher, not the pytest runner directly — but a routine that does not
    declare an ``ecosystem`` still executes on :class:`PytestGateRunner` through
    it, byte-for-byte as before.  Python is the default; nothing else runs
    unless a gate config asks for it by exact name.
    """
    if default_gate_workspace() is None:
        return None
    return EcosystemGateRunner(store)


def default_gate_workspace() -> Path | None:
    """Resolve the gate execution workspace.

    Gate execution is explicitly opt-in: ``OMNIAGENTOS_GATE_WORKSPACE`` must
    name an existing directory. Falling back to the process checkout would let
    an ordinary scheduler tick execute trusted gates against a dirty or moving
    workspace and would create evidence state before configuration is proven.
    """
    configured = os.environ.get("OMNIAGENTOS_GATE_WORKSPACE")
    if not configured:
        return None
    workspace = Path(configured).expanduser()
    if workspace.is_dir():
        return workspace
    logger.warning("OMNIAGENTOS_GATE_WORKSPACE is set but not a directory: %s", configured)
    return None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "INTERPRETER_CLASS_LOOPS",
    "INTERPRETER_CLASS_REPO",
    "LOOPS_SUBTREE",
    "TARGET_ESCAPES",
    "TARGET_INSIDE",
    "TARGET_MISSING",
    "EcosystemGateRunner",
    "GateEvidenceOutcome",
    "GateRunRequest",
    "GateTargetLocation",
    "PytestGateRunner",
    "TrustedGateRunner",
    "build_gate_evidence",
    "default_gate_runner",
    "default_gate_workspace",
    "default_loops_interpreter",
    "interpreter_class_for_targets",
    "locate_gate_target",
    "parse_gate_command",
    "produce_gate_evidence",
    "resolve_targets",
]
