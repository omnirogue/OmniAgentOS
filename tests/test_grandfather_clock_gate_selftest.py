"""Self-test for the grandfather clock GATE — does the gate catch what it claims?

``tests/test_grandfather_clock_gate.py`` is an ``e2e`` outcome probe: it reads
the artifact the loop filed into the operator's tree, so it can only be honest
about an artifact that exists. This file grades the gate's own machinery against
clocks it builds itself, in a temporary home, and is therefore hermetic and part
of the default suite.

It exists because a gate that has never failed on the real defect is not a gate,
and because the counterfeit corpus needs node ids it can drive: the harness runs
``must_fail`` node ids WITHOUT ``-o addopts=``, so anything it names must not be
marker-deselected. Nothing here is marked.

Defects pinned here, all of them shipped:

* the gate accepting an artifact no run produced ("latest dated directory wins");
* the clock re-parsing a formatted locale string, and the fixed-offset
  regression a reviewer wrote to prove the substring checks were vacuous;
* the node-absent grading path degrading SILENTLY, so a statically graded
  settlement was byte-indistinguishable from one that executed the clock;
* that same path settling FAVOURABLE on a clock rendering the wrong time —
  proved twice, by two reviewers, with two different constructions, both of
  which live below as executable evidence that the static analyser is not a
  weaker grade but no grade;
* an artifact that no run can be named for, and a copy of a valid artifact —
  including the ``cp -p``/``rsync -a`` copy that RESTORES the mtime and so walked
  straight past the rule that was the whole replay defence;
* a settlement window that condemned a CORRECT artifact for arriving late.

And one thing that is NOT a defect but a boundary:
:func:`test_gate_accepts_a_contemporaneous_forgery` asserts that a document
hand-written now, with a fresh stamp and an invented run id, PASSES. The gate
cannot establish origin — see ORIGIN in ``get_artifact_path`` — and the limit is
pinned here so that no later reader can mistake the copy rule for proof of it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests import test_grandfather_clock_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_SOURCE = (
    REPO_ROOT / "loops" / "omniagentos_loops" / "instances" / "grandfather_clock_html.py"
)

# The instance's own guarantees are graded HERE rather than under
# ``loops/tests/`` for one mechanical reason: the counterfeit harness executes
# every ``must_fail`` node id on the PRODUCTION venv, so a corpus entry cannot
# drive a test that only the loops venv can import. The instance is pure stdlib
# plus ``omniagentos``/``omniagentos_loops`` modules that carry no LangGraph
# dependency, so it imports cleanly here — which is also, conveniently, an
# anti-drift check that the instance stays free of the loop runtime's heavy
# imports.
#
# The path entry is REMOVED again immediately, exactly as
# ``tests/scheduler/test_gate_interpreter.py`` does it. The production venv not
# having ``omniagentos_loops`` on its path is a real boundary that other tests
# reason about (it is why the gate restates the filing convention instead of
# importing it), and a module-scope insert that stayed would quietly grant every
# later test in the session an import the real runtime does not have.
_LOOPS_PATH = str(REPO_ROOT / "loops")
sys.path.insert(0, _LOOPS_PATH)
try:
    from omniagentos_loops.instances import grandfather_clock_html as instance  # noqa: E402
    from omniagentos_loops.templates.generate_evaluate_improve import (  # noqa: E402
        _publish_key,
    )
finally:
    sys.path.remove(_LOOPS_PATH)


def shipped_clock_html() -> str:
    """The clock the loop actually ships, read as TEXT out of the instance.

    Deliberately not an import: the instance lives in the loops venv's package
    and this suite runs on the production venv. Reading the source also makes
    this an anti-drift pin — the template that ships must pass the grading its
    own gate applies, and the default suite says so without needing the operator
    artifact to exist.
    """
    source = INSTANCE_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"CLOCK_TEMPLATE = '''(.*?)'''", source, re.DOTALL)
    assert match, f"CLOCK_TEMPLATE not found in {INSTANCE_SOURCE}"
    return match.group(1)


#: The defect that shipped in 0249b28a: a New York wall-clock string re-parsed
#: as browser-local and then formatted into New York a second time.
DOUBLE_CONVERSION_SCRIPT = """
        function updateClock() {
            const now = new Date();
            const eastTimeStr = now.toLocaleString('en-US', { timeZone: 'America/New_York' });
            const eastTime = new Date(eastTimeStr);
            const hours = eastTime.getHours() % 12;
            const minutes = eastTime.getMinutes();
            const seconds = eastTime.getSeconds();
            document.getElementById('hour-hand').style.transform =
                `rotateZ(${(hours / 12) * 360 + (minutes / 60) * 30}deg)`;
            document.getElementById('minute-hand').style.transform =
                `rotateZ(${(minutes / 60) * 360 + (seconds / 60) * 6}deg)`;
            document.getElementById('second-hand').style.transform =
                `rotateZ(${(seconds / 60) * 360}deg)`;
            document.getElementById('time-display').textContent =
                eastTime.toLocaleString('en-US', {
                    hour: '2-digit', minute: '2-digit', second: '2-digit',
                    hour12: true, timeZone: 'America/New_York'
                });
        }
        updateClock();
"""

#: The reviewer's regression: a hardcoded EDT offset, wrong four months a year,
#: with every substring the old gate looked for kept alive in dead code.
FIXED_OFFSET_SCRIPT = """
        const REQUIRED_SUBSTRINGS_FOR_THE_GATE = [
            "America/New_York",
            "toLocaleString('en-US', {",
            "timeZone: 'America/New_York'",
            "Intl.DateTimeFormat",
            "formatToParts"
        ];
        function updateClock() {
            const eastTime = new Date(Date.now() - 4*3600*1000);
            const hours = eastTime.getUTCHours() % 12;
            const minutes = eastTime.getUTCMinutes();
            const seconds = eastTime.getUTCSeconds();
            document.getElementById('hour-hand').style.transform =
                `rotateZ(${(hours / 12) * 360 + (minutes / 60) * 30}deg)`;
            document.getElementById('minute-hand').style.transform =
                `rotateZ(${(minutes / 60) * 360 + (seconds / 60) * 6}deg)`;
            document.getElementById('second-hand').style.transform =
                `rotateZ(${(seconds / 60) * 360}deg)`;
            const hh = ((hours === 0) ? 12 : hours).toString().padStart(2, '0');
            const mm = minutes.toString().padStart(2, '0');
            const ss = seconds.toString().padStart(2, '0');
            document.getElementById('time-display').textContent =
                hh + ':' + mm + ':' + ss + ' ' + ((eastTime.getUTCHours() < 12) ? 'AM' : 'PM');
        }
        updateClock();
