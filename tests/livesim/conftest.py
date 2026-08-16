"""LiveSim pytest plugin: telemetry capture + isolation + evidence.

Every LiveSim test runs against the LIVE system. This conftest gives each test:

  * `livesim`      — a recorder to attach model/provider/cost/tokens/inputs/
                     outputs/evidence; wall-clock latency is captured for free.
  * `live_api`     — base URL + read-only GET/authed helpers for :8485.
  * `live_db_ro`   — a READ-ONLY sqlite connection to the live runtime DB.
  * `livesim_ns`   — a unique namespace string for any row/file a test creates,
                     so cleanup deletes only what the test made.
  * `scratch_dir`  — a per-test throwaway directory under the run's evidence.

At the end of every test the plugin writes ONE `livesim.v1` record to
`var/livesim/runs/<run_id>/<nodeid>.json` and appends it to
`var/livesim/ledger.jsonl` (status comes from the pytest report, so a record
can never claim green over a crash).

The suite is marked `livesim` and is excluded from the default and fast lanes
(see pyproject addopts) — it never gates a merge.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

# Make scripts/livesim importable without installing it.
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "livesim"))

# LS-TEST-009 (blocker, 2026-08-06): `pytest_plugins = ["pytester"]` used to
# live HERE. pytest forbids declaring pytest_plugins in a non-top-level
# conftest -- this file is only a legal "initial conftest" (where it's
# allowed) when tests/livesim is passed as an explicit collection path, which
# is EVERY invocation this lane ever tested with. Passed as a nested
# directory under a broader root (`pytest tests`, i.e. what `make test`
# actually runs, per Makefile's `test: uv run pytest -q`), this file is no
# longer top-level and pytest aborted collection entirely at IMPORT time --
# before any hook, including LS-TEST-002's trylast=True fix, ever runs. The
# `pytester` fixture is now registered from the sanctioned top-level list in
# tests/conftest.py instead; nothing in this file needs it declared locally.
# Methodology note this cost real breakage to learn: verify a suite-scoped
# change against the SAME invocation the default lane actually uses (`make
# test` / `pytest tests`), not just the suite's own directory -- the same
# blind spot this line exemplifies bit LS-TEST-002 first.
# --------------------------------------------------------------------------
# documents_open_defect(id="LS-###") cross-check
#
# A test that asserts OBSERVED-broken product behaviour on purpose (rather
# than the correct behaviour) must be marked @pytest.mark.documents_open_defect
# (id="LS-###", or id=("LS-###", "LS-###") for a test covering more than one
# entry). At COLLECTION TIME, every marked test's id is cross-checked against
# docs/testing/LIVESIM-ISSUES.yaml: if that id's status has moved to a RACED
# status (a lane has fixed it or is actively fixing it) and the test is a
# BARE assertion (not itself a strict xfail), collection fails LOUD rather
# than letting a stale defect-pinning test silently rot until a fix lands and
# turns it into a false failure nobody connects back to the YAML. Built
# 2026-08-06 after four live instances of exactly that miss in one session
# (event-hub, today-dashboard, board_files denylist, a second classifier
# fail-open test) -- a doctrine written down is not enforcement; this hook
# is.
#
# REWORKED 2026-08-06 after a coordinator-directed adversarial review found
# five further problems in the FIRST version of this hook (LS-TEST-002
# through LS-TEST-005 below) -- the review that found four defects in the
# suite this hook exists to prevent, found five more IN the hook itself:
#
#   LS-TEST-002 (hook ordering): pytest calls same-named hook implementations
#     LIFO by default, so this conftest-registered hook ran BEFORE the
#     builtin mark plugin's own pytest_collection_modifyitems, which is what
#     actually deselects items for `-m`/`-k`. One offending marked test could
#     therefore abort the ENTIRE session (exit 4) even when the default lane
#     (`-m 'not (... or livesim)'`) had already excluded it and it would
#     never have run. Fixed with `@pytest.hookimpl(trylast=True)`, which
#     pushes this hook to run AFTER deselection, so `items` here only ever
#     contains what is actually about to execute.
#   LS-TEST-003 (wrong error type): every YAML-loading failure mode raised a
#     bare RuntimeError, which is not pytest-native and surfaces as an
#     INTERNALERROR with a raw traceback at exit 3, bypassing reporting
#     plugins -- while the SAME hook's other failure mode used a clean
#     pytest.UsageError at exit 4. Fixed: every raise in this module is now
#     pytest.UsageError.
#   LS-TEST-004 (fails open on vocabulary): `_status_is_raced` only matched
#     two prefixes and nothing validated `status` against the enum
#     LIVESIM-ISSUES.yaml's own header now declares mandatory, so a typo
#     ("fix in flight: A1", a space instead of a hyphen) or a synonym
#     ("resolved", "closed", "merged", "fix-landed: A1") silently took the
#     PERMISSIVE branch -- a human typo defeating the exact mechanism built
#     to catch human misses. Fixed: `_load_issue_statuses` now validates
#     every status against `_ALLOWED_STATUS_LITERALS` /
#     `_FIX_IN_FLIGHT_RE` and raises on anything unrecognised, and duplicate
#     ids are now detected and raised on rather than silently last-wins.
#   LS-TEST-005 (zero subjects / escape hatch): no test carried the marker
#     when this hook shipped, so it was permanently inert; and its own
#     recommended remedy ("convert it to strict xfail") REMOVED a test from
#     the cross-check entirely, since a strict-xfail test carried no marker.
#     Fixed: the two mechanisms now COMPOSE. A raced status is legal on a
#     documents_open_defect-marked test IF AND ONLY IF that test is also a
#     strict xfail (the xfail's own XPASS->FAIL is the alarm in that case);
#     a raced status on a bare (non-xfail) marked assertion is still an
#     error. Conversely, ANY strict-xfail test whose `reason=` text names an
#     LS-### id must ALSO carry documents_open_defect(id=...) covering that
#     id, or the strict-xfail path is invisible to this cross-check and
#     LS-TEST-005(b)'s "flip to fixed with zero test consequence" gap
#     reopens.
#
# FAILS CLOSED: a missing/unparseable/empty YAML, a malformed issue entry, a
# status outside the documented vocabulary, a duplicate id, or a marked
# test's id simply absent from the YAML are all ERRORS, never silent skips --
# a cross-check that quietly passes when it cannot read its own source of
# truth (or its own vocabulary) is the exact defect class this convention
# exists to catch.
# --------------------------------------------------------------------------
import re  # noqa: E402

import livesim_common as lc  # noqa: E402

_ISSUES_YAML_PATH = _REPO / "docs" / "testing" / "LIVESIM-ISSUES.yaml"

#: Exact-match allowed `status` literals (see LIVESIM-ISSUES.yaml's schema
#: header, which this set must stay in sync with).
_ALLOWED_STATUS_LITERALS = frozenset(
    {
        "open",
        "confirmed",
        "needs-rerun",
        "wont-fix-this-session",
        "reported-by-livesim-author",
        "fixed-this-session",
        "fixed",
    }
)

#: The one parameterised status: `fix-in-flight: <lane-id>` -- colon-space
#: required, exact hyphenation required. "fix in flight" (space), "fix_in_
#: flight" (underscore), "fix-inflight" (missing hyphen) are typos, not this
#: status, and LS-TEST-004 proved the OLD prefix-only check accepted them
#: silently.
_FIX_IN_FLIGHT_RE = re.compile(r"^fix-in-flight:\s*\S+$")

#: YAML `status` PREFIXES meaning "a fix has landed or is actively landing" --
#: a test still pinning the OBSERVED-broken behaviour under one of these
#: statuses is racing (or has already lost to) a real fix. Legal ONLY on a
#: strict-xfail test (see LS-TEST-005 above); an error on a bare assertion.
#: Prefix match (not exact-set) so "fix-in-flight: A1" / "fix-in-flight:
#: lane/ls-fsguard-0806" all match without spelling out every lane-id here.
#: Deliberately DOES include "fixed-this-session" via the "fixed" prefix
#: (ruling: 2026-08-06) -- a documents_open_defect-marked test can exist
#: against a `kind: test-infra` entry too, and a test still pinning the old
#: broken shape when that entry flips to "fixed-this-session" is exactly the
#: miss this hook exists to catch; a false positive costs someone thirty
#: seconds updating a test, a miss costs a silently-wrong assertion that
#: outlives everyone's memory of why it was written.
RACED_DEFECT_STATUS_PREFIXES = ("fixed", "fix-in-flight")

#: LS-### id pattern, used both to parse marker ids and to scan strict-xfail
#: `reason=` text for ids that SHOULD carry a matching marker (LS-TEST-005).
_LS_ID_RE = re.compile(r"LS-\d{3}")


def _status_is_valid(status: str) -> bool:
    return status in _ALLOWED_STATUS_LITERALS or bool(_FIX_IN_FLIGHT_RE.match(status))


def _status_is_raced(status: str) -> bool:
    return status.strip().lower().startswith(RACED_DEFECT_STATUS_PREFIXES)


def _load_issue_statuses(yaml_path: Path) -> dict[str, str]:
    """{id: status} for every entry in LIVESIM-ISSUES.yaml. Raises
    pytest.UsageError (never RuntimeError -- LS-TEST-003 -- and never a
    partial/empty result silently) on ANY read/parse/shape/vocabulary/
    duplicate-id problem -- see module docstring above for why this must
    fail closed."""
    if not yaml_path.exists():
        raise pytest.UsageError(
            f"documents_open_defect cross-check: canonical issues YAML missing at {yaml_path} "
            "-- cannot verify any @documents_open_defect marker; fix the path or the file, "
            "do not silently skip the check."
        )
    try:
        import yaml as _yaml  # noqa: PLC0415

        doc = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise pytest.UsageError(
            f"documents_open_defect cross-check: cannot parse {yaml_path}: {exc}"
        ) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("issues"), list):
        raise pytest.UsageError(
            f"documents_open_defect cross-check: {yaml_path} did not parse to a dict with an "
            f"'issues' list (got {type(doc).__name__}) -- schema drift or a corrupt file."
        )

    statuses: dict[str, str] = {}
    duplicate_ids: list[str] = []
    bad_statuses: list[str] = []
    for entry in doc["issues"]:
        if not isinstance(entry, dict) or "id" not in entry or "status" not in entry:
            raise pytest.UsageError(
                f"documents_open_defect cross-check: malformed issue entry (needs id+status): {entry!r}"
            )
        issue_id = str(entry["id"])
        status = str(entry["status"])
        if issue_id in statuses:
            duplicate_ids.append(issue_id)
            continue  # do not silently last-wins -- collected below and raised on
        if not _status_is_valid(status):
            bad_statuses.append(f"{issue_id}={status!r}")
        statuses[issue_id] = status

    problems: list[str] = []
    if duplicate_ids:
        problems.append(f"duplicate id(s) in {yaml_path}: {sorted(set(duplicate_ids))}")
    if bad_statuses:
        problems.append(
            f"status value(s) outside the documented enum in {yaml_path} (see its schema "
            "header for the allowed list, including the `fix-in-flight: <lane-id>` format): "
            + ", ".join(bad_statuses)
        )
    if problems:
        raise pytest.UsageError("documents_open_defect cross-check: " + "; ".join(problems))
    if not statuses:
        raise pytest.UsageError(
            f"documents_open_defect cross-check: {yaml_path} parsed with ZERO issues -- "
            "that is almost certainly the file being truncated/emptied, not a real state; "
            "fail closed rather than let every marker pass by default."
        )
    return statuses


def _marker_ids(marker: pytest.Mark) -> list[str]:
    """Normalize documents_open_defect(id=...) to a list of id strings --
    accepts a single string or an iterable of strings (a test can cover more
    than one YAML entry, e.g. the board_files F-1/F-2 test covers both
    LS-017 and LS-018)."""
    value = marker.kwargs.get("id")
    if value is None and marker.args:
        value = marker.args[0]
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value]
    return [str(value)]


def _marker_verified_pending_receipt(marker: pytest.Mark) -> str | None:
    """documents_open_defect(..., verified_fixed_pending_promotion="<receipt>"):
    the THIRD legal composition state (added 2026-08-06, LS-TEST review
    round, closing a design gap Opus found). The first two states are
    marked+strict-xfail (asserts the FIXED behaviour, currently red,
    self-alarms XPASS->FAIL the instant a fix lands) and unmarked (status
    still `open`, a bare assertion of the OBSERVED-broken behaviour is
    fine). Neither fits "a fix has been independently VERIFIED live/real,
    the test now asserts the fixed behaviour PLAINLY and passes, but a
    human still needs to promote the YAML to a terminal `fixed` status" --
    exactly LS-005's state after this session found `error.code` already
    reading `method_not_allowed` live.

    LS-TEST-010 (hardening, same review round): the flag originally accepted
    a bare `True`, which is an UNAUDITABLE attestation -- no collection hook
    can inspect what an assertion actually means, so a truthful "I verified
    this live" and an untrue "I'm marking this to make CI green" are
    indistinguishable, and the hook's own remedy message was the
    discoverability path for the untrue case. The flag now REQUIRES a
    non-empty evidence string (a LiveSim run-id, a commit sha, a live-probe
    date) -- rejecting bare `True`/`False` and the empty string -- and every
    flagged test is named in a terminal summary line (see
    pytest_terminal_summary below) so an attestation cannot sit silently for
    months; a human reviewing CI output sees exactly which tests are making
    this claim and can go verify the receipt.

    Returns the receipt string if present and valid. Raises ValueError (the
    caller converts this to a collection problem, not a crash) if the key is
    present but not a valid non-empty string -- including literal `True`."""
    if "verified_fixed_pending_promotion" not in marker.kwargs:
        return None
    value = marker.kwargs["verified_fixed_pending_promotion"]
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise ValueError(
            "verified_fixed_pending_promotion must be a non-empty evidence string "
            "(a LiveSim run-id, commit sha, or live-probe date) -- bare True/False and "
            f"the empty string are not accepted attestations, got {value!r}"
        )
    return value


def _xfail_marker_reason(item: pytest.Item) -> str | None:
    """The xfail marker's reason text if this item carries ANY xfail marker
    (strict or not), else None. Not itself gated on `strict` -- see
    _strict_xfail_reason below for why the orphan-id scan deliberately does
    NOT require strict."""
    marker = item.get_closest_marker("xfail")
    if marker is None:
        return None
    reason = marker.kwargs.get("reason")
    if reason is None and marker.args:
        reason = marker.args[0]
    return str(reason or "")


def _strict_xfail_reason(item: pytest.Item) -> str | None:
    """The xfail marker's reason text IF this item is a STRICT xfail, else
    None. Used to legalise a raced status (LS-TEST-005 composition) -- only a
    strict xfail actually self-alarms (XPASS->FAIL) when a fix lands, so only
    a strict xfail may stand in for a bare assertion on a raced-status
    defect."""
    marker = item.get_closest_marker("xfail")
    if marker is None or not marker.kwargs.get("strict", False):
        return None
    reason = marker.kwargs.get("reason")
    if reason is None and marker.args:
        reason = marker.args[0]
    return str(reason or "")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Collection-time gate -- see the LS-TEST-002 note above for why this is
    `trylast=True` (must run AFTER `-m`/`-k` deselection, not before).

    Fails LOUD (pytest.UsageError) if:
      * any @documents_open_defect(id=...)-marked, NON-strict-xfail test's
        defect id's YAML status has moved to a RACED value (fixed / actively
        being fixed) -- that test needs inverting or converting to xfail;
      * any marked test's id, or the parenthetical id, is absent from the
        YAML, or the marker carries no id at all;
      * ANY xfail test (strict or not -- deliberately not gated on strict,
        see the drift-prevention note below) whose `reason=` names an LS-###
        id does not ALSO carry a documents_open_defect marker covering that
        id (LS-TEST-005: otherwise the xfail path is invisible to this
        check);
      * the YAML itself cannot be read/parsed, has a malformed entry, a
        duplicate id, or a status outside the documented vocabulary (all
        raised from _load_issue_statuses, itself fail-closed).

    Drift-prevention note (2026-08-06, minor from the LS-TEST review round):
    the orphan-id scan below intentionally checks EVERY xfail marker naming
    an LS id, not just strict ones. A repo-wide `xfail_strict = true` default
    was considered and deliberately NOT applied here -- this repo has 20+
    other `@pytest.mark.xfail` sites outside tests/livesim (tests/acceptance,
    tests/simharness, tests/toolplane, ...) whose strict-semantics reliance
    this lane has no way to verify without running the full ~15k-test suite,
    and LS-TEST-002/009 already proved this lane's methodology can miss a
    global blast radius it isn't scoped to see. Enforcing the requirement
    LOCALLY (any LS-id-naming xfail must carry the marker, whether or not it
    is strict) gets the same drift protection -- a bare, non-strict xfail
    naming an id still fails collection here for lacking the marker, and
    once marked, the SEPARATE "raced status is legal only on a STRICT xfail"
    check below still requires strict=True specifically -- without a config
    change whose blast radius this lane cannot bound.
    """
    marked_items = [
        item for item in items if item.get_closest_marker("documents_open_defect") is not None
    ]

    # LS-TEST-005 (widened, LS-TEST-009-round): ANY xfail test naming an LS
    # id MUST carry the marker too, whether or not it is strict -- checked
    # before we even know whether any marker exists, so this can fire even
    # in a run with zero documents_open_defect markers. A bare (non-strict)
    # xfail with an LS-id reason is EXACTLY "a stale xfail hiding a real
    # green test" (this hook's own marker docstring's words) if it is ever
    # forgotten about; requiring the marker here means the id-vs-YAML-status
    # cross-check below still reaches it, even though non-strict xfail can't
    # itself self-alarm on XPASS.
    #
    # Known residual limitation (documented, not closed): this is a text
    # match on `LS-\d{3}` in the reason STRING. A strict-xfail defect test
    # whose reason OMITS the id (poor authoring, not a tooling gap) escapes
    # this scan entirely and relies solely on the test carrying the marker
    # for some other reason. There is no structural link from an xfail
    # marker to a YAML id short of parsing free text; closing this fully
    # would mean requiring every LS-id-tracking xfail to route through a
    # single project helper that takes the id as a real parameter, which is
    # a larger refactor than this hook is scoped to do today.
    problems: list[str] = []
    for item in items:
        reason = _xfail_marker_reason(item)
        if not reason:
            continue
        reason_ids = set(_LS_ID_RE.findall(reason))
        if not reason_ids:
            continue
        marker = item.get_closest_marker("documents_open_defect")
        have_ids = set(_marker_ids(marker)) if marker is not None else set()
        missing = reason_ids - have_ids
        if missing:
            problems.append(
                f"{item.nodeid}: xfail reason names {sorted(reason_ids)} but "
                f"@documents_open_defect covers {sorted(have_ids) if have_ids else '(no marker at all)'} "
                f"-- missing {sorted(missing)}. Every xfail defect test (strict or not) must carry "
                "documents_open_defect(id=...) for every id it names, or the YAML cross-check "
                "can never see it (LS-TEST-005)."
            )

    if not marked_items and not problems:
        return

    # Fail closed: any problem loading/validating the YAML aborts collection
    # for the WHOLE (already-deselected) run, not just the marked tests --
    # see _load_issue_statuses. Only load it if there is something to check
    # against it (a marked item) -- the strict-xfail-orphan problems above
    # don't need the YAML at all.
    statuses = _load_issue_statuses(_ISSUES_YAML_PATH) if marked_items else {}

    # LS-TEST-010: every valid verified_fixed_pending_promotion attestation
    # this run collects, for the terminal summary (see
    # pytest_terminal_summary below) -- an attestation must not be able to
    # sit silently for months with nothing naming it in the run's output.
    verified_pending_attestations: list[tuple[str, list[str], str]] = []

    for item in marked_items:
        marker = item.get_closest_marker("documents_open_defect")
        ids = _marker_ids(marker)
        if not ids:
            problems.append(
                f'{item.nodeid}: @pytest.mark.documents_open_defect(...) is missing id="LS-###"'
            )
            continue
        is_strict_xfail = _strict_xfail_reason(item) is not None
        try:
            verified_receipt = _marker_verified_pending_receipt(marker)
        except ValueError as exc:
            problems.append(f"{item.nodeid}: {exc}")
            continue
        is_verified_pending = verified_receipt is not None

        # LS-TEST-010: the two claims contradict each other ("already
        # verified fixed live" vs. "currently expected to fail") -- the
        # strict xfail would still govern at runtime so this is incoherent
        # rather than dangerous, but it should not be expressible.
        if is_verified_pending and is_strict_xfail:
            problems.append(
                f"{item.nodeid}: documents_open_defect(id={ids}, "
                f"verified_fixed_pending_promotion={verified_receipt!r}) is combined with strict "
                "xfail on the same test -- these claims contradict each other ('already verified "
                "fixed' vs. 'currently expected to fail'). Pick one: strict xfail if the fix is "
                "NOT yet verified here, or verified_fixed_pending_promotion (with the xfail marker "
                "removed) if it is."
            )
            continue

        if is_verified_pending:
            verified_pending_attestations.append((item.nodeid, ids, verified_receipt))

        for issue_id in ids:
            if issue_id not in statuses:
                problems.append(
                    f"{item.nodeid}: documents_open_defect(id={issue_id!r}) -- this id does not "
                    f"exist in {_ISSUES_YAML_PATH}. Every marked test's id must be a real, "
                    "current LIVESIM-ISSUES entry; add it or fix the typo."
                )
                continue
            status = statuses[issue_id]
            raced = _status_is_raced(status)
            if raced and not is_strict_xfail and not is_verified_pending:
                problems.append(
                    f"{item.nodeid}: documents_open_defect(id={issue_id!r}) asserts the "
                    f"OBSERVED-broken behaviour via a BARE (non-xfail) assertion, but "
                    f"LIVESIM-ISSUES.yaml now records status={status!r} for {issue_id} -- a fix "
                    "has landed or is landing. Convert this test to strict xfail asserting the "
                    "FIXED behaviour (like the LS-022 pair), mark it "
                    'verified_fixed_pending_promotion="<run-id/commit/probe-date>" if the fix is '
                    "independently verified live and the test now passes plainly (like LS-005), "
                    "or if it has already been inverted permanently, drop the marker."
                )
                # A raced status on a strict-xfail test, OR on a test marked
                # verified_fixed_pending_promotion=<receipt>, is LEGAL
                # (LS-TEST-005 composition, three states): the strict-xfail's
                # own XPASS->FAIL IS the alarm in the first case; in the
                # second, the test is deliberately plain-green because the
                # fix was independently verified (evidenced by the receipt),
                # and the marker's job is only to keep the YAML linkage alive
                # until a human promotes the status to a terminal `fixed`
                # (or reopens it).
            elif is_verified_pending and not raced:
                problems.append(
                    f"{item.nodeid}: documents_open_defect(id={issue_id!r}, "
                    f"verified_fixed_pending_promotion={verified_receipt!r}) claims an "
                    f"independently-verified fix, but LIVESIM-ISSUES.yaml status for {issue_id} is "
                    f"{status!r} -- not a raced status at all. Either promote the YAML "
                    "(fix-in-flight/fixed) to match what this test claims, or drop the flag if the "
                    "fix was never actually verified."
                )

    if problems:
        raise pytest.UsageError(
            "documents_open_defect cross-check FAILED at collection "
            f"({len(problems)} problem(s)):\n  " + "\n  ".join(problems)
        )

    # LS-TEST-010: stash for pytest_terminal_summary -- named on the config
    # object rather than a module global so parallel/nested pytest sessions
    # (pytester's own runs included) never leak state between them.
    config._livesim_verified_pending_attestations = verified_pending_attestations  # type: ignore[attr-defined]
    # Under xdist this hook runs in the WORKER; pytest_terminal_summary runs in
    # the controller, which would otherwise see nothing and print nothing --
    # the attestation vanishing exactly like the silence it exists to break.
    # Ship it up the workeroutput channel instead.
    if hasattr(config, "workeroutput"):
        config.workeroutput["livesim_attestations"] = [  # type: ignore[attr-defined]
            [nodeid, list(ids), receipt] for nodeid, ids, receipt in verified_pending_attestations
        ]


