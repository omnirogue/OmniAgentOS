"""Per-ecosystem gate executors behind the same counted-evidence seam.

WHAT THIS CLOSES (audit finding M2, ``mp-gates-python-only``)
-------------------------------------------------------------
Before this module the only trusted verifier was pytest, so a Node, Go or Rust
project could not declare a gate at all: :func:`gate_runner.parse_gate_command`
refused every command that was not ``pytest`` / ``python -m pytest``, and the
routine write path (``routines._is_non_vacuous_gate_command``) refused to store
one.  A multi-project autonomy loop therefore had exactly one ecosystem whose
work could ever be objectively graded.

WHAT "COUNTED EVIDENCE" MEANS, AND WHY AN EXIT CODE IS NOT IT
-------------------------------------------------------------
:func:`gate_evidence.evidence_rejections` does not ask "did the command exit
zero".  It asks for four independently counted integers — collected, passed,
skipped, failed — and then requires ``collected > 0`` (a suite that collected
nothing is a *vacuous pass*), ``failed == 0``, ``deselected == 0`` and
``passed + skipped == collected`` (anything else is a partial execution).  An
executor that cannot produce those four numbers from a trusted machine-readable
stream has produced NO evidence, and this module says so rather than falling
back on the child's exit status.  Every executor here therefore:

* runs the toolchain itself, in the caller's ephemeral run tree, as a process
  group with a hard timeout;
* parses ONLY the output of that one invocation — the stdout it captured from
  the child, or an artifact written into a scratch directory this process minted
  for this invocation and proved empty beforehand.  A machine-readable report
  committed into the candidate tree can never be read, so a fabricated
  ``junit.xml`` on disk is not evidence of anything;
* cross-checks the per-test records against the toolchain's own aggregate line,
  so a truncated, tampered or half-written report is an inconclusive refusal
  instead of a smaller-but-green count.

WHERE THE GRADER COMES FROM
---------------------------
The program is resolved from OPERATOR-controlled configuration only — an
explicit ``OMNIAGENTOS_GATE_{GO,CARGO,VITEST}`` override, else ``PATH`` — and
never from the candidate tree.  ``node_modules/.bin/vitest`` is code the
candidate ships, and the thing being graded may not supply its own grader; that
is the same rule the pytest runner states in its own header ("a routine's own
run output is the thing being graded, so it can never also be the grader").
Practical consequence, stated plainly because it bites: the gate executes in a
fresh ``git worktree``, so gitignored trees like ``node_modules/`` and
``target/`` are ABSENT there.  A vitest gate needs a PATH-resolvable or
env-pinned vitest, and a cargo gate needs a warm registry cache (it runs
``--locked --offline``); when that is not true on this host the gate is
UNAVAILABLE, which is a fact about the host, never a verdict on the candidate.

REFUSAL CLASSES (this is the whole safety argument)
---------------------------------------------------
``GateWorkspaceUnusable`` — toolchain absent or unidentifiable.  A fact about
    the host.  Settles NULL/NULL, stays out of the acceptance floor, cannot
    auto-pause a routine.  Same class as a missing loops venv, for the same
    reason: an operator who has not installed Go must not thereby manufacture
    failures for every Go routine.
``GateExecutionInfraError`` — the toolchain ran but did not produce a parseable,
    self-consistent report; or it hit the timeout.  This is the INCONCLUSIVE
    bucket: no counted evidence exists, so no verdict is minted.  It is
    deliberately not a silent pass and deliberately not a hang.
``GateEvidenceRefusal`` — a fact about the CANDIDATE, which SETTLES the gate as
    failed: an unknown ecosystem string, a command that is not this ecosystem's
    verifier shape, a suite that collected zero tests, a candidate that does not
    compile, a package whose failure happened outside its tests, a Cargo target
    that opts out of libtest, or a vitest config that is not the one the
    operator pinned.  The dividing line is not severity, it is authorship: if
    the candidate could fix it by changing its own code, it is a refusal, not an
    inconclusive.

Nothing here widens the Python path.  A gate with no ``ecosystem`` key is
``python`` and behaves exactly as it did before this module existed, and a
``merge_candidate`` gate may not name any ecosystem but ``python`` at all
(``gate_evidence.MERGE_GATE_TOOL``) — that receipt authorizes merging THIS
repository and is minted under a fixed identity, so another language's tests
must never be able to produce one.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from omniagentos.contracts import digest
from omniagentos.harnesses.release_gate import normalize_executable
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scheduler.gate_evidence import (
    GateEvidenceRefusal,
    GateExecutionInfraError,
    GateWorkspaceUnusable,
)

ECOSYSTEM_PYTHON = "python"
ECOSYSTEM_NPM = "npm"
ECOSYSTEM_GO = "go"
ECOSYSTEM_CARGO = "cargo"

#: The complete set a gate config may name.  Anything else — including the empty
#: string, a non-string, or a plausible-looking near miss such as ``"nodejs"`` —
#: is a refusal, never a fallback to Python.  Failing open on an unknown
#: ecosystem would let a typo silently grade a Go project with pytest.
SUPPORTED_ECOSYSTEMS = frozenset({ECOSYSTEM_PYTHON, ECOSYSTEM_NPM, ECOSYSTEM_GO, ECOSYSTEM_CARGO})

#: ``evidence.tool`` each ecosystem is allowed to stamp.  Used at the decision
#: point to bind the adjudicated config to the executor that actually ran.
TOOL_FOR_ECOSYSTEM = {
    ECOSYSTEM_PYTHON: "pytest",
    ECOSYSTEM_NPM: "vitest",
    ECOSYSTEM_GO: "go test",
    ECOSYSTEM_CARGO: "cargo test",
}

#: Shared with ``routines._is_non_vacuous_gate_command``.  A denylist is not
#: sufficient here either: ``go test ./x && echo ok`` must not be storable.
SHELL_CONTROL_RE = re.compile(r"(?:&&|\|\||[;&|<>`$(){}]|[\r\n])")

_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: Extensions Vite/Vitest will load a config module from.
_VITEST_CONFIG_EXTENSIONS = ("ts", "mts", "cts", "js", "mjs", "cjs")
#: Config basenames, in Vitest's own resolution order: a ``vitest.config.*``
#: wins over a ``vite.config.*``.
_VITEST_CONFIG_BASENAMES = ("vitest.config", "vite.config")
#: Workspace/projects basenames. Vitest v2 resolves BOTH spellings, and either
#: can redirect the whole run to a different set of projects — a deselect with
#: extra steps — so both are pinned.
_VITEST_WORKSPACE_BASENAMES = ("vitest.workspace", "vitest.projects")
#: ``.json`` is only meaningful for the workspace/projects files.
_VITEST_WORKSPACE_EXTENSIONS = (*_VITEST_CONFIG_EXTENSIONS, "json")

#: Every filename Vitest could load configuration from. Derived from the lists
#: above rather than typed out, because the failure mode this pin exists to stop
#: is a resolvable filename being MISSING from it: any name absent here is a file
#: the candidate may add — changing which tests run — without moving the digest.
#: The order is deterministic and the digest depends on it.
_VITEST_CONFIG_FILENAMES: tuple[str, ...] = tuple(
    [
        f"{basename}.{extension}"
        for basename in _VITEST_CONFIG_BASENAMES
        for extension in _VITEST_CONFIG_EXTENSIONS
    ]
    + [
        f"{basename}.{extension}"
        for basename in _VITEST_WORKSPACE_BASENAMES
        for extension in _VITEST_WORKSPACE_EXTENSIONS
    ]
)


def sanitize_path_env(value: str | None) -> str:
    """A PATH containing only absolute, non-empty directories.

    An empty ``PATH`` component and a literal ``.`` both mean "the current
    working directory", and the gate's current working directory is the
    CANDIDATE'S OWN TREE.  Forwarding either lets a file the candidate commits
    at ``./go`` or ``./vitest`` be resolved as the toolchain by the child (and,
    via ``shutil.which``, by this process).  That is the same
    candidate-supplies-its-own-grader hole the executor closes for the program
    itself, arriving through the environment instead.
    """
    entries = [entry for entry in (value or "").split(os.pathsep) if entry]
    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))


#: How far a report's mtime may sit past the measured process's exit before it
#: is treated as a post-run rewrite. Generous enough for coarse filesystem
#: timestamp granularity and clock jitter, far tighter than the seconds a
#: surviving descendant needs to notice the leader is gone and act.
_ARTIFACT_MTIME_TOLERANCE_NS = 2_000_000_000


def read_artifact_nofollow(
    path: Path,
    *,
    missing_message: str,
    not_modified_after_ns: int | None = None,
) -> bytes:
    """Read a report artifact once, from a descriptor whose identity is proven.

    ``Path.is_file()`` then ``Path.read_bytes()`` is two resolutions of the same
    name and anything still alive can swap the name between them.  This opens
    ONCE with ``O_NOFOLLOW`` — so a symlink planted at the report path fails at
    ``open`` instead of silently delivering whatever it points at — then works
    exclusively from that descriptor.  There is no second path-based access to
    race.

    A symlink here is not a corner case: the executor passes the report path to
    the child on its command line, so the child and every descendant of it knows
    exactly where to plant one, and the obvious use is to aim it at a fabricated
    report committed in the candidate tree.

    THE DESCENDANT THAT ESCAPED THE PROCESS GROUP
    ----------------------------------------------
    Reaping the child's process group kills everything that stayed in it, but a
    descendant that calls ``setsid`` for itself leaves that group and survives.
    It knows the report path for the same reason it knows anything — it can read
    its parent's argv — so it can try to overwrite the report in the window
    between the leader exiting and this read.  Two facts close that:

    * the whole file is read from the already-verified descriptor in one pass,
      and the descriptor is ``fstat``-ed again afterwards.  A rewrite that lands
      DURING the read changes the size, mtime or inode, and is refused rather
      than silently yielding a spliced file;
    * an honest report is written BEFORE the process that wrote it exits, so its
      mtime cannot postdate that exit.  ``not_modified_after_ns`` is the moment
      the measured process was observed to finish; a report modified after that
      was modified by something this seam was not measuring, and is refused.

    Neither makes the window zero, and it is worth being exact about what is
    left rather than rounding it to "closed".  A rewrite is caught if it lands
    DURING the read (the second ``fstat`` sees a changed size, mtime or inode)
    or more than :data:`_ARTIFACT_MTIME_TOLERANCE_NS` after the measured process
    finished (the mtime check).  What still gets through is a rewrite that lands
    in between: after the leader exited, before this ``open``, and inside that
    tolerance.

    Stated as a bound rather than a feeling: a rewrite survives if it lands
    **within the 2 s mtime tolerance of the measured process finishing AND
    before this open**.  Both conditions, and the tolerance is the governing one
    — it is deliberately looser than the gap it guards, because coarse
    filesystem timestamps must not cause false refusals, so it is 2 s and not
    the handful of milliseconds the open usually takes.  The gap binds only when
    it is the shorter of the two, which is the common case (sample a clock,
    probe an empty process group, ``open``) but not the guaranteed one: the
    reap's grace period stretches it whenever the group really had survivors to
    kill.  So the claim is "narrowed from unbounded to at most 2 s, usually
    milliseconds, and only for a descendant that both escaped the process group
    and was already waiting on the path".  Not closed.

    Closing it properly means not using a filesystem path at all — handing the
    child an inherited descriptor and reading the report back through that, so
    there is no name for anything else to write to.  That is a bigger change
    than this seam needed, and it is the right one if this window ever matters.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise GateExecutionInfraError(missing_message) from None
    except OSError as exc:
        # ELOOP on Linux, ELOOP/EMLINK on macOS — the name was a symlink.
        raise GateExecutionInfraError(
            f"the report artifact at {path.name} could not be opened as a regular file "
            f"({exc.strerror}); a link planted at the report path is not evidence"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise GateExecutionInfraError(
                f"the report artifact at {path.name} is not a regular file"
            )
        if before.st_nlink != 1:
            raise GateExecutionInfraError(
                f"the report artifact at {path.name} has {before.st_nlink} hard links; "
                "its content is reachable from outside the directory this run minted"
            )
        if (
            not_modified_after_ns is not None
            and before.st_mtime_ns > not_modified_after_ns + _ARTIFACT_MTIME_TOLERANCE_NS
        ):
            raise GateExecutionInfraError(
                f"the report artifact at {path.name} was modified after the run it is "
                "supposed to describe had already finished; something outside the measured "
                "process wrote it"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_dev,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise GateExecutionInfraError(
                f"the report artifact at {path.name} changed while it was being read"
            )
    finally:
        os.close(fd)
    return b"".join(chunks)


def _first_line_matching(text: str, pattern: re.Pattern[str]) -> str:
    for line in text.splitlines():
        if pattern.search(line):
            return line.strip()[:200]
    return ""


def is_safe_relative_target(arg: str) -> bool:
    """True for an explicit, repository-relative positional target.

    Extracted verbatim from ``routines._is_non_vacuous_gate_command`` so the
    ecosystem grammars enforce the identical rule rather than a lookalike:
    configuration and selection flags can turn a recognized checker into
    collection-only, exit-zero, ignored, or mutating work, so only positional
    paths inside the checkout are accepted.
    """
    if not arg or arg.startswith(("-", "@")):
        return False
    path_part = arg.split("::", 1)[0]
    path = PurePosixPath(path_part)
    return path_part == "." or (not path.is_absolute() and ".." not in path.parts)


def args_are_executing_targets(args: Sequence[str]) -> bool:
    """True when *args* is a non-empty list of safe positional targets."""
    return bool(args) and all(is_safe_relative_target(arg) for arg in args)


def normalize_ecosystem(value: Any) -> str:
    """Return the validated ecosystem name, or refuse.

    ``None`` and a missing key mean Python — the pre-M2 default — because every
    routine written before this existed declared a pytest gate and none of them
    may change meaning.  Every other value must be an exact member of
    :data:`SUPPORTED_ECOSYSTEMS`; there is no case folding, no aliasing and no
    default-on-unknown.
    """
    if value is None:
        return ECOSYSTEM_PYTHON
    if not isinstance(value, str) or value not in SUPPORTED_ECOSYSTEMS:
        raise GateEvidenceRefusal(
            f"gate_config.ecosystem must be one of {sorted(SUPPORTED_ECOSYSTEMS)}, got {value!r}"
        )
    return value


def ecosystem_of_gate_config(gate_config: Mapping[str, Any]) -> str:
    """The validated ecosystem a gate config selects."""
    if not isinstance(gate_config, Mapping):
        raise GateEvidenceRefusal("gate config must be a mapping")
    return normalize_ecosystem(gate_config.get("ecosystem"))


def expected_tool_for_gate_config(gate_config: Mapping[str, Any]) -> str:
    """The one ``evidence.tool`` value this gate config may be graded by."""
    return TOOL_FOR_ECOSYSTEM[ecosystem_of_gate_config(gate_config)]


@dataclass(frozen=True, slots=True)
class CountedOutcome:
    """Four independently counted integers plus the node inventory behind them.

    ``node_ids`` is what makes the count auditable rather than asserted: it is
    the actual identity of every test the report named, and it is what the
    evidence's ``node_inventory_digest`` is taken over.
    """

    collected: int
    passed: int
    skipped: int
    failed: int
    deselected: int
    node_ids: tuple[str, ...]


class EcosystemExecutor:
    """One ecosystem's argv, environment and report parser.

    Subclasses own three decisions and nothing else: which program to run, how
    to spell "run these targets and emit a machine-readable report", and how to
    turn that report into a :class:`CountedOutcome`.  Process containment,
    timeout handling, workspace pinning and evidence assembly stay in
    ``gate_runner`` so that every ecosystem inherits the identical seam.
    """

    ecosystem: str = ""
    tool: str = ""
    #: Program name looked up on PATH when no env override is set.
    program: str = ""
    #: Operator-only override, same trust tier as ``OMNIAGENTOS_GATE_WORKSPACE``.
    program_env_var: str = ""
    #: argv tail that prints a version; must exit 0 and print something.
    version_argv: tuple[str, ...] = ()
    #: Extra child environment on top of the shared allowlist.
    env_overrides: Mapping[str, str] = {}
    #: ``None`` means any number of targets; an int pins the exact count.
    exact_target_count: int | None = None
    #: argv prefix the command must literally start with, e.g. ``("go", "test")``.
    command_prefix: tuple[str, ...] = ()
    #: gate_config key holding this ecosystem's operator-approved config digest.
    #: Empty means the ecosystem's report can prove its own completeness and
    #: needs no pin; see :class:`NpmVitestExecutor` for the one that cannot.
    config_pin_key: str = ""

    def containment_paths(self, targets: tuple[str, ...]) -> tuple[str, ...]:
        """The on-disk paths every target implies, for the containment check.

        Identity for ecosystems whose targets ARE paths.  Overridden where a
        target is a pattern (Go's ``./pkg/...``) so the part that must exist is
        still proven to exist inside the workspace: a pattern must never be a
        hole in containment.
        """
        return targets

    def preflight(self, run_tree: Path, targets: tuple[str, ...]) -> None:
        """Refuse candidate configurations this executor cannot honestly grade.

        Runs against the EPHEMERAL RUN TREE — the exact content about to be
        executed — after containment has been proven and before the toolchain
        starts.  This is where "the candidate is trying to supply its own
        grader" is caught, so anything it raises is a
        :class:`GateEvidenceRefusal`: a fact about the candidate, not the host.
        """
        return None

    def check_config_pin(self, run_tree: Path, gate_config: Mapping[str, Any]) -> None:
        """Refuse unless the candidate's test configuration is operator-approved.

        A no-op for ecosystems whose report can prove its own completeness.
        Overridden where it cannot — see :class:`NpmVitestExecutor`.
        """
        return None

    def parse_command(self, command: str) -> tuple[str, ...]:
        """Validate *command* against this ecosystem's grammar; return targets."""
        if SHELL_CONTROL_RE.search(command):
            raise GateEvidenceRefusal("gate command contains shell control operators")
        try:
            parts = shlex.split(command, posix=True)
        except ValueError as exc:
            raise GateEvidenceRefusal(f"gate command is unparseable: {exc}") from None
        prefix = self.command_prefix
        if tuple(parts[: len(prefix)]) != prefix:
            raise GateEvidenceRefusal(
                f"a {self.ecosystem} gate command must start with {' '.join(prefix)!r}"
            )
        targets = tuple(parts[len(prefix) :])
        if not args_are_executing_targets(targets):
            raise GateEvidenceRefusal(
                f"a {self.ecosystem} gate command must name explicit repository-relative "
                "targets and no flags"
            )
        if self.exact_target_count is not None and len(targets) != self.exact_target_count:
            raise GateEvidenceRefusal(
                f"a {self.ecosystem} gate command takes exactly "
                f"{self.exact_target_count} target(s), got {len(targets)}"
            )
        return targets

    def command_is_valid(self, command: str) -> bool:
        """Bool form of :meth:`parse_command`, for the routine write path."""
        try:
            self.parse_command(command)
        except GateEvidenceRefusal:
            return False
        return True

    def build_argv(
        self, program: str, targets: tuple[str, ...], artifacts_dir: Path
    ) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def count(
        self,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        artifacts_dir: Path,
        finished_at_ns: int | None = None,
    ) -> CountedOutcome:  # pragma: no cover - abstract
        """Count one invocation's report.

        ``finished_at_ns`` is ``time.time_ns()`` sampled the moment the measured
        process was observed to exit. Executors that read an on-disk artifact
        use it to refuse a report modified after the run it claims to describe.
        """
        raise NotImplementedError


class NpmVitestExecutor(EcosystemExecutor):
    """vitest, counted from a JUnit XML this process asked for by absolute path.

    The report path is inside a scratch directory the caller minted for this one
    invocation and proved empty; nothing committed to the candidate tree is ever
    read.  The per-``<testcase>`` tally is then cross-checked against the
    ``<testsuite>`` elements' own ``tests``/``failures``/``skipped`` attributes,
    so a truncated or edited report is inconclusive rather than green.

    THE CONFIG IS OPERATOR-PINNED, BECAUSE THE JUNIT ALONE CANNOT SEE A DESELECT
    ----------------------------------------------------------------------------
    vitest has no equivalent of the certification plugin's collected-vs-executed
    record, so the JUnit report describes only the tests that RAN.  A
    candidate-committed ``vitest.config.ts`` carrying ``test.exclude:
    ['**/failing.test.ts']`` therefore produces a perfectly consistent, entirely
    green report with ``deselected = 0``, and no amount of parsing that report
    can detect the omission — the excluded file leaves no trace in it.

    The fix is not more parsing, it is moving the decision.  An npm gate must
    declare ``gate_config.vitest_config_digest``: the digest of the config the
    OPERATOR approved, recorded outside the candidate tree in the routine row.
    At run time this executor hashes the config files actually resolvable in the
    run tree and refuses on any mismatch, and refuses outright when a
    declaration carries no pin.  A candidate that edits, adds or deletes a
    config file changes the digest and stops gating until a human looks.

    Residual weakness, stated plainly: a pinned config can itself contain
    excludes.  This mechanism does not make exclusion impossible, it makes it an
    OPERATOR's decision recorded in a place the candidate cannot reach — which
    is exactly where this seam draws every other line.  ``deselected`` is still
    reported as 0 because the executor genuinely does not know; it is not
    asserting there were none.
    """

    ecosystem = ECOSYSTEM_NPM
    tool = "vitest"
    program = "vitest"
    program_env_var = "OMNIAGENTOS_GATE_VITEST"
    version_argv = ("--version",)
    env_overrides = {"CI": "true", "FORCE_COLOR": "0", "NO_COLOR": "1"}
    command_prefix = ("vitest", "run")
    #: The gate_config key carrying the operator's approved config digest.
    config_pin_key = "vitest_config_digest"
    #: The only directory the config walk skips — see `_nested_config_paths`.
    _PRUNED_DIRNAMES = frozenset({".git"})
    #: A directory this runner never creates, so a committed one is refused.
    _VENDOR_DIRNAME = "node_modules"

    _REPORT_NAME = "vitest-junit.xml"
    #: See :data:`_VITEST_CONFIG_FILENAMES` for how this list is derived and why
    #: a missing entry is the failure mode that matters.
    _CONFIG_FILENAMES = _VITEST_CONFIG_FILENAMES

    def _refuse_symlinked_config(self, run_tree: Path, path: Path, label: str) -> None:
        """A config path that is a symlink is refused outright, before hashing.

        Encoding a symlink as "absent" (the previous behaviour) left a hole big
        enough to drive the whole mechanism through: pin a tree that genuinely
        has no ``vitest.config.ts``, then plant a SYMLINK there pointing at
        candidate-controlled content.  Both trees hash identically, the pin
        matches, and vitest happily follows the link — the operator approved
        "no config" and got whatever the link points at.

        Refusing makes the digest's meaning exact instead of merely careful:
        every entry is either a regular file's content or a true absence, and
        there is no third state for an attacker to alias one onto the other.
        A symlinked config is also unambiguously a fact about the candidate, so
        it settles rather than reporting a host problem.
        """
        if path.is_symlink():
            raise GateEvidenceRefusal(
                f"the candidate's {label} is a symlink; a gate cannot be graded under a "
                "configuration that resolves to content outside the tree it pinned"
            )
        if not path.exists():
            return
        with contextlib.suppress(OSError):
            if inode_relative_parts_anchored(path.resolve(), run_tree) is None:
                raise GateEvidenceRefusal(f"the candidate's {label} resolves outside the run tree")

    def _nested_config_paths(self, run_tree: Path) -> list[tuple[str, Path]]:
        """Every config file BELOW the root, as ``(relative-posix-path, path)``.

        WHY THE ROOT IS NOT ENOUGH
        --------------------------
        A pinned ``vitest.workspace.json`` (or any of the workspace/projects
        spellings) does not contain the configuration — it REFERENCES it, by
        glob, at ``packages/*``.  Each referenced project then loads its own
        ``vitest.config.ts``, so pinning only the root left every one of those
        sub-configs unpinned and freely editable: exactly the exclusion hole the
        pin exists to close, one directory further down.

        Following the references is not an option worth taking seriously — a
        workspace file is arbitrary TypeScript and its project list can be
        computed — so this does the stronger and simpler thing and hashes EVERY
        config file in the tree regardless of whether anything references it.
        That over-covers (a config in an unrelated package also moves the
        digest, and a candidate touching one must get re-approval), and
        over-covering is the correct direction: the cost is an operator glance,
        the alternative is a silent deselect.

        Two directory-level rules keep that coverage from being trivially
        sidestepped, and both FAIL CLOSED rather than skipping quietly:

        * a symlinked directory is REFUSED, not passed over. ``followlinks=False``
          stops the walk descending, but vitest's own globbing follows symlinked
          directories (fast-glob defaults ``followSymbolicLinks: true``), so a
          committed ``packages/proj -> elsewhere`` under a pinned workspace glob
          would load a config the digest never hashed — editing the file behind
          the link left the digest byte-identical;
        * a committed ``node_modules`` is REFUSED. This runner never installs
          dependencies, so one can only be there because the candidate committed
          it, and pruning it on the assumption that it is generated and
          gitignored was an assumption nothing enforced.

        Only ``.git`` is pruned: it is the only directory that cannot hold a
        config vitest would load from the candidate's source, and walking it
        would be the one expensive part of this.
        """
        wanted = set(self._CONFIG_FILENAMES)
        found: list[tuple[str, Path]] = []
        for parent, dirnames, filenames in os.walk(run_tree, followlinks=False):
            parent_path = Path(parent)
            # Directory names are judged BEFORE the walk descends, because both
            # decisions below have to be made about a directory this walk is
            # otherwise about to silently skip.
            for name in sorted(dirnames):
                child = parent_path / name
                if name == self._VENDOR_DIRNAME:
                    raise GateEvidenceRefusal(
                        f"the candidate has committed a {self._VENDOR_DIRNAME} directory at "
                        f"{child.relative_to(run_tree).as_posix()}; this gate never installs "
                        "one, so it is candidate-authored content that can hold a config this "
                        "pin would not hash"
                    )
                if name in self._PRUNED_DIRNAMES:
                    continue
                if child.is_symlink():
                    # NOT merely skipped. `followlinks=False` stops the walk
                    # descending, which without this would be a silent hole
                    # rather than a safeguard: vitest's own globbing follows
                    # symlinked directories (fast-glob defaults
                    # `followSymbolicLinks: true`), so a `packages/proj ->
                    # elsewhere` under a pinned workspace glob loads a config
                    # that the digest never saw. Editing the file behind the
                    # link left the digest byte-identical. This is the
                    # directory-level analogue of the file-level rule, and it
                    # fails closed for the same reason.
                    raise GateEvidenceRefusal(
                        f"the candidate's {child.relative_to(run_tree).as_posix()} is a "
                        "symlinked directory; vitest follows those when resolving projects, "
                        "so its contents would run without ever being pinned"
                    )
            dirnames[:] = sorted(name for name in dirnames if name not in self._PRUNED_DIRNAMES)
            if parent_path == run_tree:
                # Root-level names are hashed separately, including their
                # absence, so they are not re-added here.
                continue
            for filename in sorted(filenames):
                if filename not in wanted:
                    continue
                path = parent_path / filename
                relative = path.relative_to(run_tree).as_posix()
                self._refuse_symlinked_config(run_tree, path, relative)
                found.append((relative, path))
        return sorted(found)

    def config_digest(self, run_tree: Path) -> str:
        """Digest over every config vitest could resolve, at the root or below.

        The hashed material is the (PATH, content-digest) pair for every name in
        :data:`_CONFIG_FILENAMES` at the tree root — with the empty digest
        standing for "absent" — plus one entry for every config file found
        anywhere below it.  Each half of the pair is load-bearing:

        * without the content, editing a pinned config would not move the
          digest;
        * without the path, the same content under ``vite.config.js`` and under
          ``vitest.config.ts`` would collide — and which file is present decides
          which one vitest actually loads, so moving a config between them
          changes behaviour while looking identical.

        Recording root-level absence explicitly is what makes that part of the
        material a fixed-length description of the whole resolution order rather
        than a description of whatever happens to be there, so adding or
        deleting a root config is a change to the same shape of statement, not a
        change of shape.

        Symlinks never reach the hash: they are refused first, so "absent" means
        absent and nothing else.
        """
        material: list[dict[str, str]] = []
        for name in self._CONFIG_FILENAMES:
            path = run_tree / name
            self._refuse_symlinked_config(run_tree, path, name)
            if not path.is_file():
                material.append({"name": name, "digest": ""})
                continue
            material.append(
                {
                    "name": name,
                    "digest": digest(path.read_bytes().decode("utf-8", "surrogateescape")),
                }
            )
        for relative, path in self._nested_config_paths(run_tree):
            material.append(
                {
                    "name": relative,
                    "digest": digest(path.read_bytes().decode("utf-8", "surrogateescape")),
                }
            )
        return digest(json.dumps(material, sort_keys=True, separators=(",", ":")))

    def preflight(self, run_tree: Path, targets: tuple[str, ...]) -> None:
        # The pin is checked in `check_config_pin`, which needs the declaration;
        # preflight only proves the targets are real files or directories.
        for target in targets:
            if not (run_tree / target.split("::", 1)[0]).exists():
                raise GateEvidenceRefusal(f"vitest gate target does not exist: {target!r}")

    def check_config_pin(self, run_tree: Path, gate_config: Mapping[str, Any]) -> None:
        """Refuse unless the tree's vitest config is the one the operator pinned."""
        declared = gate_config.get(self.config_pin_key)
        if not isinstance(declared, str) or not _HEX64_RE.match(declared):
            raise GateEvidenceRefusal(
                f"an npm gate must declare gate_config.{self.config_pin_key} — a 64-hex "
                "digest of the vitest config the operator approved; without it a "
                "candidate-committed config can silently exclude failing tests and the "
                "JUnit report cannot show it"
            )
        actual = self.config_digest(run_tree)
        if not hmac.compare_digest(actual, declared):
            raise GateEvidenceRefusal(
                "the candidate's vitest configuration is not the one this gate pinned "
                f"(declared {declared[:12]}…, found {actual[:12]}…); a config change must be "
                "re-approved by an operator before it can grade anything"
            )

    def build_argv(self, program: str, targets: tuple[str, ...], artifacts_dir: Path) -> list[str]:
        return [
            program,
            "run",
            "--reporter=junit",
            f"--outputFile={artifacts_dir / self._REPORT_NAME}",
            *targets,
        ]

    def count(
        self,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        artifacts_dir: Path,
        finished_at_ns: int | None = None,
    ) -> CountedOutcome:
        payload = read_artifact_nofollow(
            artifacts_dir / self._REPORT_NAME,
            missing_message=(
                "vitest produced no JUnit report at the path this run minted; "
                "no counted evidence exists for this invocation"
            ),
            not_modified_after_ns=finished_at_ns,
        )
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise GateExecutionInfraError(f"vitest JUnit report is unparseable: {exc}") from exc

        node_ids: list[str] = []
        passed = skipped = failed = 0
        for case in root.iter("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            if not name:
                raise GateExecutionInfraError("vitest JUnit report has an unnamed testcase")
            node_ids.append(f"{classname}::{name}" if classname else name)
            if case.find("skipped") is not None:
                skipped += 1
            elif case.find("failure") is not None or case.find("error") is not None:
                failed += 1
            else:
                passed += 1

        declared_tests = declared_failures = declared_skipped = 0
        suites = list(root.iter("testsuite"))
        if not suites:
            raise GateExecutionInfraError("vitest JUnit report declares no test suites")
        for suite in suites:
            declared_tests += _required_int(suite, "tests", "vitest JUnit testsuite")
            declared_failures += _required_int(suite, "failures", "vitest JUnit testsuite")
            declared_failures += _required_int(suite, "errors", "vitest JUnit testsuite")
            declared_skipped += _required_int(suite, "skipped", "vitest JUnit testsuite")

        collected = len(node_ids)
        if (collected, failed, skipped) != (declared_tests, declared_failures, declared_skipped):
            raise GateExecutionInfraError(
                f"vitest JUnit testcase tally {(collected, failed, skipped)} disagrees with the "
                f"report's own suite attributes {(declared_tests, declared_failures, declared_skipped)}"
            )
        return CountedOutcome(
            collected=collected,
            passed=passed,
            skipped=skipped,
            failed=failed,
            deselected=0,
            node_ids=tuple(node_ids),
        )


def _required_int(element: ET.Element, attribute: str, subject: str) -> int:
    raw = element.get(attribute)
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise GateExecutionInfraError(
            f"{subject} has a non-numeric {attribute} attribute: {raw!r}"
        ) from None
    if value < 0:
        raise GateExecutionInfraError(f"{subject} has a negative {attribute} attribute")
    return value


class GoTestExecutor(EcosystemExecutor):
    """``go test -json``, counted from the event stream this process captured.

    stdout is the evidence, so there is no artifact to fabricate.  Strictness is
    deliberate and total: EVERY non-empty line must be a JSON object carrying a
    string ``Action``.  A stub, a wrapper script, or an old toolchain that
    prints a human-readable ``ok  example/pkg  0.01s`` therefore yields no
    evidence at all instead of an inferred pass — which is exactly the
    counterfeit this rule exists to catch.

    ``-count=1`` is not optional.  ``go test`` caches results and will happily
    reprint a previous run's ``ok (cached)``; a cached result is a statement
    about some earlier tree, not about the candidate.

    Sub-tests are counted, their parents are not: a parent's ``pass`` is a
    summary of its children, and counting both would inflate the collected total
    over the number of assertions that actually ran.

    TARGETS ARE PACKAGES, NEVER FILES
    ---------------------------------
    ``go test ./pkg/a_test.go`` is legal Go and is a silent partial suite: it
    compiles and runs only the named files, so a package with one passing and
    one failing test file gates green on the passing one, with nothing in the
    event stream to say the other exists.  It is the same defect class as a
    pytest ``-k`` selector, which this seam has always refused, so a target must
    resolve to a DIRECTORY — optionally with Go's ``/...`` recursion suffix,
    whose non-pattern prefix is still containment-checked.
    """

    ecosystem = ECOSYSTEM_GO
    tool = "go test"
    program = "go"
    program_env_var = "OMNIAGENTOS_GATE_GO"
    version_argv = ("version",)
    #: ``GOFLAGS`` is the Go equivalent of ``PYTEST_ADDOPTS`` and is cleared for
    #: the same reason.  ``GOTOOLCHAIN=local`` stops the module's ``go`` line
    #: from silently downloading and running a DIFFERENT toolchain than the one
    #: this executor resolved, identified and recorded in the evidence.
    env_overrides = {"GOFLAGS": "", "GOTOOLCHAIN": "local", "GO111MODULE": "on"}
    command_prefix = ("go", "test")

    _TERMINAL_ACTIONS = frozenset({"pass", "fail", "skip"})
    #: Package lifecycle actions. ``start`` has been emitted by ``go test -json``
    #: since Go 1.21; requiring it is what makes a hand-rolled stream of bare
    #: run/pass pairs — the cheapest forgery — not count.
    _PACKAGE_START = "start"

    @staticmethod
    def _package_prefix(target: str) -> str:
        """The non-pattern part of a target, which must exist on disk."""
        parts = [part for part in PurePosixPath(target).parts if part not in (".", "")]
        while parts and parts[-1] == "...":
            parts.pop()
        return "/".join(parts) or "."

    def containment_paths(self, targets: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(self._package_prefix(target) for target in targets)

    def parse_command(self, command: str) -> tuple[str, ...]:
        targets = super().parse_command(command)
        for target in targets:
            if target.endswith(".go"):
                raise GateEvidenceRefusal(
                    f"a go gate target must be a package, not a file: {target!r} — naming "
                    "files runs a partial suite that no event in the stream reports"
                )
            if "..." in PurePosixPath(target).parts[:-1]:
                raise GateEvidenceRefusal(
                    f"go recursion '...' may only be the last path segment: {target!r}"
                )
        return targets

    def preflight(self, run_tree: Path, targets: tuple[str, ...]) -> None:
        for target in targets:
            resolved = run_tree / self._package_prefix(target)
            if not resolved.is_dir():
                raise GateEvidenceRefusal(
                    f"a go gate target must resolve to a package directory: {target!r}"
                )

    def build_argv(self, program: str, targets: tuple[str, ...], artifacts_dir: Path) -> list[str]:
        packages = [target if target.startswith("./") else f"./{target}" for target in targets]
        return [program, "test", "-json", "-count=1", *packages]

    def count(
        self,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        artifacts_dir: Path,
        finished_at_ns: int | None = None,
    ) -> CountedOutcome:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                raise GateExecutionInfraError(
                    "go test -json emitted a line that is not a JSON event; "
                    f"no counted evidence exists: {line.strip()[:200]!r}"
                ) from None
            if not isinstance(event, dict) or not isinstance(event.get("Action"), str):
                raise GateExecutionInfraError(
                    "go test -json emitted a JSON value that is not an event object"
                )
            events.append(event)

        if not events:
            raise GateExecutionInfraError("go test -json emitted no events")

        outcomes: dict[tuple[str, str], str] = {}
        started: set[tuple[str, str]] = set()
        package_started: set[str] = set()
        package_terminal: dict[str, str] = {}
        for event in events:
            action = event["Action"]
            package = event.get("Package")
            test = event.get("Test")
            if not isinstance(package, str) or not package:
                continue
            if not isinstance(test, str) or not test:
                if action == self._PACKAGE_START:
                    package_started.add(package)
                elif action in self._TERMINAL_ACTIONS:
                    package_terminal[package] = action
                continue
            key = (package, test)
            if action == "run":
                started.add(key)
            elif action in self._TERMINAL_ACTIONS:
                outcomes[key] = action

        # PACKAGE LIFECYCLE, not just test lines. A real `go test -json` stream
        # brackets every package with `start` and a terminal `pass`/`fail`/
        # `skip`; a forged or truncated stream of bare run/pass pairs has neither
        # and must not be counted, because without the terminal event there is
        # nothing that says the package finished rather than was cut off.
        test_packages = {package for package, _ in started | set(outcomes)}
        for package in sorted(test_packages):
            if package not in package_started:
                raise GateExecutionInfraError(
                    f"go test -json reported tests for package {package!r} with no package "
                    "start event; the stream is incomplete and cannot be counted"
                )
            if package not in package_terminal:
                raise GateExecutionInfraError(
                    f"go test -json never reported a terminal result for package {package!r}; "
                    "the stream is truncated"
                )
        package_failed = any(result == "fail" for result in package_terminal.values())

        # A parent whose sub-tests reported separately is a summary line, not an
        # assertion; drop it so the collected count is the number of leaves.
        names_by_package: dict[str, set[str]] = {}
        for package, test in started | set(outcomes):
            names_by_package.setdefault(package, set()).add(test)
        leaves = {
            (package, test)
            for package, names in names_by_package.items()
            for test in names
            if not any(other.startswith(f"{test}/") for other in names)
        }

        node_ids = sorted(f"{package}::{test}" for package, test in leaves)
        passed = sum(1 for key in leaves if outcomes.get(key) == "pass")
        failed = sum(1 for key in leaves if outcomes.get(key) == "fail")
        skipped = sum(1 for key in leaves if outcomes.get(key) == "skip")

        if package_failed and failed == 0:
            # A CANDIDATE fact, not an instrument fact. The package failed while
            # every test that reported passed, which means the failure was a
            # build error, a panic, or a failing TestMain — all of them defects
            # in the code under test. Calling this `unavailable` would park a
            # genuinely broken candidate in the excluded-from-the-floor bucket
            # forever instead of failing its gate.
            raise GateEvidenceRefusal(
                "go test reported a package failure with no failing test — the failure "
                "happened outside the counted tests (build error, panic, or TestMain)"
            )
        if failed and not package_failed:
            raise GateExecutionInfraError(
                "go test reported failing tests but no failing package; the event stream "
                "is not self-consistent"
            )
        return CountedOutcome(
            collected=len(leaves),
            passed=passed,
            skipped=skipped,
            failed=failed,
            deselected=0,
            node_ids=tuple(node_ids),
        )


class CargoTestExecutor(EcosystemExecutor):
    """``cargo test``, counted from libtest's stable summary with a cross-check.

    libtest's JSON output is still nightly-gated (``-Z unstable-options
    --format=json``), and a gate may not require a nightly toolchain, so this
    executor parses the STABLE, documented output — but only under two rules
    that together make it as hard to fake as a machine-readable stream:

    1. Every per-test line (``test path::name ... ok``) is captured
       individually, giving a real node inventory.
    2. Each binary's ``test result:`` summary must agree EXACTLY with the
       per-test lines counted before it.  A summary claiming five passes above
       two ``ok`` lines is a mismatch and yields no evidence.

    ``filtered out`` is carried through as ``deselected``, where the seam
    already refuses it: a filtered run is a partial run.

    ``--no-fail-fast`` keeps a failing binary from suppressing later binaries
    (otherwise ``passed + skipped != collected`` for reasons that have nothing
    to do with the candidate).  ``--locked --offline`` keeps the gate hermetic:
    it must not mutate ``Cargo.lock`` (the post-run tree-clean check would
    condemn the run anyway) and must not reach the network mid-verdict.

    ``harness = false`` IS A CANDIDATE-SUPPLIED GRADER, AND IS REFUSED
    ------------------------------------------------------------------
    Both rules above assume the output was written by libtest.  A Cargo target
    declared ``harness = false`` replaces libtest with the candidate's own
    ``fn main()``, which may print anything at all — including a flawless
    ``running 2 tests / test a ... ok / test b ... ok / test result: ok.
    2 passed; ...`` block that satisfies the per-test lines, the summary, and
    the cross-check between them, while running no assertions whatsoever.  No
    amount of parsing can tell that apart, because at that point the thing being
    graded is also the thing writing the grade.

    So this executor reads the manifests instead and REFUSES if any Cargo target
    opts out of the harness.  That is the same rule the module header states for
    the toolchain binary: the candidate does not supply its own grader.  A crate
    that genuinely needs a custom harness is not ungateable, it just cannot be
    graded by counting libtest's output, and saying so is the honest answer.

    WHICH MANIFESTS — `members` IS ONLY HALF THE MEMBERSHIP RULE
    ------------------------------------------------------------
    Cargo's workspace spec makes every PATH DEPENDENCY residing in the workspace
    directory an *implicit* member, auto-joined without ever appearing in
    `workspace.members`.  Checking only the literal `members` array therefore
    left a crate one `path = "../sneaky"` away whose `harness = false` target
    `cargo test` would still run.  The traversal follows both: explicit members
    (globs expanded) AND path dependencies from every table one can hide in —
    `[dependencies]`, `[dev-dependencies]`, `[build-dependencies]`,
    `[workspace.dependencies]`, and the platform-specific
    `[target.'cfg(...)'.*]` forms — transitively, with a cycle guard.

    The two spec boundaries are honoured rather than steamrollered, because
    over-refusing is its own defect: a directory in `workspace.exclude` is not a
    member, and a path dependency carrying its OWN `[workspace]` table is a
    separate workspace root that `cargo test` here never runs.  Both are skipped.

    Everything else fails CLOSED.  A manifest that resolves outside the run tree,
    is missing, or does not parse is refused rather than assumed harmless: an
    unverifiable dependency must not be able to buy a green gate.

    THE COST, WHICH IS REAL AND FALLS ON `[[bench]]`
    ------------------------------------------------
    ``[[bench]] harness = false`` is the standard criterion idiom, and criterion
    is close to universal in Rust.  Because the traversal is transitive, ONE
    criterion bench in ANY reachable path dependency refuses the whole gate —
    not just a bench in the crate under test.  That is a genuine false refusal:
    ``cargo test`` does not run benches by default, so a criterion bench cannot
    forge a summary and cannot actually reach the counted output.

    It is refused anyway because the alternative is a table-by-table exception
    whose correctness depends on cargo's default target selection staying what
    it is today, and a wrong exception here is a silent forged pass rather than
    a visible refusal.  The remedy is a one-line manifest change the candidate
    controls and that says the same thing explicitly — ``test = false`` on the
    bench target, which tells cargo not to build it under ``cargo test`` at all:

        [[bench]]
        name = "throughput"
        harness = false
        test = false          # not run by `cargo test`; not gate-relevant

    If that proves too noisy in practice, the right fix is to honour ``test =
    false`` as an exemption rather than to stop reading manifests.
    """

    ecosystem = ECOSYSTEM_CARGO
    tool = "cargo test"
    program = "cargo"
    program_env_var = "OMNIAGENTOS_GATE_CARGO"
    version_argv = ("--version",)
    env_overrides = {"CARGO_TERM_COLOR": "never", "RUSTFLAGS": ""}
    command_prefix = ("cargo", "test")
    #: One manifest per run.  ``cargo test`` takes a single ``--manifest-path``,
    #: and any extra positional argument would be read as a TEST NAME FILTER —
    #: a silent deselect wearing the costume of a target.
    exact_target_count = 1

    #: rustc/cargo compile diagnostics. Matched against the run's OWN captured
    #: streams only, and used solely to CLASSIFY an already-failed run — never to
    #: infer that anything passed.
    _COMPILE_FAILURE_RE = re.compile(
        r"^(?:error(?:\[E\d+\])?: |error: could not compile\b|error: failed to )",
        re.MULTILINE,
    )
    _CASE_RE = re.compile(r"\Atest (?P<name>\S+) \.\.\. (?P<outcome>ok|FAILED|ignored)\b")
    _SUMMARY_RE = re.compile(
        r"\Atest result: (?:ok|FAILED)\. "
        r"(?P<passed>\d+) passed; "
        r"(?P<failed>\d+) failed; "
        r"(?P<ignored>\d+) ignored; "
        r"(?P<measured>\d+) measured; "
        r"(?P<filtered>\d+) filtered out"
    )

    #: Cargo target tables that accept a ``harness`` key.
    _HARNESS_TABLES = ("lib", "bin", "test", "bench", "example")
    #: Every table a `path = "..."` dependency can hide in.
    _DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")

    @staticmethod
    def _manifest_path(target: str) -> PurePosixPath:
        manifest = PurePosixPath(target)
        if manifest.name != "Cargo.toml":
            manifest = manifest / "Cargo.toml"
        return manifest

    def preflight(self, run_tree: Path, targets: tuple[str, ...]) -> None:
        root_manifest = run_tree / self._manifest_path(targets[0])
        root_data = self._read_manifest(self._contained(run_tree, root_manifest))
        excluded = self._excluded_dirs(run_tree, root_manifest, root_data)

        seen: set[Path] = set()
        pending: list[tuple[Path, bool]] = [(root_manifest, True)]
        while pending:
            manifest, is_root = pending.pop()
            resolved = self._contained(run_tree, manifest)
            if resolved in seen:
                continue
            seen.add(resolved)
            if self._is_excluded(resolved.parent, excluded):
                # `workspace.exclude` removes a whole SUBTREE from the workspace,
                # not just the directory named: cargo treats everything under it
                # as a non-member, so a crate two levels down that this traversal
                # only reached via a path dependency is equally not run. Testing
                # exact-parent equality over-refused exactly those.
                continue
            data = self._read_manifest(resolved)
            if not is_root and self._declares_own_workspace(data):
                # Per the Cargo workspace spec a path dependency that carries its
                # own `[workspace]` table is a SEPARATE workspace root and is not
                # auto-joined as a member — so `cargo test` here never runs its
                # targets, and refusing on them would be a false adverse.
                continue
            self._refuse_custom_harness(resolved, data)
            for child in self._member_manifests(run_tree, resolved, data):
                pending.append((child, False))
            for child in self._path_dependency_manifests(run_tree, resolved, data):
                pending.append((child, False))

    @staticmethod
    def _contained(run_tree: Path, manifest: Path) -> Path:
        """Resolve *manifest* and prove it lies inside the run tree, or refuse.

        Fail-closed on escape: a manifest this executor cannot see is a manifest
        whose ``harness`` declarations it cannot check, and an unverifiable
        dependency must not be able to buy a green gate. It is also a fact about
        the candidate — a crate reaching outside its own checkout is not
        hermetically gradeable — so it refuses rather than reporting a host
        problem.
        """
        try:
            resolved = manifest.resolve()
        except OSError as exc:
            raise GateEvidenceRefusal(f"cargo manifest is unreadable: {manifest} ({exc})") from exc
        if inode_relative_parts_anchored(resolved, run_tree) is None:
            raise GateEvidenceRefusal(
                f"cargo manifest escapes the workspace and cannot be checked: {manifest}"
            )
        return resolved

    @staticmethod
    def _declares_own_workspace(data: dict[str, Any]) -> bool:
        return isinstance(data.get("workspace"), dict)

    @staticmethod
    def _read_manifest(manifest: Path) -> dict[str, Any]:
        try:
            raw = manifest.read_bytes()
        except OSError as exc:
            raise GateEvidenceRefusal(f"cargo manifest is unreadable: {manifest} ({exc})") from exc
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            # Unparseable manifest is a CANDIDATE fact: cargo would not build it
            # either, and a manifest this executor cannot read is one whose
            # harness declarations it cannot check.
            raise GateEvidenceRefusal(
                f"cargo manifest is not valid TOML: {manifest} ({exc})"
            ) from exc
        return parsed

    def _refuse_custom_harness(self, manifest: Path, data: dict[str, Any]) -> None:
        for table in self._HARNESS_TABLES:
            entries = data.get(table)
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("harness") is False:
                    name = entry.get("name") or "<unnamed>"
                    raise GateEvidenceRefusal(
                        f"cargo target [[{table}]] {name!r} in {manifest} declares "
                        "harness = false, so its 'test result:' output would be written by "
                        "the candidate's own main() rather than by libtest; this seam "
                        "cannot grade a candidate-supplied harness"
                    )

    @classmethod
    def _excluded_dirs(cls, run_tree: Path, manifest: Path, data: dict[str, Any]) -> set[Path]:
        """Resolved directories `workspace.exclude` removes from the workspace."""
        workspace = data.get("workspace")
        if not isinstance(workspace, dict):
            return set()
        excluded = workspace.get("exclude")
        if not isinstance(excluded, list):
            return set()
        found: set[Path] = set()
        for entry in excluded:
            if not isinstance(entry, str) or not entry:
                continue
            for match in cls._expand(manifest.parent, entry):
                with contextlib.suppress(OSError):
                    found.add(match.resolve())
        return found

    @staticmethod
    def _is_excluded(directory: Path, excluded: set[Path]) -> bool:
        """True when *directory* is an excluded directory or lies beneath one."""
        return any(directory == entry or entry in directory.parents for entry in excluded)

    @staticmethod
    def _expand(base: Path, pattern: str) -> list[Path]:
        """Expand a members/exclude entry, which may be a glob."""
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts:
            raise GateEvidenceRefusal(f"cargo workspace path escapes the workspace: {pattern!r}")
        if any(ch in pattern for ch in "*?["):
            return sorted(base.glob(pattern))
        return [base / pattern]

    @classmethod
    def _member_manifests(cls, run_tree: Path, manifest: Path, data: dict[str, Any]) -> list[Path]:
        """Every manifest `workspace.members` names explicitly."""
        workspace = data.get("workspace")
        if not isinstance(workspace, dict):
            return []
        members = workspace.get("members")
        if not isinstance(members, list):
            return []
        found: list[Path] = []
        for member in members:
            if not isinstance(member, str) or not member:
                continue
            # Members may be globs (`crates/*`); expand them against the tree so
            # a member added by a glob cannot dodge the harness check.
            for match in cls._expand(manifest.parent, member):
                candidate = match / "Cargo.toml"
                if candidate.is_file():
                    found.append(candidate)
        return found

    @classmethod
    def _path_dependency_manifests(
        cls, run_tree: Path, manifest: Path, data: dict[str, Any]
    ) -> list[Path]:
        """Every PATH DEPENDENCY manifest, which Cargo joins to the workspace.

        The `members` array is not the membership rule, it is half of it. Cargo's
        workspace spec makes every path dependency residing inside the workspace
        directory an IMPLICIT member unless it is excluded or carries its own
        `[workspace]` table — so a crate that appears nowhere in `members` still
        has its tests run by `cargo test`, and a `harness = false` target hidden
        one `path = "../sneaky"` away was invisible to the previous check.

        Every dependency table is scanned, including the platform-specific
        `[target.'cfg(...)'.dependencies]` forms and `[workspace.dependencies]`
        (dependency inheritance), because a path can hide in any of them.
        """
        base = manifest.parent
        specs: list[tuple[str, str]] = []

        def scan(table: object) -> None:
            if not isinstance(table, dict):
                return
            for name, spec in table.items():
                if not isinstance(spec, dict):
                    continue
                path_value = spec.get("path")
                if isinstance(path_value, str) and path_value:
                    specs.append((str(name), path_value))

        for key in cls._DEPENDENCY_TABLES:
            scan(data.get(key))
        workspace = data.get("workspace")
        if isinstance(workspace, dict):
            scan(workspace.get("dependencies"))
        # `[patch.<source>]` and `[replace]` redirect a dependency to a local
        # path, and whether the redirected crate ends up a workspace member is
        # version-dependent. Scanning them is the fail-closed reading and costs
        # nothing when they are absent, which is the usual case.
        patch = data.get("patch")
        if isinstance(patch, dict):
            for source_table in patch.values():
                scan(source_table)
        scan(data.get("replace"))
        targets = data.get("target")
        if isinstance(targets, dict):
            for platform_table in targets.values():
                if not isinstance(platform_table, dict):
                    continue
                for key in cls._DEPENDENCY_TABLES:
                    scan(platform_table.get(key))

        found: list[Path] = []
        for name, path_value in specs:
            pure = PurePosixPath(path_value)
            if pure.is_absolute():
                raise GateEvidenceRefusal(
                    f"cargo path dependency {name!r} is absolute and cannot be checked: "
                    f"{path_value!r}"
                )
            candidate = base / path_value / "Cargo.toml"
            # `_contained` refuses anything resolving outside the run tree: a
            # path dependency pointing out of the checkout is unverifiable, and
            # unverifiable must not be able to buy a green gate.
            resolved = cls._contained(run_tree, candidate)
            if not resolved.is_file():
                raise GateEvidenceRefusal(
                    f"cargo path dependency {name!r} has no manifest at {path_value!r}; "
                    "cargo could not build this candidate either"
                )
            found.append(resolved)
        return found

    def build_argv(self, program: str, targets: tuple[str, ...], artifacts_dir: Path) -> list[str]:
        manifest = self._manifest_path(targets[0])
        return [
            program,
            "test",
            "--locked",
            "--offline",
            "--no-fail-fast",
            "--manifest-path",
            str(manifest),
        ]

    def count(
        self,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        artifacts_dir: Path,
        finished_at_ns: int | None = None,
    ) -> CountedOutcome:
        node_ids: list[str] = []
        passed = skipped = failed = deselected = 0
        block_index = 0
        pending: list[tuple[str, str]] = []
        saw_summary = False

        for line in stdout.splitlines():
            case = self._CASE_RE.match(line)
            if case is not None:
                pending.append((case.group("name"), case.group("outcome")))
                continue
            summary = self._SUMMARY_RE.match(line)
            if summary is None:
                continue
            saw_summary = True
            block_passed = sum(1 for _, outcome in pending if outcome == "ok")
            block_failed = sum(1 for _, outcome in pending if outcome == "FAILED")
            block_skipped = sum(1 for _, outcome in pending if outcome == "ignored")
            declared = (
                int(summary.group("passed")),
                int(summary.group("failed")),
                int(summary.group("ignored")),
            )
            if (block_passed, block_failed, block_skipped) != declared:
                raise GateExecutionInfraError(
                    f"cargo test summary {declared} disagrees with the "
                    f"{(block_passed, block_failed, block_skipped)} per-test lines that "
                    "preceded it; the output is not self-consistent"
                )
            node_ids.extend(f"{block_index}::{name}" for name, _ in pending)
            passed += block_passed
            failed += block_failed
            skipped += block_skipped
            deselected += int(summary.group("filtered"))
            pending = []
            block_index += 1

        if not saw_summary:
            # Distinguish "the candidate does not compile" from "the instrument
            # said nothing useful". A compile error is a defect in the code under
            # test and must SETTLE as a failed gate; parking it in the
            # excluded-from-the-floor bucket would let a repo that has not built
            # for a week never register a single adverse outcome.
            if self._COMPILE_FAILURE_RE.search(stderr) or self._COMPILE_FAILURE_RE.search(stdout):
                raise GateEvidenceRefusal(
                    "cargo could not compile the candidate, so no test ever ran: "
                    f"{_first_line_matching(stderr, self._COMPILE_FAILURE_RE) or ''}"
                )
            raise GateExecutionInfraError(
                "cargo test printed no `test result:` summary line; no counted evidence exists"
            )
        if pending:
            raise GateExecutionInfraError(
                f"cargo test printed {len(pending)} test lines after the last summary; "
                "the run was truncated"
            )
        return CountedOutcome(
            collected=len(node_ids),
            passed=passed,
            skipped=skipped,
            failed=failed,
            deselected=deselected,
            node_ids=tuple(node_ids),
        )


_EXECUTORS: dict[str, EcosystemExecutor] = {
    ECOSYSTEM_NPM: NpmVitestExecutor(),
    ECOSYSTEM_GO: GoTestExecutor(),
    ECOSYSTEM_CARGO: CargoTestExecutor(),
}


def executor_for(ecosystem: str) -> EcosystemExecutor:
    """The executor for a validated, non-Python ecosystem."""
    validated = normalize_ecosystem(ecosystem)
    executor = _EXECUTORS.get(validated)
    if executor is None:
        raise GateEvidenceRefusal(f"{validated} gates are not executed by this seam")
    return executor


def ecosystem_command_is_valid(ecosystem: Any, command: str) -> bool:
    """Write-path predicate: may this (ecosystem, command) pair be stored?

    Python is deliberately absent: it keeps its own predicate in
    ``routines._is_non_vacuous_gate_command``, untouched, so this module cannot
    change what a Python gate is allowed to be.
    """
    try:
        return executor_for(ecosystem).command_is_valid(command)
    except GateEvidenceRefusal:
        return False


def resolve_program(executor: EcosystemExecutor, *, path: str | None) -> Path:
    """The absolute toolchain binary, or an UNAVAILABLE refusal.

    The override is read from THIS process's environment — the operator's launch
    profile, the same trust tier that already decides which tree the gate
    executes — and never from anything a routine row or the candidate tree can
    reach.
    """
    override = os.environ.get(executor.program_env_var)
    if override:
        candidate = normalize_executable(Path(override).expanduser())
    else:
        # Sanitized here as well as in the child environment: `shutil.which`
        # honours "." and empty components exactly like execvp does, so an
        # unsanitized lookup would resolve the candidate's own `./go` in THIS
        # process and then record it as the trusted interpreter.
        found = shutil.which(executor.program, path=sanitize_path_env(path))
        if found is None:
            raise GateWorkspaceUnusable(
                f"{executor.ecosystem} toolchain is absent on this host: "
                f"{executor.program!r} is not on PATH and {executor.program_env_var} is unset"
            )
        candidate = normalize_executable(Path(found))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise GateWorkspaceUnusable(
            f"{executor.ecosystem} toolchain is not executable: {candidate}"
        )
    return candidate


__all__ = [
    "ECOSYSTEM_CARGO",
    "ECOSYSTEM_GO",
    "ECOSYSTEM_NPM",
    "ECOSYSTEM_PYTHON",
    "SHELL_CONTROL_RE",
    "SUPPORTED_ECOSYSTEMS",
    "TOOL_FOR_ECOSYSTEM",
    "CargoTestExecutor",
    "CountedOutcome",
    "EcosystemExecutor",
    "GoTestExecutor",
    "NpmVitestExecutor",
    "args_are_executing_targets",
    "ecosystem_command_is_valid",
    "ecosystem_of_gate_config",
    "executor_for",
    "expected_tool_for_gate_config",
    "is_safe_relative_target",
    "normalize_ecosystem",
    "read_artifact_nofollow",
    "resolve_program",
    "sanitize_path_env",
]
