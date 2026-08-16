"""``grandfather_clock_html`` — a mechanical HTML grandfather clock on Eastern time.

An instance that generates an HTML grandfather clock using Intl with the IANA
zone ``America/New_York``, NOT a hardcoded offset (a fixed offset is wrong four
months a year).

Template: generate_evaluate_improve (refinement loop, bounded).

Effect: T1 (local file write, reversible, deterministic HTML).

THE LOOP FILES ITS OWN ARTIFACT
-------------------------------

``publish`` writes the clock TWICE, and both writes are the effect:

* ``<var>/loops/artifacts/grandfather_clock_html/clock.html`` — the parent-seam
  artifact convention, derived from arguments alone so a verification predicate
  can find it without reading the actor's narrative (Rule E);
* ``<destination>/Grandfather-Clock-<ET date>/clock.html`` — the OPERATOR's
  declared output directory, which is the directory the routine's objective gate
  (``tests/test_grandfather_clock_gate.py``) reads.

It writes the second one because until 2026-08-02 it did not: the only writer of
the gated directory was a hand-run script, so in production the gate certified a
file the tick had never produced, and a dead loop settled FAVOURABLE forever
(the artifact from any earlier day kept the gate green, acceptance stayed 1.0,
and the ≥3-adverse auto-pause floor could never trip). ``destination`` is an
explicit parameter rather than a constant so the caller — a test, the manual
tick, a future operator preference — names the tree it is filing into.

THE RUN STAMP
-------------

Both copies carry ``<meta name="loop-instance">``, ``<meta name="loop-run">``
and ``<meta name="loop-published-at">`` (UTC, second precision). The stamp is
what tells "this run wrote it" from "something wrote it once": the gate re-reads
it and refuses an artifact whose stamp is missing, unattributable, dated in the
future, disagreeing with the dated directory it sits in, or contradicted by the
file's own mtime.

ONE RUN, ONE ARTIFACT — AND WHY THE CANDIDATE IS NO LONGER BYTE-DETERMINISTIC
----------------------------------------------------------------------------

Until 2026-08-02 ``generate`` returned a byte-deterministic candidate, and this
docstring gave the reason: so "the template's content-digest idempotency key does
not change every tick". That reason was exactly backwards, and it is the defect
this section exists to record.

``generate_evaluate_improve`` keys the publish effect's receipt on
``digest_key(brief, candidate)`` (its ``_publish_key``), and the receipt table
has no expiry. A byte-deterministic candidate therefore produced ONE business
key for all time, so the FIRST successful publish in the table suppressed every
later one: each subsequent tick replayed the recorded result, reported
``completed``, and wrote nothing. Measured directly — two ticks, one control
plane — the second tick reported ``status=completed`` while the filed artifact
still carried the first tick's stamp and the first tick's mtime. A second
routine run on the same day is then certified against the FIRST run's artifact,
which is a favourable settlement for a run that never did anything.

Note that :attr:`LoopTool.idempotency_key` — the ``_clock_key`` below — is NOT
what keys that receipt. The loops runtime never invokes it (the field is
assigned by every instance and read by nothing); the template's ``key_fn`` is
the only key that reaches :func:`omniagentos_loops.receipts.receipt_key`. It is
kept correct here anyway, because a key that is dead today is a trap tomorrow.

So the candidate now carries :func:`new_run_id` — a fresh random identity per
``generate`` — which makes the effect's business key per-run and makes every
tick publish its own artifact. Two consequences, both deliberate:

* The receipt no longer de-duplicates ACROSS ticks. It still de-duplicates
  within one, which is the hazard the guard exists for (P1/P7: LangGraph replays
  a node whose superstep never committed, and a replayed ``publish`` re-reads
  the same checkpointed candidate, hence the same key). Cross-tick de-duplication
  was never a safety property here: the effect is an idempotent overwrite of a
  path derived from arguments, so performing it twice leaves exactly the state
  performing it once leaves. **Do not copy this to an outbound send, a payment
  or a merge** — there, re-running is the harm and the deterministic key is
  right.
* The run id is the ONLY thing that lets :func:`verify` say "the artifact on
  disk is the one THIS publish wrote". It reads it from ``args["candidate"]``,
  never from *result*, so the tool cannot steer its own verification (Rule E).
"""