def pytest_testnodedown(node, error) -> None:
    """Controller side of the workeroutput hand-off above. Each worker reports
    the attestations it collected; merge them onto the controller's config so
    the terminal summary is identical serial and under -n."""
    shipped = getattr(node, "workeroutput", {}).get("livesim_attestations")
    if not shipped:
        return
    config = node.config
    merged = list(getattr(config, "_livesim_verified_pending_attestations", None) or [])
    seen = {row[0] for row in merged}
    for nodeid, ids, receipt in shipped:
        if nodeid not in seen:
            seen.add(nodeid)
            merged.append((nodeid, list(ids), receipt))
    config._livesim_verified_pending_attestations = merged  # type: ignore[attr-defined]


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    """LS-TEST-010: name every verified_fixed_pending_promotion attestation
    in this run's terminal output, so a truthful-but-unverified claim (the
    hook itself cannot tell truthful from untrue -- it can only require that
    a claim be MADE, with evidence, and then say so out loud) cannot sit
    silently for months without anyone seeing it was ever made."""
    attestations = getattr(config, "_livesim_verified_pending_attestations", None)
    if not attestations:
        return
    terminalreporter.section("documents_open_defect: verified_fixed_pending_promotion attestations")
    for nodeid, ids, receipt in attestations:
        terminalreporter.write_line(f"  {nodeid}  ids={ids}  receipt={receipt!r}")
    terminalreporter.write_line(
        f"{len(attestations)} test(s) claim an independently-verified fix pending YAML "
        "promotion -- verify the receipt above before trusting it; the hook can require "
        "evidence be NAMED, it cannot verify the evidence is true."
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "livesim: LiveSim observational live-simulation test (never gates a merge)",
    )
    config.addinivalue_line(
        "markers",
        'documents_open_defect(id="LS-###" | id=("LS-###", "LS-###"), '
        'verified_fixed_pending_promotion="<receipt>"): this test tracks known-open defect(s) '
        "keyed to docs/testing/LIVESIM-ISSUES.yaml, in one of three states: (1) a BARE assertion "
        "of OBSERVED-broken behaviour while the YAML status is still open; (2) combined with "
        "strict xfail, asserts the FIXED behaviour ahead of a landing fix (XPASS->FAILs the "
        'instant it lands); (3) verified_fixed_pending_promotion="<run-id/commit/probe-date>" '
        "-- a NAMED receipt, never a bool and never blank (collection REFUSES both) -- the fix was "
        "independently verified live, the test asserts the FIXED behaviour PLAINLY and passes, "
        "and a human still needs to promote the YAML status to a terminal `fixed`. "
        "Cross-checked at collection by pytest_collection_modifyitems above.",
    )
    for t in (
        "positive",
        "negative",
        "boundary",
        "concurrency",
        "recovery",
        "permission",
        "security",
        "degradation",
        "e2e_live",
    ):
        config.addinivalue_line("markers", f"{t}: LiveSim test type '{t}'")
    run_id = os.environ.get("LIVESIM_RUN_ID") or f"adhoc-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    config._livesim_run_id = run_id  # type: ignore[attr-defined]
    config._livesim_ctx = lc.RunContext.capture(  # type: ignore[attr-defined]
        run_id,
        config={
            "reaper_enforce_env": os.environ.get("OMNIAGENTOS_REAPER_ENFORCE"),
            "idle_minutes_env": os.environ.get("OMNIAGENTOS_SESSION_IDLE_MINUTES"),
            "max_park_env": os.environ.get("OMNIAGENTOS_SESSION_MAX_PARK_MINUTES"),
            "invocation": " ".join(sys.argv[:3]),
        },
    )
    lc.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    lc.evidence_dir(run_id).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Per-test telemetry recorder