"""


#: The UTC counterfeit (reviewer 1). Every ``America/New_York`` that CONTROLS a
#: formatter replaced with ``UTC``; the zone string re-inserted twice as dead
#: code so the literal count still passes. Renders UTC — four or five hours
#: wrong, every hour of the year, for a clock whose entire purpose is Eastern.
UTC_WITH_DEAD_ZONE_SCRIPT = """
        function updateClock() {
            // eslint-disable-next-line no-unused-vars
            const __zoneDoc = ['America/New_York', 'America/New_York'];
            if (false) { console.log(__zoneDoc); }
            const now = new Date();
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'UTC',
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
            }).formatToParts(now);
            const field = (type) => Number(parts.find((part) => part.type === type).value);
            const hours = field('hour') % 12;
            const minutes = field('minute');
            const seconds = field('second');
            document.getElementById('hour-hand').style.transform =
                `rotateZ(${(hours / 12) * 360 + (minutes / 60) * 30}deg)`;
            document.getElementById('minute-hand').style.transform =
                `rotateZ(${(minutes / 60) * 360 + (seconds / 60) * 6}deg)`;
            document.getElementById('second-hand').style.transform =
                `rotateZ(${(seconds / 60) * 360}deg)`;
            document.getElementById('time-display').textContent =
                now.toLocaleString('en-US', {
                    hour: '2-digit', minute: '2-digit', second: '2-digit',
                    hour12: true, timeZone: 'UTC'
                });
        }
        updateClock();
"""

#: The Chicago counterfeit (reviewer 2), and the subtler of the two: the READOUT
#: is honestly Eastern, only the HANDS are Chicago, and the second zone literal
#: the analyser counts is supplied by a COMMENT. One hour wrong, on the part of
#: a clock a human reads first, in a document that says the right thing twice.
CHICAGO_HANDS_SCRIPT = """
        function updateClock() {
            // Hands and readout both track America/New_York.
            const now = new Date();
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/Chicago',
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
            }).formatToParts(now);
            const field = (type) => Number(parts.find((part) => part.type === type).value);
            const hours = field('hour') % 12;
            const minutes = field('minute');
            const seconds = field('second');
            document.getElementById('hour-hand').style.transform =
                `rotateZ(${(hours / 12) * 360 + (minutes / 60) * 30}deg)`;
            document.getElementById('minute-hand').style.transform =
                `rotateZ(${(minutes / 60) * 360 + (seconds / 60) * 6}deg)`;
            document.getElementById('second-hand').style.transform =
                `rotateZ(${(seconds / 60) * 360}deg)`;
            document.getElementById('time-display').textContent =
                now.toLocaleString('en-US', {
                    hour: '2-digit', minute: '2-digit', second: '2-digit',
                    hour12: true, timeZone: 'America/New_York'
                });
        }
        updateClock();
"""

#: Both wrong clocks, by the name of the reviewer's attack.
WRONG_CLOCKS_THE_STATIC_ANALYSER_ACCEPTS = {
    "renders-UTC-with-dead-zone-literals": UTC_WITH_DEAD_ZONE_SCRIPT,
    "renders-America_Chicago-hands": CHICAGO_HANDS_SCRIPT,
}


def _run_id() -> str:
    return "".join(f"{b:02x}" for b in bytes(range(16)))


def _document(
    script: str,
    *,
    stamp: str | None,
    instance: str | None = gate.INSTANCE_ID,
    run_id: str | None = "",
) -> str:
    """A minimally well-formed clock document wrapping *script*."""
    meta = ""
    if instance is not None:
        meta += f'\n    <meta name="{gate.INSTANCE_META}" content="{instance}">'
    if run_id is not None:
        meta += f'\n    <meta name="{gate.RUN_META}" content="{run_id or _run_id()}">'
    if stamp is not None:
        meta += f'\n    <meta name="{gate.PUBLISHED_AT_META}" content="{stamp}">'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">{meta}
    <title>Grandfather Clock</title>
</head>
<body>
    <div class="clock-face">
        <div class="clock-hand hour-hand" id="hour-hand"></div>
        <div class="clock-hand minute-hand" id="minute-hand"></div>
        <div class="clock-hand second-hand" id="second-hand"></div>
    </div>
    <div class="time-display" id="time-display">--:--:--</div>
    <script>{script}</script>
</body>
</html>"""