from __future__ import annotations

import os
import re
import secrets
import zoneinfo
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos_loops.artifacts import probe_html
from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.parent_seam import artifact_path, var_dir
from omniagentos_loops.tools import LoopTool

#: Instance id this module is registered under.
INSTANCE_ID = "grandfather_clock_html"

#: The zone the clock displays, and the zone whose calendar day names the
#: operator's dated output directory. One zone for both so the directory name is
#: a fact about the CLOCK, not about the timezone of the box that filed it.
CLOCK_ZONE = "America/New_York"

#: ``~/omniagentos-output`` — the operator's declared output tree.
OPERATOR_OUTPUT_SUBPATH = ("Work", "OmniAgentOS", "Development")

OUTPUT_DIR_PREFIX = "Grandfather-Clock-"
ARTIFACT_NAME = "clock.html"

#: Stamp names. DUPLICATED, on purpose, in ``tests/test_grandfather_clock_gate.py``:
#: the gate executes on the PRODUCTION venv (``gate_runner`` derives the
#: interpreter from the target path, and ``tests/`` is repo-class), which has no
#: ``omniagentos_loops`` on its path, so the gate cannot import this module and
#: must restate the convention — the same reason ``parent_seam`` restates
#: ``loop_effects``' protocol constants rather than importing them.
INSTANCE_META = "loop-instance"
PUBLISHED_AT_META = "loop-published-at"

#: The identity of the RUN that filed the artifact. Minted by :func:`generate`
#: into the candidate, so it is part of the effect's business key and part of
#: the arguments :func:`verify` reads — the two places a run identity has to
#: exist for "this run's artifact" to be a checkable statement.
RUN_META = "loop-run"

#: Shape of a run id: 32 lowercase hex characters. Pinned so both the instance
#: and the gate can refuse a hand-typed one without agreeing on anything else.
RUN_ID_PATTERN = r"[0-9a-f]{32}"

#: How far the file's mtime may sit from the stamp it carries. This is a fact
#: about ONE filing operation — :func:`publish` takes the instant, renders the
#: stamp and writes the bytes inside a few milliseconds — so a correct artifact
#: cannot trip it, and a copy of a valid artifact (fresh mtime, old stamp) does.
#:
#: Contrast the rule this REPLACED: "the stamp must be younger than one hour".
#: That was an assumption about SETTLEMENT LATENCY, not about the artifact, so a
#: correct clock published at 06:00 and settled at 07:01 by a late scheduler
#: settled ADVERSE — the control plane condemning a good artifact, three of
#: which trip the auto-pause floor. See routine_runs 741-749 for the last time
#: this repo paid for that class of rule.
MAX_FILING_SKEW_SECONDS = 300

#: Tolerated clock skew for a stamp dated ahead of the reader, mirroring the 60s
#: anti-forgery window ``gate_evidence`` applies to receipts.
MAX_CLOCK_SKEW_SECONDS = 60

#: RETIRED as a refusal boundary; kept as the name of the horizon
#: :func:`verify` uses to describe an artifact, and re-exported so nothing that
#: imported it breaks. Freshness is now expressed as "the artifact's own clock
#: day", which is a property of the artifact rather than of the scheduler.
SETTLEMENT_WINDOW_SECONDS = 3600