# --------------------------------------------------------------------------


class LiveSimRecorder:
    def __init__(self, ctx: lc.RunContext, nodeid: str) -> None:
        self._ctx = ctx
        self.nodeid = nodeid
        self.tel = lc.TestTelemetry()
        self._t0 = time.perf_counter()

    # -- telemetry attachers ------------------------------------------------
    def record(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        cost_usd: float | None = None,
        cost_quality: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        inputs: Any = None,
        outputs: Any = None,
    ) -> None:
        t = self.tel
        if model is not None:
            t.model = model
        if provider is not None:
            t.provider = provider
        if cost_usd is not None:
            t.cost_usd = round(float(cost_usd), 8)
        if cost_quality is not None:
            t.cost_quality = cost_quality
        if tokens_in is not None:
            t.tokens_in = int(tokens_in)
        if tokens_out is not None:
            t.tokens_out = int(tokens_out)
        if inputs is not None:
            t.inputs_digest = lc.digest(inputs)
            t.inputs_preview = lc.preview(inputs)
        if outputs is not None:
            t.outputs_digest = lc.digest(outputs)
            t.outputs_preview = lc.preview(outputs)

    def target(self, *names: str) -> None:
        for n in names:
            if n not in self.tel.live_target:
                self.tel.live_target.append(n)

    def note(self, msg: str) -> None:
        self.tel.notes.append(str(msg))

    def cleanup(self, ok: bool) -> None:
        self.tel.cleanup_ok = bool(ok)

    def extra(self, **kv: Any) -> None:
        self.tel.extra.update(kv)

    def evidence(self, name: str, content: str | bytes) -> str:
        d = lc.evidence_dir(self._ctx.run_id) / _safe(self.nodeid)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(str(content), encoding="utf-8")
        rel = str(p.relative_to(lc.REPO)) if str(p).startswith(str(lc.REPO)) else str(p)
        self.tel.evidence_paths.append(rel)
        return str(p)

    @property
    def latency_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 1)