def _stamp(offset: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_datetime(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _dir_for(stamp: str) -> str:
    day = _as_datetime(stamp).astimezone(ZoneInfo(gate.CLOCK_ZONE)).date()
    return f"{gate.OUTPUT_DIR_PREFIX}{day.isoformat()}"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A throwaway ``$HOME`` the gate resolves its operator tree under."""
    home = tmp_path / "home"
    (home / "Work" / "OmniAgentOS" / "Development").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _file(home: Path, dir_name: str, document: str, *, mtime: datetime | None = None) -> Path:
    """File *document*, with an mtime that CORROBORATES its own stamp.

    ``publish`` stamps and writes inside one call, so a real artifact's mtime is
    within milliseconds of its stamp and the gate now checks that. A helper that
    always wrote ``now`` would hand every stale-stamp case a fresh mtime — i.e.
    it would silently build the COPY counterfeit — and the case would then pass
    for the wrong reason. *mtime* overrides, for the tests that want exactly
    that disagreement.
    """
    target = home / "Work" / "OmniAgentOS" / "Development" / dir_name / gate.ARTIFACT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    if mtime is None:
        stamped = re.search(
            rf'name="{gate.PUBLISHED_AT_META}" content="([^"]+)"', document
        )
        if stamped:
            mtime = datetime.strptime(stamped.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
    if mtime is not None:
        stamp_epoch = mtime.timestamp()
        os.utime(target, (stamp_epoch, stamp_epoch))
    return target


# ---------------------------------------------------------------------------
# The time expression: the gate must refuse both shipped ways of being wrong.
# ---------------------------------------------------------------------------


def test_static_grading_accepts_the_clock_the_loop_ships():
    """Anti-drift: the shipped template must survive its own gate's grading."""
    script = gate._clock_script(shipped_clock_html())
    assert gate._static_time_logic_refusals(script) == []


def test_static_grading_refuses_a_reparsed_locale_string():
    """The 0249b28a defect: double conversion, wrong outside Eastern."""
    refusals = gate._static_time_logic_refusals(DOUBLE_CONVERSION_SCRIPT)
    assert refusals, "the static grading accepted the double-conversion clock"
    assert any("constructs a Date from a value" in reason for reason in refusals), refusals


def test_static_grading_refuses_a_hardcoded_offset():
    """The reviewer's regression: `Date.now() - 4*3600*1000`, wrong in winter."""
    refusals = gate._static_time_logic_refusals(FIXED_OFFSET_SCRIPT)
    assert refusals, "the static grading accepted the fixed-offset clock"
    assert any("arithmetic on Date.now()" in reason for reason in refusals), refusals


def test_static_grading_refuses_a_clock_that_names_the_zone_only_once():
    """A readout formatted in the zone with hands that are not is still wrong."""
    half_right = """
        function updateClock() {
            const now = new Date();
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/New_York', hour: '2-digit', hour12: false
            }).formatToParts(now);
            document.getElementById('time-display').textContent =
                parts.map((p) => p.value).join('');
        }
        updateClock();
    """
    refusals = gate._static_time_logic_refusals(half_right)
    assert any("fewer than twice" in reason for reason in refusals), refusals


# ---------------------------------------------------------------------------
# The behavioural grading, where node exists.
# ---------------------------------------------------------------------------

_NODE = gate._node_binary()
requires_node = pytest.mark.skipif(_NODE is None, reason="node is not installed")


@requires_node
@pytest.mark.parametrize("fixed_ms", gate.FIXED_INSTANTS_MS)
@pytest.mark.parametrize("viewer_tz", gate.VIEWER_ZONES)
def test_shipped_clock_renders_new_york_in_every_viewer_zone(fixed_ms, viewer_tz):
    """The template's rendered readout must equal zoneinfo truth, everywhere."""
    script = gate._clock_script(shipped_clock_html())
    rendered = gate._render_with_node(_NODE, script, fixed_ms=fixed_ms, tz=viewer_tz)
    expected = gate._expected(fixed_ms)
    display = re.sub(r"\s+", " ", rendered["display"]).strip()
    assert display == (
        f"{expected['hour12']:02d}:{expected['minute']:02d}:"
        f"{expected['second']:02d} {expected['meridiem']}"
    ), f"TZ={viewer_tz} @{expected['iso']}"
    assert abs(gate._degrees(rendered["hour"]) - expected["hour_deg"]) < 1e-6


@pytest.mark.parametrize("attack", sorted(WRONG_CLOCKS_THE_STATIC_ANALYSER_ACCEPTS))
def test_static_analysis_cannot_grade_the_zone_that_controls_the_formatter(attack):
    """THE PREMISE THAT WAS FALSE, pinned so it cannot be believed again.

    Both of these clocks render the WRONG TIME. Both pass every static rule:
    two zone literals, ``formatToParts``, ``toLocaleString('en-US'``, no
    ``new Date(arg)``, no ``Date.now()`` arithmetic, no ``getTimezoneOffset``,
    no offset-shaped literal. That is not a gap to patch with a sharper regex —
    deciding which zone reaches the formatter means evaluating the expression,
    and the analyser does not evaluate anything.

    This test asserts the WEAKNESS, which is unusual and deliberate: if someone
    later teaches :func:`_static_time_logic_refusals` to catch these two by
    name, this goes red and they must come here and decide whether the analyser
    has genuinely become a grade (it will not have — the next counterfeit is one
    zone name away) or whether they have merely over-fitted two known samples.
    """
    script = WRONG_CLOCKS_THE_STATIC_ANALYSER_ACCEPTS[attack]
    assert gate._static_time_logic_refusals(script) == [], (
        f"{attack}: the static analyser now flags this specific counterfeit. It still "
        "cannot decide which zone controls the formatter, so it is still not a grading "
        "path — see the docstring before changing the node-absent behaviour back."
    )


@requires_node
@pytest.mark.parametrize("attack", sorted(WRONG_CLOCKS_THE_STATIC_ANALYSER_ACCEPTS))
def test_execution_catches_both_clocks_the_static_analyser_accepts(attack):
    """...and the ONE grade that works kills both, instantly, at every instant."""
    script = WRONG_CLOCKS_THE_STATIC_ANALYSER_ACCEPTS[attack]
    wrong_at = []
    for fixed_ms in gate.FIXED_INSTANTS_MS:
        rendered = gate._render_with_node(_NODE, script, fixed_ms=fixed_ms, tz="UTC")
        expected = gate._expected(fixed_ms)
        display = re.sub(r"\s+", " ", rendered["display"]).strip()
        truth = (
            f"{expected['hour12']:02d}:{expected['minute']:02d}:"
            f"{expected['second']:02d} {expected['meridiem']}"
        )
        hands_wrong = abs(gate._degrees(rendered["hour"]) - expected["hour_deg"]) > 1e-6
        if display != truth or hands_wrong:
            wrong_at.append(expected["iso"])
    assert wrong_at, (
        f"{attack} rendered America/New_York correctly at every instant — impossible "
        "for a clock formatted in another zone"
    )


def test_no_js_engine_refuses_instead_of_passing_an_ungraded_clock(monkeypatch, fake_home):
    """BLOCKER 1: node absent must not be a favourable settlement.

    Before: the case degraded to static analysis and returned 10 passed for both
    counterfeits above. Now the ONE collected case refuses, names the grading
    path in its id, and names the GRADER'S ENVIRONMENT as the cause so nobody is
    sent to debug a clock that may be perfect.
    """
    monkeypatch.setattr(gate, "_node_binary", lambda: None)
    monkeypatch.setattr(gate, "_engine_absence_detail", lambda: "no node binary on PATH")

    cases = gate.grading_cases()
    assert [case.id for case in cases] == [gate.GRADING_UNGRADED]

    stamp = _stamp()
    _file(fake_home, _dir_for(stamp), _document(UTC_WITH_DEAD_ZONE_SCRIPT, stamp=stamp))
    with pytest.raises(AssertionError) as caught:
        gate.test_rendered_time_matches_america_new_york(None, None)
    message = str(caught.value)
    assert gate.GRADING_UNGRADED in message
    assert "NOT GRADED" in message
    assert "GRADER'S ENVIRONMENT" in message


def test_a_broken_node_is_a_grader_defect_not_an_artifact_defect(monkeypatch):
    """A node that EXISTS but cannot resolve the zone must not grade anything.

    A node built without full ICU answers every ``timeZone`` with UTC. Grading
    the clock with it would compare UTC against UTC and pass the very defect
    this gate exists to catch — so "node exists" was never the right question.
    """
    monkeypatch.setattr(gate, "_candidate_node_binaries", lambda: ["/nonexistent/node"])
    gate._engine_is_usable.cache_clear()
    try:
        assert gate._node_binary() is None
        assert [case.id for case in gate.grading_cases()] == [gate.GRADING_UNGRADED]
    finally:
        gate._engine_is_usable.cache_clear()


@requires_node
def test_the_engine_probe_accepts_this_machines_node():
    """The other direction: the probe must not refuse a WORKING engine.

    A probe that is too strict re-creates the false-adverse class it exists to
    avoid, so it is pinned against the engine actually installed here.
    """
    usable, detail = gate._engine_is_usable(_NODE)
    assert usable, f"the engine probe refused a working node: {detail}"


@requires_node
def test_node_grading_catches_the_double_conversion_the_substring_checks_missed():
    rendered = gate._render_with_node(
        _NODE, DOUBLE_CONVERSION_SCRIPT, fixed_ms=gate.FIXED_INSTANTS_MS[0], tz="UTC"
    )
    expected = gate._expected(gate.FIXED_INSTANTS_MS[0])
    display = re.sub(r"\s+", " ", rendered["display"]).strip()
    assert display != (
        f"{expected['hour12']:02d}:{expected['minute']:02d}:"
        f"{expected['second']:02d} {expected['meridiem']}"
    ), "the double-conversion clock rendered correctly under TZ=UTC — impossible"


# ---------------------------------------------------------------------------
# The run binding: an artifact this run did not produce must be unreachable.
# ---------------------------------------------------------------------------


def test_gate_accepts_a_freshly_filed_stamped_artifact(fake_home):
    stamp = _stamp()
    expected = _file(fake_home, _dir_for(stamp), _document(shipped_clock_html(), stamp=stamp))
    assert gate.get_artifact_path() == expected


def test_gate_refuses_an_unstamped_artifact(fake_home):
    """Kimi's attack: a hand-written file in a dated directory."""
    _file(
        fake_home,
        _dir_for(_stamp()),
        _document(shipped_clock_html(), stamp=None, instance=None),
    )
    with pytest.raises(AssertionError, match="nothing proves a loop wrote this file"):
        gate.get_artifact_path()


def test_gate_refuses_an_artifact_stamped_by_another_instance(fake_home):
    stamp = _stamp()
    _file(
        fake_home,
        _dir_for(stamp),
        _document(shipped_clock_html(), stamp=stamp, instance="some_other_loop"),
    )
    with pytest.raises(AssertionError, match="nothing proves a loop wrote this file"):
        gate.get_artifact_path()


def test_gate_refuses_an_artifact_from_a_previous_clock_day(fake_home):
    """The stale-artifact defect: an old clock keeping the gate green.

    The horizon is the ARTIFACT'S OWN calendar — clock days — not a wall-clock
    settlement deadline. Reaching it means the loop stopped producing.
    """
    stale = _stamp(-timedelta(days=gate.MAX_ARTIFACT_AGE_DAYS + 2))
    _file(fake_home, _dir_for(stale), _document(shipped_clock_html(), stamp=stale))
    with pytest.raises(AssertionError, match="clock days before today"):
        gate.get_artifact_path()


def test_gate_accepts_a_correct_artifact_whose_settlement_was_delayed(
    fake_home, monkeypatch
):
    """THE FALSE-ADVERSE CASE, and the reason the 3600s window is gone.

    A correct clock published at 06:00 and settled at 07:01 by a scheduler that
    crashed and restarted is a defect in the CONTROL PLANE. Condemning the
    artifact for it manufactures an adverse verdict against work that was right,
    and three of those trip the auto-pause floor — the exact class this repo
    already paid for in routine_runs 741-749.

    Deliberately tested well past the old boundary: the delay here is FOUR
    hours, and would have to run past the next Eastern midnight to refuse.

    WHY THE INODE READER IS SUBSTITUTED HERE, AND NOWHERE ELSE. A clock
    published four hours ago has an inode four hours old. This fixture was
    created a millisecond ago and there is no way to age it: ``st_ctime`` cannot
    be set backwards from userspace, which is precisely why
    :func:`~tests.test_grandfather_clock_gate.get_artifact_path` is entitled to
    trust it. So the reader returns what a real four-hour-old artifact would
    report, and the rule it stands in for is graded — unpatched, against real
    files — by :func:`test_gate_refuses_a_metadata_preserving_copy` and
    :func:`test_a_real_publish_survives_the_real_gate`.
    """
    delayed = _stamp(-timedelta(hours=4))
    expected = _file(fake_home, _dir_for(delayed), _document(shipped_clock_html(), stamp=delayed))
    monkeypatch.setattr(gate, "_inode_change_time", lambda path: _as_datetime(delayed))
    assert gate.get_artifact_path() == expected


def test_gate_refuses_an_artifact_with_no_run_id(fake_home):
    """An artifact no run can be NAMED for is not attributable to one."""
    stamp = _stamp()
    _file(fake_home, _dir_for(stamp), _document(shipped_clock_html(), stamp=stamp, run_id=None))
    with pytest.raises(AssertionError, match="not a run id"):
        gate.get_artifact_path()


def test_gate_refuses_a_hand_typed_run_id(fake_home):
    stamp = _stamp()
    _file(
        fake_home,
        _dir_for(stamp),
        _document(shipped_clock_html(), stamp=stamp, run_id="today"),
    )
    with pytest.raises(AssertionError, match="not a run id"):
        gate.get_artifact_path()


def test_gate_refuses_a_copy_of_a_valid_artifact(fake_home):
    """THE REPLAY: valid stamped HTML, copied, carrying a FRESH mtime.

    This is the shape the run-attribution hole took on disk. The bytes are a
    genuine artifact — real run id, real stamp, real clock — and the only thing
    that betrays the copy is that the file was written long after it claims to
    have been published. ``publish`` stamps and writes in one operation, so that
    gap cannot exist in an artifact this loop produced.
    """
    stamp = _stamp(-timedelta(hours=3))
    _file(
        fake_home,
        _dir_for(stamp),
        _document(shipped_clock_html(), stamp=stamp),
        mtime=datetime.now(UTC),  # `cp` gives the copy today's mtime
    )
    with pytest.raises(AssertionError, match="different origins"):
        gate.get_artifact_path()


#: Copiers that RESTORE the source's mtime, by the command that spells them.
#: The mtime rule above was the whole replay defence, and every one of these
#: walks past it — which is the hole this block exists to close and to bound.
#: Keys carry no whitespace on purpose: they become pytest parametrise ids, and
#: those ids are quoted into the counterfeit corpus as node ids.
METADATA_PRESERVING_COPIERS = {
    "cp-p": ["cp", "-p"],
    "rsync-a": ["rsync", "-a"],
}


@pytest.mark.parametrize("copier", sorted(METADATA_PRESERVING_COPIERS))
def test_gate_refuses_a_metadata_preserving_copy(fake_home, tmp_path, monkeypatch, copier):
    """THE REPLAY THE OLD RULE MISSED: ``cp -p`` and ``rsync -a``.

    ``test_gate_refuses_a_copy_of_a_valid_artifact`` only ever built the copy
    plain ``cp`` makes — fresh mtime, old stamp — so the mtime rule looked like
    a replay defence. It is not one against a copier that preserves metadata,
    and most copiers do. Here the copy is made by the real tool, on real files,
    and nothing about the timestamps is simulated.

    The case asserts BOTH halves, in order:

    1. with the inode reader standing in for the gate as it was before this rule
       (mtime only, which is what it had), the copy is ACCEPTED — the hole,
       executable, so that removing the rule cannot quietly reopen it;
    2. with the real reader, the copy is REFUSED, because ``st_ctime`` is the
       one timestamp the copier could not restore.

    What this does NOT establish is origin — see
    :func:`test_gate_accepts_a_contemporaneous_forgery` immediately below.
    """
    argv = METADATA_PRESERVING_COPIERS[copier]
    command = " ".join(argv)
    tool = shutil.which(argv[0])
    if tool is None:
        pytest.skip(f"{argv[0]} is not installed on this machine")

    # A genuine artifact, three hours old, living somewhere the gate cannot see.
    stamp = _stamp(-timedelta(hours=3))
    source = tmp_path / "elsewhere" / gate.ARTIFACT_NAME
    source.parent.mkdir(parents=True)
    source.write_text(_document(shipped_clock_html(), stamp=stamp), encoding="utf-8")
    epoch = _as_datetime(stamp).timestamp()
    os.utime(source, (epoch, epoch))

    target = (
        fake_home / "Work" / "OmniAgentOS" / "Development" / _dir_for(stamp)
        / gate.ARTIFACT_NAME
    )
    target.parent.mkdir(parents=True)
    subprocess.run([tool, *argv[1:], str(source), str(target)], check=True)

    stat = target.stat()
    assert abs(stat.st_mtime - epoch) <= gate.MAX_FILING_SKEW_SECONDS, (
        f"{command} did not preserve the mtime on this machine — this case is no longer "
        "the counterfeit it was written to be"
    )
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None and sys.platform == "darwin":
        assert abs(birth - epoch) <= gate.MAX_FILING_SKEW_SECONDS, (
            "st_birthtime no longer follows a copy on this platform. That would make it "
            "a second column a copier cannot restore; the rule does not depend on it "
            "(on APFS os.utime drags birthtime backwards, so it is forgeable), but the "
            "measurement in get_artifact_path's ORIGIN table would need redoing"
        )

    with monkeypatch.context() as before_this_rule:
        before_this_rule.setattr(
            gate,
            "_inode_change_time",
            lambda path: datetime.fromtimestamp(path.stat().st_mtime, UTC),
        )
        assert gate.get_artifact_path() == target, (
            f"the {command} copy was refused by a rule that predates the inode check, so "
            "this case no longer demonstrates the hole it was written for"
        )

    with pytest.raises(AssertionError, match="inode was last changed"):
        gate.get_artifact_path()


def test_gate_accepts_a_contemporaneous_forgery(fake_home):
    """THE LIMIT OF EVERY RULE ABOVE, asserted so it cannot be overstated.

    This document was hand-written a millisecond ago by this test. Its run id is
    ``deadbeef`` typed four times. No run produced it, no ``publish`` wrote it,
    and the gate ACCEPTS it — because the bytes really were created at the
    instant they claim, and that is the only thing a filesystem can testify to.

    The inode rule makes replay of an OLD artifact impossible; it does not make
    a NEW forgery impossible, and nothing readable from the filesystem could.
    The worker under judgement chooses the candidate, owns both artifact
    locations and holds ``--db`` in its argv, so origin is not a property this
    gate can establish — only ``verify``'s run binding narrows it, and that is
    the actor's own report, which may lower a verdict and never raise one.

    Asserting a weakness is deliberate, exactly as in
    :func:`test_static_analysis_cannot_grade_the_zone_that_controls_the_formatter`:
    if someone later believes they have closed this, this case goes red and they
    have to come here and say what they think they proved.
    """
    stamp = _stamp()
    forged = _file(
        fake_home,
        _dir_for(stamp),
        _document(shipped_clock_html(), stamp=stamp, run_id="deadbeef" * 4),
    )
    assert gate.get_artifact_path() == forged, (
        "the gate refused a contemporaneous forgery. If that is now a real capability "
        "rather than an accident of this fixture, ORIGIN in get_artifact_path is out of "
        "date and understates the gate"
    )


def test_st_ctime_is_the_timestamp_a_writer_cannot_backdate(tmp_path):
    """The platform property the copy rule RESTS on, graded not assumed.

    ``utimes`` moves atime and mtime and stamps ctime with NOW; there is no
    userspace call that sets ctime to a past value. If a future OS or filesystem
    changes that, :func:`test_gate_refuses_a_metadata_preserving_copy` would
    still pass — the copiers do not try — while the rule would in fact be
    defeated by ``os.utime`` alone. This is the case that goes red instead.
    """
    path = tmp_path / gate.ARTIFACT_NAME
    path.write_text("x", encoding="utf-8")
    back = datetime.now(UTC) - timedelta(hours=3)
    os.utime(path, (back.timestamp(), back.timestamp()))

    assert abs(path.stat().st_mtime - back.timestamp()) < 1, "os.utime did not move mtime"
    assert gate._inode_change_time(path) - back > timedelta(
        seconds=gate.MAX_FILING_SKEW_SECONDS
    ), (
        "st_ctime moved backwards with mtime on this platform. The inode rule in "
        "get_artifact_path assumes it cannot, and is defeated by os.utime if it can"
    )

    birth = getattr(path.stat(), "st_birthtime", None)
    if birth is not None and sys.platform == "darwin":
        assert abs(birth - back.timestamp()) < 1, (
            "st_birthtime did NOT follow mtime backwards here. On APFS it does, which is "
            "why the rule uses st_ctime; if that has changed, birthtime becomes a second "
            "and better column and ORIGIN's table needs redoing"
        )


def test_gate_refuses_a_future_dated_stamp(fake_home):
    ahead = _stamp(timedelta(seconds=gate.MAX_CLOCK_SKEW_SECONDS + 120))
    _file(fake_home, _dir_for(ahead), _document(shipped_clock_html(), stamp=ahead))
    with pytest.raises(AssertionError, match="in the FUTURE"):
        gate.get_artifact_path()


def test_gate_refuses_a_stamp_that_disagrees_with_its_directory(fake_home):
    """A genuine fresh artifact relabelled into another day's directory."""
    stamp = _stamp()
    yesterday = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    ).astimezone(ZoneInfo(gate.CLOCK_ZONE)).date() - timedelta(days=1)
    _file(
        fake_home,
        f"{gate.OUTPUT_DIR_PREFIX}{yesterday.isoformat()}",
        _document(shipped_clock_html(), stamp=stamp),
    )
    with pytest.raises(AssertionError, match="but sits in"):
        gate.get_artifact_path()


def test_gate_does_not_reach_back_past_an_empty_newest_directory(fake_home):
    """"Latest dated directory wins" is the defect; today's absence is a refusal."""
    stamp = _stamp()
    yesterday = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    ).astimezone(ZoneInfo(gate.CLOCK_ZONE)).date() - timedelta(days=1)
    _file(
        fake_home,
        f"{gate.OUTPUT_DIR_PREFIX}{yesterday.isoformat()}",
        _document(shipped_clock_html(), stamp=stamp.replace(stamp[:10], yesterday.isoformat())),
    )
    (fake_home / "Work" / "OmniAgentOS" / "Development" / _dir_for(stamp)).mkdir()
    with pytest.raises(AssertionError, match="is missing"):
        gate.get_artifact_path()