#: The HTML for the grandfather clock. Deterministic. Uses Intl.DateTimeFormat
#: with timeZone="America/New_York" for proper EDT handling year-round.
CLOCK_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Grandfather Clock - Eastern Time (America/New_York)</title>
    <style>
        body {
            margin: 0;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            background: linear-gradient(135deg, #2c3e50 0%, #34495e 100%);
            font-family: 'Georgia', serif;
        }

        .clock-container {
            background: linear-gradient(145deg, #8B4513, #D2691E);
            border-radius: 30px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5),
                        inset 0 1px 0 rgba(255, 255, 255, 0.3);
            text-align: center;
            max-width: 500px;
        }

        h1 {
            color: #fffacd;
            margin-top: 0;
            font-size: 28px;
        }

        .clock-face {
            position: relative;
            width: 300px;
            height: 300px;
            background: radial-gradient(circle at 30% 30%, #fffacd, #f0e68c);
            border: 8px solid #8B4513;
            border-radius: 50%;
            margin: 0 auto;
            box-shadow: 0 0 20px rgba(0, 0, 0, 0.3),
                        inset 0 0 10px rgba(0, 0, 0, 0.1);
        }

        .clock-hand {
            position: absolute;
            left: 50%;
            bottom: 50%;
            transform-origin: bottom center;
            background: #000;
            border-radius: 2px;
        }

        .hour-hand {
            width: 8px;
            height: 80px;
            margin-left: -4px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }

        .minute-hand {
            width: 6px;
            height: 110px;
            margin-left: -3px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        }

        .second-hand {
            width: 2px;
            height: 120px;
            margin-left: -1px;
            background: #ff0000;
            box-shadow: 0 1px 2px rgba(255, 0, 0, 0.3);
        }

        .center-dot {
            position: absolute;
            width: 16px;
            height: 16px;
            background: #000;
            border-radius: 50%;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            z-index: 10;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }

        .hour-marker {
            position: absolute;
            width: 2px;
            height: 15px;
            background: #000;
            left: 50%;
            top: 10px;
            transform-origin: 1px 140px;
            margin-left: -1px;
        }

        .number {
            position: absolute;
            width: 100%;
            height: 100%;
            text-align: center;
            font-size: 20px;
            font-weight: bold;
            color: #333;
        }

        .number span {
            display: inline-block;
            position: absolute;
            left: 50%;
            width: 30px;
            text-align: center;
            transform: translateX(-50%);
        }

        .time-display {
            margin-top: 30px;
            font-size: 24px;
            font-weight: bold;
            color: #fffacd;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }

        .timezone-info {
            margin-top: 15px;
            font-size: 14px;
            color: #f0e68c;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="clock-container">
        <h1>Grandfather Clock</h1>

        <div class="clock-face">
            <!-- Hour markers (12 ticks for 12 hours) -->
            <div class="hour-marker" style="transform: rotateZ(0deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(30deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(60deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(90deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(120deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(150deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(180deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(210deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(240deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(270deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(300deg);"></div>
            <div class="hour-marker" style="transform: rotateZ(330deg);"></div>

            <!-- Hour numbers (12, 3, 6, 9) -->
            <div class="number">
                <span style="top: 15px;">12</span>
                <span style="top: 50%; right: 15px; transform: translateY(-50%) translateX(0);">3</span>
                <span style="bottom: 15px;">6</span>
                <span style="top: 50%; left: 15px; transform: translateY(-50%) translateX(0);">9</span>
            </div>

            <!-- Clock hands -->
            <div class="clock-hand hour-hand" id="hour-hand"></div>
            <div class="clock-hand minute-hand" id="minute-hand"></div>
            <div class="clock-hand second-hand" id="second-hand"></div>

            <!-- Center pivot dot -->
            <div class="center-dot"></div>
        </div>

        <div class="time-display" id="time-display">--:--:--</div>
        <div class="timezone-info">Eastern Time (America/New_York)</div>
    </div>

    <script>
        function updateClock() {
            // The instant. Everything below reads America/New_York out of THIS
            // value through Intl, and nothing re-parses a formatted string with
            // the Date constructor: that reads a New York wall-clock string as
            // BROWSER-local time, and formatting the result into New York again
            // shifts it a second time — four hours and a wrong date for every
            // viewer outside Eastern.
            const now = new Date();

            // Wall-clock fields in New York, read directly from the formatter.
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/New_York',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            }).formatToParts(now);
            const field = (type) => Number(parts.find((part) => part.type === type).value);

            // `% 12` also normalises the h24 cycle's midnight (24 -> 0).
            const hours = field('hour') % 12;
            const minutes = field('minute');
            const seconds = field('second');

            const secondDegrees = (seconds / 60) * 360;
            const minuteDegrees = (minutes / 60) * 360 + (seconds / 60) * 6;
            const hourDegrees = (hours / 12) * 360 + (minutes / 60) * 30;

            // Update hand rotations
            document.getElementById('hour-hand').style.transform = `rotateZ(${hourDegrees}deg)`;
            document.getElementById('minute-hand').style.transform = `rotateZ(${minuteDegrees}deg)`;
            document.getElementById('second-hand').style.transform = `rotateZ(${secondDegrees}deg)`;

            // Digital readout: format `now` ONCE, in the zone.
            const timeString = now.toLocaleString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: true,
                timeZone: 'America/New_York'
            });
            document.getElementById('time-display').textContent = timeString;
        }

        // Update clock immediately and then every 1000ms
        updateClock();
        setInterval(updateClock, 1000);
    </script>
</body>
</html>'''


def operator_output_root() -> Path:
    """``~/omniagentos-output`` — the default filing destination."""
    return Path.home().joinpath(*OPERATOR_OUTPUT_SUBPATH)


def output_dir_name(day: date) -> str:
    """``Grandfather-Clock-YYYY-MM-DD`` for *day*."""
    return f"{OUTPUT_DIR_PREFIX}{day.isoformat()}"


def clock_day(moment: datetime | None = None) -> date:
    """The calendar day of *moment* IN THE CLOCK'S ZONE (defaults to now)."""
    zone = zoneinfo.ZoneInfo(CLOCK_ZONE)
    if moment is None:
        return datetime.now(zone).date()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone).date()


def stamp_text(moment: datetime) -> str:
    """The ``loop-published-at`` value: UTC, second precision, ``Z``-suffixed.

    ``Z`` rather than ``+00:00`` so the stamp can never introduce a
    ``±HH:MM``-shaped substring into a document whose gate refuses hardcoded
    UTC offsets.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """A fresh run identity: 32 hex characters from the system CSPRNG.

    Random rather than derived from the clock or the tick number, because the
    two properties it must have are (a) a value no other run will pick and (b) a
    value nobody can predict and pre-write into a file. A timestamp fails both.
    """
    return secrets.token_hex(16)


def _stamped(html_content: str, *, published_at: datetime, run_id: str) -> str:
    """Return *html_content* with this run's stamp injected into ``<head>``."""
    anchor = '<meta charset="UTF-8">'
    if anchor not in html_content:
        raise ValueError("clock HTML has no <meta charset> anchor to stamp against")
    if not re.fullmatch(RUN_ID_PATTERN, run_id or ""):
        raise ValueError(
            f"refusing to file an artifact with run id {run_id!r}: an artifact that "
            "cannot be attributed to a run must not exist"
        )
    stamp = (
        f'\n    <meta name="{INSTANCE_META}" content="{INSTANCE_ID}">'
        f'\n    <meta name="{RUN_META}" content="{run_id}">'
        f'\n    <meta name="{PUBLISHED_AT_META}" content="{stamp_text(published_at)}">'
    )
    return html_content.replace(anchor, anchor + stamp, 1)


def read_stamp(html_content: str) -> dict[str, str]:
    """The ``<meta name=... content=...>`` stamp values present in *html_content*."""
    found: dict[str, str] = {}
    for match in re.finditer(r"<meta\s+[^>]*>", html_content, re.IGNORECASE):
        tag = match.group(0)
        name = re.search(r'name\s*=\s*"([^"]+)"', tag)
        content = re.search(r'content\s*=\s*"([^"]*)"', tag)
        if name and content:
            found[name.group(1)] = content.group(1)
    return found


def _generate_clock_html() -> str:
    """Generate the HTML clock. Deterministic — no model, no API."""
    return CLOCK_TEMPLATE


def generate(**kwargs: Any) -> dict[str, Any]:
    """Generate the clock HTML candidate, carrying THIS run's identity.

    ``html`` stays byte-identical every tick — the clock is deterministic and a
    diff of two days' artifacts should show only the stamp. ``run_id`` is the
    part that varies, and it varies on purpose: the template digests this whole
    dict into the publish effect's business key, so without it one receipt
    suppresses every future publish (see the module docstring).
    """
    html_content = _generate_clock_html()
    return {
        "html": html_content,
        "run_id": new_run_id(),
        "artifact_name": "clock.html",
        "size_bytes": len(html_content),
    }


def evaluate(**kwargs: Any) -> dict[str, Any]:
    """Evaluate the candidate. Mechanical checks only — NOT model opinion."""
    candidate = kwargs.get("candidate") or {}
    html_content = candidate.get("html") or ""

    if not html_content:
        return {"score": 0.0, "reason": "No HTML content generated"}

    checks = {
        "contains_america_new_york": "America/New_York" in html_content,
        "no_hardcoded_offset": "-04:00" not in html_content and "-05:00" not in html_content,
        "uses_locale_string": "toLocaleString" in html_content,
        "uses_timezone_param": "timeZone" in html_content,
        "has_clock_face": 'class="clock-face"' in html_content,
        "has_hour_hand": 'class="clock-hand hour-hand"' in html_content,
        "has_minute_hand": 'class="clock-hand minute-hand"' in html_content,
        "has_second_hand": 'class="clock-hand second-hand"' in html_content,
        # The double-conversion defect: a New York wall-clock string re-parsed
        # as browser-local and formatted into New York a second time.
        "no_locale_string_reparse": not re.search(r"new\s+Date\s*\(\s*[^)\s]", html_content),
    }

    all_passed = all(checks.values())
    score = 1.0 if all_passed else 0.0

    return {
        "score": score,
        "checks": checks,
        "feedback": "" if all_passed else "Clock structure incomplete",
        "reasoning": (
            "All mechanical requirements passed" if all_passed
            else "Some mechanical checks failed"
        ),
    }


def _destination_root(args: Mapping[str, Any] | None) -> Path:
    """The filing root named by *args*, or the operator's declared tree."""
    destination = (args or {}).get("destination")
    if destination:
        return Path(str(destination)).expanduser()
    return operator_output_root()


def write_without_following_symlinks(target: Path, document: str) -> None:
    """Write *document* AT *target*, replacing a symlink rather than its target.

    ``Path.write_text`` opens for writing and FOLLOWS a symlink, so a
    pre-planted ``clock.html -> ~/.ssh/authorized_keys`` in the operator's dated
    directory redirects this loop's write to that file. No privilege boundary is
    crossed — the loop runs as the operator, who could overwrite the file
    directly — but it runs UNATTENDED ON A TIMER, which is precisely when the
    redirection would not be noticed, and the dated directory is a well-known,
    date-predictable path that anything running as the user can pre-create.

    Writing a temp sibling and :func:`os.replace`-ing it over the name is the
    fix: ``rename(2)`` operates on the LINK, never on what it points at, so a
    planted symlink is replaced by the real file and the sentinel is untouched.
    The atomic swap is a bonus with its own value — a reader (the gate) never
    observes a half-written clock.

    ``O_NOFOLLOW`` was the alternative. It refuses instead of correcting, which
    would turn a planted symlink into a publish failure, and a publish failure
    settles the run adverse: an attacker who cannot redirect the write could
    still pause the routine. Replacing the link is strictly better.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(document, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def publish(**kwargs: Any) -> dict[str, Any]:
    """Write the clock to var/ AND file it into *destination*. T1 effect.

    ``destination`` is the ROOT the dated directory is created under; the dated
    directory itself (``Grandfather-Clock-<ET date>``) stays owned by this
    module, because the gate reconstructs the same name and one of the two would
    otherwise drift. Defaults to :func:`operator_output_root`.

    Both writes go through :func:`write_without_following_symlinks`.
    """
    candidate = kwargs.get("candidate") or {}
    evaluation = kwargs.get("evaluation") or {}

    html_content = candidate.get("html") or ""
    if not html_content:
        return {
            "success": False,
            "error": "No HTML content in candidate",
        }

    run_id = str(candidate.get("run_id") or "")
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        # Refusing here rather than minting one: a run id invented at FILING
        # time is not the run's identity, it is the filer's, and `verify` would
        # then be checking the artifact against a value the artifact's own
        # writer chose a moment earlier. The identity has to come from the
        # candidate, because that is what the effect's business key is derived
        # from — the same value in both places is the whole binding.
        return {
            "success": False,
            "error": (
                f"candidate carries no usable run id ({run_id!r}); refusing to file an "
                "artifact that cannot be attributed to a run"
            ),
        }

    try:
        published_at = datetime.now(UTC).replace(microsecond=0)
        document = _stamped(html_content, published_at=published_at, run_id=run_id)

        path = artifact_path(var_dir(), INSTANCE_ID, ARTIFACT_NAME)
        write_without_following_symlinks(path, document)

        filed_dir = _destination_root(kwargs) / output_dir_name(clock_day(published_at))
        filed_path = filed_dir / ARTIFACT_NAME
        write_without_following_symlinks(filed_path, document)

        # Verify both were written
        missing = [str(p) for p in (path, filed_path) if not p.is_file()]
        if missing:
            return {
                "success": False,
                "error": f"Failed to write {', '.join(missing)}",
            }

        return {
            "success": True,
            "artifact_path": str(path),
            "filed_path": str(filed_path),
            "published_at": stamp_text(published_at),
            "run_id": run_id,
            "size_bytes": len(document),
            "score": evaluation.get("score", 0.0),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def _locate_filed(root: Path, *, now: datetime) -> Path | None:
    """The filed artifact for today, or for yesterday across a midnight roll."""
    today = clock_day(now)
    for day in (today, today - timedelta(days=1)):
        candidate = root / output_dir_name(day) / ARTIFACT_NAME
        if candidate.is_file():
            return candidate
    return None


def verify(result: Any = None, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Independent verification of the effect.

    Signature is ``(result, args)`` because that is how ``receipts._judge``
    invokes it. It used to be ``(**kwargs)``, which cannot accept two positional
    arguments — so every publish raised ``TypeError`` inside the receipt guard
    and was recorded as ``EffectStateUnknown``: the verification this loop
    advertises had never once executed.

    Nothing here reads *result*. The paths AND the run identity are derived from
    ARGUMENTS alone (Rule E: the actor's narrative is never the verdict);
    *result* is accepted and ignored so a tool that lies about where it wrote —
    or about which run it wrote for — cannot steer the check.

    THIS IS WHERE RUN ATTRIBUTION LIVES, and it is the only place it can. The
    objective gate runs in ``gate_runner``'s sanitised environment, which carries
    PATH/HOME/LANG/LC_ALL/TMPDIR/SYSTEMROOT and nothing else: no database, no
    routine id, no run id. A filesystem-only gate can therefore ask "is this
    artifact self-consistent and recent" but never "is this artifact the run
    under judgement's". Here, inside the run, ``args["candidate"]["run_id"]`` IS
    the run under judgement — so the check that a replayed or copied artifact
    fails is ``expected_run_id`` against what is actually on disk, and a
    ``verified=False`` receipt keeps the tick out of FAVOURABLE regardless of
    what the gate later says (a self-report may lower a verdict, never raise
    one).
    """
    try:
        now = datetime.now(UTC)
        expected_run_id = str(((args or {}).get("candidate") or {}).get("run_id") or "")
        path = artifact_path(var_dir(), INSTANCE_ID, ARTIFACT_NAME)
        content = path.read_text(encoding='utf-8')

        root = _destination_root(args)
        filed = _locate_filed(root, now=now)

        # Parse HTML
        fact = probe_html(path)

        stamp = read_stamp(content)
        published_raw = stamp.get(PUBLISHED_AT_META, "")
        try:
            published_at = datetime.strptime(published_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            published_at = None
        age = (now - published_at).total_seconds() if published_at else None
        filed_stamp = read_stamp(filed.read_text(encoding="utf-8")) if filed else {}

        checks = {
            "contains_america_new_york": "America/New_York" in content,
            "uses_tolocalestring": "toLocaleString" in content,
            "uses_timezone_param": "timeZone: 'America/New_York'" in content or (
                'timeZone: "America/New_York"' in content
            ),
            "no_hardcoded_offset": (
                "-04:00" not in content and "-05:00" not in content
            ),
            "no_locale_string_reparse": not re.search(r"new\s+Date\s*\(\s*[^)\s]", content),
            "has_hour_hand": 'class="clock-hand hour-hand"' in content,
            "has_minute_hand": 'class="clock-hand minute-hand"' in content,
            "has_second_hand": 'class="clock-hand second-hand"' in content,
            "has_clock_face": 'class="clock-face"' in content,
            # The postcondition the gate actually reads: the loop filed its own
            # artifact, this run, into the dated directory the gate looks in.
            "filed_to_destination": filed is not None,
            "filed_copy_matches": bool(
                filed and filed.read_text(encoding='utf-8') == content
            ),
            "stamped_with_instance": stamp.get(INSTANCE_META) == INSTANCE_ID,
            "stamp_parses": published_at is not None,
            "stamp_is_fresh": age is not None
            and -MAX_CLOCK_SKEW_SECONDS <= age <= SETTLEMENT_WINDOW_SECONDS,
            "stamp_day_matches_directory": bool(
                published_at and filed and filed.parent.name == output_dir_name(
                    clock_day(published_at)
                )
            ),
            # THE RUN BINDING. Both copies must carry the run id that THIS
            # tick's candidate was generated with. A replayed publish (the
            # receipt short-circuit that made a second run on one day certify
            # the first run's file) leaves a PREDECESSOR's id on disk, and this
            # is the check that sees it.
            "run_id_is_well_formed": bool(re.fullmatch(RUN_ID_PATTERN, expected_run_id)),
            "artifact_is_this_run": (
                bool(expected_run_id) and stamp.get(RUN_META) == expected_run_id
            ),
            "filed_copy_is_this_run": (
                bool(expected_run_id) and filed_stamp.get(RUN_META) == expected_run_id
            ),
        }

        verified = all(checks.values())
        failed = sorted(name for name, ok in checks.items() if not ok)

        return {
            "verified": verified,
            "state": "clock_valid" if verified else "clock_invalid",
            "checks": checks,
            "filed_path": str(filed) if filed else "",
            "published_at": published_raw,
            "tag_count": fact.tag_count,
            "run_id": expected_run_id,
            "detail": (
                f"Clock is mechanically valid: {fact.tag_count} HTML tags, "
                f"IANA zone America/New_York confirmed, no hardcoded offsets, "
                f"filed to {filed} stamped {published_raw} by run {expected_run_id}"
            ) if verified else f"Clock verification failed: {', '.join(failed)}"
        }
    except Exception as e:
        return {
            "verified": False,
            "state": "error",
            "error": str(e),
            "detail": f"Verification error: {e}"
        }


def _clock_key(args: Mapping[str, Any]) -> str:
    """Business key: one clock per RUN.

    DEAD TODAY, AND CORRECT ANYWAY. The loops runtime never calls
    :attr:`LoopTool.idempotency_key`: ``templates.common.add_effect`` takes the
    business key from the TEMPLATE's ``key_fn`` and hands it to
    ``receipts.receipt_key``, and a grep of the tree finds this field assigned
    by every instance and read by nothing. It is kept exact because the day it
    is wired up, a stale key here would silently restore the replay below.

    It used to return ``grandfather-clock:<Eastern day>``, which reads as a
    considered choice and is why the day-level story was believed. The key that
    actually ran was worse: ``digest_key(brief, candidate)`` over a
    byte-deterministic candidate, i.e. one key for all time. Either way a second
    run inside the window publishes nothing and is certified against the first
    run's artifact. Per-run is the only key for which "this run's artifact" is a
    statement with content.
    """
    run_id = str(((args or {}).get("candidate") or {}).get("run_id") or "")
    return f"grandfather-clock:{run_id or 'unattributed'}"


def register(ctx: Any) -> None:
    """Register the grandfather clock instance's tools."""
    ctx.tools.register(
        LoopTool(
            name="generate",
            tier=RiskTier.T0,
            idempotency_key=_clock_key,
            call=generate,
            description="Generate the HTML grandfather clock candidate",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="evaluate",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "evaluate",
            call=evaluate,
            description="Mechanical evaluation of the clock (NOT model opinion)",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="publish",
            tier=RiskTier.T1,
            action_class=ActionClass.SANDBOXED_CREATION,
            idempotency_key=_clock_key,
            call=publish,
            verify=verify,
            description="Write the HTML grandfather clock with EDT timezone",
        )
    )


TOOLS = ("generate", "evaluate", "publish")

__all__ = [
    "ARTIFACT_NAME",
    "CLOCK_ZONE",
    "INSTANCE_ID",
    "INSTANCE_META",
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_FILING_SKEW_SECONDS",
    "OUTPUT_DIR_PREFIX",
    "PUBLISHED_AT_META",
    "RUN_ID_PATTERN",
    "RUN_META",
    "SETTLEMENT_WINDOW_SECONDS",
    "TOOLS",
    "clock_day",
    "evaluate",
    "generate",
    "new_run_id",
    "operator_output_root",
    "output_dir_name",
    "publish",
    "read_stamp",
    "register",
    "stamp_text",
    "verify",
    "write_without_following_symlinks",
]