def _safe(nodeid: str) -> str:
    return (
        nodeid.replace("/", "_")
        .replace("::", "__")
        .replace("[", "_")
        .replace("]", "_")
        .replace(" ", "_")
    )


@pytest.fixture()
def livesim(request: pytest.FixtureRequest) -> LiveSimRecorder:
    ctx = request.config._livesim_ctx  # type: ignore[attr-defined]
    rec = LiveSimRecorder(ctx, request.node.nodeid)
    request.node._livesim_rec = rec  # type: ignore[attr-defined]
    return rec


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):  # type: ignore[override]
    outcome = yield
    report = outcome.get_result()
    # Record on the call phase for tests that ran, and on the setup phase for
    # tests that were skipped/errored before calling (so a skip is counted in
    # coverage rather than vanishing). Never double-record: a called test's
    # setup report is not skipped.
    is_call = report.when == "call"
    is_setup_skip = report.when == "setup" and (report.skipped or report.failed)
    if not (is_call or is_setup_skip):
        return
    rec: LiveSimRecorder | None = getattr(item, "_livesim_rec", None)
    ctx: lc.RunContext = item.config._livesim_ctx  # type: ignore[attr-defined]
    tel = rec.tel if rec is not None else lc.TestTelemetry()
    latency = rec.latency_ms if rec is not None else None

    status = "pass" if report.passed else ("fail" if report.failed else "skip")
    if getattr(report, "wasxfail", None) is not None:
        status = "xfail"
    types = [m for m in _TYPE_MARKERS if item.get_closest_marker(m)]
    message = ""
    if report.failed and report.longrepr is not None:
        message = lc.preview(str(report.longrepr), cap=800)

    record: dict[str, Any] = {
        "schema": lc.SCHEMA,
        "run_id": ctx.run_id,
        "nodeid": item.nodeid,
        "category": item.parent.name if item.parent else "",
        "types": types,
        "status": status,
        "ts": lc.iso_now(),
        "latency_ms": latency,
        "duration_s": round(getattr(report, "duration", 0.0), 3),
        # environment / commit / config
        "git_sha": ctx.git_sha,
        "git_dirty": ctx.git_dirty,
        "env_label": ctx.env_label,
        "host": ctx.host,
        "host_load_1m": ctx.host_load_1m,
        "python": ctx.python,
        "api_base": ctx.api_base,
        "config": ctx.config,
        # model / provider / cost / tokens
        "model": tel.model,
        "provider": tel.provider,
        "cost_usd": tel.cost_usd,
        "cost_quality": tel.cost_quality if tel.model else "n/a",
        "tokens_in": tel.tokens_in,
        "tokens_out": tel.tokens_out,
        # inputs / outputs
        "inputs_digest": tel.inputs_digest,
        "outputs_digest": tel.outputs_digest,
        "inputs_preview": tel.inputs_preview,
        "outputs_preview": tel.outputs_preview,
        # live targets / evidence / hygiene
        "live_target": tel.live_target,
        "evidence_paths": tel.evidence_paths,
        "cleanup_ok": tel.cleanup_ok,
        "notes": tel.notes,
        "extra": tel.extra,
        "message": message,
    }
    try:
        (lc.run_dir(ctx.run_id) / f"{_safe(item.nodeid)}.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        lc.append_jsonl(lc.ledger_path(), record)
    except Exception as exc:  # never let telemetry break a run
        print(f"[livesim] failed to persist record for {item.nodeid}: {exc}", file=sys.stderr)


_TYPE_MARKERS = (
    "positive",
    "negative",
    "boundary",
    "concurrency",
    "recovery",
    "permission",
    "security",
    "degradation",
    "e2e_live",
)


# --------------------------------------------------------------------------
# Live-system access fixtures
# --------------------------------------------------------------------------


class LiveApi:
    """Thin HTTP helper for the live API. Read-only by default.

    A test that needs to mutate must pass `allow_write=True` explicitly and is
    responsible for cleaning up (via livesim_ns-tagged rows).
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 8.0,
        allow_write: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and not allow_write:
            raise AssertionError(
                f"LiveApi refuses {method} {path} without allow_write=True (live prod safety)"
            )
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _maybe_json(raw), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace") if e.fp else ""
            return e.code, _maybe_json(raw), dict(e.headers or {})
        except (urllib.error.URLError, OSError) as e:
            # OSError covers TimeoutError/socket.timeout AND the plain OSError
            # "cannot read from timed out object" raised when a chunked body read
            # times out mid-stream (a large/slow endpoint under load) — status 0
            # so a sweeping test records the endpoint as unreachable, never crashes.
            return 0, {"error": str(e)}, {}

    def get(self, path: str, **kw: Any) -> tuple[int, Any, dict[str, str]]:
        return self.request("GET", path, **kw)


def socket_timeout():  # small shim to include socket.timeout in the except tuple
    import socket

    return socket.timeout


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@pytest.fixture()
def live_api(request: pytest.FixtureRequest) -> LiveApi:
    ctx = request.config._livesim_ctx  # type: ignore[attr-defined]
    return LiveApi(ctx.api_base)


@pytest.fixture()
def live_db_ro() -> Any:
    """READ-ONLY connection to the live runtime DB (mode=ro; physically cannot write)."""
    if not lc.LIVE_DB.exists():
        pytest.skip(f"live runtime DB not found at {lc.LIVE_DB}")
    conn = sqlite3.connect(f"file:{lc.LIVE_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def livesim_ns(request: pytest.FixtureRequest) -> str:
    """A unique, greppable namespace for rows/files a test creates."""
    ctx = request.config._livesim_ctx  # type: ignore[attr-defined]
    node = _safe(request.node.name)[:32]
    return f"livesim_{ctx.run_id}_{node}"


@pytest.fixture()
def scratch_dir(request: pytest.FixtureRequest) -> Path:
    ctx = request.config._livesim_ctx  # type: ignore[attr-defined]
    d = lc.evidence_dir(ctx.run_id) / _safe(request.node.nodeid) / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def scratch_db(scratch_dir: Path):
    """Opt-in isolated WRITABLE DB copied from the live runtime DB, with
    OMNIAGENTOS_DB / OMNIAGENTOS_VAR_DIR repointed at scratch for the test's
    duration and restored after.

    This is the safe path for any test that drives a product module which writes
    through the default DB path: use `scratch_db` (or patch the env yourself) so a
    module that ignores an explicit connection can never reach the live DB. Live
    reads stay on the read-only `live_db_ro` fixture. Skips if the live DB is
    absent. Yields (db_path, sqlite3.Connection)."""
    import os
    import shutil

    if not lc.LIVE_DB.exists():
        pytest.skip(f"live runtime DB not found at {lc.LIVE_DB}")
    dst = scratch_dir / "scratch.sqlite3"
    shutil.copy2(lc.LIVE_DB, dst)
    saved = {k: os.environ.get(k) for k in ("OMNIAGENTOS_DB", "OMNIAGENTOS_VAR_DIR")}
    os.environ["OMNIAGENTOS_DB"] = str(dst)
    os.environ["OMNIAGENTOS_VAR_DIR"] = str(scratch_dir)
    conn = sqlite3.connect(str(dst), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield dst, conn
    finally:
        conn.close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_observation_counters():
    """Reproducibility guard: clear the product modules' global observation
    counters before each test so a test that asserts a before/after delta on them
    is not contaminated by a prior test (matters the moment this suite is ever run
    under xdist; the default runner is serial). Best-effort — a module that has
    moved or renamed the counter is silently skipped, never a hard dependency."""
    def _clear() -> None:
        # Target the underlying mutable Counter objects, NOT the read-only accessor
        # functions: omniagentos.skills.resolve._DROP_COUNTS is the Counter,
        # skill_resolution_drop_counts() is a function that returns dict(_DROP_COUNTS)
        # and has no .clear (a round-2 correction).
        for mod_name, attr in (
            ("omniagentos.skills.resolve", "_DROP_COUNTS"),
            ("omniagentos.memlife.render", "RENDERED_CLAIM_DIAGNOSTICS"),
        ):
            try:
                import importlib

                obj = getattr(importlib.import_module(mod_name), attr, None)
                if hasattr(obj, "clear"):
                    obj.clear()
            except Exception:
                pass

    _clear()
    yield
    _clear()
