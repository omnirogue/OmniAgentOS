"""Objective gate for the ``grandfather_clock_html`` loop.

Mechanical verification only. NOT the model's opinion of its own output.

WHAT THIS GATE GRADES
---------------------

The loop's ``publish`` files its artifact into the operator's declared output
directory — ``~/omniagentos-output/Grandfather-Clock-<ET date>/clock.html``
— and this gate reads exactly that file. The path is stable across every context
the gate runs in (settlement gate workspace, local checkout, cwd isolation)
because it depends only on the operator's contract, and ``HOME`` survives the
gate runner's environment sanitisation by design.

BOUND TO THE RUN
----------------

Until 2026-08-02 this gate took "latest dated directory wins" with no binding to
any run, and the loop never wrote that directory at all — only a hand-run script
did. An independent review pointed ``HOME`` at a fake home containing a
hand-written ``Grandfather-Clock-<yesterday>/clock.html`` and the gate returned
9 passed: stale AND foreign. A gate that certifies an artifact no run produced
keeps a dead loop's acceptance at 1.0 forever, so the ≥3-adverse auto-pause floor
can never trip.

:func:`get_artifact_path` now refuses unless the artifact is attributable:

* the newest dated directory must hold ``clock.html`` (no falling back to an
  older directory — an artifact that stopped being produced is a refusal, not a
  reason to reach further back);
* the file must carry this loop's ``<meta name="loop-instance">``, a well-formed
  ``<meta name="loop-run">`` and a parseable ``<meta name="loop-published-at">``;
* the stamp must be no more than :data:`MAX_CLOCK_SKEW_SECONDS` in the future;
* the file's mtime must corroborate the stamp to within
  :data:`MAX_FILING_SKEW_SECONDS`;
* the file's INODE must have last changed no later than the stamp claims, to
  the same tolerance — ``st_ctime`` is the one timestamp a metadata-preserving
  copier cannot restore, and it is what refuses a ``cp -p`` replay that walks
  past the mtime rule;
* the stamp's Eastern calendar day must equal the directory's date, so the
  directory name is a fact about the artifact rather than a label anyone can
  type, and must be within :data:`MAX_ARTIFACT_AGE_DAYS` clock days of today.

That is attribution, and it is not origin. It can prove the artifact names A
run, and that its bytes arrived on this filesystem when it says they did; it
cannot prove the artifact belongs to THE run under judgement, because
``_sanitized_env`` gives this process no run id to compare against, and it
cannot prove that ``publish`` — rather than anything else running as the
operator — wrote it. See "ORIGIN" in :func:`get_artifact_path` for exactly which
adversary each rule stops, measured rather than argued. The other half of the
binding lives in the instance's ``verify()``, which runs inside the run and
checks the filed artifact against ``args["candidate"]["run_id"]``.

WHAT THIS GATE CANNOT SAY, AND WHY IT MATTERS HERE
--------------------------------------------------

Settlement has three outcomes. ``routines_settle`` records FAVOURABLE
(``gate_passed=1``), ADVERSE (``gate_passed=0``) and — for absence of evidence —
``unavailable``, which writes NULL/NULL, stays out of the acceptance floor's
denominator, and never counts toward the ≥3-adverse auto-pause. That third
bucket is exactly what "the grader could not run" is, and it is why
``GateWorkspaceUnusable`` is deliberately NOT a ``GateEvidenceRefusal``.

**A pytest gate cannot reach it.** ``unavailable`` is produced only PARENT-side,
by :func:`~omniagentos.scheduler.gate_runner.produce_gate_evidence`, from the
CLASS of exception raised in the settling process: a dirty workspace, an infra
error, an unloadable evidence store, no runner configured. What crosses the
process boundary from a gate is ``exit_code`` plus the four check counts, the
deselected count and the node inventory digest — and
:func:`~omniagentos.scheduler.gate_evidence.evidence_rejections` maps every
combination other than "exit 0, ≥1 collected, all passed, none skipped, none
deselected" to ``gate_passed=0``. There is no exit code, marker, or count that
means "I could not judge". Skipping does not work either: skipped checks are an
explicit rejection.

So when no JS engine exists, this gate's only honest options are to pass an
ungraded clock or to fail one it never looked at. It fails, because the first
option certified two reviewers' deliberately-wrong clocks (see
:func:`test_rendered_time_matches_america_new_york`), and a gate that can be
made green by a broken artifact is worse than no gate. The refusal names the
environment as the cause so the operator is not sent to debug the clock.

Closing this properly is a one-module change OUTSIDE this lane, and it is worth
making: give ``PytestGateRunner`` a sentinel exit code that raises
``GateExecutionInfraError`` (already mapped to ``unavailable``), and let a gate
exit with it via ``pytest.exit(..., returncode=N)``. It was not done here
because it widens a trust boundary every gate in the system shares — any gate
could then exempt itself from the acceptance floor — and that deserves its own
review rather than riding along with a clock fix.

BEHAVIOUR, NOT SUBSTRINGS
-------------------------

``test_rendered_hour_matches_timezone`` used to be two substring assertions with
a name that promised a rendering. The same review replaced the clock's time
source with ``Date.now() - 4*3600*1000`` (a fixed offset, wrong four months a
year), kept the required substrings alive in dead code, and the gate returned
9 passed again. :func:`test_rendered_time_matches_america_new_york` now EXECUTES
the clock's script — under node, at fixed instants on both sides of the DST
boundary, with the process timezone set to zones that are not Eastern — and
compares the rendered readout and hand angles against
``zoneinfo.ZoneInfo("America/New_York")``.

Where no JS engine exists it REFUSES rather than falling back to source
analysis. :func:`_static_time_logic_refusals` survives as an independent check
of the two defects that are genuinely decidable from the source, but it is no
longer a grading PATH, because two reviewers independently proved it is not one:
see :func:`test_rendered_time_matches_america_new_york`.

"Node exists" is also no longer the question. :func:`_node_binary` looks past
``PATH`` at the known install locations and then PROBES the engine — a node that
errors, or one built without the ICU data to resolve ``America/New_York``, is a
broken grader, not a broken clock, and an engine whose ``timeZone`` silently
answers UTC would certify the defect this file exists to catch.

RUNNING IT
----------

The module is marked ``e2e`` (the repo's category for live user-visible outcome
probes) so ``make test`` is not hostage to whether the loop ran in the last hour.
``gate_runner`` executes gates with ``-o addopts=``, which clears the default
marker deselection, so the routine's gate runs every check. By hand, mirror it::

    .venv/bin/python -m pytest -q -o addopts= tests/test_grandfather_clock_gate.py
"""