def test_gate_refuses_when_the_loop_has_never_filed_anything(fake_home):
    with pytest.raises(AssertionError, match="has never filed an artifact here"):
        gate.get_artifact_path()


# ---------------------------------------------------------------------------
# The grading path must be legible in what settlement records.
# ---------------------------------------------------------------------------


def test_the_grading_path_is_named_in_the_case_ids():
    """A weaker grade must be visible in the evidence, not only in stdout.

    ``gate_runner`` throws the gate's stdout away and deletes its JUnit file, so
    the case id is the channel: it reaches ``checks_collected`` and
    ``node_inventory_digest``, both of which the signed receipt carries. If this
    goes red, a node-less settlement has become indistinguishable from one that
    executed the clock across three timezones and a DST boundary.
    """
    ids = [case.id for case in gate.grading_cases()]
    assert ids, "grading_cases() collected nothing — the gate would pass vacuously"
    if gate._node_binary() is None:
        assert ids == [gate.GRADING_UNGRADED]
    else:
        assert len(ids) == len(gate.FIXED_INSTANTS_MS) * len(gate.VIEWER_ZONES)
        assert all(case_id.startswith(gate.GRADING_NODE) for case_id in ids)
    assert gate.GRADING_NODE not in gate.GRADING_UNGRADED, (
        "the two grading paths must not share a prefix, or a digest cannot tell them apart"
    )


