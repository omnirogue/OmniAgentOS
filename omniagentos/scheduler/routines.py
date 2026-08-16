"""Routine model validation: "loops with teeth".

A routine is a scheduled/event-triggered task template that is allowed to run
unattended ONLY because it declares, up front:

1. a ``trigger`` (cron schedule or event name) that fires it,
2. a ``task_template`` describing the work to instantiate,
3. an OBJECTIVE ``gate`` that decides goal-met — an exit code, a test command,
   or a metric threshold. Never a model's opinion of its own work.
4. a hard stop-condition on TOP of the gate — ``max_iterations``,
   ``budget_usd``, or ``human_checkpoint`` — so a broken gate can't spin
   forever or burn an unbounded budget, and
5. a ``notification_target`` so a human is told what happened.

:func:`validate_routine` is the single source of truth for "is this a valid
routine" and is called from every write path (API create/update AND the DAL
directly, so a future runner/importer can't bypass it). The migration's CHECK
constraints are a floor, not a replacement — they catch bad enum values but
can't validate JSON config shape.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, TypeGuard

TRIGGER_TYPES = frozenset({"cron", "event"})
GATE_TYPES = frozenset({"exit_code", "test_command", "metric_threshold", "merge_candidate"})
HARD_CAP_TYPES = frozenset({"max_iterations", "budget_usd", "human_checkpoint"})
NOTIFICATION_CHANNELS = frozenset({"slack", "email", "webhook", "desktop"})
# Advisory metadata only — never consulted by firing, gating, settlement, or notify.
SCOPE_VALUES = frozenset({"personal", "company", "project", "system"})

#: ``task_template.input.module`` that routes a routine's work to the LangGraph
#: loop runtime. The value is MIRRORED, never imported, by
#: :data:`omniagentos.scheduler.loop_jobs.LOOP_MODULE` (the execution side, which
#: imports this module — the edge cannot run the other way) and by
#: ``omniagentos_loops.registry.LOOP_MODULE`` (a different venv entirely, which
#: must stay import-light). ``tests/scheduler/test_loop_gate_refusal.py`` binds
#: the copies together so a drift is red rather than silent.
#:
#: A row is judged a loop row on the module ALONE — not on ``input.kind`` —
#: because that is what ``loop_jobs._spec`` dispatches on. Keying the refusal
#: below on ``kind`` would let ``kind='anything'`` evade the gate rule and still
#: execute as a loop.
LOOP_TASK_MODULE = "omniagentos.loops"

_METRIC_OPERATORS = frozenset({">=", "<=", ">", "<", "=="})
_GIT_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# Objective gates are deliberately restricted to verifier-shaped commands.
# A denylist is not sufficient: ``echo ok``, ``ls -la``, and infinitely many
# shell compositions can all exit zero without proving the task objective.
_SHELL_CONTROL_RE = re.compile(r"(?:&&|\|\||[;&|<>`$(){}]|[\r\n])")
_DIRECT_VERIFIERS = frozenset({"pytest", "mypy", "pyright"})
_PYTHON_EXECUTABLE_RE = re.compile(r"python(?:3(?:\.\d+)?)?")

# A loop's gate may not name the loop MACHINERY. Everything under this root
# grades the scheduler→worker mechanism, which is the same code for every loop
# instance and therefore passes on every tick no matter what the instance
# produced — the exact property that makes a gate vacuous *for a loop*, even
# though the same command is a perfectly good gate for a routine whose work IS
# the scheduler. `pytest tests/scheduler/test_loop_jobs.py` was the seeded
# default until 2026-08-02, so every loop registered without an explicit gate
# settled favourable over whatever it actually did, forever.
#
# Stated as a denylist of the machinery rather than an allowlist of `loops/`
# deliberately: a loop's real gate is often NOT in this repo's tree at all
# (the live clock loop gates on `pytest tests/test_grandfather_clock_gate.py`
# resolved inside the gate workspace), so an allowlist would refuse the honest
# rows and teach authors to route around the rule.
_LOOP_MECHANISM_GATE_PARTS = ("tests", "scheduler")

# Bounded missed-fire catch-up window.  Eight days covers daily and weekly
# schedules while putting a hard ceiling on the minute scan.
_CRON_CATCHUP_MINUTES = 8 * 24 * 60

# Forward search bound for compute_next_run. Distinct from catch-up: annual
# crons (``0 0 1 1 *``) must still resolve to the next real occurrence.
_CRON_NEXT_RUN_MINUTES = 400 * 24 * 60

# Hard caps that are inherently a whole-number count of iterations, not a
# continuous quantity like a dollar budget.
_INTEGER_HARD_CAPS = frozenset({"max_iterations", "human_checkpoint"})


class RoutineValidationError(ValueError):
    """Raised when a routine payload is missing a gate or a hard stop-condition.

    ``errors`` holds every violation found (not just the first) so an operator
    fixing the create/edit form sees every problem in one round trip.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _nonempty_str(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and value.strip() != ""


def _validate_ecosystem(value: Any, errors: list[str]) -> str | None:
    """The gate's declared ecosystem, or ``None`` after recording the error.

    Imported lazily: ``gate_ecosystems`` is the executor-side module and this is
    the write-side one; keeping the edge lazy means the routines DAL has no
    import-time dependency on anything that shells out.
    """
    from omniagentos.scheduler.gate_ecosystems import normalize_ecosystem
    from omniagentos.scheduler.gate_evidence import GateEvidenceRefusal

    try:
        return normalize_ecosystem(value)
    except GateEvidenceRefusal as exc:
        errors.append(str(exc))
        return None


def _ecosystem_command_is_valid(ecosystem: str, command: str) -> bool:
    from omniagentos.scheduler.gate_ecosystems import ecosystem_command_is_valid

    return ecosystem_command_is_valid(ecosystem, command)


def _validate_ecosystem_config_pin(
    ecosystem: str, config: dict[str, Any], errors: list[str]
) -> None:
    """Require the operator-owned config digest an ecosystem's executor needs.

    An npm gate cannot prove from its JUnit report that nothing was excluded, so
    the executor demands a digest of the config the operator approved. Checking
    it here as well means the routine is rejected at CREATION rather than
    silently refusing on every tick forever.
    """
    from omniagentos.scheduler.gate_ecosystems import executor_for

    try:
        pin_key = executor_for(ecosystem).config_pin_key
    except Exception:  # noqa: BLE001 — an invalid ecosystem is already reported
        return
    if not pin_key:
        return
    declared = config.get(pin_key)
    if not isinstance(declared, str) or not re.fullmatch(r"[0-9a-f]{64}", declared):
        errors.append(
            f"gate_config.{pin_key} is required for a {ecosystem} gate and must be a "
            "64-character lowercase hex digest of the test configuration the operator "
            "approved (the report alone cannot show a silently excluded test file)"
        )


def _gate_command_targets(command: str) -> list[str] | None:
    """The verifier's positional targets, or ``None`` when it is not a verifier.

    ONE definition of the gate grammar with two readers.
    :func:`_is_non_vacuous_gate_command` asks whether the command is a
    recognized objective verifier at all; :func:`_validate_loop_gate` asks what
    that verifier actually grades. Answering the second question with a second
    parser is the "two copies of one rule" failure this repo has already paid
    for — the copies drift and the weaker one becomes the real rule.

    ``[]`` (recognized, no targets) and ``None`` (not recognized) are different
    answers and callers must not conflate them: ``git diff --check`` takes no
    targets and is still a verifier.
    """
    if _SHELL_CONTROL_RE.search(command):
        return None
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not parts:
        return None

    def checker_args_are_executing(args: list[str]) -> list[str] | None:
        # Configuration/selection flags can turn a recognized checker into
        # collection-only, exit-zero, ignored, or mutating work.  Routine gates
        # therefore accept only explicit, repository-relative positional
        # targets. Response files and paths outside the checkout could smuggle
        # options or prove an unrelated surface.
        def safe_target(arg: str) -> bool:
            if not arg or arg.startswith(("-", "@")):
                return False
            path_part = arg.split("::", 1)[0]
            path = PurePosixPath(path_part)
            return path_part == "." or (not path.is_absolute() and ".." not in path.parts)

        return args if args and all(safe_target(arg) for arg in args) else None

    executable = parts[0]
    if executable in _DIRECT_VERIFIERS:
        return checker_args_are_executing(parts[1:])
    if executable == "ruff":
        if len(parts) >= 3 and parts[1] == "check":
            return checker_args_are_executing(parts[2:])
        return None
    if _PYTHON_EXECUTABLE_RE.fullmatch(executable):
        if (
            len(parts) >= 4
            and parts[1] == "-m"
            and parts[2] in _DIRECT_VERIFIERS | {"ruff"}
            and (parts[2] != "ruff" or parts[3] == "check")
        ):
            return checker_args_are_executing(parts[4:] if parts[2] == "ruff" else parts[3:])
        return None
    if executable == "git":
        return [] if parts[1:] == ["diff", "--check"] else None
    return None


def _is_non_vacuous_gate_command(command: str) -> bool:
    """Return true only for a single, recognized objective verifier command."""
    return _gate_command_targets(command) is not None


def _validate_trigger(trigger_type: Any, config: Any, errors: list[str]) -> None:
    if trigger_type not in TRIGGER_TYPES:
        errors.append(
            f"trigger_type must be one of {sorted(TRIGGER_TYPES)} (a routine must declare a trigger)"
        )
        return
    if not isinstance(config, dict):
        errors.append("trigger_config must be an object")
        return
    if trigger_type == "cron":
        cron = config.get("cron")
        if not _nonempty_str(cron):
            errors.append("trigger_config.cron is required for a cron trigger")
        elif len(cron.split()) not in (5, 6):
            errors.append("trigger_config.cron must be a 5- or 6-field cron expression")
    elif trigger_type == "event":
        if not _nonempty_str(config.get("event")):
            errors.append("trigger_config.event is required for an event trigger")


def _validate_gate(gate_type: Any, config: Any, errors: list[str]) -> None:
    """The objective gate: NEVER a model opinion, always a checkable fact."""
    if gate_type not in GATE_TYPES:
        errors.append(
            f"gate_type must be one of {sorted(GATE_TYPES)} "
            "(every routine must declare an objective goal-met gate)"
        )
        return
    if not isinstance(config, dict):
        errors.append("gate_config must be an object")
        return
    if gate_type in ("exit_code", "test_command", "merge_candidate"):
        # M2: which verifier grammar applies is chosen by the gate's declared
        # ecosystem, and by nothing inferred from the repo. An absent key means
        # `python`, so every routine written before ecosystems existed keeps the
        # identical rule; an unknown value is an ERROR, never a fall back to
        # Python — a typo must not silently grade a Go project with pytest.
        ecosystem = _validate_ecosystem(config.get("ecosystem"), errors)
        if ecosystem is not None and ecosystem != "python" and gate_type == "merge_candidate":
            # A merge_candidate gate does not grade an arbitrary project: its
            # evidence is written under the FIXED merge-gate identity and is read
            # by merge-gate.sh as authorization to merge THIS Python repository.
            # Letting it declare another ecosystem would let a handful of passing
            # tests in another language mint that authorization.
            errors.append(
                "gate_config.ecosystem must be 'python' for a merge_candidate gate "
                "(the merge gate grades this repository and accepts only pytest evidence)"
            )
            ecosystem = None
        command = config.get("command")
        if not _nonempty_str(command):
            errors.append(f"gate_config.command is required for a {gate_type} gate")
        elif ecosystem is None:
            pass  # the ecosystem itself is already reported as invalid
        elif ecosystem == "python":
            if not _is_non_vacuous_gate_command(command):
                errors.append(
                    "gate_config.command must be one recognized objective verifier "
                    "(pytest, ruff check, mypy, pyright, or git diff --check) "
                    "with an actual target and without shell operators/non-executing flags"
                )
        elif not _ecosystem_command_is_valid(ecosystem, command):
            errors.append(
                f"gate_config.command must be the {ecosystem} objective verifier "
                "with explicit repository-relative targets and no flags"
            )
        if ecosystem is not None and ecosystem != "python":
            _validate_ecosystem_config_pin(ecosystem, config, errors)
        expected = config.get("expected_exit_code", 0)
        if not isinstance(expected, int) or isinstance(expected, bool):
            errors.append("gate_config.expected_exit_code must be an integer")
        elif expected != 0:
            errors.append("gate_config.expected_exit_code must be 0 for an objective verifier")
        if gate_type == "merge_candidate":
            candidate_sha = config.get("candidate_sha")
            merge_base_sha = config.get("merge_base_sha")
            if not isinstance(candidate_sha, str) or not _GIT_SHA_RE.match(candidate_sha):
                errors.append(
                    "gate_config.candidate_sha must be a 40-char lowercase git SHA "
                    "for a merge_candidate gate"
                )
            if not isinstance(merge_base_sha, str) or not _GIT_SHA_RE.match(merge_base_sha):
                errors.append(
                    "gate_config.merge_base_sha must be a 40-char lowercase git SHA "
                    "for a merge_candidate gate"
                )
    elif gate_type == "metric_threshold":
        if not _nonempty_str(config.get("metric")):
            errors.append("gate_config.metric is required for a metric_threshold gate")
        operator = config.get("operator")
        if operator not in _METRIC_OPERATORS:
            errors.append(f"gate_config.operator must be one of {sorted(_METRIC_OPERATORS)}")
        threshold = config.get("threshold")
        if not isinstance(threshold, int | float) or isinstance(threshold, bool):
            errors.append("gate_config.threshold must be a number")


def _real_target_parts(target: str) -> tuple[str, ...]:
    """Where *target* really is in this checkout — resolved if it can be, spelled if not.

    The classification below must not be defeatable by SPELLING, because the
    thing it classifies is executed by ``gate_runner``, which resolves every
    target inode-anchored and symlink-following before running it
    (:func:`~omniagentos.scheduler.gate_runner.locate_gate_target`, the one
    definition, called from here so the two layers cannot disagree about what a
    path IS — two definitions of one rule is this repo's settled-definition
    divergence, and it has cost four incidents).

    The import is lazy because ``gate_runner`` imports THIS module: the write
    side is the lower layer and the edge may only run the other way at call
    time, never at import time.

    A target that does not exist in this checkout falls back to its spelled
    parts, DELIBERATELY and this is the explicit decision the reviewer asked
    for:

    * refusing it outright would make ``validate_routine`` environment-dependent — the
      same payload valid on the API host and invalid on a developer's — and
      would refuse a gate whose target exists only at the gate workspace's
      pinned commit;
    * accepting it unclassified would be fail-open. The spelled parts still
      catch every direct spelling.

    The residue — an alias that resolves into the machinery HERE but not in the
    validating checkout — is not reachable in practice: the gate workspace is a
    pinned, clean checkout of this same repository (``workspace_tree_clean`` is
    part of the evidence), so an effective alias must be COMMITTED, and a
    committed alias resolves here too.
    """
    from omniagentos.contracts import _repo_root
    from omniagentos.scheduler.gate_runner import TARGET_INSIDE, locate_gate_target

    spelled = PurePosixPath(target.split("::", 1)[0]).parts
    try:
        located = locate_gate_target(Path(_repo_root()), target)
    except OSError:  # pragma: no cover — an unreadable checkout is not a verdict
        return spelled
    if located.status == TARGET_INSIDE and located.parts is not None:
        return located.parts
    return spelled


def loop_gate_target_verdict(parts: tuple[str, ...]) -> str | None:
    """``"machinery"``, ``"blanket"``, or ``None`` for a target a loop may gate on.

    ONE prefix relation decides both, in both directions:

    * ``parts`` starts with the machinery — the target IS the scheduler suite or
      a file inside it;
    * the machinery starts with ``parts`` — the target is a DIRECTORY that
      contains the scheduler suite: ``tests``, or the workspace root (``.``,
      which anchors to ``()``). A blanket gate sweeps the whole repository, so
      it passes for every loop for reasons that have nothing to do with any
      loop, and it is red for every loop whenever anything anywhere is red.

    Both are the same property — a verdict uncorrelated with what the instance
    did — so both are refused, and a gate naming a directory that merely happens
    to sit beside the machinery (``loops/tests/instances``) is untouched.
    """
    if parts[: len(_LOOP_MECHANISM_GATE_PARTS)] == _LOOP_MECHANISM_GATE_PARTS:
        return "machinery"
    if _LOOP_MECHANISM_GATE_PARTS[: len(parts)] == parts:
        return "blanket"
    return None


#: Spelled once so every refusal below names the same remedy.
_LOOP_GATE_REMEDY = (
    "gate_type='test_command' with e.g. "
    "gate_config={'command': 'pytest loops/tests/instances/test_<instance>.py'}"
)


def _executed_gate_types() -> frozenset[str]:
    """The gate types whose ``command`` is actually EXECUTED at settlement.

    Imported from the settler rather than restated, because the whole defect
    class this module is fighting is a second copy of a rule drifting from the
    first: a loop admitted here on the strength of a command that
    ``routines_settle`` never runs is a loop that grades itself, and a
    hand-maintained duplicate of that set would go stale silently the first time
    a gate type is added.

    Lazy because ``routines_settle`` imports THIS module (and the store, which
    also calls the function below): the edge may only run at call time. There is
    no fallback on ImportError on purpose — an import that fails here means the
    settler is not installed, and guessing the set would be exactly the silent
    second copy.
    """
    from omniagentos.scheduler.routines_settle import EXECUTED_GATE_TYPES

    return EXECUTED_GATE_TYPES


def loop_gate_errors(task_template: Any, gate_type: Any, gate_config: Any) -> list[str]:
    """Every reason this row is a LOOP whose gate cannot judge it. ``[]`` = fine.

    Fail-closed at seed time, which is the only place it can be closed. A loop
    tick reports its own status, and the single thing in the system authorised
    to contradict that report is ``gate_config.command``, executed by
    ``routines_settle`` in the gate workspace on every tick. So a loop whose
    gate cannot possibly fail on its work is a loop that grades itself — the
    defect this refusal exists for, observed live: the public seeding helper
    substituted ``pytest tests/scheduler/test_loop_jobs.py`` whenever an author
    omitted a gate, and that suite passes whether the loop healed a host or did
    nothing at all (rtn_1e5567b9f3314a2c9d76: 10 runs, 10 "accepted", zero
    heals, acceptance 1.0).

    Refused:

    * a ``gate_type`` whose command is never EXECUTED at settlement
      (:func:`_executed_gate_types`). This is the DECOY case and it is the
      original incident wearing a disguise: ``gate_type='metric_threshold'``
      with a perfectly good ``command`` alongside it reads like a gated loop in
      every list, every dashboard and every review, and settles with
      ``gate_passed=NULL`` forever because nothing ever runs that string;
    * no command at all;
    * a command this validator cannot recognise as an objective verifier
      (``echo ok``). Refused HERE rather than left to :func:`_validate_gate`,
      which the draft exemption and both DAL activation belts skip — and
      because an unrecognised verifier is a fact about the CANDIDATE, which the
      executor itself refuses, so the fail-closed answer is the same at both
      ends. Note the consequence: a loop gate must be recognisable by the
      PYTHON verifier grammar, since that is the only grammar whose targets the
      classification below can read. A non-python loop gate would have to
      extend both ends deliberately;
    * a recognized verifier that declares NO targets (``git diff --check``):
      the executor refuses it on every tick anyway;
    * a target that resolves into the loop machinery, in any spelling;
    * a blanket gate — ``.``, ``tests``, the repo root — see
      :func:`loop_gate_target_verdict`.

    NOT refused, because it is not decidable here: a gate naming a real but
    unrelated suite. Nothing at write time can prove a suite exercises this
    instance; that is the author's claim. The activation proof
    (``api/routes/routines._run_activation_gate``) and a settled ADVERSE
    MITIGATE it — they do not catch it, and saying they did would be the same
    overclaim this lane exists to remove: a permanently green unrelated suite
    passes activation and never settles adverse.

    Returned as a list rather than raised so ONE definition serves several
    callers with different shapes: :func:`validate_routine` merges them into its
    all-errors report, and the DAL's activation guard raises immediately.

    Nothing here touches settlement: a row already in the database keeps
    settling exactly as before, because a rule that retroactively invalidated
    live rows would auto-pause working loops — the failure mode this repo has
    hit four times and must not hit again. What DOES bind at settlement is the
    execution-time twin of the target policy below
    (``gate_runner.refuse_loop_gate_on_the_machinery``), which judges the run
    tree the gate really executes in.
    """
    payload = task_template.get("input") if isinstance(task_template, dict) else None
    if not isinstance(payload, dict) or payload.get("module") != LOOP_TASK_MODULE:
        return []
    if gate_type not in _executed_gate_types():
        return [
            f"a loop routine may not use gate_type={gate_type!r}: its command is never "
            "EXECUTED at settlement, so the row carries a gate that reads like one and "
            "rules on nothing — a loop tick's own report would stand unchallenged "
            f"forever ({_LOOP_GATE_REMEDY}). Executed gate types: "
            f"{sorted(_executed_gate_types())}."
        ]
    command = gate_config.get("command") if isinstance(gate_config, dict) else None
    if not _nonempty_str(command):
        return [
            "a loop routine must declare gate_config.command itself — the objective "
            "verifier that goes RED when this instance's work is missing or wrong "
            f"({_LOOP_GATE_REMEDY}). There is deliberately no default: settlement EXECUTES this "
            "command on every tick and it is the only thing that can contradict the "
            "loop's own report, so a substituted default settles every tick "
            "favourable over whatever the loop actually produced."
        ]
    targets = _gate_command_targets(command)
    if targets is None:
        # NOT delegated to _validate_gate: the draft exemption returns before it
        # and neither DAL activation belt calls it, so `echo ok` reached ACTIVE
        # through disabled-then-enable. An unrecognised verifier is a fact about
        # the candidate at both ends — the executor refuses it too.
        return [
            f"gate_config.command {command!r} is not a recognized objective verifier, so "
            "nothing can execute it and this loop would settle on its own report "
            "(pytest, ruff check, mypy, pyright or git diff --check, with explicit "
            f"repository-relative targets — {_LOOP_GATE_REMEDY})."
        ]
    if not targets:
        return [
            f"gate_config.command {command!r} declares no targets, so it says nothing "
            "about this loop — the gate executor refuses a targetless command on every "
            f"tick as well ({_LOOP_GATE_REMEDY})."
        ]

    root = "/".join(_LOOP_MECHANISM_GATE_PARTS)
    verdicts: dict[str, list[str]] = {"machinery": [], "blanket": []}
    for target in targets:
        verdict = loop_gate_target_verdict(_real_target_parts(target))
        if verdict is not None and target not in verdicts[verdict]:
            verdicts[verdict].append(target)

    errors: list[str] = []
    if verdicts["machinery"]:
        errors.append(
            f"gate_config.command {command!r} grades the loop machinery, not this "
            f"loop's work: {', '.join(sorted(verdicts['machinery']))} resolves inside "
            f"{root}/, which exercises the scheduler→worker mechanism identical for "
            "every loop instance and passes on every tick whatever the instance "
            "produced (it was the seeded default until 2026-08-02, so a loop "
            "registered without a gate settled favourable forever). Declare a verifier "
            f"that fails when THIS loop's work is missing ({_LOOP_GATE_REMEDY})."
        )
    if verdicts["blanket"]:
        errors.append(
            f"gate_config.command {command!r} is a blanket gate: "
            f"{', '.join(sorted(verdicts['blanket']))} contains the loop machinery "
            f"({root}/) and sweeps unrelated suites, so its verdict is uncorrelated "
            "with what this instance did — green whenever the tree is green, red "
            "whenever anything anywhere is red. Name the suite that proves THIS "
            f"loop's work ({_LOOP_GATE_REMEDY})."
        )
    return errors


def _validate_hard_cap(hard_cap_type: Any, hard_cap_value: Any, errors: list[str]) -> None:
    """The second, independent stop-condition: a ceiling the gate can't override."""
    if hard_cap_type not in HARD_CAP_TYPES:
        errors.append(
            f"hard_cap_type must be one of {sorted(HARD_CAP_TYPES)} "
            "(a routine must declare a hard stop-condition beyond the gate)"
        )
        return
    if not isinstance(hard_cap_value, int | float) or isinstance(hard_cap_value, bool):
        errors.append("hard_cap_value must be a number")
        return
    if hard_cap_value <= 0:
        errors.append("hard_cap_value must be greater than zero")
        return
    if hard_cap_type in _INTEGER_HARD_CAPS and hard_cap_value != int(hard_cap_value):
        errors.append(f"hard_cap_value must be a whole number for hard_cap_type={hard_cap_type}")


def _validate_notification_target(config: Any, errors: list[str]) -> None:
    if not isinstance(config, dict):
        errors.append("notification_target must be an object")
        return
    channel = config.get("channel")
    if channel not in NOTIFICATION_CHANNELS:
        errors.append(
            f"notification_target.channel must be one of {sorted(NOTIFICATION_CHANNELS)} "
            "(every routine must declare a notification target)"
        )
        return
    if channel in ("email", "webhook") and not _nonempty_str(config.get("target")):
        errors.append(f"notification_target.target is required for channel={channel}")


def _validate_scope_purpose(payload: dict[str, Any], errors: list[str]) -> None:
    """Optional advisory metadata. Scope is a closed set; purpose is free text."""
    scope = payload.get("scope")
    if scope is not None and scope != "":
        if not isinstance(scope, str) or scope not in SCOPE_VALUES:
            errors.append(f"scope must be one of {sorted(SCOPE_VALUES)} (or null for untagged)")
    purpose = payload.get("purpose")
    if purpose is not None and purpose != "":
        if not isinstance(purpose, str):
            errors.append("purpose must be a string (or null)")


def validate_routine(payload: dict[str, Any]) -> None:
    """Validate a routine create/update payload.

    Raises :class:`RoutineValidationError` (with every violation, not just the
    first) when the routine is missing its trigger, task template, objective
    gate, hard stop-condition, or notification target — or when a declared
    field's config doesn't match its declared type.

    **Loop rows (2026-08-02):** a routine whose ``task_template.input.module``
    names the loop runtime must additionally declare a gate that can go RED on
    its own work — see :func:`loop_gate_errors`. The rule is enforced HERE,
    once, rather than in ``RoutinesStore.create_routine``, so that every write
    path (HTTP create/update, the DAL, a future importer) reaches the same
    definition; a second copy in the store is how a weaker rule becomes the
    real one.

    It is checked BEFORE the draft exemption and independently of ``status``,
    because it is a rule about the row's SHAPE and not about its lifecycle: a
    payload that says "my work is one tick of the loop runtime" and cannot say
    how that tick would be judged is malformed while it is a draft too. Keying
    it on status was a real escape — seed the gateless loop disabled, then flip
    it active through a path that does not re-validate (``set_status``), and the
    rule never ran (counterfeit ``cf-loop-gate-draft-exemption-escape``). A
    draft that has not chosen its work yet is untouched: no ``task_template``
    means no loop module means no rule.

    **Draft exemption (V2.2 / LOOPS1-E2):** keyed strictly on the row's
    effective ``status``. ``status='disabled'`` rows may omit template /
    trigger / gate / cap / notify (COMPOSER drafts, Template Accept). Any
    other status — including ``active`` and ``auto_paused``, and rows that
    *become* active via update — still require ALL engine fields. The
    exemption is status-keyed only; anything else is a counterfeit.
    """
    errors: list[str] = []

    if not _nonempty_str(payload.get("name")):
        errors.append("name is required")

    _validate_scope_purpose(payload, errors)
    errors.extend(
        loop_gate_errors(
            payload.get("task_template"), payload.get("gate_type"), payload.get("gate_config")
        )
    )

    # Status-keyed draft exemption: only exact 'disabled' may omit engine fields.
    if payload.get("status") == "disabled":
        if errors:
            raise RoutineValidationError(errors)
        return

    task_template = payload.get("task_template")
    if not isinstance(task_template, dict) or not task_template:
        errors.append("task_template is required (a routine must declare what work it does)")

    _validate_trigger(payload.get("trigger_type"), payload.get("trigger_config"), errors)
    _validate_gate(payload.get("gate_type"), payload.get("gate_config"), errors)
    _validate_hard_cap(payload.get("hard_cap_type"), payload.get("hard_cap_value"), errors)
    _validate_notification_target(payload.get("notification_target"), errors)

    if errors:
        raise RoutineValidationError(errors)


# Auto-pause: an operator should never come back to find a routine has spent
# its whole budget on changes nobody wanted. Require a minimum sample so one
# unlucky first run doesn't trip it, but pause fast once the pattern is clear.
AUTO_PAUSE_MIN_RUNS = 3
AUTO_PAUSE_ACCEPTANCE_FLOOR = 0.5

# Absence of evidence is not evidence of rejection. These stop_reasons mean the
# gate never rendered a verdict — no configured workspace, no trusted evidence
# path for the gate type — so they report the gate's availability, not the
# quality of what the routine produced. ``settle_routine_run`` writes those
# settlements with gate_passed=NULL, which already keeps them out of the floor;
# this set is the floor's own backstop for rows that carry the reason with a
# non-NULL gate_passed (rows written before that convention, or by any caller
# that settles False). It deliberately excludes ``gate_refused``: a gate that
# ran and refused DID rule on the run and still counts as a rejection.
UNGATEABLE_STOP_REASONS = frozenset(
    {
        "gate_evidence_unavailable",
        "gate_unconfigured",
        "gate_unverifiable",  # pre-a6c0cc7e name for gate_unconfigured
    }
)


# ---------------------------------------------------------------------------
# The run-outcome taxonomy. THREE values, never two.
#
# House doctrine, one layer down, already says a non-result is neither success
# nor failure: ``produce_gate_evidence`` returns ``status="unavailable"`` and
# ``routines_settle`` writes NULL/NULL so evidence-absence leaves the floor's
# denominator untouched. This is the SAME doctrine at the loop-acceptance layer.
#
# It has to be three-valued in both directions, because this repo has been
# burned by both collapses:
#
#   * FAVOURABLE-collapse (the defect this taxonomy fixes): a loop that parks or
#     idles every tick forever scored 100% acceptance, so it never tripped the
#     floor and a dead loop was indistinguishable from a working one
#     (rtn_1e5567b9f3314a2c9d76, 2 runs / 2 "accepted" / healed nothing).
#   * ADVERSE-collapse: scoring a non-result as a rejection auto-paused this
#     repo's routines four separate times on 2026-07-31 (record/settle rollup
#     94e34b23, fire-time floor 1a4226a9, pulse settled-definition divergence,
#     no-gate settlement a6c0cc7e).
#
# NEUTRAL is the only safe home for a non-result: excluded from the acceptance
# denominator entirely, so it can neither inflate nor depress the rate.
# ---------------------------------------------------------------------------

#: The tick produced the result it exists to produce.
OUTCOME_FAVOURABLE = "favourable"

#: The tick BEHAVED but produced no result the system can be judged on — it is
#: waiting on a human, or there was nothing to do. Excluded from the acceptance
#: denominator. Never counted as success, never counted as failure.
OUTCOME_NEUTRAL = "neutral"

#: A non-result the SYSTEM caused and can act on (execution error, policy
#: refusal, a dead credential). Counts against the floor.
OUTCOME_ADVERSE = "adverse"

OUTCOME_CLASSES = frozenset({OUTCOME_FAVOURABLE, OUTCOME_NEUTRAL, OUTCOME_ADVERSE})

#: Machine-readable ``routine_runs.stop_reason`` codes that name a NEUTRAL
#: outcome, one code per distinguishable cause. Prose in ``notes`` is not
#: queryable; these are. The distinctions matter operationally:
#:
#:   ``loop_parked_awaiting_human`` — the loop is behaving correctly and a human
#:       owes it a decision. Chasing the human is the remedy.
#:   ``loop_idle_no_work``          — there was genuinely nothing to do. No
#:       remedy; but N of these in a row is what a /today loops card should
#:       surface, because "nothing to do, forever" is usually a lie told by a
#:       broken input.
#:   ``curator_disabled``           — the selfimprove-curator builtin is gated
#:       off by ``OMNIAGENTOS_CURATOR_ENABLED`` (default off). A tick behaved
#:       correctly by declining to run; it is an operator's OWN feature flag,
#:       not something the routine did wrong (2026-08-14 xcrit F1: settlement
#:       reconstructs the claim from THIS set, not from
#:       ``BuiltinResult.outcome_class`` — an unrecognised reason here falls
#:       through to adverse and would trip auto-pause on every disabled tick).
#:   ``improve_mode_off``           — the improve-lane dispatcher is gated off
#:       by improve-mode (default off, human-approved). Same shape as
#:       ``curator_disabled``: an operator flag, not a failure.
#:
#: A cause the system OWNS is deliberately NOT here — see
#: :data:`ADVERSE_STOP_REASONS`. A dead credential renders as
#: ``loop_blocked``, is adverse, and trips the floor, because unlike a human's
#: pending decision it is something the system can and must act on.
NEUTRAL_STOP_REASONS = frozenset(
    {
        "loop_parked_awaiting_human",
        "loop_idle_no_work",
        "curator_disabled",
        "improve_mode_off",
    }
)

#: Machine-readable stop_reason codes that name an ADVERSE outcome. Listed for
#: symmetry and queryability; the floor does not need the set (a row with
#: ``accepted=0`` already counts against it), but an operator reading
#: ``routine_runs`` does.
ADVERSE_STOP_REASONS = frozenset(
    {
        "builtin_failed",
        "fire_failed",
        "gate_refused",
        "loop_aborted",
        "loop_blocked",
        "loop_failed",
        "loop_status_unrecognized",
    }
)


#: The ``routine_runs`` column that carries a built-in tick's OWN account of
#: itself from the firing tick to settlement (migration 104). Named here because
#: three modules must agree on it: the write path (:mod:`.store`), the firing
#: tick (:mod:`.routines_tick`) and the settler (:mod:`.routines_settle`).
SELF_REPORT_COLUMN = "self_reported_status"


def self_reported_outcome(stop_reason: Any) -> str:
    """The outcome class a built-in tick CLAIMED for itself, from its reason code.

    This is the claim, never the verdict. It exists because the two must be
    composed at settlement and therefore have to survive the trip there
    separately: ``gate_passed`` is reserved for what an EXECUTED gate found, so
    the executor's own account travels in ``stop_reason`` (machine-readable, one
    code per distinguishable cause) plus
    :data:`SELF_REPORT_COLUMN` (the verbatim status, for audit).

    No new authority is created here. The reason code was produced by
    :meth:`~omniagentos.scheduler.builtin_jobs.BuiltinResult.stop_reason`, which
    is the same call that produced the outcome class — this function only reads
    it back out of the row, using the same taxonomy sets every other consumer
    reads (:data:`NEUTRAL_STOP_REASONS`, :data:`ADVERSE_STOP_REASONS`).

    An unrecognised reason is ADVERSE, deliberately and for the same argument
    as :func:`~omniagentos.scheduler.loop_jobs.classify_loop_status`: a producer
    that starts emitting a code this layer has never heard of must not thereby
    stop being judged. Favourable is the ONE class that requires a positive
    signal — the empty reason every favourable built-in tick writes.
    """
    reason = str(stop_reason or "")
    if reason == "":
        return OUTCOME_FAVOURABLE
    if reason in NEUTRAL_STOP_REASONS or reason in UNGATEABLE_STOP_REASONS:
        return OUTCOME_NEUTRAL
    return OUTCOME_ADVERSE


def classify_run_outcome(
    *,
    gate_passed: Any,
    accepted: Any,
    stop_reason: Any,
    finished_at: Any,
) -> str | None:
    """Three-valued outcome of one ``routine_runs`` row.

    Returns one of :data:`OUTCOME_FAVOURABLE` / :data:`OUTCOME_NEUTRAL` /
    :data:`OUTCOME_ADVERSE`, or ``None`` for a run that has not been judged yet
    (still pending — ``finished_at`` is NULL). ``None`` is NOT neutral: a
    pending run may still become any of the three, and conflating "not yet"
    with "never will be" is how the persisted counters were poisoned before.

    Pure and DB-free so both the write path (``RoutinesStore``) and any reader
    can classify a row identically. Deriving it here rather than trusting a
    caller's boolean is what makes a legacy row (written before the
    ``outcome_class`` column existed) classify the same way a new one does.
    """
    if finished_at is None or finished_at == "":
        return None
    reason = str(stop_reason or "")
    if reason in NEUTRAL_STOP_REASONS or reason in UNGATEABLE_STOP_REASONS:
        return OUTCOME_NEUTRAL
    if gate_passed is None and accepted is None:
        # A settled run the gate rendered no verdict on. The settlement layer
        # writes exactly this shape for evidence-absence; do not reinterpret it.
        return OUTCOME_NEUTRAL
    if accepted:
        return OUTCOME_FAVOURABLE
    return OUTCOME_ADVERSE


def _judged_runs(total_runs: int, neutral_runs: int) -> int:
    """The acceptance DENOMINATOR: runs that produced a judgeable result.

    ``total_runs`` counts firings (it is what the ``max_iterations`` hard cap
    reads and must keep counting every tick). Neutral runs are subtracted here
    and nowhere else, so a loop that parks or idles forever has a denominator
    of zero rather than a denominator full of non-results.
    """
    return max(int(total_runs) - int(neutral_runs), 0)


def acceptance_rate(total_runs: int, accepted_runs: int, neutral_runs: int = 0) -> float | None:
    """Persisted ``routines.acceptance_rate``, or ``None`` when UNKNOWN.

    An empty denominator means "this routine has produced no judgeable result
    yet", which is not 0%. Returning 0.0 there would be a lie with teeth: the
    fire-time floor reads this same ratio, so an all-parked routine would
    auto-pause itself for behaving correctly — the exact 2026-07-31 defect.
    NULL/None is the honest answer and every consumer already handles it
    (``migrations/015_routines.sql`` declares the column nullable, the pulse
    aggregator emits None for an empty window, the API serialises it as null).
    """
    denominator = _judged_runs(total_runs, neutral_runs)
    if denominator <= 0:
        return None
    return int(accepted_runs) / denominator


def should_auto_pause(total_runs: int, accepted_runs: int, neutral_runs: int = 0) -> bool:
    """True when a routine's acceptance rate has fallen below the floor.

    Requires at least :data:`AUTO_PAUSE_MIN_RUNS` JUDGED runs so a single early
    failure can't pause a routine before it has a track record — and so a
    routine whose whole history is non-results is never paused at all. Neutral
    runs leave both the numerator and the denominator alone.
    """
    denominator = _judged_runs(total_runs, neutral_runs)
    if denominator < AUTO_PAUSE_MIN_RUNS:
        return False
    return (accepted_runs / denominator) < AUTO_PAUSE_ACCEPTANCE_FLOOR


def should_auto_pause_on_settled(settled_runs: int, settled_accepted: int) -> bool:
    """True when acceptance rate of SETTLED runs has fallen below the floor.

    Unlike should_auto_pause, this function only considers SETTLED runs
    (where gate_passed is not None). If there are fewer than MIN_RUNS
    settled runs, returns False — pending runs are never counted as rejections.

    Args:
        settled_runs: Count of runs that have been settled (gate_passed != None)
        settled_accepted: Count of settled runs that were accepted

    Returns:
        True only if settled_runs >= MIN_RUNS and acceptance_rate < FLOOR
    """
    if settled_runs < AUTO_PAUSE_MIN_RUNS:
        return False
    return (settled_accepted / settled_runs) < AUTO_PAUSE_ACCEPTANCE_FLOOR


# ---------------------------------------------------------------------------
# Firing: DUE-checking + the two independent stop-conditions, kept pure/DB-free
# so they're cheaply unit-testable. omniagentos.scheduler.routines_tick is the
# only caller that touches the database or actually instantiates a task/run.
# ---------------------------------------------------------------------------

# max_iterations/human_checkpoint are both "a whole-number count of fired
# runs" caps (validate_routine groups them the same way in _INTEGER_HARD_CAPS)
# — a human_checkpoint routine must stop auto-firing once it has fired
# hard_cap_value times and wait for a human to look at it and re-enable it,
# same mechanics as max_iterations, just a different operator-facing reason.
_ITERATION_HARD_CAPS = frozenset({"max_iterations", "human_checkpoint"})

# crontab(5) day-of-week: Sunday is 0, and 7 is an accepted second spelling of
# it. Callers pass the canonical 0; the alias is only ever offered to a field
# on Sunday's behalf, so no other day can be matched through it.
_DOW_SUNDAY = 0
_DOW_SUNDAY_ALIAS = 7


def _cron_field_matches(field: str, value: int, low: int, high: int, *, dow: bool = False) -> bool:
    """True when *value* satisfies one comma-separated cron field.

    Supports the common subset every routine's ``trigger_config.cron`` may
    use: ``*``, lists (``1,2,3``), ranges (``1-5``), and steps (``*/5``,
    ``1-10/2``).

    ``dow=True`` marks the day-of-week field, where crontab(5) allows ``7`` as
    a second spelling of Sunday. Sunday is therefore matched by asking the
    field about BOTH of its numbers — the caller's canonical ``0`` and the
    alias ``7`` — rather than by rewriting the field's text. Text rewriting is
    what this replaced, and it corrupted range endpoints: ``6-7`` became
    ``6-0``, an interval no value satisfies, so a weekend routine matched
    nothing at all. Asking twice leaves every already-correct shape alone
    (a bare ``7`` still means Sunday; ``1-5`` still excludes it) because ``7``
    is only ever offered for the one value it can legally mean.
    """
    if dow and value == _DOW_SUNDAY and _cron_field_matches(field, _DOW_SUNDAY_ALIAS, low, high):
        return True
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                continue
            if step <= 0:
                continue
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            try:
                start, end = int(start_str), int(end_str)
            except ValueError:
                continue
        else:
            try:
                start = end = int(part)
            except ValueError:
                continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def _minute_key(iso_ts: str) -> str:
    """Truncate a canonical ``utc_now_iso()``-style timestamp to the minute."""
    return iso_ts[:16]


def cron_is_due(
    cron_expr: str,
    now: datetime,
    last_fired: str | None,
    *,
    schedule_started: str | None = None,
) -> bool:
    """True when *cron_expr* matches *now* or a missed minute since *last_fired*.

    Exact-minute matching alone drops fires when the 5-minute tick wakes after
    the scheduled minute. Within an eight-day bound, any unmatched minute after
    *last_fired* (exclusive), or from *schedule_started* for a never-fired
    routine, through *now* (inclusive) counts as due once. The bound covers
    weekly schedules without an unbounded historical scan.

    A 6-field crontab (seconds first) is accepted by :func:`validate_routine`
    for forward compatibility, but the tick runs on minute boundaries, so the
    leading seconds field is dropped here rather than matched.
    """
    fields = cron_expr.split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    def matches_time(t: datetime) -> bool:
        cron_dow = t.isoweekday() % 7
        return (
            _cron_field_matches(minute_f, t.minute, 0, 59)
            and _cron_field_matches(hour_f, t.hour, 0, 23)
            and _cron_field_matches(dom_f, t.day, 1, 31)
            and _cron_field_matches(month_f, t.month, 1, 12)
            and _cron_field_matches(dow_f, cron_dow, 0, 6, dow=True)
        )

    reference = last_fired or schedule_started
    if reference is None:
        return matches_time(now)

    if last_fired is not None and _minute_key(last_fired) == now.strftime("%Y-%m-%dT%H:%M"):
        return False
    # Exact-minute behavior is authoritative even for callers injecting a
    # historical clock earlier than the stored creation timestamp. In normal
    # scheduler operation creation is never in the future; schedule_started is
    # used only to bound missed-fire backfill.
    if matches_time(now):
        return True

    try:
        reference_dt = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return matches_time(now)

    # Normalize naive/aware mix so catch-up never TypeErrors at the boundary.
    if reference_dt.tzinfo is None and now.tzinfo is not None:
        reference_dt = reference_dt.replace(tzinfo=now.tzinfo)
    elif reference_dt.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=reference_dt.tzinfo)

    try:
        delta = now - reference_dt
    except TypeError:
        return matches_time(now)

    end = now.replace(second=0, microsecond=0)
    if delta < timedelta(0):
        return False
    earliest = end - timedelta(minutes=_CRON_CATCHUP_MINUTES - 1)
    if last_fired is None:
        # A never-fired routine is eligible from its creation minute.  This
        # closes the permanent miss when a five-minute tick is consistently a
        # few minutes offset from a daily/weekly cron minute.
        after_reference = reference_dt.replace(second=0, microsecond=0)
    else:
        after_reference = (reference_dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
    floor = max(earliest, after_reference)
    curr = end
    while curr >= floor:
        if matches_time(curr):
            return True
        curr -= timedelta(minutes=1)
    return False


def compute_next_run(
    routine: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    """Next fire timestamp for a cron routine, or ``None`` for event/invalid.

    Reuses the same field matcher as :func:`cron_is_due` (:func:`_cron_field_matches`)
    and scans *forward* from *now* (inclusive of the current minute when it
    matches). Never derived from ``last_fired`` + interval — that is the
    ``cf-next-run-from-last-fired`` counterfeit. Returns a canonical
    ``YYYY-MM-DDTHH:MM:00Z`` string in UTC, or ``None`` when the trigger is
    not cron or no match falls inside the forward search bound
    (:data:`_CRON_NEXT_RUN_MINUTES`, 400 days — not the eight-day catch-up).
    """
    if routine.get("trigger_type") != "cron":
        return None
    trigger_config = routine.get("trigger_config") or {}
    cron_expr = trigger_config.get("cron") if isinstance(trigger_config, dict) else None
    if not _nonempty_str(cron_expr):
        return None

    fields = cron_expr.split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    def matches_time(t: datetime) -> bool:
        cron_dow = t.isoweekday() % 7
        return (
            _cron_field_matches(minute_f, t.minute, 0, 59)
            and _cron_field_matches(hour_f, t.hour, 0, 23)
            and _cron_field_matches(dom_f, t.day, 1, 31)
            and _cron_field_matches(month_f, t.month, 1, 12)
            and _cron_field_matches(dow_f, cron_dow, 0, 6, dow=True)
        )

    # One representation everywhere: compute in UTC from an aware datetime.
    if now is None:
        clock = datetime.now(UTC)
    elif now.tzinfo is None:
        clock = now.replace(tzinfo=UTC)
    else:
        clock = now.astimezone(UTC)
    # Drop sub-minute noise; next_run is minute-granular like the tick.
    cursor = clock.replace(second=0, microsecond=0)
    # If this minute already fired, start at the next minute.
    last_fired = routine.get("last_fired")
    if last_fired is not None and _minute_key(str(last_fired)) == cursor.strftime("%Y-%m-%dT%H:%M"):
        cursor = cursor + timedelta(minutes=1)

    for _ in range(_CRON_NEXT_RUN_MINUTES):
        if matches_time(cursor):
            # Always emit canonical Z-suffixed UTC (never naive-local with a Z).
            return cursor.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")
        cursor = cursor + timedelta(minutes=1)
    return None


def event_is_due(latest_event_ts: str | None, last_fired: str | None) -> bool:
    """True when a matching event exists that hasn't already triggered a fire.

    ``latest_event_ts`` is the newest recorded timestamp for the routine's
    declared event type (``None`` if that event type has never been seen).
    Canonical timestamps (:func:`omniagentos.contracts.utc_now_iso`) are
    lexicographically sortable, so a plain string comparison is correct.
    """
    if not latest_event_ts:
        return False
    if not last_fired:
        return True
    return latest_event_ts > last_fired


def hard_cap_hit(
    hard_cap_type: Any, hard_cap_value: Any, *, total_runs: int, total_cost_usd: float | None
) -> bool:
    """True when a routine has reached its declared hard stop-condition.

    This is the SECOND, independent check on top of the objective gate — it
    fires purely off the routine's own rollup counters, never off whether the
    gate itself looks broken, so a gate that always reports success still
    can't spin the routine forever or past its budget.

    ISSUE-8 safety fix: ``total_cost_usd`` can now be ``None`` (migration 119
    — at least one contributing run's cost was never reported, so the rollup
    cannot be trusted). For a ``budget_usd`` cap, unknown counts as CAP
    EXCEEDED — failing closed — never as an exact ``$0`` that could never
    trip anything. Iteration-style caps never read cost at all, so an
    unknown total is irrelevant to them.
    """
    if hard_cap_type in _ITERATION_HARD_CAPS:
        return total_runs >= hard_cap_value
    if hard_cap_type == "budget_usd":
        return total_cost_usd is None or total_cost_usd >= hard_cap_value
    return False


def should_fire(
    routine: dict[str, Any],
    *,
    now: datetime,
    latest_event_ts: str | None = None,
    settled_runs: int | None = None,
    settled_accepted: int | None = None,
) -> tuple[bool, str]:
    """Decide whether *routine* must fire right now.

    Returns ``(True, "")`` when it should fire, or ``(False, reason)`` when
    it must be skipped. Checked in order: routine lifecycle status (paused /
    disabled / already auto_paused never fires), the hard stop-condition
    (max_iterations / budget_usd / human_checkpoint), the auto-pause
    acceptance floor (checked directly off the rollup counters too, as
    defense in depth alongside the ``status`` check — ``record_run`` already
    flips status to ``auto_paused`` when this trips, but a routine's rollups
    and status should never be allowed to silently disagree about whether
    it's safe to fire), and finally whether the declared trigger is due.

    Pure and DB-free: *routine* is a plain dict (as returned by
    :class:`omniagentos.scheduler.store.RoutinesStore`) and *latest_event_ts*
    is supplied by the caller (the tick entrypoint queries it for
    event-triggered routines only).
    """
    status = routine.get("status")
    if status != "active":
        return False, f"status={status!r} (not active)"

    total_runs = int(routine.get("total_runs") or 0)
    accepted_runs = int(routine.get("accepted_runs") or 0)
    # Non-results are excluded from the acceptance denominator, so the fallback
    # below cannot punish a routine for parking or idling. Missing column (a DB
    # older than migration 103) reads 0, which is exactly the pre-taxonomy
    # behaviour rather than a new failure mode.
    neutral_runs = int(routine.get("neutral_runs") or 0)
    # NULL (migration 119) means unknown, never a manufactured $0 — see
    # hard_cap_hit for why a budget_usd cap fails closed on it below.
    _total_cost_raw = routine.get("total_cost_usd")
    total_cost_usd: float | None = None if _total_cost_raw is None else float(_total_cost_raw)

    hard_cap_type = routine.get("hard_cap_type")
    hard_cap_value = routine.get("hard_cap_value")
    if hard_cap_type is not None and hard_cap_value is not None:
        if hard_cap_hit(
            hard_cap_type,
            float(hard_cap_value),
            total_runs=total_runs,
            total_cost_usd=total_cost_usd,
        ):
            if hard_cap_type == "budget_usd" and total_cost_usd is None:
                return False, (
                    f"hard stop-condition unverifiable: budget_usd cap={hard_cap_value} but "
                    "total_cost_usd is unknown (a contributing run's cost was never reported) "
                    "— failing closed rather than assuming it is within budget"
                )
            cost_display = "unknown" if total_cost_usd is None else total_cost_usd
            return False, (
                f"hard stop-condition reached: {hard_cap_type}={hard_cap_value} "
                f"(total_runs={total_runs}, total_cost_usd={cost_display})"
            )

    # Use settled-only counts if provided (from tick path), else fall back to persisted
    # (persisted counts may be poisoned by pending runs, but settled counts are accurate)
    if settled_runs is not None and settled_accepted is not None:
        if should_auto_pause_on_settled(settled_runs, settled_accepted):
            return False, "acceptance rate below the 50% auto-pause floor"
    else:
        # Fallback to persisted counters (for tests/callers without DB access)
        if should_auto_pause(total_runs, accepted_runs, neutral_runs):
            return False, "acceptance rate below the 50% auto-pause floor"

    trigger_type = routine.get("trigger_type")
    trigger_config = routine.get("trigger_config") or {}
    last_fired = routine.get("last_fired")
    if trigger_type == "cron":
        cron = trigger_config.get("cron")
        if not _nonempty_str(cron) or not cron_is_due(
            cron,
            now,
            last_fired,
            schedule_started=routine.get("created_at"),
        ):
            return False, "cron trigger not due"
    elif trigger_type == "event":
        if not event_is_due(latest_event_ts, last_fired):
            return False, "no new matching event since the last fire"
    else:
        return False, f"unrecognized trigger_type={trigger_type!r}"

    return True, ""


# ---------------------------------------------------------------------------
# Built-in routine: the improvement-lane dispatcher tick.
# ---------------------------------------------------------------------------

IMPROVE_DISPATCHER_TICK_SECONDS = 300
IMPROVE_DISPATCHER_ROUTINE_NAME = "improve-lane-dispatcher"
IMPROVE_DISPATCHER_CRON = "*/5 * * * *"  # == 300s, on the existing cron trigger


def improve_dispatcher_routine() -> dict[str, Any]:
    """A fresh, VALID payload for the 300-second improvement-lane dispatcher tick."""
    return {
        "name": IMPROVE_DISPATCHER_ROUTINE_NAME,
        "description": "Improvement-lane dispatcher tick.",
        "trigger_type": "cron",
        "trigger_config": {"cron": IMPROVE_DISPATCHER_CRON},
        "task_template": {
            "title": "Improvement-lane dispatcher tick",
            "harness": "mock",
            "input": {"module": "omniagentos.improve.dispatcher"},
        },
        "gate_type": "test_command",
        "gate_config": {
            "command": "pytest tests/improve",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 25.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


# ---------------------------------------------------------------------------
# Lab job drain (lane 11). /api/lab/run enqueues and returns 202; without a
# drain the queue is durable andnever advances — jobs sit enqueued forever.
# ---------------------------------------------------------------------------

LAB_JOBS_ROUTINE_NAME = "lab-jobs-drain"
LAB_JOBS_CRON = "*/5 * * * *"  # 300s, same trigger family as the dispatcher


def lab_jobs_drain_routine() -> dict[str, Any]:
    """A fresh, VALID payload for the lab-job drain tick."""
    return {
        "name": LAB_JOBS_ROUTINE_NAME,
        "description": "Claim and run one queued lab job under an owner lease.",
        "trigger_type": "cron",
        "trigger_config": {"cron": LAB_JOBS_CRON},
        "task_template": {
            "title": "Lab job drain tick",
            "harness": "mock",
            "input": {"module": "omniagentos.lab.jobs"},
        },
        "gate_type": "test_command",
        "gate_config": {
            "command": "pytest tests/lab/test_jobs.py",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 25.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


# ---------------------------------------------------------------------------
# Memlife dream cycle (lane L4). Nightly consolidation of episodic JSONL
# into staged candidates. Seeded on API startup alongside improve-dispatcher.
# ---------------------------------------------------------------------------

DREAM_CYCLE_ROUTINE_NAME = "memlife-dream-cycle"
DREAM_CYCLE_CRON = "0 3 * * *"  # nightly 03:00 UTC


def dream_cycle_routine() -> dict[str, Any]:
    """A fresh, VALID payload for the nightly memlife dream-cycle tick."""
    return {
        "name": DREAM_CYCLE_ROUTINE_NAME,
        "description": "Nightly memlife dream cycle — consolidate episodic memory.",
        "trigger_type": "cron",
        "trigger_config": {"cron": DREAM_CYCLE_CRON},
        "task_template": {
            "title": "Memlife dream cycle",
            "harness": "mock",
            "input": {"module": "omniagentos.memlife.dream"},
        },
        "gate_type": "test_command",
        "gate_config": {
            "command": "pytest tests/memlife/test_dream.py",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 10.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


# ---------------------------------------------------------------------------
# W3 health monitor & self-heal loop. Detects component failures via
# health-sentinel snapshots and auto-repairs allowlisted components via
# launchctl kickstart. Unknown remedies escalate to human approval (T3 tier).
# Seeded on API startup to enable autonomous fleet recovery.
# ---------------------------------------------------------------------------

W3_HEALTH_MONITOR_ROUTINE_NAME = "w3-health-monitor"
W3_HEALTH_MONITOR_CRON = "*/10 * * * *"  # Every 10 minutes


def w3_health_monitor_routine() -> dict[str, Any]:
    """A fresh, VALID payload for the W3 health monitor & self-heal loop."""
    # The W3 loop package is deployed separately from this repository. Its
    # import-light registry is still part of this checkout, so make that tree
    # visible without importing the loops runtime or its LangGraph deps.
    import sys

    loops_root_path = Path(__file__).resolve().parents[2] / "loops"
    if not loops_root_path.is_dir():
        raise ImportError(
            f"loops tree not found at {loops_root_path}; the W3 registry is "
            "not importable from this install layout"
        )
    loops_root = str(loops_root_path)
    if loops_root not in sys.path:
        sys.path.insert(0, loops_root)
    from omniagentos_loops.registry import loop_routine_row  # type: ignore[import-not-found]

    return loop_routine_row(
        name=W3_HEALTH_MONITOR_ROUTINE_NAME,
        template="monitor_diagnose_repair_verify",
        instance_id="w3_health_monitor",
        instance_module="omniagentos_loops.instances.health_monitor",
        cron=W3_HEALTH_MONITOR_CRON,
        description="W3 health monitor & self-heal loop — detect and repair component failures",
        params={
            "instance_id": "w3_health_monitor",
            # Extended allowlist: includes fleet repair labels (reflection, watchdogs, etc.)
            "allowed_remedies": [
                "kickstart_api",
                "kickstart_runner",
                "kickstart_routines",
                "kickstart_health_sentinel",
            ],
        },
        gate_command="pytest loops/tests/instances/test_health_monitor.py",
        timeout_s=600,
    )