import functools
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.e2e

INSTANCE_ID = "grandfather_clock_html"

# --------------------------------------------------------------------------
# The filing convention, DUPLICATED from
# loops/omniagentos_loops/instances/grandfather_clock_html.py on purpose.
#
# `gate_runner` derives the interpreter from the target path and `tests/` is
# repo-class, so this file executes on the PRODUCTION venv — which has no
# `omniagentos_loops` on its path and cannot import the instance. Same boundary,
# same reason, as `parent_seam` restating `loop_effects`' protocol constants
# rather than importing them. The instance side is authoritative; drift here
# shows up as this gate refusing a good artifact, which is the safe direction.
# --------------------------------------------------------------------------
CLOCK_ZONE = "America/New_York"
OPERATOR_OUTPUT_SUBPATH = ("Work", "OmniAgentOS", "Development")
OUTPUT_DIR_PREFIX = "Grandfather-Clock-"
ARTIFACT_NAME = "clock.html"
INSTANCE_META = "loop-instance"
PUBLISHED_AT_META = "loop-published-at"
RUN_META = "loop-run"
RUN_ID_PATTERN = r"[0-9a-f]{32}"
MAX_FILING_SKEW_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 60

#: How many CLOCK days back an artifact may be dated. 1 = today or yesterday in
#: ``America/New_York``. See :func:`get_artifact_path` for why this replaced a
#: wall-clock settlement window.
MAX_ARTIFACT_AGE_DAYS = 1

DATED_DIR_RE = re.compile(r"^Grandfather-Clock-(\d{4}-\d{2}-\d{2})$")

#: Fixed instants, so the behaviour check cannot flake and cannot be satisfied
#: by an offset that happens to be right today. ``2026-08-02T05:24:04Z`` is EDT
#: (UTC-4) and the two January/December instants are EST (UTC-5); a clock with a
#: hardcoded -04:00 is wrong at the latter two, and a clock that re-parses a
#: locale string is wrong at all three in any non-Eastern viewer zone.
FIXED_INSTANTS_MS = (
    1785648244000,  # 2026-08-02 01:24:04 EDT
    1768454644000,  # 2026-01-15 00:24:04 EST (midnight hour: h24-cycle trap)
    1798221600000,  # 2026-12-25 13:00:00 EST
)

#: Viewer timezones. Eastern is the CONTROL — the defect this catches is
#: invisible there, which is exactly why it survived review on an Eastern Mac.
VIEWER_ZONES = ("UTC", "Asia/Kolkata", "America/New_York")

#: Names of the two outcomes of the behavioural check. They appear in the
#: parametrised case ids and therefore in ``checks_collected`` and
#: ``node_inventory_digest`` — the only two recorded fields that can carry them.
#: See :func:`grading_cases`.
GRADING_NODE = "graded-by-node"
GRADING_UNGRADED = "UNGRADED-no-js-engine-grader-environment-defect"

#: A JS harness that freezes the clock's instant and stubs the DOM, so the
#: artifact's own ``updateClock`` can be executed and its output read back.
NODE_HARNESS = """
const FIXED_MS = Number(process.env.CLOCK_FIXED_MS);
const RealDate = Date;
class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) { super(FIXED_MS); } else { super(...args); }
  }
  static now() { return FIXED_MS; }
}
globalThis.Date = FrozenDate;
const __els = {};
globalThis.document = {
  getElementById(id) {
    if (!__els[id]) { __els[id] = { style: {}, textContent: "" }; }
    return __els[id];
  }
};
globalThis.setInterval = function () { return 0; };
globalThis.window = globalThis;

%(script)s

const read = (id, key) => (__els[id] ? (key === "text" ? __els[id].textContent : __els[id].style.transform) : null);
console.log(JSON.stringify({
  display: read("time-display", "text"),
  hour: read("hour-hand", "transform"),
  minute: read("minute-hand", "transform"),
  second: read("second-hand", "transform")
}));
"""


class _MetaCollector(HTMLParser):
    """Collect ``<meta name=... content=...>`` pairs."""

    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        attrs_dict = {key.lower(): (value or "") for key, value in (attrs or [])}
        name = attrs_dict.get("name")
        if name:
            self.meta[name] = attrs_dict.get("content", "")


def _operator_output_root() -> Path:
    return Path.home().joinpath(*OPERATOR_OUTPUT_SUBPATH)


def _clock_day(moment: datetime):
    return moment.astimezone(ZoneInfo(CLOCK_ZONE)).date()


def _refuse(reason: str) -> NoReturn:
    raise AssertionError(
        f"gate refuses the grandfather clock artifact: {reason}. "
        "This gate certifies the artifact THIS run produced; an artifact it "
        "cannot attribute to a recent run is a failure, not a pass."
    )