def test_the_two_grading_paths_are_distinguishable_in_recorded_evidence(monkeypatch):
    """Same computation ``gate_runner`` does: sorted node ids -> sha256 digest."""
    import hashlib
    import json

    def digest_of(ids: list[str]) -> str:
        return hashlib.sha256(json.dumps(sorted(ids)).encode("utf-8")).hexdigest()

    monkeypatch.setattr(gate, "_node_binary", lambda: "/usr/bin/node")
    with_node = [case.id for case in gate.grading_cases()]
    monkeypatch.setattr(gate, "_node_binary", lambda: None)
    without_node = [case.id for case in gate.grading_cases()]

    assert len(with_node) != len(without_node), (
        "checks_collected must differ between the grading paths"
    )
    assert digest_of(with_node) != digest_of(without_node), (
        "node_inventory_digest must differ between the grading paths"
    )


# ---------------------------------------------------------------------------
# The INSTANCE's guarantees: everything about the RUN, which the gate — running
# in a sanitised environment with no run id — structurally cannot check.
# ---------------------------------------------------------------------------


@pytest.fixture
def var_tree(tmp_path, monkeypatch):
    """A throwaway ``OMNIAGENTOS_VAR_DIR`` so ``publish`` writes nothing real."""
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    return var


def _publish(destination: Path, candidate: dict) -> dict:
    return instance.publish(
        candidate=candidate, evaluation={"score": 1.0}, destination=str(destination)
    )


