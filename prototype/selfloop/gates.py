"""Three gate runners, and the rule that a zero-check pass is not a pass.

A ``selfloop`` deployment with no gate is not neutral — it is starved. With no
verifier, :func:`selfloop.outcome.compose` renders every favourable tick
``neutral/uncorroborated``, so no non-neutral evidence is ever written, so
nothing ever clusters, so nothing ever promotes. The loop is correctly wired and
mathematically incapable of learning, which is precisely the failure this
package was extracted from.

The obvious answer — refuse to build a loop without a gate — does not solve it.
It moves the hole to the integrator, who has a deadline and now writes a gate
that returns true. That is not a hypothetical either: the predecessor's default
gate command named a test file in its own repository, which passed regardless of
what the loop had produced, and every loop seeded without an explicit gate
settled favourable over garbage for months.

So this module ships a default that is trivial to satisfy honestly and
impossible to satisfy vacuously:

* :class:`ArtifactGate` — the effect's declared artifact exists and is
  non-empty. Cheap, stdlib, and it cannot pass without something having
  actually been produced.
* :class:`CommandGate` — runs a :class:`~selfloop.contracts.GateSpec` and parses
  COUNTED evidence out of its output. It takes a command to EXECUTE and never a
  verdict to record.
* :class:`NullGate` — refuses to rule, loudly, and its docstring says what that
  costs you.

And the rule underneath all three: a :class:`~selfloop.contracts.GateReceipt`
with ``checks_collected == 0`` raises
:class:`~selfloop.contracts.GateUnavailable`. **A vacuous gate is worse than no
gate.** No gate settles every tick as visibly uncorroborated; a vacuous gate
settles every tick as invisibly accepted, and the loop's own optimism becomes
its training signal.

That rule is applied here by ``_guard_receipt``, which is package-internal on
purpose. It is not an API a caller needs: the
:class:`~selfloop.ports.GateRunner` protocol already obliges every runner to
raise on a zero-check receipt, and
:attr:`~selfloop.contracts.GateReceipt.is_vacuous` exists so that the check
reads identically wherever it is written. If you implement your own runner, the
whole of your obligation is::

    if receipt.is_vacuous:
        raise GateUnavailable("vacuous", "gate collected zero checks")
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

from selfloop.contracts import EvidenceGrade, GateReceipt, GateSpec, GateUnavailable
from selfloop.outcome import Settlement, artifact_bytes, classify_settlement
from selfloop.ports import Clock

#: Sentinel head of a :class:`~selfloop.contracts.GateSpec` addressed to an
#: :class:`ArtifactGate`. ``GateSpec.command`` is a tuple because a gate normally
#: executes something; an artifact gate does not, so a spec aimed at one reads
#: ``("selfloop:artifact", "out/report.json", ...)``.
#:
#: The sentinel is deliberately not a plausible executable name. A spec that is
#: routed to the wrong runner then fails loudly — a :class:`CommandGate` handed
#: this spec cannot find a binary called ``selfloop:artifact`` and raises
#: :class:`~selfloop.contracts.GateUnavailable` — instead of quietly executing
#: something that happens to exist on the PATH.
ARTIFACT_GATE_COMMAND = "selfloop:artifact"

#: A gate command may declare its own counts by printing this line. It is the
#: portable half of the contract: any gate, in any language, can end with
#: ``selfloop-checks: collected=12 passed=12 failed=0 skipped=1 deselected=0``.
#:
#: A gate that reports only an exit code is indistinguishable from a gate that
#: tested nothing, and this package refuses to guess which one it is looking at.
CHECKS_LINE_PREFIX = "selfloop-checks:"

_CHECKS_LINE_RE = re.compile(rf"^\s*{re.escape(CHECKS_LINE_PREFIX)}\s*(?P<body>.*?)\s*$", re.I)
_CHECKS_PAIR_RE = re.compile(r"([a-z_]+)\s*=\s*(\d+)", re.I)

#: A line consisting only of ``<n> <word>`` pairs, optionally ``=``-padded and
#: optionally followed by pytest's ``in 1.23s`` tail. Matching the whole line —
#: rather than scanning the output for count-like fragments — is what stops a
#: test that merely PRINTS "3 passed" from contributing counts.
_SUMMARY_LINE_RE = re.compile(
    r"^=*\s*(?P<body>\d+\s+[a-z]+(?:\s*,\s*\d+\s+[a-z]+)*)"
    r"(?:\s+in\s+[\d.,]+\s*s(?:\s*\([^)]*\))?)?"
    r"\s*=*\s*$",
    re.IGNORECASE,
)
_SUMMARY_PAIR_RE = re.compile(r"(\d+)\s+([a-z]+)", re.IGNORECASE)

#: Words that name a check which RULED on something. Only these enter
#: ``checks_collected``, and that is the whole reason a suite where everything
#: was skipped or deselected settles as unavailable rather than as a pass.
_PASSING_WORDS = frozenset({"passed", "xpassed"})
_FAILING_WORDS = frozenset({"failed", "error", "errors"})
#: ``xfailed`` sits with ``skipped`` on purpose. An expected failure is a test
#: that ran and did not demonstrate the property it names; counting it as
#: evidence would let a suite of known-broken tests corroborate a green tick.
_SKIPPED_WORDS = frozenset({"skipped", "xfailed"})
_DESELECTED_WORDS = frozenset({"deselected"})


@dataclass(frozen=True)
class CheckCounts:
    """How many checks a gate command reported, by category.

    ``collected`` counts only checks that produced a verdict — passes and
    failures. Skips and deselections are recorded separately and are explicitly
    NOT collected, so a run that skipped everything reads as zero collected and
    is refused as vacuous. That is the intended behaviour: a suite where nothing
    executed has verified nothing, whatever its exit status was.
    """

    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    deselected: int = 0

    @property
    def fully_accounted(self) -> bool:
        """True when every collected check reported a pass.

        A gate that says it collected ten checks and reports three passes and no
        failures has seven checks whose outcome it never stated. That is a
        partial execution, and a partial execution may not pass: the seven are
        exactly where a real failure would be hiding. Under a pytest summary this
        is true whenever there are no failures, so it only ever bites a gate that
        declared its own counts with :data:`CHECKS_LINE_PREFIX`.
        """
        return self.collected > 0 and self.passed == self.collected


def parse_check_counts(output: str) -> CheckCounts:
    """Extract counted evidence from a gate command's output.

    Two formats, tried in this order:

    1. The explicit :data:`CHECKS_LINE_PREFIX` line, which any gate in any
       language can print.
    2. A pytest terminal summary line (``3 passed, 1 skipped in 0.12s``, with or
       without its ``=`` padding).

    **The LAST matching line wins**, in both formats. A real summary is the last
    thing a runner writes, so a check that prints a summary-shaped line of its
    own cannot get in front of the genuine one. Taking the first match instead
    would make the counts forgeable by the very thing being counted.

    Unrecognised words (``warnings``, ``rerun``, ...) are ignored rather than
    guessed at. An output this function cannot read yields zero counts, which
    :func:`_guard_receipt` turns into unavailability — not into a pass.
    """
    explicit: CheckCounts | None = None
    summary: CheckCounts | None = None

    for line in output.splitlines():
        explicit_match = _CHECKS_LINE_RE.match(line)
        if explicit_match:
            pairs = {
                name.lower(): int(value)
                for name, value in _CHECKS_PAIR_RE.findall(explicit_match.group("body"))
            }
            passed = pairs.get("passed", 0)
            failed = pairs.get("failed", 0)
            explicit = CheckCounts(
                collected=pairs.get("collected", passed + failed),
                passed=passed,
                failed=failed,
                skipped=pairs.get("skipped", 0),
                deselected=pairs.get("deselected", 0),
            )
            continue

        summary_match = _SUMMARY_LINE_RE.match(line)
        if summary_match:
            passed = failed = skipped = deselected = 0
            for raw_count, raw_word in _SUMMARY_PAIR_RE.findall(summary_match.group("body")):
                count = int(raw_count)
                word = raw_word.lower()
                if word in _PASSING_WORDS:
                    passed += count
                elif word in _FAILING_WORDS:
                    failed += count
                elif word in _SKIPPED_WORDS:
                    skipped += count
                elif word in _DESELECTED_WORDS:
                    deselected += count
            summary = CheckCounts(
                collected=passed + failed,
                passed=passed,
                failed=failed,
                skipped=skipped,
                deselected=deselected,
            )

    if explicit is not None:
        return explicit
    return summary or CheckCounts()


def _guard_receipt(receipt: GateReceipt, *, source: str = "", detail: str = "") -> GateReceipt:
    """Return *receipt*, or raise if it collected nothing. A zero-check pass is not a pass.

    Applied by every runner in this module, at the moment of minting, so that no
    code path in the package can produce a vacuous receipt for
    :func:`selfloop.outcome.compose` to interpret. Composition refuses one too,
    but a rule that decides whether an unattended loop grades itself honestly
    should not have exactly one enforcement point.

    The refusal is :class:`~selfloop.contracts.GateUnavailable` — absence — and
    NOT a failing receipt. A gate that tested nothing has not ruled against the
    work, and recording it as a failure would feed the auto-pause floor with
    non-evidence. The tick settles ``neutral/uncorroborated``, which is visible
    to an operator, rather than accepted, which is not.
    """
    if receipt.is_vacuous:
        raise GateUnavailable(
            "vacuous_gate",
            (
                f"gate collected zero checks{f' ({source})' if source else ''} — a gate that "
                "tested nothing is not a passing gate, and recording it as one would make "
                f"the loop's own optimism its training signal.{f' {detail}' if detail else ''}"
            ),
        )
    return receipt


@dataclass(frozen=True, kw_only=True)
class ArtifactGate:
    """THE DEFAULT GATE: the declared artifacts exist and are non-empty.

    Trivial, real, and impossible to satisfy without having actually produced
    something. It is the answer to "what do I put here on day one" that is not
    ``None`` and is not a lie — and having such an answer is what stops an
    integrator inventing a gate that returns true.

    It reads a path the loop itself wrote, so it sits on the
    :attr:`~selfloop.contracts.EvidenceGrade.LOCAL_ARTIFACT` rung of the
    evidence ladder and it says so. That is one rung above the actor's own
    account of itself and two below a system of record. **Do not leave it here
    forever.** Existence and size cannot tell you the file's contents are
    correct, and an effect that matters — an outbound send, a deployment —
    deserves a :class:`CommandGate` that asks something with no stake in the
    loop's report.

    Two ways to declare what to check, and they compose:

    * at construction, ``ArtifactGate(artifacts=["out/report.json"], clock=...)``,
      for artifacts every tick of this loop owes;
    * per spec, ``GateSpec(command=(ARTIFACT_GATE_COMMAND, "out/2026-08-04.json"))``,
      for artifacts whose name depends on the tick. A spec carrying the sentinel
      REPLACES the constructor list rather than adding to it, because a gate
      whose scope silently grows as specs arrive is a gate nobody can predict.

    Relative paths resolve against ``spec.cwd`` when the spec supplies one, and
    are otherwise left alone for the process's own working directory to
    interpret. Nothing here guesses a repository root from ``__file__``; that
    guess is what made the predecessor impossible to install.

    A path that is a directory, a broken symlink, or unreadable settles as
    FAILED rather than as unavailable. This gate only ever looks at files that
    THIS loop declared it would write, in a filesystem it has access to — so
    "cannot stat it" is the loop's own output missing, which is adverse, not an
    absence of evidence.
    """

    clock: Clock
    artifacts: Sequence[str | os.PathLike[str]] = ()
    #: Minimum size that counts as non-empty. A zero-byte artifact is evidence
    #: that a writer ran, never evidence that it produced anything.
    min_bytes: int = 1

    evidence_grade: ClassVar[EvidenceGrade] = EvidenceGrade.LOCAL_ARTIFACT

    def run(self, spec: GateSpec) -> GateReceipt:
        """Check every declared artifact. Raises ``GateUnavailable`` if none is declared."""
        started = self.clock.elapsed()
        paths = self._paths_for(spec)
        if not paths:
            raise GateUnavailable(
                "no_artifacts_declared",
                "an ArtifactGate was asked to rule with no artifacts declared, either on "
                f"the gate or on the spec (expected a command beginning "
                f"{ARTIFACT_GATE_COMMAND!r}) — with nothing to look at it would be a "
                "vacuous gate, so it refuses to rule instead of passing",
            )

        failures: list[str] = []
        passed_count = 0
        for path in paths:
            if classify_settlement(path, required=True, min_bytes=self.min_bytes) is Settlement.OK:
                passed_count += 1
            else:
                size = artifact_bytes(path)
                why = "no readable file" if size is None else f"{size} bytes"
                failures.append(f"{path} ({why})")

        detail = f"{passed_count}/{len(paths)} declared artifact(s) present and non-empty"
        if failures:
            detail = f"{detail}; missing or empty: {', '.join(failures)}"

        return _guard_receipt(
            GateReceipt(
                passed=not failures,
                checks_collected=len(paths),
                checks_passed=passed_count,
                detail=detail,
                ran_at=self.clock.now_iso(),
                duration_s=self.clock.elapsed() - started,
            ),
            source="ArtifactGate",
        )

    def _paths_for(self, spec: GateSpec) -> list[str]:
        """Artifacts for THIS spec: the sentinel command's tail, or the gate's own list."""
        if spec.command and spec.command[0] == ARTIFACT_GATE_COMMAND:
            raw: Sequence[str | os.PathLike[str]] = spec.command[1:]
        else:
            raw = self.artifacts
        resolved: list[str] = []
        for item in raw:
            text = os.fspath(item)
            if spec.cwd and not os.path.isabs(text):
                text = os.path.join(spec.cwd, text)
            resolved.append(text)
        return resolved


@dataclass(frozen=True, kw_only=True)
class CommandGate:
    """Run a :class:`~selfloop.contracts.GateSpec` and read the counts it printed.

    **This runner takes a COMMAND TO EXECUTE and never a verdict to record.**
    There is no constructor argument, no spec field and no environment variable
    through which a caller can hand in ``passed=True``. That single constraint is
    what makes the outcome ledger worth reading, and it costs nothing: the
    predecessor's evidence directory contains a hand-written prose blob with a
    ``gate_verdict`` key sitting next to the signed records, and the only reason
    it never counted is that the loader refuses anything it did not mint itself.

    The verdict is ``exit status 0`` AND ``zero failed checks`` AND ``every
    collected check accounted for as a pass`` (see
    :attr:`CheckCounts.fully_accounted`). Exit status is a necessary condition
    and never a sufficient one — a command can exit 0 having collected nothing,
    and that is the vacuous pass this whole module exists to refuse.

    Counted evidence comes from :func:`parse_check_counts`: either the explicit
    ``selfloop-checks:`` line or a pytest summary. If your gate is neither, print
    the line — it is one ``echo`` and it is the difference between a gate and a
    rumour.

    Unavailability, all of which raises :class:`~selfloop.contracts.GateUnavailable`
    rather than settling adverse, because absence of evidence is not evidence of
    failure and a refusal recorded as a failure feeds the auto-pause floor:

    * the working directory does not exist;
    * the command is not on the PATH or is not executable;
    * the process could not be spawned at all;
    * the command exceeded ``spec.timeout_s``: it ran, but it never ruled;
    * the output carried no counted evidence, including a collection error — a
      suite that could not be collected did not test anything.

    Two honest limits. ``env`` is an OVERLAY on *base_env* (the process
    environment by default), so a gate inherits whatever the worker was started
    with — pass ``base_env={}`` for a hermetic gate, and be aware that
    ``PYTEST_ADDOPTS`` and ``PYTEST_PLUGINS`` in an inherited environment can
    weaken a pytest gate from outside the code you reviewed. And the timeout
    kills the command, not its process group; a gate that spawns background
    children can leave them behind. If that matters to you, supply a runner that
    manages a process group — the port is one method wide.
    """

    clock: Clock
    #: Base environment the spec's ``env`` overlays. ``None`` means the current
    #: process environment, read at run time — never at import.
    base_env: Mapping[str, str] | None = None
    #: Which rung of the evidence ladder this particular command sits on.
    #: Declare it honestly: a suite that re-reads the loop's own output files is
    #: ``LOCAL_ARTIFACT``; a probe that asks a supervisor, a mail provider or a
    #: URL served by somebody else is ``SYSTEM_OF_RECORD``. Nothing enforces
    #: this — it is a claim you are making to whoever reads the ledger.
    evidence_grade: EvidenceGrade = EvidenceGrade.LOCAL_ARTIFACT
    #: How much of the command's output to keep on the receipt. Bounded because
    #: a receipt is a row in a ledger, not a log file.
    detail_chars: int = 600

    def run(self, spec: GateSpec) -> GateReceipt:
        """Execute *spec* and report what happened. Raises ``GateUnavailable``."""
        started = self.clock.elapsed()
        if spec.cwd and not os.path.isdir(spec.cwd):
            raise GateUnavailable(
                "cwd_missing",
                f"gate working directory does not exist: {spec.cwd!r} — the gate was not run, "
                "so nothing has been ruled on",
            )

        env = dict(os.environ if self.base_env is None else self.base_env)
        env.update(spec.env)

        try:
            completed = subprocess.run(  # noqa: S603 - the spec IS the caller's command
                list(spec.command),
                cwd=spec.cwd or None,
                env=env,
                capture_output=True,
                timeout=spec.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GateUnavailable(
                "command_not_found",
                f"gate command {spec.command[0]!r} was not found: {exc}",
            ) from exc
        except PermissionError as exc:
            raise GateUnavailable(
                "command_not_executable",
                f"gate command {spec.command[0]!r} is not executable: {exc}",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GateUnavailable(
                "timeout",
                f"gate exceeded {spec.timeout_s}s and was killed: it ran but never ruled, "
                "which is an absence of evidence and not a verdict against the work",
            ) from exc
        except OSError as exc:
            raise GateUnavailable(
                "spawn_failed",
                f"gate command could not be started: {exc}",
            ) from exc

        # Decoded permissively: a gate whose output is not valid UTF-8 has still
        # told us something, and a UnicodeDecodeError inside the grader would
        # turn a readable verdict into a crash.
        stdout = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
        stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
        output = f"{stdout}\n{stderr}"
        counts = parse_check_counts(output)

        label = spec.label or " ".join(spec.command)
        detail = (
            f"{label}: exit {completed.returncode}, "
            f"{counts.passed} passed, {counts.failed} failed, "
            f"{counts.skipped} skipped, {counts.deselected} deselected"
        )
        return _guard_receipt(
            GateReceipt(
                passed=(
                    completed.returncode == 0 and counts.failed == 0 and counts.fully_accounted
                ),
                checks_collected=counts.collected,
                checks_passed=counts.passed,
                checks_skipped=counts.skipped,
                checks_deselected=counts.deselected,
                detail=detail,
                ran_at=self.clock.now_iso(),
                duration_s=self.clock.elapsed() - started,
            ),
            source=label,
            detail=(
                f"exit {completed.returncode}; no {CHECKS_LINE_PREFIX} line and no test "
                f"summary in the last {self.detail_chars} characters of output: "
                f"{_tail(output, self.detail_chars)!r}"
            ),
        )


@dataclass(frozen=True)
class NullGate:
    """A gate that refuses to rule. Choosing it is a decision, not a default.

    Every tick under this gate settles ``neutral/uncorroborated``: not accepted,
    not blamed, and — this is the part that matters — **not evidence**. Since the
    learning pass only mines non-neutral outcomes, a loop configured with a
    :class:`NullGate` produces no signals, forms no clusters and can never
    promote a lesson. It can run forever and it cannot learn. That is not a bug
    in this class; it is the honest consequence of having no independent
    verifier, made visible rather than hidden.

    It exists for exactly two situations. The first is a loop that genuinely has
    no result worth grading and is not expected to learn — a pure notifier. The
    second is a loop being brought up, where you want ticks in the ledger before
    you have written a gate, and you want every one of them stamped
    uncorroborated so that nobody later mistakes them for a track record.

    Raising rather than returning ``GateReceipt(passed=False)`` is deliberate:
    "no verifier is configured" is an absence, and recording an absence as a
    failure feeds the acceptance floor with non-evidence and auto-pauses a loop
    that was working. Raising rather than returning a zero-check
    ``passed=True`` needs no explanation — that is the vacuous gate this module
    exists to refuse.

    If you want a real gate and do not know where to start, use
    :class:`ArtifactGate`. It is four lines to configure and it cannot pass
    without something having been produced.
    """

    #: Free-text note, surfaced in the detail so an operator reading a ledger
    #: full of uncorroborated ticks can find out who decided that and why.
    reason: str = "this loop is configured with no independent verifier"

    def run(self, spec: GateSpec) -> GateReceipt:
        """Always raises ``GateUnavailable``. It never returns a receipt."""
        raise GateUnavailable(
            "null_gate",
            f"{self.reason} — every tick settles neutral/uncorroborated, no non-neutral "
            "evidence is written, and this loop will never promote a lesson",
        )


def _tail(text: str, limit: int) -> str:
    """The last *limit* characters, for putting bounded output on a receipt."""
    stripped = text.strip()
    if limit <= 0 or len(stripped) <= limit:
        return stripped
    return f"...{stripped[-limit:]}"


__all__ = [
    "ARTIFACT_GATE_COMMAND",
    "CHECKS_LINE_PREFIX",
    "ArtifactGate",
    "CheckCounts",
    "CommandGate",
    "NullGate",
    "parse_check_counts",
]