def _refuse_ungraded() -> NoReturn:
    """Refuse because the GRADER cannot run, and say so in those words.

    The distinction matters to whoever reads the failure: every other refusal in
    this file is a fact about the artifact and the fix is to fix the clock; this
    one is a fact about the machine and the fix is ``brew install node``. It is
    spelled out because the settlement this produces (``gate_passed=0``) does
    NOT carry the distinction — see the module docstring.
    """
    raise AssertionError(
        f"[{GRADING_UNGRADED}] the clock was NOT GRADED: {_engine_absence_detail()}. "
        "This is a defect in the GRADER'S ENVIRONMENT, not in the artifact — the "
        "clock may be perfect. It is reported as a failure because a pytest gate "
        "has no way to report 'unavailable', and the alternative (grading the "
        "source statically and passing) certified two different reviewers' wrong "
        "clocks: one rendering UTC, one rendering America/Chicago, both wearing "
        "the required zone string as dead code. Install a node with full ICU on "
        "the machine the scheduler runs on, or give this gate an unavailable "
        "channel (see the module docstring)."
    )


def _inode_change_time(path: Path) -> datetime:
    """``st_ctime`` — when this inode last CHANGED — as an aware UTC datetime.

    Compared ONE-SIDED against the stamp: a refusal only when the inode changed
    LATER than the document claims to have been published. The other direction
    has no adversary — a copy can only be made after the thing it copies exists,
    so a replayed artifact's inode is always the newer of the two — and a rule
    with no adversary is only a way to manufacture false adverses.

    IT CAN FIRE ON A CORRECT ARTIFACT, in exactly one way, and this is the
    honest cost of the rule. ctime moves forward on ANY inode change, so a
    genuine clock that is later ``chmod``-ed, hardlinked, renamed, or given an
    extended attribute (macOS adds ``com.apple.macl`` when a sandboxed app is
    granted access to a file) more than :data:`MAX_FILING_SKEW_SECONDS` after
    publication will be refused for something that was never wrong with it.
    Measured: reading the file — which is what the operator does with a clock —
    does NOT move ctime, so the ordinary use of the artifact is safe. The
    exposure is a tool that writes metadata to the operator's tree. It is
    accepted because the alternative is the ``cp -p`` replay walking through,
    and because the failure is loud and its message names the cause.

    A NAMED SEAM ON PURPOSE. This is the one timestamp the SELF-TEST cannot
    forge either — that is the entire reason it is trustworthy — so the one
    hermetic case that needs a genuinely four-hour-old artifact substitutes this
    reader rather than pretending to age a file. Everything that grades the rule
    itself makes a real copy and reads the real value.
    """
    return datetime.fromtimestamp(path.stat().st_ctime, UTC)