def _publish_key_for(candidate: dict) -> str:
    return _publish_key({
        "instance_id": instance.INSTANCE_ID,
        "template": "generate_evaluate_improve",
        "params": {"score_threshold": 1.0, "max_rounds": 1},
        "data": {"candidate": candidate},
    })


def test_every_generate_mints_a_distinct_run_id():
    ids = {instance.generate()["run_id"] for _ in range(20)}
    assert len(ids) == 20
    assert all(re.fullmatch(instance.RUN_ID_PATTERN, run_id) for run_id in ids)


def test_the_publish_business_key_is_per_run():
    """THE REPLAY, at its root.

    ``generate_evaluate_improve`` keys the publish receipt on
    ``digest_key(brief, candidate)``, and the receipt table never expires a row.
    While the candidate was byte-deterministic that digest was ONE key for all
    time, so the first successful publish suppressed every later one. Measured
    on 2026-08-02 before the fix: two ticks against one control plane, the
    second reporting ``status=completed`` while the filed artifact still carried
    the FIRST tick's stamp and mtime — a run certified against a file it never
    wrote.

    (``LoopTool.idempotency_key`` — the instance's ``_clock_key`` — is NOT what
    keys that receipt. The runtime never calls it; the template's ``key_fn`` is
    the only key that reaches ``receipts.receipt_key``.)
    """
    keys = {_publish_key_for(instance.generate()) for _ in range(10)}
    assert len(keys) == 10, (
        "two runs share a publish business key — the second one's effect is a "
        "no-op replay and its gate certifies the first one's artifact"
    )