def get_artifact_path() -> Path:
    """The clock artifact, or a refusal.

    Returns the artifact in the NEWEST dated directory, and only if that
    artifact carries a self-consistent, attributable run stamp. There is no
    fallback to an older directory and no "latest wins": a stale or foreign file
    must not be reachable from here, because every other check in this module
    reads whatever this function hands back.

    WHAT REPLACED THE 3600-SECOND SETTLEMENT WINDOW
    -----------------------------------------------
    Until 2026-08-02 this function refused unconditionally when the stamp (or
    the mtime) was more than an hour old. That number was an assumption about
    the CONTROL PLANE — how long publish→settlement takes — dressed as a
    property of the artifact, and a rule like that condemns good work for the
    scheduler's sins: a correct clock published at 06:00 and settled at 07:01
    after a scheduler crash settled ADVERSE, and three adverse settlements trip
    the auto-pause floor. This repo has already paid that bill once
    (routine_runs 741-749, passing gates scored adverse by a clock comparison
    that was about elapsed real time rather than about the evidence).

    Three rules replace it. The first two cannot fire on a correct artifact at
    all; the third can, in one named way that is spelled out where it is defined
    rather than hidden behind "and neither can fire on a correct artifact",
    which is what the previous draft of this paragraph said about two rules and
    would now have been false about three.

    * **The mtime must corroborate the stamp** (within
      :data:`MAX_FILING_SKEW_SECONDS`). ``publish`` takes the instant, renders
      the stamp and writes the bytes inside one call, so a real artifact's mtime
      is always within milliseconds of what it claims. This is a statement about
      one filing operation and involves no assumption about when anyone reads
      it. It also does what the old mtime window was reaching for and missed —
      but only against a copier that does not preserve metadata. ``cp`` gives
      the copy a fresh mtime against an old stamp and this rule refuses it;
      ``cp -p`` and ``rsync -a`` restore the mtime and walk straight past it.
      See ORIGIN below for the rule that does not let them.

    * **The artifact's inode must be as old as the stamp** (also
      :data:`MAX_FILING_SKEW_SECONDS`, and one-sided — see
      :func:`_inode_change_time`).

    * **The artifact's clock day must be current** (today or yesterday in
      ``America/New_York``, :data:`MAX_ARTIFACT_AGE_DAYS`). The unit is the
      artifact's OWN — it names its day in its directory and in its stamp — and
      the horizon is the clock's day rather than the scheduler's latency.

      WHAT THAT COSTS, PLAINLY. This is a LOOSER rule than the hour it replaced,
      and it gives up a detection the gate used to make. Under the old window a
      tick whose ``publish`` had silently stopped working — leaving the previous
      artifact in place while the run still reported ``completed``, the shape
      this lane measured on 2026-08-02 and fixed at its root with a per-run
      publish key — went red within the hour. Under this rule the same artifact
      settles FAVOURABLE for the rest of the Eastern day. This gate is no longer
      a fast detector of a loop whose publishing has died.

      AND THERE IS NO MISSED-TICK ALARM TO CATCH IT INSTEAD. An earlier draft of
      this docstring justified the widening by saying such an outage is one
      "which the routine's own missed-tick alarm reports first". **No such alarm
      exists in this repository.** What exists is
      ``scripts/health-sentinel/health_sentinel.py::check_scheduler``, which
      fails when ``var/log/routines.log`` carries no tick record from the
      SCHEDULER in 15 minutes, and ``steward/alerts/rules.py``'s
      ``reliability_deadman``, which watches the twice-daily reliability audit.
      Neither is per-routine; neither can say "``grandfather_clock_html`` has
      not published today". Citing a detector that does not exist, to justify
      removing one that did, is the same defect as a gate certifying an artifact
      it never looked at — so it is written down here rather than repaired in
      prose.

      WHAT DOES COVER THE STALE-PUBLISH CASE, AND HOW FAR. The instance's
      ``verify()`` compares the FILED artifact against
      ``args["candidate"]["run_id"]`` and fails the receipt when the file on disk
      belongs to an earlier run — the case above, caught inside the tick rather
      than an hour later (``test_verify_refuses_a_predecessors_artifact``). That
      is real, and faster than the window ever was. Its limit is the rule in
      ``routines_settle``: a self-report may lower a verdict and may never raise
      one, so it is a control against a BROKEN loop, not against a lying one.

      WHAT NOTHING COVERS: a routine that stops ticking at all. Note that the
      old window did not cover it either — ``routines_settle`` settles rows in
      ``routine_runs``, so a routine that produces no runs produces no
      settlements and this gate is never executed. A dead loop is invisible to
      every rule in this file, at an hour or at a day; what changed is only the
      publishing-is-dead case above. That gap is ACCEPTED DEBT, recorded here
      and not paid: an alarm for the ABSENCE of ticks cannot live inside a
      per-run gate, and building it is its own lane.

    ORIGIN: WHICH ADVERSARY THE TIME RULES STOP, AND WHICH THEY DO NOT
    -------------------------------------------------------------------
    The replay defence used to be stated as "a COPY of a valid artifact carries
    a fresh mtime and an old stamp". That is true of ``cp``. It is FALSE of
    every copier that preserves metadata, and the self-test only ever built
    fresh-mtime copies, which is why the hole looked closed. Measured on this
    machine (macOS 26.6, APFS, ``/usr/bin/rsync`` = openrsync) on 2026-08-02,
    copying a file whose mtime had been set three hours back:

    ====================  ==========  ==========  ==============
    copier                mtime       st_ctime    st_birthtime
    ====================  ==========  ==========  ==============
    ``cp``                fresh       fresh       fresh
    ``cp -p``             PRESERVED   fresh       PRESERVED
    ``rsync -a``          PRESERVED   fresh       PRESERVED
    ``ditto``             PRESERVED   fresh       PRESERVED
    ``tar xpf``           PRESERVED   fresh       PRESERVED
    ``shutil.copy2``      PRESERVED   fresh       PRESERVED
    ====================  ==========  ==========  ==============

    Birthtime looks like the answer and is not one: on APFS, moving mtime
    backwards drags birthtime back with it, so ``os.utime`` alone forges it, and
    ``setattrlist(ATTR_CMN_CRTIME)`` sets it outright (measured: returns 0, and
    the value takes). ``st_ctime`` is the only column no copier filled in.
    Nothing in userspace sets it: ``utimes`` moves atime and mtime and stamps
    ctime with NOW, and ``setattrlist(ATTR_CMN_CHGTIME)`` returns success on
    this kernel while changing nothing (measured). Reading the file does not
    move it; rename, hardlink, ``chmod`` and xattr writes move it FORWARD only.

    **STOPPED** — replay of bytes that already existed somewhere else: a
    metadata-preserving copy by any tool in the table, an archive extraction, a
    backup restore, a hardlink, a ``mv`` of an older artifact into today's
    directory. All of them put the bytes on this filesystem long after the
    instant the document claims, and none of them can say otherwise. That is the
    shape the run-attribution hole actually took on disk, and it is now
    executable in the self-test rather than asserted here.

    **NOT STOPPED** — contemporaneous fabrication. Anything running as the
    operator can write a fresh document NOW, carrying a fresh stamp and a
    freshly invented 32-hex run id, and satisfy every rule in this function,
    because the bytes really were created now — by someone. This gate holds no
    key, is handed no run id, and cannot tell ``publish``'s write from any other
    write to the same path. Nor does it stop an adversary who can move the
    system clock backwards (ctime follows the clock), or mount a filesystem of
    its own at that path.

    That limit is structural and it is not going to be closed from here: the
    worker under judgement chooses the candidate, owns both artifact locations,
    and holds ``--db`` in its own argv (``loop_jobs.py:322``). A gate that reads
    only the filesystem can never make ORIGIN unforgeable, and claiming
    otherwise is the failure mode this whole file is a reaction to. What the
    ctime rule buys is narrower and real: **forgery must now be CURRENT.** One
    good day's artifact can no longer be re-presented, day after day, by a
    ``cp -p`` in a cron job; a forger has to mint today's bytes today, every
    tick, which is a live process doing the loop's work rather than a copy
    standing in for it. Both halves are pinned as tests —
    ``test_gate_refuses_a_metadata_preserving_copy`` makes a real ``cp -p`` and
    expects a refusal, and ``test_gate_accepts_a_contemporaneous_forgery``
    hand-writes a fake and expects a PASS, so nobody can read this rule as proof
    of origin.

    RUN ATTRIBUTION IS ENFORCED IN THE LOOP, NOT HERE
    -------------------------------------------------
    This function requires a well-formed ``loop-run`` id, so an artifact no run
    can be named for is refused — but it CANNOT check that the id belongs to the
    run under judgement, because ``gate_runner._sanitized_env`` admits only
    PATH/HOME/LANG/LC_ALL/TMPDIR/SYSTEMROOT: there is no database, no routine id
    and no run id in this process. The binding lives where the run identity
    exists, in the instance's ``verify()``, which compares the artifact on disk
    against ``args["candidate"]["run_id"]``. That is not a weaker place for it —
    a failed ``verify`` makes the receipt unsuccessful and the tick non-
    favourable, and a self-report may lower a verdict even though it may never
    raise one.
    """
    now = datetime.now(UTC)
    base_dir = _operator_output_root()

    if not base_dir.is_dir():
        _refuse(f"the operator output directory {base_dir} does not exist")

    dated = sorted(
        (path for path in base_dir.iterdir() if path.is_dir() and DATED_DIR_RE.match(path.name)),
        key=lambda path: path.name,
    )
    if not dated:
        _refuse(
            f"no {OUTPUT_DIR_PREFIX}YYYY-MM-DD directory in {base_dir}; "
            "the loop has never filed an artifact here"
        )

    latest = dated[-1]
    artifact = latest / ARTIFACT_NAME
    if not artifact.is_file():
        _refuse(f"{artifact} is missing (newest dated directory is {latest.name})")

    content = artifact.read_text(encoding="utf-8", errors="replace")
    collector = _MetaCollector()
    collector.feed(content)
    meta = collector.meta

    instance = meta.get(INSTANCE_META, "")
    if instance != INSTANCE_ID:
        _refuse(
            f"{artifact} carries <meta name={INSTANCE_META!r}> = {instance!r}, "
            f"expected {INSTANCE_ID!r} — nothing proves a loop wrote this file"
        )

    run_id = meta.get(RUN_META, "")
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        _refuse(
            f"{artifact} carries <meta name={RUN_META!r}> = {run_id!r}, which is not a "
            "run id — no run can be named for this artifact"
        )

    raw_stamp = meta.get(PUBLISHED_AT_META, "")
    try:
        published_at = datetime.strptime(raw_stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        _refuse(
            f"{artifact} has no parseable <meta name={PUBLISHED_AT_META!r}> "
            f"(found {raw_stamp!r}); an unstamped file is not attributable to a run"
        )

    age = (now - published_at).total_seconds()
    if age < -MAX_CLOCK_SKEW_SECONDS:
        _refuse(f"{artifact} is stamped {abs(age):.0f}s in the FUTURE ({raw_stamp})")

    stamped_day = _clock_day(published_at)
    stamped_dir = f"{OUTPUT_DIR_PREFIX}{stamped_day.isoformat()}"
    if latest.name != stamped_dir:
        _refuse(
            f"{artifact} is stamped {raw_stamp} ({CLOCK_ZONE} day "
            f"{stamped_day.isoformat()}) but sits in {latest.name}; "
            f"expected {stamped_dir}"
        )

    days_old = (_clock_day(now) - stamped_day).days
    if days_old > MAX_ARTIFACT_AGE_DAYS:
        _refuse(
            f"{artifact} is dated {CLOCK_ZONE} day {stamped_day.isoformat()}, {days_old} "
            f"clock days before today — the loop has not filed a current clock. This is "
            "the artifact's own calendar, not a settlement deadline: reaching it means "
            "the loop stopped producing, not that judgement was slow"
        )

    mtime = datetime.fromtimestamp(artifact.stat().st_mtime, UTC)
    filing_skew = (mtime - published_at).total_seconds()
    if abs(filing_skew) > MAX_FILING_SKEW_SECONDS:
        _refuse(
            f"{artifact} was last written {mtime.strftime('%Y-%m-%dT%H:%M:%SZ')} but claims "
            f"to have been published {raw_stamp} — {filing_skew:.0f}s apart. `publish` "
            "stamps and writes in one operation, so the file and its stamp disagreeing "
            "means the bytes and the claim have different origins (a COPY of a valid "
            "artifact looks exactly like this)"
        )
    if (now - mtime).total_seconds() < -MAX_CLOCK_SKEW_SECONDS:
        _refuse(
            f"{artifact} has an mtime "
            f"{abs((now - mtime).total_seconds()):.0f}s in the future"
        )

    inode_lag = (_inode_change_time(artifact) - published_at).total_seconds()
    if inode_lag > MAX_FILING_SKEW_SECONDS:
        _refuse(
            f"{artifact} claims to have been published {raw_stamp}, but its inode was "
            f"last changed {inode_lag:.0f}s AFTER that. `publish` creates the file at the "
            "instant it stamps, so this file's bytes arrived here long after the moment "
            "they claim: a metadata-preserving copy (`cp -p`, `rsync -a`, `ditto`, "
            "`tar xp`), an archive extraction, a restore, or a rename of an older "
            "artifact into place. mtime and birthtime are both restorable by the copier "
            "and agree with the stamp; st_ctime is not restorable and does not. See "
            "ORIGIN in get_artifact_path for what this does NOT stop"
        )

    return artifact


class ClockStructureValidator(HTMLParser):
    """Validate that the HTML contains a proper clock structure."""

    def __init__(self):
        super().__init__()
        self.clock_face_count = 0
        self.hour_hand_count = 0
        self.minute_hand_count = 0
        self.second_hand_count = 0
        self.found_tags = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs or [])
        class_str = attrs_dict.get("class", "")

        self.found_tags.append((tag, class_str))

        if "clock-face" in class_str:
            self.clock_face_count += 1
        if "hour-hand" in class_str:
            self.hour_hand_count += 1
        if "minute-hand" in class_str:
            self.minute_hand_count += 1
        if "second-hand" in class_str:
            self.second_hand_count += 1