def test_the_clock_html_itself_stays_byte_identical_between_runs():
    """Only the identity varies. A diff of two days' clocks is the stamp."""
    assert instance.generate()["html"] == instance.generate()["html"]


def test_publish_refuses_a_candidate_with_no_run_id(tmp_path, var_tree):
    result = _publish(tmp_path, {"html": instance.CLOCK_TEMPLATE})
    assert result["success"] is False
    assert "cannot be attributed to a run" in result["error"]
    assert not list(tmp_path.rglob(instance.ARTIFACT_NAME)), (
        "an unattributable artifact was filed anyway"
    )


def test_publish_stamps_the_run_id_into_both_copies(tmp_path, var_tree):
    candidate = instance.generate()
    result = _publish(tmp_path, candidate)
    assert result["success"] is True
    for path in (Path(result["artifact_path"]), Path(result["filed_path"])):
        stamp = instance.read_stamp(path.read_text(encoding="utf-8"))
        assert stamp[instance.RUN_META] == candidate["run_id"]
        assert stamp[instance.INSTANCE_META] == instance.INSTANCE_ID


def test_verify_accepts_the_artifact_this_run_filed(tmp_path, var_tree):
    candidate = instance.generate()
    result = _publish(tmp_path, candidate)
    verdict = instance.verify(result, {"candidate": candidate, "destination": str(tmp_path)})
    assert verdict["verified"] is True, verdict["detail"]
    assert verdict["run_id"] == candidate["run_id"]


def test_verify_refuses_a_predecessors_artifact(tmp_path, var_tree):
    """THE REPLAY, caught where the run identity exists.

    Run A publishes. Run B's publish never happens — a replayed receipt, a
    crashed writer, anything — and B is asked to verify. The artifact on disk is
    valid, fresh, correctly stamped, in the right directory and rendering the
    right time: every property the objective gate is able to check. It is simply
    not B's, and only ``args["candidate"]["run_id"]`` can say so.
    """
    run_a = instance.generate()
    _publish(tmp_path, run_a)
    run_b = instance.generate()

    verdict = instance.verify(None, {"candidate": run_b, "destination": str(tmp_path)})

    assert verdict["verified"] is False
    assert verdict["checks"]["artifact_is_this_run"] is False
    assert verdict["checks"]["filed_copy_is_this_run"] is False
    # ...and everything the GATE could have looked at is still perfectly fine.
    assert verdict["checks"]["filed_to_destination"] is True
    assert verdict["checks"]["stamp_is_fresh"] is True
    assert verdict["checks"]["stamp_day_matches_directory"] is True
    assert verdict["checks"]["contains_america_new_york"] is True


def test_verify_refuses_when_the_run_id_is_not_a_run_id(tmp_path, var_tree):
    """A malformed identity is no identity: verify must not accept it as a match.

    Without this, ``artifact_is_this_run`` could be satisfied by any value that
    happens to appear in both the arguments and the document — including an
    empty one.
    """
    candidate = instance.generate()
    _publish(tmp_path, candidate)
    verdict = instance.verify(
        None, {"candidate": {**candidate, "run_id": "today"}, "destination": str(tmp_path)}
    )
    assert verdict["verified"] is False
    assert verdict["checks"]["run_id_is_well_formed"] is False