def _clock_script(content: str) -> str:
    """Every ``<script>`` body in the document, concatenated."""
    bodies = re.findall(r"<script\b[^>]*>(.*?)</script>", content, re.DOTALL | re.IGNORECASE)
    assert bodies, "clock has no <script> block — nothing renders a time"
    return "\n".join(bodies)


def _static_time_logic_refusals(script: str) -> list[str]:
    """Refusals derivable from the time expression WITHOUT executing it.

    Deliberately conservative and specific: each rule names a way to produce a
    wrong Eastern time that the substring checks let through.
    """
    refusals = []

    if re.search(r"new\s+Date\s*\(\s*[^)\s]", script):
        refusals.append(
            "constructs a Date from a value: re-parsing a formatted locale string reads it "
            "as BROWSER-local time, and formatting that instant into the zone again shifts "
            "it twice"
        )
    if re.search(r"Date\.now\s*\(\s*\)\s*[-+*/]", script) or re.search(
        r"[-+*/]\s*Date\.now\s*\(", script
    ):
        refusals.append("does arithmetic on Date.now(): a manual offset, not a zone")
    if "getTimezoneOffset" in script:
        refusals.append("reads getTimezoneOffset(): the viewer's offset is not Eastern")
    if re.search(r"get(?:UTC)?(?:Hours|Minutes|Seconds)\s*\(", script):
        refusals.append(
            "reads raw Date fields: getHours()/getUTCHours() answer in the viewer's zone or "
            "UTC, never in America/New_York"
        )
    for literal in re.findall(r"\b\d+\b", script):
        if literal in {"3600", "86400", "43200"} or len(literal) >= 7:
            refusals.append(f"contains the offset-shaped literal {literal}")
    if script.count(CLOCK_ZONE) < 2:
        refusals.append(
            f"names {CLOCK_ZONE} fewer than twice: the hands and the digital readout must "
            "each be formatted in the zone"
        )
    if "formatToParts" not in script and "Intl.DateTimeFormat" not in script:
        refusals.append("does not use Intl.DateTimeFormat to read wall-clock fields")
    if not re.search(r"toLocaleString\s*\(\s*'en-US'", script):
        refusals.append("does not format the digital readout with toLocaleString('en-US', ...)")

    return refusals