def test_verify_reads_the_run_id_from_arguments_not_from_the_result(tmp_path, var_tree):
    """Rule E: the actor's narrative is never the verdict.

    A publish that LIES in its return value — claiming a run id it did not
    stamp — must not be able to talk its way past its own verification.
    """
    run_a = instance.generate()
    _publish(tmp_path, run_a)
    run_b = instance.generate()
    lying_result = {"success": True, "run_id": run_a["run_id"], "filed_path": "wherever"}

    verdict = instance.verify(lying_result, {"candidate": run_b, "destination": str(tmp_path)})
    assert verdict["verified"] is False, (
        "the tool steered its own verification by reporting a different run id"
    )


def test_filing_replaces_a_planted_symlink_instead_of_writing_through_it(tmp_path, var_tree):
    """A pre-planted ``clock.html -> <sentinel>`` must not redirect the write.

    The dated directory's name is derived from the calendar, so anything running
    as the operator can create tomorrow's directory today and plant a link in
    it. The loop then writes it UNATTENDED, on a timer, which is exactly when a
    redirected write goes unnoticed. ``os.replace`` acts on the LINK, never on
    what it points at.
    """
    destination = tmp_path / "operator"
    sentinel = tmp_path / "precious.txt"
    sentinel.write_text("DO NOT OVERWRITE", encoding="utf-8")

    candidate = instance.generate()
    planted_dir = destination / instance.output_dir_name(instance.clock_day())
    planted_dir.mkdir(parents=True)
    planted = planted_dir / instance.ARTIFACT_NAME
    planted.symlink_to(sentinel)
    assert planted.is_symlink()

    result = _publish(destination, candidate)

    assert result["success"] is True
    assert sentinel.read_text(encoding="utf-8") == "DO NOT OVERWRITE", (
        "the loop's scheduled write was redirected through a planted symlink"
    )
    assert not planted.is_symlink(), "the symlink survived the write"
    assert instance.read_stamp(planted.read_text(encoding="utf-8"))[instance.RUN_META] == (
        candidate["run_id"]
    )


def test_the_var_copy_also_refuses_to_follow_a_symlink(tmp_path, var_tree):
    """Same hazard, same fix, on the parent-seam copy under ``var/``."""
    from omniagentos_loops.parent_seam import artifact_path, var_dir

    sentinel = tmp_path / "var-precious.txt"
    sentinel.write_text("DO NOT OVERWRITE", encoding="utf-8")
    target = artifact_path(var_dir(), instance.INSTANCE_ID, instance.ARTIFACT_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(sentinel)

    result = _publish(tmp_path / "operator", instance.generate())

    assert result["success"] is True
    assert sentinel.read_text(encoding="utf-8") == "DO NOT OVERWRITE"
    assert not target.is_symlink()


def test_filing_leaves_no_temp_file_behind(tmp_path, var_tree):
    """The temp sibling is an implementation detail, not an artifact."""
    destination = tmp_path / "operator"
    _publish(destination, instance.generate())
    filed_dir = destination / instance.output_dir_name(instance.clock_day())
    assert sorted(p.name for p in filed_dir.iterdir()) == [instance.ARTIFACT_NAME]


def test_the_filed_artifacts_mtime_corroborates_its_stamp(tmp_path, var_tree):
    """The invariant that replaced the gate's 3600s settlement window.

    ``publish`` takes the instant, renders the stamp and writes the bytes in one
    call, so these two cannot disagree in a real artifact. A COPY does disagree,
    which is what makes this a replacement for a rule about how late settlement
    is allowed to be rather than merely a weakening of it.
    """
    result = _publish(tmp_path, instance.generate())
    filed = Path(result["filed_path"])
    stamped = datetime.strptime(result["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    mtime = datetime.fromtimestamp(filed.stat().st_mtime, UTC)
    assert abs(mtime - stamped) <= timedelta(seconds=instance.MAX_FILING_SKEW_SECONDS)


def test_the_instance_and_the_gate_agree_on_the_filing_convention():
    """Anti-drift across the duplication the two venvs force.

    The gate cannot import the instance, so both restate these constants. Drift
    shows up as the gate refusing a good artifact, which is the safe direction —
    but it is still an outage, so it is pinned.
    """
    assert instance.CLOCK_ZONE == gate.CLOCK_ZONE
    assert instance.OUTPUT_DIR_PREFIX == gate.OUTPUT_DIR_PREFIX
    assert instance.ARTIFACT_NAME == gate.ARTIFACT_NAME
    assert instance.INSTANCE_META == gate.INSTANCE_META
    assert instance.PUBLISHED_AT_META == gate.PUBLISHED_AT_META
    assert instance.RUN_META == gate.RUN_META
    assert instance.RUN_ID_PATTERN == gate.RUN_ID_PATTERN
    assert instance.MAX_FILING_SKEW_SECONDS == gate.MAX_FILING_SKEW_SECONDS
    assert instance.MAX_CLOCK_SKEW_SECONDS == gate.MAX_CLOCK_SKEW_SECONDS
    assert instance.INSTANCE_ID == gate.INSTANCE_ID


def test_a_real_publish_survives_the_real_gate(tmp_path, var_tree, monkeypatch):
    """End to end, with no hand-built document anywhere in the path.

    ``publish`` files a clock; ``get_artifact_path`` — the real one, with every
    attribution rule live — accepts it. This is the check that would catch a
    stamp the gate cannot parse, a directory name the two disagree about, or an
    mtime rule the writer cannot satisfy.
    """
    home = tmp_path / "home"
    (home / "Work" / "OmniAgentOS" / "Development").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    candidate = instance.generate()
    result = _publish(home / "Work" / "OmniAgentOS" / "Development", candidate)
    assert result["success"] is True

    accepted = gate.get_artifact_path()
    assert accepted == Path(result["filed_path"])
    meta = gate._MetaCollector()
    meta.feed(accepted.read_text(encoding="utf-8"))
    assert meta.meta[gate.RUN_META] == candidate["run_id"]