#: Absolute places a JS engine lives on this class of machine, tried when PATH
#: does not name one. ``gate_runner`` passes the scheduler's PATH through
#: unchanged, and the scheduler's PATH comes from launchd running ``/bin/sh -lc``
#: — so today ``/opt/homebrew/bin`` is on it only because ``/etc/paths.d``
#: happens to list it. Nobody chose that, and it can be un-chosen by a Homebrew
#: reinstall. Looking in the known locations too means "no engine" reports a
#: MACHINE with no node, not a thin PATH, which is what keeps the refusal below
#: rare enough to be honest.
NODE_FALLBACK_PATHS = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)

#: Proves the engine can do the ONE thing this gate needs: resolve an IANA zone
#: through Intl. A node built without full ICU answers every ``timeZone`` with
#: UTC and silently renders the exact defect this gate exists to catch, so an
#: engine that fails this is not an engine for our purposes.
ENGINE_PROBE = (
    "const p = new Intl.DateTimeFormat('en-US', "
    "{timeZone: 'America/New_York', hour: '2-digit', hour12: false})"
    ".formatToParts(new Date(1798221600000));"
    "const h = p.find((x) => x.type === 'hour').value;"
    "if (Number(h) !== 13) { throw new Error('ICU tz data missing: got hour ' + h); }"
    "console.log('ok');"
)


def _candidate_node_binaries() -> list[str]:
    found = shutil.which("node")
    candidates = [found] if found else []
    candidates += [path for path in NODE_FALLBACK_PATHS if Path(path).exists()]
    seen, unique = set(), []
    for path in candidates:
        resolved = str(Path(path).resolve()) if Path(path).exists() else path
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


@functools.cache
def _engine_is_usable(node: str) -> tuple[bool, str]:
    """Does *node* actually execute JS and resolve ``America/New_York``?

    Cached: :func:`grading_cases` asks at collection and every case asks again,
    and the answer cannot change inside one gate run without the workspace
    changing under it (which ``workspace_tree_clean`` already condemns).
    """
    try:
        proc = subprocess.run(
            [node, "-e", ENGINE_PROBE],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{node}: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"{node}: exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    if proc.stdout.strip() != "ok":
        return False, f"{node}: probe printed {proc.stdout.strip()[:200]!r}"
    return True, node


def _node_binary() -> str | None:
    """A JS engine that is present AND proven able to resolve the zone.

    Presence alone was the old test, and it treated three different worlds as
    one: no node, a node that errors, and a node whose ICU cannot resolve
    ``America/New_York``. The last two are the dangerous pair — they are not
    "the artifact is broken", they are "the grader is broken", and grading an
    artifact with a formatter that silently answers UTC would certify the exact
    defect this file exists to catch.
    """
    for node in _candidate_node_binaries():
        usable, _detail = _engine_is_usable(node)
        if usable:
            return node
    return None


def _engine_absence_detail() -> str:
    """Why there is no usable engine — for the refusal message."""
    candidates = _candidate_node_binaries()
    if not candidates:
        return (
            "no node binary on PATH or at "
            f"{', '.join(NODE_FALLBACK_PATHS)}"
        )
    return "; ".join(_engine_is_usable(node)[1] for node in candidates)


def _render_with_node(node: str, script: str, *, fixed_ms: int, tz: str) -> dict:
    """Execute the clock's script at a frozen instant in viewer zone *tz*."""
    program = NODE_HARNESS % {"script": script}
    proc = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TZ": tz,
            "CLOCK_FIXED_MS": str(fixed_ms),
            "HOME": str(Path.home()),
        },
    )
    assert proc.returncode == 0, (
        f"the clock's own script failed to execute under node (TZ={tz}): "
        f"{proc.stderr.strip()[:2000]}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _expected(fixed_ms: int) -> dict:
    """The truth, from the standard library's tz database."""
    moment = datetime.fromtimestamp(fixed_ms / 1000, ZoneInfo(CLOCK_ZONE))
    hour12 = moment.hour % 12
    return {
        "hour12": 12 if hour12 == 0 else hour12,
        "minute": moment.minute,
        "second": moment.second,
        "meridiem": "AM" if moment.hour < 12 else "PM",
        "hour_deg": (hour12 / 12) * 360 + (moment.minute / 60) * 30,
        "minute_deg": (moment.minute / 60) * 360 + (moment.second / 60) * 6,
        "second_deg": (moment.second / 60) * 360,
        "iso": moment.isoformat(),
    }


def _degrees(transform: str | None) -> float:
    assert transform, "a clock hand was never given a rotation"
    match = re.search(r"rotateZ\(\s*(-?[\d.]+)deg\s*\)", transform)
    assert match, f"unreadable hand transform {transform!r}"
    return float(match.group(1))


def test_clock_artifact_is_from_this_run():
    """The artifact must name a run, and its bytes must corroborate its claim.

    Every other check in this file reads :func:`get_artifact_path`, so this test
    is a restatement rather than an extra belt — it exists to name the property
    and to make the refusal reason the first thing a reader sees.

    Note what it deliberately does NOT assert: an age in minutes. That was the
    false-adverse rule; see :func:`get_artifact_path` for what replaced it and
    why. What is asserted instead is a run id, a WRITE time that agrees with the
    publication time, and an INODE no younger than the stamp — the third being
    the one a metadata-preserving copy cannot satisfy.

    It is not a claim of origin. A document hand-written NOW, with a fresh stamp
    and an invented run id, satisfies all three; see ORIGIN in
    :func:`get_artifact_path`, and
    ``test_gate_accepts_a_contemporaneous_forgery`` in the self-test, which
    executes that limit so it cannot quietly stop being true.
    """
    path = get_artifact_path()
    content = path.read_text(encoding="utf-8")
    collector = _MetaCollector()
    collector.feed(content)

    assert collector.meta.get(INSTANCE_META) == INSTANCE_ID
    assert re.fullmatch(RUN_ID_PATTERN, collector.meta.get(RUN_META, "")), (
        f"artifact carries no run id: {collector.meta.get(RUN_META)!r}"
    )
    stamped = datetime.strptime(
        collector.meta[PUBLISHED_AT_META], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    assert abs(mtime - stamped) <= timedelta(seconds=MAX_FILING_SKEW_SECONDS), (
        f"artifact stamped {stamped.isoformat()} was written {mtime.isoformat()}"
    )
    changed = _inode_change_time(path)
    assert changed - stamped <= timedelta(seconds=MAX_FILING_SKEW_SECONDS), (
        f"artifact stamped {stamped.isoformat()} has an inode last changed "
        f"{changed.isoformat()} — the bytes arrived after the claim (a copy)"
    )
    assert path.parent.name == f"{OUTPUT_DIR_PREFIX}{_clock_day(stamped).isoformat()}"
    assert (_clock_day(datetime.now(UTC)) - _clock_day(stamped)).days <= MAX_ARTIFACT_AGE_DAYS


def test_clock_file_exists():
    """File must exist and be non-empty."""
    path = get_artifact_path()
    assert path.is_file(), f"Clock artifact not found at {path}"
    content = path.read_bytes()
    assert len(content) > 0, f"Clock artifact is empty: {path}"


def test_clock_parses_as_html():
    """File must parse as valid HTML."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8', errors='replace')
    assert len(content) > 0, "Clock content is empty"

    parser = HTMLParser()
    try:
        parser.feed(content)
    except Exception as e:
        raise AssertionError(f"HTML parsing failed: {e}") from e


def test_clock_contains_iana_timezone():
    """Clock MUST use IANA zone 'America/New_York', not hardcoded offset."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8')

    assert "America/New_York" in content, (
        "Clock must explicitly contain 'America/New_York' IANA timezone string"
    )


def test_clock_no_hardcoded_offsets():
    """Clock MUST NOT contain hardcoded timezone offsets like -04:00 or -05:00."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8')

    # Check for hardcoded offsets (a fixed offset is wrong 4 months/year)
    assert "-04:00" not in content, "Clock contains hardcoded EDT offset -04:00"
    assert "-05:00" not in content, "Clock contains hardcoded EST offset -05:00"
    assert "+04:00" not in content, "Clock contains hardcoded +04:00 offset"
    assert "+05:00" not in content, "Clock contains hardcoded +05:00 offset"


def test_clock_uses_intl_datetime_format():
    """Clock MUST use Intl.DateTimeFormat with timeZone parameter."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8')

    assert "toLocaleString" in content, (
        "Clock must use Intl.DateTimeFormat (toLocaleString)"
    )
    assert "timeZone" in content, (
        "Clock must pass timeZone parameter to Intl.DateTimeFormat"
    )


def test_clock_structure():
    """Clock must contain the proper visual structure."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8')

    validator = ClockStructureValidator()
    validator.feed(content)

    assert validator.clock_face_count > 0, "Clock must have a clock-face element"
    assert validator.hour_hand_count > 0, "Clock must have an hour-hand element"
    assert validator.minute_hand_count > 0, "Clock must have a minute-hand element"
    assert validator.second_hand_count > 0, "Clock must have a second-hand element"


def test_exactly_one_clock_artifact():
    """Only ONE clock artifact should exist (operator scope)."""
    path = get_artifact_path()
    content = path.read_text(encoding='utf-8')

    # Count clock-face divs
    clock_faces = re.findall(r'class\s*=\s*["\']clock-face["\']', content)
    assert len(clock_faces) == 1, f"Expected exactly 1 clock, found {len(clock_faces)}"


def test_time_source_is_not_reparsed_or_offset():
    """The time expression must not re-parse a locale string or bake an offset.

    A static check, and it stands on its own: these are the two ways this clock
    has actually been wrong, and both are decidable from the source.
    """
    content = get_artifact_path().read_text(encoding='utf-8')
    refusals = _static_time_logic_refusals(_clock_script(content))
    assert not refusals, "clock time logic is unsound: " + "; ".join(refusals)


def grading_cases() -> list:
    """One case per graded (instant, viewer zone) — or ONE case naming the
    ABSENCE of a grade, when no JS engine can be found.

    THE CASE IDS ARE THE EVIDENCE CHANNEL. ``gate_runner`` discards the gate's
    stdout (``exit_code, _stdout, _stderr = _run_process_group(...)``) and
    deletes the JUnit XML and the certification inventory with their scratch
    ``TemporaryDirectory`` after reading counts out of them. What survives into
    the signed receipt is ``exit_code``, ``checks_collected/passed/skipped/failed``,
    ``deselected_count`` and ``node_inventory_digest`` — and that digest is
    ``sha256(json.dumps(sorted(collected_nodeids)))``. So a ``print()`` about
    which grading path ran reaches nobody, while the id of the case reaches two
    recorded fields at once, and an ungraded settlement can never be mistaken
    for one that executed the clock.

    Neither field is pinned to an expected value by ``evidence_rejections`` (the
    digest is shape-checked only, and the counts must merely be non-zero and
    all-passing), so varying them is safe.
    """
    if _node_binary() is None:
        return [pytest.param(None, None, id=GRADING_UNGRADED)]
    return [
        pytest.param(fixed_ms, tz, id=f"{GRADING_NODE}-{fixed_ms}-{tz.replace('/', '_')}")
        for fixed_ms in FIXED_INSTANTS_MS
        for tz in VIEWER_ZONES
    ]


@pytest.mark.parametrize(("fixed_ms", "viewer_tz"), grading_cases())
def test_rendered_time_matches_america_new_york(fixed_ms, viewer_tz):
    """EXECUTE the clock and compare its output against zoneinfo truth.

    Not a substring check. The script runs at a fixed instant — the set spans
    both sides of the DST boundary — in a viewer timezone that is usually not
    Eastern, and both the digital readout and all three hand angles are graded
    against ``ZoneInfo("America/New_York")``.

    WHY THE NODE-ABSENT PATH NO LONGER PASSES
    ------------------------------------------
    It used to degrade to :func:`_static_time_logic_refusals` and settle green,
    on the argument that a missing tool is a defect in the GRADER's environment
    and refusing on one manufactures a false adverse. The RULE is right; the
    premise was false, and two independent reviewers broke it with two different
    clocks:

    * every ``America/New_York`` in the real operator artifact replaced with
      ``UTC``, then the zone string re-inserted twice as dead code. Renders UTC;
      the static analyser found nothing; 10 passed.
    * hands formatted in ``America/Chicago`` while the readout stays
      ``America/New_York``, the second zone literal supplied by a comment.
      Renders an hour wrong; the static analyser found nothing; 10 passed.

    The analyser counts zone literals and looks for formatter keywords. It never
    establishes that the zone CONTROLS the formatter, and it cannot: deciding
    what a JS expression computes requires evaluating it. Both counterfeits are
    pinned in the self-test as executable proof, and both die instantly the
    moment the clock is actually run. A degraded grade is acceptable; a grade
    that any wrong clock can wear is not a grade, and settling FAVOURABLE on one
    certifies a lie.

    So with no usable engine this check REFUSES, and says whose fault it is.
    See the module docstring, "WHAT THIS GATE CANNOT SAY", for why it cannot
    instead settle ``unavailable``, which is what this outcome actually is.
    """
    if fixed_ms is None:
        _refuse_ungraded()

    content = get_artifact_path().read_text(encoding='utf-8')
    script = _clock_script(content)
    node = _node_binary()
    assert node, "grading_cases() promised node and it is gone"
    expected = _expected(fixed_ms)
    rendered = _render_with_node(node, script, fixed_ms=fixed_ms, tz=viewer_tz)
    failures = []

    # ICU 72+ renders en-US day periods with U+202F; normalise every
    # whitespace flavour rather than pinning one ICU version's spelling.
    display = re.sub(r"\s+", " ", rendered.get("display") or "")
    match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)", display, re.IGNORECASE)
    if not match:
        failures.append(f"TZ={viewer_tz} @{expected['iso']}: unreadable readout {display!r}")
    else:
        actual = (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4).upper(),
        )
        wanted = (
            expected["hour12"],
            expected["minute"],
            expected["second"],
            expected["meridiem"],
        )
        if actual != wanted:
            failures.append(
                f"TZ={viewer_tz} @{expected['iso']}: readout {display.strip()!r} but "
                f"America/New_York is "
                f"{wanted[0]:02d}:{wanted[1]:02d}:{wanted[2]:02d} {wanted[3]}"
            )

    for hand, key in (("hour", "hour_deg"), ("minute", "minute_deg"),
                      ("second", "second_deg")):
        got = _degrees(rendered.get(hand))
        if abs(got - expected[key]) > 1e-6:
            failures.append(
                f"TZ={viewer_tz} @{expected['iso']}: {hand} hand at {got}deg, "
                f"America/New_York wants {expected[key]}deg"
            )

    assert not failures, "clock disagrees with America/New_York:\n" + "\n".join(failures)
