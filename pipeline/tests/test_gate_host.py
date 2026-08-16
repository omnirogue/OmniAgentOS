"""Where a gate runs, and — mostly — when it must NOT be sent away.

The rule this file protects is the one that was broken twice in a single day:
**a routing decision may only use a reading taken at dispatch time.** Every
other test here exists because the alternative failure — shipping a suite to a
host that cannot grade it — comes back looking like a defect in the candidate's
code rather than a fact about the twin.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import gate_host as gh
from bridge.governor import LOAD_READING_MAX_AGE_S


def reading(host, load, age=0.0):
    return gh.LoadReading(host, load, time.monotonic() - age)


def probes(local, remote):
    calls = {"local": 0, "remote": 0, "hosts": []}

    def pl():
        calls["local"] += 1
        return local

    def pr(host=gh.TWIN_HOST):
        calls["remote"] += 1
        calls["hosts"].append(host)
        # The pool asks each twin about ITSELF; a canned reading carrying the
        # wrong host name would let a test pass while the router paired a load
        # with the wrong box.
        return gh.LoadReading(host, remote.load1, remote.taken_at, remote.error)

    return pl, pr, calls


def no_host_probed_twice(calls):
    """The rule that actually forbids a storm: never ask the SAME box again.

    Pre-pool this was spelled `calls["remote"] == 1`, because the pool had one
    member and the two statements were indistinguishable. With more than one
    twin they diverge, and this is the one that carries the original meaning.
    """
    return len(calls["hosts"]) == len(set(calls["hosts"]))


# -------------------------------------------------------------- freshness --


def test_a_stale_reading_is_refused_not_used():
    """The named defect: 'I sent agents to a quiet box twice today based on
    readings that were minutes old and already wrong.'"""
    fresh = reading("local", 40.0, age=1.0)
    stale = reading("local", 40.0, age=LOAD_READING_MAX_AGE_S + 1)
    assert fresh.usable
    assert not stale.usable, "a reading older than the bound must not be usable"


def test_stale_local_reading_keeps_the_gate_local_and_probes_nothing():
    pl, pr, calls = probes(reading("local", 2.0, age=LOAD_READING_MAX_AGE_S + 5),
                           reading(gh.TWIN_HOST, 0.1))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert "unusable" in c.reason
    assert calls["remote"] == 0, "never probe the twin on an unknown local premise"


def test_unmeasurable_local_load_does_not_read_as_idle():
    pl, pr, _ = probes(gh.LoadReading("local", None, time.monotonic(), "no getloadavg"),
                       reading(gh.TWIN_HOST, 0.1))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"


def test_a_stale_twin_reading_is_refused_too():
    pl, pr, _ = probes(reading("local", 40.0),
                       reading(gh.TWIN_HOST, 0.5, age=LOAD_READING_MAX_AGE_S + 5))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    # Every twin in the pool was stale, so the summary must say so PER HOST —
    # a bare "no usable twin" would hide which box is sick.
    assert c.considered, "a refusal must name the twins it considered"
    assert all("unreachable/unreadable" in e["why"] for e in c.considered)


# ------------------------------------------------------------------ routing --


def test_busy_local_and_idle_twin_routes_to_the_twin():
    pl, pr, _ = probes(reading("local", 35.0), reading(gh.TWIN_HOST, 1.5))
    c = gh.choose_gate_host(["tests/api/"], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == gh.TWIN_HOST
    assert c.is_remote


def test_quiet_local_stays_local_without_probing_the_twin():
    pl, pr, calls = probes(reading("local", 4.0), reading(gh.TWIN_HOST, 0.1))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert calls["remote"] == 0, "an idle local box must not pay for an ssh round-trip"


def test_twin_that_is_not_materially_quieter_loses_the_tie():
    pl, pr, _ = probes(reading("local", 20.0), reading(gh.TWIN_HOST, 18.0))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert "not materially quieter" in c.reason


def test_unreachable_twin_falls_back_to_local_with_one_probe():
    pl, pr, calls = probes(reading("local", 40.0),
                           gh.LoadReading(gh.TWIN_HOST, None, time.monotonic(), "ssh rc=255"))
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert calls["remote"] == len(gh.TWIN_SPECS), "one probe per twin, no more"
    assert no_host_probed_twice(calls), "one preflight probe per host, never a retry storm"


# ------------------------------------------------------------- sensitivity --


def test_host_state_sensitive_suite_stays_local_whatever_the_load_says():
    pl, pr, calls = probes(reading("local", 90.0), reading(gh.TWIN_HOST, 0.1))
    c = gh.choose_gate_host(["tests/counterfeits/patches/cf-x.patch"],
                            ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert c.sensitivity, "the carve-out must say which rule it tripped"
    assert calls["local"] == 0 and calls["remote"] == 0, (
        "the carve-out is evaluated before any probe, so no load reading can "
        "talk the router into shipping a suite the twin cannot grade")


def test_every_carve_out_entry_carries_its_measurement():
    """A carve-out without evidence is a superstition that costs throughput
    forever. `tests/test_grandfather_clock_gate.py` was expected to need one and
    measurably does not — it pins ZoneInfo('America/New_York') itself and
    returned 54 passed under four zones spanning 25 hours, and on the twin."""
    assert gh.LOCAL_ONLY_PATTERNS, "the list may be short, never empty by accident"
    for pattern, why in gh.LOCAL_ONLY_PATTERNS:
        assert len(why) > 80, f"{pattern} has no stated evidence"
        assert any(k in why for k in ("Measured", "measured")), \
            f"{pattern} is asserted, not measured"


def test_the_clock_gate_is_not_carved_out():
    """Guards against re-adding the refuted carve-out on the strength of the
    plausible-sounding story that it is a 'clock-sensitive suite'."""
    assert not gh.suite_sensitivity(["tests/test_grandfather_clock_gate.py"])
    assert not gh.suite_sensitivity(["tests/scheduler/test_settle_clock_freshness.py"])


# ---------------------------------------------------------------- dispatch --


def test_remote_command_pins_the_timezone():
    """The divergence is removed rather than carved around: a suite that DOES
    read the host zone cannot tell the two boxes apart."""
    cmd = gh.remote_gate_command(
        "twin", workspace="/w", candidate="feat/x", receipt="/w/r.json",
        evidence_root="/e", env_pins={"TZ": "America/New_York", "LC_ALL": "C"})
    inner = cmd[-1]
    assert "TZ=America/New_York" in inner
    assert "LC_ALL=C" in inner
    assert cmd[0] == "ssh" and "BatchMode=yes" in cmd


def test_remote_command_pins_the_gate_workspace_and_interpreter():
    inner = gh.remote_gate_command("twin", workspace="/w", candidate="c",
                                   receipt="/r.json", evidence_root="/e")[-1]
    assert "MERGE_GATE_PINNED=1" in inner
    assert "OMNIAGENTOS_GATE_WORKSPACE=/w" in inner
    assert "MERGE_GATE_EVIDENCE_ROOT=/e" in inner
    assert "MERGE_GATE_PY=/w/.venv/bin/python" in inner


def test_remote_command_quotes_hostile_refs():
    inner = gh.remote_gate_command("twin", workspace="/w",
                                   candidate="x; rm -rf ~", receipt="/r.json",
                                   evidence_root="/e")[-1]
    assert "; rm -rf ~" not in inner.replace("'x; rm -rf ~'", "")
    assert "'x; rm -rf ~'" in inner


def test_system_tz_is_read_from_the_host_not_hard_coded():
    pins = gh.remote_env_pins()
    assert pins["TZ"], "a dispatch with no TZ pin re-opens the divergence"


def test_preflight_quotes_hostile_candidate_workspace_and_evidence():
    """The preflight script is shipped as the literal argument to `ssh host
    <script>`, so every interpolated value must be shell-quoted or a hostile
    branch/ref name executes on the twin. Regression guard for the injection
    found 2026-08-08 (preflight interpolated `candidate` raw while
    remote_gate_command already quoted it)."""
    import shlex
    captured = {}

    def fake_run(argv, **kw):
        captured["script"] = argv[-1]
        class P:
            returncode = 0
            stdout = "\n".join(f"OK\t{label}" for label, _ in gh.READINESS_CHECKS)
            stderr = ""
        return P()

    orig = gh.subprocess.run
    gh.subprocess.run = fake_run
    try:
        gh.preflight_remote("twin", workspace="/w; touch /tmp/pwned",
                            evidence_root="/e", candidate="x`id`; rm -rf ~")
    finally:
        gh.subprocess.run = orig

    script = captured["script"]
    # The hostile substrings appear ONLY inside single-quoted tokens; after
    # shell tokenization neither `rm` nor `touch` nor `id` is a command word.
    words = [w for w in shlex.split(script)
             if "/w;" not in w and "rm" not in w[:3] and "id" != w]
    assert "rm" not in words and "touch" not in words
    assert "'/w; touch /tmp/pwned'" in script
    assert "'x`id`; rm -rf ~'" in script


# ------------------------------------------------------------- twin pool --
# Added 2026-08-10 with the second rented Mac (MW0002). The pool exists to lift
# the gate ceiling from two concurrent runs to three; every test here guards a
# way that lifting could quietly go wrong.


def pool_probes(local, loads):
    """Probe stub with a DIFFERENT load per twin, so preference is observable."""
    calls = {"local": 0, "remote": 0, "hosts": []}

    def pl():
        calls["local"] += 1
        return local

    def pr(host=gh.TWIN_HOST):
        calls["remote"] += 1
        calls["hosts"].append(host)
        return gh.LoadReading(host, loads[host], time.monotonic())

    return pl, pr, calls


def test_the_quietest_twin_in_the_pool_wins():
    pl, pr, calls = pool_probes(reading("local", 40.0),
                                {"mw0001-owner": 9.0, "mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002"
    assert calls["remote"] == 2 and no_host_probed_twice(calls)


def test_an_exact_tie_keeps_the_proven_twin_in_front():
    """Ties must not flap. MW0001 has months of receipts; MW0002 is new.

    F-A4: supply an equal HEADROOM (not equal raw load) — mw0001-owner is the
    bigger box (16 perf cores), so a load6.0 gives it the SAME 10.0 headroom
    as mw0002 at load2.0 (12 perf cores). An equal-load test would not
    exercise the tie-break at all once ranking runs on headroom."""
    pl, pr, _ = pool_probes(reading("local", 40.0),
                            {"mw0001-owner": 6.0, "mw0002": 2.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    by_host = {e["host"]: e for e in c.considered}
    assert by_host["mw0001-owner"]["headroom"] == by_host["mw0002"]["headroom"] == 10.0
    assert c.host == gh.TWIN_HOST


def test_the_chosen_host_carries_ITS_OWN_paths_never_the_other_twins():
    """The prefixes genuinely differ: MW0001 runs as `youruser`, MW0002 as
    `cloud`. Pairing a host with the other's prefix gates against a path that
    does not exist and returns a workspace error shaped like a code defect."""
    pl, pr, _ = pool_probes(reading("local", 40.0),
                            {"mw0001-owner": 9.0, "mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002"
    assert c.workspace == gh.twin_spec("mw0002").workspace
    assert "/Users/cloud/" in c.workspace
    assert c.evidence_root == gh.twin_spec("mw0002").evidence_root


def test_a_busy_twin_is_excluded_and_the_free_one_is_used():
    pl, pr, calls = pool_probes(reading("local", 40.0),
                                {"mw0001-owner": 0.1, "mw0002": 3.0})
    c = gh.choose_gate_host([], ceiling=16.0, exclude=("mw0001-owner",),
                            probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002", "the quieter box is busy; the free one must be used"
    assert calls["hosts"] == ["mw0002"], "an excluded twin must not even be probed"


def test_when_every_twin_is_busy_the_gate_stays_local_and_probes_nothing():
    pl, pr, calls = pool_probes(reading("local", 40.0),
                                {"mw0001-owner": 0.1, "mw0002": 0.1})
    c = gh.choose_gate_host([], ceiling=16.0,
                            exclude=("mw0001-owner", "mw0002"),
                            probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert calls["remote"] == 0, "a full pool must not pay for ssh round-trips"


def test_one_dead_twin_does_not_deny_the_pool():
    """The failure that must NOT happen: MW0001 being unreachable making the
    router forget MW0002 exists and sending everything local."""
    calls = {"remote": 0, "hosts": []}

    def pl():
        return reading("local", 40.0)

    def pr(host=gh.TWIN_HOST):
        calls["remote"] += 1
        calls["hosts"].append(host)
        if host == "mw0001-owner":
            return gh.LoadReading(host, None, time.monotonic(), "ssh rc=255")
        return gh.LoadReading(host, 1.0, time.monotonic())

    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002"
    assert no_host_probed_twice(calls)


def test_pinning_one_twin_by_name_still_works():
    """Back-compat: --twin and the pre-pool call shape must keep their meaning."""
    pl, pr, calls = pool_probes(reading("local", 40.0),
                                {"mw0001-owner": 9.0, "mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, twin="mw0001-owner",
                            probe_local=pl, probe_remote=pr)
    assert c.host == "mw0001-owner", "an explicit twin must not be overridden by a quieter one"
    assert calls["hosts"] == ["mw0001-owner"]


def test_an_unregistered_twin_raises_rather_than_guessing_a_prefix():
    try:
        gh.twin_spec("mw9999")
    except KeyError as exc:
        assert "no twin spec" in str(exc)
    else:
        raise AssertionError("an unknown twin must not silently inherit a prefix")


def test_an_unregistered_twin_never_gets_a_guessed_prefix_from_the_router():
    """Cross-lineage review 2026-08-10 (blocker 3): twin_spec raised, but
    choose_gate_host quietly synthesised a spec from the FIRST twin's paths, so
    the public entry point could still pair host A with host B's filesystem."""
    pl, pr, calls = pool_probes(reading("local", 40.0),
                                {"mw0001-owner": 0.1, "mw0002": 0.1})
    c = gh.choose_gate_host([], ceiling=16.0, twin="mw9999",
                            probe_local=pl, probe_remote=pr)
    assert c.host == "local", "an unknown twin must not be routed to"
    assert c.workspace is None and c.evidence_root is None
    assert "unregistered twin" in c.reason and "instrument" in c.reason
    assert calls["remote"] == 0, "never probe a host we cannot resolve paths for"


def test_the_bigger_box_wins_when_raw_load_would_pick_the_smaller_one():
    """Cross-lineage review finding 4. MW0001 at 6/16 has 10 cores spare;
    MW0002 at 5/12 has 7. Raw load1 picks MW0002 — the tighter box — and the
    8-wide ladder then runs into contention that gets charged to the candidate."""
    pl, pr, _ = pool_probes(reading("local", 40.0),
                            {"mw0001-owner": 6.0, "mw0002": 5.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "mw0001-owner", "ranking must use headroom, not raw load1"
    by_host = {e["host"]: e for e in c.considered}
    assert by_host["mw0001-owner"]["headroom"] > by_host["mw0002"]["headroom"]


def test_the_smaller_box_still_wins_when_it_genuinely_has_more_room():
    pl, pr, _ = pool_probes(reading("local", 40.0),
                            {"mw0001-owner": 14.0, "mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002"


def _reload_gate_host_with_env(value):
    """Set THREELOOPS_ACTIVE_TWINS to `value` (or delete it for None) and
    importlib.reload the module so module-level TWIN_SPECS is recomputed —
    `_active_twin_specs()` alone does not touch the frozen-at-import
    TWIN_SPECS a real caller actually reads. ALWAYS paired with
    `_restore_gate_host_env` in a try/finally so a reload-based test cannot
    poison node order for the rest of the file (discipline: run the whole
    file twice back-to-back with no order-dependence)."""
    import os as _os
    prev = _os.environ.get("THREELOOPS_ACTIVE_TWINS")
    if value is None:
        _os.environ.pop("THREELOOPS_ACTIVE_TWINS", None)
    else:
        _os.environ["THREELOOPS_ACTIVE_TWINS"] = value
    import importlib
    importlib.reload(gh)
    return prev


def _restore_gate_host_env(prev):
    import importlib
    import os as _os
    if prev is None:
        _os.environ.pop("THREELOOPS_ACTIVE_TWINS", None)
    else:
        _os.environ["THREELOOPS_ACTIVE_TWINS"] = prev
    importlib.reload(gh)


def test_the_active_pool_can_be_narrowed_back_to_one_twin():
    """Finding 5: without this an installation with a single box keeps claiming
    a slot on an absent host and burning a tick per gate on instrument errors.

    F-A4: verify the RELOADED module's TWIN_SPECS (the thing every real caller
    actually reads), not just the return of `_active_twin_specs()` in
    isolation."""
    prev = _reload_gate_host_with_env("mw0001-owner")
    try:
        assert [s.host for s in gh._active_twin_specs()] == ["mw0001-owner"]
        assert [s.host for s in gh.TWIN_SPECS] == ["mw0001-owner"]
        import importlib

        from bridge import gate_loop as gl
        importlib.reload(gl)
        assert gl.MAX_CONCURRENT_GATES == 2
    finally:
        _restore_gate_host_env(prev)
        importlib.reload(gl)
    # Restored: the reload back to a clean env must land the full pool again.
    assert {s.host for s in gh.TWIN_SPECS} == {s.host for s in gh.KNOWN_TWIN_SPECS}


def test_an_all_unknown_active_pool_fails_closed_with_a_loud_warning(capsys):
    """F-A2 REVERSED (round-2 review): a typo used to fall back to 'all
    twins active' (favourable absence). Now it must fail CLOSED — zero active
    remote twins — and say so on stderr so the mistake is visible the same
    tick rather than silently degrading."""
    prev = _reload_gate_host_with_env("mwTYPO")
    try:
        assert gh._active_twin_specs() == ()
        assert gh.TWIN_SPECS == ()
        err = capsys.readouterr().err
        assert "gate-host CONFIG:" in err
        assert "mwTYPO" in err
    finally:
        _restore_gate_host_env(prev)
    assert {s.host for s in gh.TWIN_SPECS} == {s.host for s in gh.KNOWN_TWIN_SPECS}


def test_a_partially_unknown_active_pool_keeps_the_known_subset_and_warns(capsys):
    """One bad token in a list must not take down the whole pool — the known
    subset stays active, and the warning names what was dropped."""
    prev = _reload_gate_host_with_env("mw0001-owner,mwTYPO")
    try:
        assert [s.host for s in gh._active_twin_specs()] == ["mw0001-owner"]
        err = capsys.readouterr().err
        assert "gate-host CONFIG:" in err
        assert "mwTYPO" in err
    finally:
        _restore_gate_host_env(prev)


def test_a_twin_filtered_out_of_the_pool_is_still_resolvable_by_name():
    """twin_spec answers about KNOWN twins, so a --twin pin or an in-flight
    legacy gate on a de-activated box still resolves to the right paths.

    F-A4: actually CONFIGURE the filter (env set + reload) before asserting —
    the un-rewritten version asserted this with the pool untouched, so it
    never exercised "filtered out" at all."""
    prev = _reload_gate_host_with_env("mw0001-owner")
    try:
        assert [s.host for s in gh.TWIN_SPECS] == ["mw0001-owner"]
        assert gh.twin_spec("mw0002").workspace.startswith("/Users/cloud/")
    finally:
        _restore_gate_host_env(prev)


# ------------------------------------------------- I2' -- comparative headroom --
# Replaces v1 F-A1's absolute floor, which was one-sided (enforced on twins,
# never on local) and could route to a host with STRICTLY LESS headroom than
# local — repro: $RUN/.fusion/repro/B3-admission-floor-picks-the-worse-host.py


def test_reviewer_scenario_only_mw0002_reachable_stays_local_on_headroom():
    """The round-2 BLOCKER, reproduced directly: local is 16/16 cores
    (headroom 0.00); only mw0002 is active/reachable at 11/12 cores (headroom
    1.00). The twin's headroom edge (1.00) is far short of TWIN_MARGIN (4.0),
    so local must win — and the reason must show HEADROOM numbers (0.00 /
    1.00), not the raw loads (16.00 / 11.00) that a unit-mismatched
    comparison would have shown."""
    pl, pr, _ = pool_probes(reading("local", 16.0), {"mw0002": 11.0})
    c = gh.choose_gate_host([], ceiling=16.0, exclude=("mw0001-owner",),
                            probe_local=pl, probe_remote=pr)
    assert c.host == "local"
    assert "16.00" not in c.reason and "11.00" not in c.reason
    assert "0.00" in c.reason and "1.00" in c.reason


def test_a_genuinely_clean_twin_still_wins_under_comparative_headroom():
    pl, pr, _ = pool_probes(reading("local", 15.0), {"mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, exclude=("mw0001-owner",),
                            probe_local=pl, probe_remote=pr)
    assert c.host == "mw0002" and c.is_remote


def test_comparative_headroom_never_picks_a_host_with_less_headroom_than_local():
    """PROPERTY, the repro script's 4 cases: (a) the router's decision matches
    the comparative rule exactly, (b) whenever it DOES go remote, the chosen
    host has more headroom than every other probed twin (ranking is
    consistent), and (c) whenever it goes remote, that host's headroom clears
    local's by at least TWIN_MARGIN — the actual comparative-admission
    invariant (I2'), not a bare 'never less than local' rule, because a small
    edge under margin correctly stays local even though the twin technically
    has more headroom (case 1 below)."""
    cases = [
        # (local_load, {twin: load}, local_cores, expected_host)
        (16.0, {"mw0002": 11.0}, 16.0, "local"),      # reviewer's scenario
        (15.0, {"mw0002": 1.0}, 16.0, "mw0002"),       # genuinely clean twin
        (12.0, {"mw0002": 5.0}, 16.0, "local"),        # 4 vs 7: not worth the rsync
        (16.0, {"mw0001-owner": 9.0}, 16.0, "mw0001-owner"),  # 0 vs 7: twin clearly better
    ]
    for local_load, twin_loads, local_cores, expect in cases:
        excl = tuple(s.host for s in gh.KNOWN_TWIN_SPECS if s.host not in twin_loads)
        pl, pr, _ = pool_probes(reading("local", local_load), twin_loads)
        c = gh.choose_gate_host([], ceiling=local_cores, exclude=excl,
                                probe_local=pl, probe_remote=pr)
        assert c.host == expect, f"{local_load, twin_loads} -> {c.host}, want {expect}"
        local_headroom = local_cores - local_load
        by_host = {e["host"]: e for e in c.considered if "headroom" in e}
        if c.is_remote:
            winner_headroom = by_host[c.host]["headroom"]
            assert all(winner_headroom >= h["headroom"] for h in by_host.values()), (
                "the chosen host must be the highest-headroom probed twin")
            assert winner_headroom >= local_headroom + gh.TWIN_MARGIN - 1e-9, (
                "a remote choice must clear local headroom by at least the margin")


# ------------------------------------------------------- m2 -- considered schema --


def test_considered_schema_for_a_probed_twin_is_frozen():
    """{host, load1, perf_cores, headroom, admitted, reason} — the shape a
    caller/operator can rely on for every successfully-probed twin."""
    pl, pr, _ = pool_probes(reading("local", 40.0),
                            {"mw0001-owner": 9.0, "mw0002": 1.0})
    c = gh.choose_gate_host([], ceiling=16.0, probe_local=pl, probe_remote=pr)
    for entry in c.considered:
        assert set(entry) == {"host", "load1", "perf_cores", "headroom",
                              "admitted", "reason"}
        assert isinstance(entry["admitted"], bool)
        assert isinstance(entry["reason"], str)


def test_considered_schema_for_an_unreachable_twin_is_the_distinct_why_shape():
    pl, pr, _ = pool_probes(reading("local", 40.0), {"mw0001-owner": None})
    # pool_probes cannot express "unreachable"; build directly instead.
    def pr_unreachable(host=gh.TWIN_HOST):
        return gh.LoadReading(host, None, __import__("time").monotonic(), "ssh rc=255")
    c = gh.choose_gate_host([], ceiling=16.0, exclude=("mw0002",),
                            probe_local=pl, probe_remote=pr_unreachable)
    assert c.host == "local"
    assert len(c.considered) == 1
    entry = c.considered[0]
    assert set(entry) == {"host", "why"}


# --------------------------------------------------------- m1 -- CLI (main(argv)) --


def _run_main(argv, monkeypatch=None):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh.main(argv)
    import json
    return rc, json.loads(buf.getvalue())


def test_cli_twin_defaults_to_none_and_pool_routes():
    rc, out = _run_main(["--target", "tests/api/"])
    assert rc == 0
    assert "host" in out


def test_cli_unknown_twin_exits_nonzero_with_instrument_stderr_and_no_preflight(capsys):
    rc = gh.main(["--twin", "mw9999", "--preflight", "feat/x"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "gate-host CONFIG:" in err and "mw9999" in err


def test_cli_resolves_workspace_and_evidence_root_from_the_named_twins_own_spec(monkeypatch):
    captured = {}

    def fake_preflight(host, *, workspace, evidence_root, candidate, **kw):
        captured.update(host=host, workspace=workspace, evidence_root=evidence_root)
        return {"ready": True, "failed": [], "checked": 0}

    monkeypatch.setattr(gh, "preflight_remote", fake_preflight)
    rc, out = _run_main(["--twin", "mw0002", "--preflight", "feat/x"])
    assert rc == 0
    assert captured["host"] == "mw0002"
    assert captured["workspace"] == gh.twin_spec("mw0002").workspace
    assert captured["workspace"].startswith("/Users/cloud/")
    assert captured["evidence_root"] == gh.twin_spec("mw0002").evidence_root


def test_cli_explicit_workspace_flag_still_wins_over_the_resolved_default(monkeypatch):
    captured = {}

    def fake_preflight(host, *, workspace, evidence_root, candidate, **kw):
        captured.update(workspace=workspace, evidence_root=evidence_root)
        return {"ready": True, "failed": [], "checked": 0}

    monkeypatch.setattr(gh, "preflight_remote", fake_preflight)
    _run_main(["--twin", "mw0002", "--preflight", "feat/x",
              "--workspace", "/custom/ws", "--evidence-root", "/custom/ev"])
    assert captured["workspace"] == "/custom/ws"
    assert captured["evidence_root"] == "/custom/ev"


def test_cli_explicit_preflight_probes_the_named_twin_even_when_choice_is_local(monkeypatch):
    """Operators use --preflight to diagnose a twin the router just refused —
    it must still probe THAT twin, against its own resolved paths, never
    local's or the other twin's."""
    captured = {}

    def fake_preflight(host, *, workspace, evidence_root, candidate, **kw):
        captured.update(host=host, workspace=workspace)
        return {"ready": True, "failed": [], "checked": 0}

    def fake_choose(*a, **kw):
        return gh.HostChoice("local", "twin refused", local=gh.LoadReading("local", 40.0, 0))

    monkeypatch.setattr(gh, "preflight_remote", fake_preflight)
    monkeypatch.setattr(gh, "choose_gate_host", fake_choose)
    rc, out = _run_main(["--twin", "mw0002", "--preflight", "feat/x"])
    assert rc == 0
    assert out["host"] == "local"
    assert captured["host"] == "mw0002"
    assert captured["workspace"].startswith("/Users/cloud/")


def test_cli_preflight_with_no_twin_and_local_choice_reports_no_twin_rather_than_guessing():
    def fake_choose(*a, **kw):
        return gh.HostChoice("local", "quiet", local=gh.LoadReading("local", 1.0, 0))
    import unittest.mock
    with unittest.mock.patch.object(gh, "choose_gate_host", fake_choose):
        rc, out = _run_main(["--preflight", "feat/x"])
    assert rc == 0
    assert out["preflight"]["ready"] is False
    assert "no twin" in out["preflight"]["failed"][0]


# ---------------------------------------------------------------- I6 -- gtimeout self-bound --


def test_remote_gate_command_is_byte_identical_without_a_timeout():
    """timeout_s left unset (the default): the argv must not change shape at
    all for every existing caller of this function."""
    kw = dict(host="twin", workspace="/w", candidate="feat/x", receipt="/w/r.json",
              evidence_root="/e")
    assert gh.remote_gate_command(**kw) == gh.remote_gate_command(**kw, timeout_s=None)
    inner = gh.remote_gate_command(**kw)[-1]
    assert "gtimeout" not in inner


def test_remote_gate_command_wraps_the_bash_invocation_in_gtimeout_when_bounded():
    inner = gh.remote_gate_command("twin", workspace="/w", candidate="c",
                                   receipt="/r.json", evidence_root="/e",
                                   timeout_s=5400)[-1]
    assert "gtimeout -k 30 5400 bash /w/scripts/merge-gate.sh" in inner


def test_remote_gate_command_timeout_is_coerced_to_int_seconds():
    """The signature says `int`; a caller handing a float must not leak a
    fractional gtimeout argument onto the twin's argv."""
    inner = gh.remote_gate_command("twin", workspace="/w", candidate="c",
                                   receipt="/r.json", evidence_root="/e",
                                   timeout_s=90.7)[-1]
    assert "gtimeout -k 30 90 bash" in inner


# -------------------------------------------------------- I7 -- pick_twin --
# The DAEMON's claim function: production never calls choose_gate_host to
# actually dispatch. Every test here uses a probe stub keyed by host, same
# shape as pool_probes above but returning (spec, considered) rather than a
# HostChoice.


def _pt_probe(loads):
    calls = {"hosts": []}

    def probe(host):
        calls["hosts"].append(host)
        if loads.get(host) is None:
            return gh.LoadReading(host, None, __import__("time").monotonic(), "ssh rc=255")
        return gh.LoadReading(host, loads[host], __import__("time").monotonic())

    return probe, calls


def test_pick_twin_admits_at_the_headroom_floor_boundary():
    """headroom == GATE_LADDER_WORKERS admits — the boundary is inclusive."""
    load_at_floor = 16.0 - gh.GATE_LADDER_WORKERS   # mw0001-owner: 16 perf cores
    probe, calls = _pt_probe({"mw0001-owner": load_at_floor, "mw0002": 99.0})
    spec, considered = gh.pick_twin(probe=probe)
    assert spec is not None and spec.host == "mw0001-owner"
    by_host = {e["host"]: e for e in considered}
    assert by_host["mw0001-owner"]["admitted"] is True
    assert by_host["mw0001-owner"]["headroom"] == gh.GATE_LADDER_WORKERS
    assert by_host["mw0002"]["admitted"] is False


def test_pick_twin_refuses_just_under_the_floor():
    just_under = 16.0 - gh.GATE_LADDER_WORKERS + 0.5
    probe, _ = _pt_probe({"mw0001-owner": just_under, "mw0002": 99.0})
    spec, considered = gh.pick_twin(probe=probe)
    assert spec is None
    by_host = {e["host"]: e for e in considered}
    assert by_host["mw0001-owner"]["admitted"] is False
    assert "headroom" in by_host["mw0001-owner"]["reason"]


def test_pick_twin_min_clamps_the_floor_on_a_smaller_box():
    """A hypothetical small box (test-only TwinSpec, never a real host) is not
    held to the full GATE_LADDER_WORKERS floor — the clamp is
    min(GATE_LADDER_WORKERS, perf_cores), so a 4-core box only needs 4 of
    headroom, not 8."""
    tiny = gh.TwinSpec("tiny-test-box", "/tmp/w", "/tmp/e", perf_cores=4)
    orig_specs = gh.TWIN_SPECS
    gh.TWIN_SPECS = (tiny,)
    try:
        probe, _ = _pt_probe({"tiny-test-box": 0.0})   # headroom 4, floor min(8,4)=4
        spec, considered = gh.pick_twin(probe=probe)
        assert spec is tiny
        assert considered[0]["admitted"] is True
        assert considered[0]["headroom"] == 4.0

        probe2, _ = _pt_probe({"tiny-test-box": 1.0})  # headroom 3 < floor 4
        spec2, considered2 = gh.pick_twin(probe=probe2)
        assert spec2 is None
        assert considered2[0]["admitted"] is False
    finally:
        gh.TWIN_SPECS = orig_specs


def test_pick_twin_respects_exclude():
    probe, calls = _pt_probe({"mw0001-owner": 0.0, "mw0002": 0.0})
    spec, considered = gh.pick_twin(exclude={"mw0001-owner"}, probe=probe)
    assert spec is not None and spec.host == "mw0002"
    assert "mw0001-owner" not in calls["hosts"], "an excluded twin must not even be probed"
    assert all(e["host"] != "mw0001-owner" for e in considered)


def test_pick_twin_probes_each_active_twin_exactly_once():
    probe, calls = _pt_probe({"mw0001-owner": 0.0, "mw0002": 0.0})
    gh.pick_twin(probe=probe)
    assert calls["hosts"] == list(dict.fromkeys(calls["hosts"])), \
        "one probe per host, never a retry"
    assert set(calls["hosts"]) == {s.host for s in gh.TWIN_SPECS}


def test_pick_twin_records_why_for_an_unreachable_host():
    probe, _ = _pt_probe({"mw0001-owner": None, "mw0002": 0.0})
    spec, considered = gh.pick_twin(probe=probe)
    assert spec is not None and spec.host == "mw0002"
    by_host = {e["host"]: e for e in considered}
    assert "why" in by_host["mw0001-owner"]
    assert "unreachable" in by_host["mw0001-owner"]["why"]
    assert "load1" not in by_host["mw0001-owner"]  # the distinct unreachable shape


def test_pick_twin_returns_first_admissible_in_preference_order():
    """Both admissible; preference order (mw0001-owner first) still wins even
    though mw0002 has more raw headroom — pick_twin does not re-rank."""
    probe, _ = _pt_probe({"mw0001-owner": 0.0, "mw0002": -50.0})
    spec, considered = gh.pick_twin(probe=probe)
    assert spec.host == "mw0001-owner"
    by_host = {e["host"]: e for e in considered}
    assert by_host["mw0002"]["admitted"] is True, "mw0002 is also admissible, just not first"


def test_pick_twin_returns_none_when_no_twin_clears_the_floor():
    """Caller must defer rather than dispatch to an inadmissible box."""
    probe, _ = _pt_probe({"mw0001-owner": 15.0, "mw0002": 11.5})
    spec, considered = gh.pick_twin(probe=probe)
    assert spec is None


def test_effective_ladder_workers_validates_override_and_drives_admission(monkeypatch, caplog):
    """R13: invalid env values cannot widen admission; valid values affect it."""
    monkeypatch.setenv("MERGE_GATE_LADDER_WORKERS", "bad")
    assert gh.effective_ladder_workers() == gh.GATE_LADDER_WORKERS
    assert "invalid MERGE_GATE_LADDER_WORKERS" in caplog.text
    monkeypatch.setenv("MERGE_GATE_LADDER_WORKERS", "999")
    assert gh.effective_ladder_workers() == gh.GATE_LADDER_WORKERS
    monkeypatch.setenv("MERGE_GATE_LADDER_WORKERS", "4")
    probe, _ = _pt_probe({"mw0001-owner": 12.0, "mw0002": 99.0})
    spec, _ = gh.pick_twin(probe=probe)
    assert gh.effective_ladder_workers() == 4
    assert spec is not None and spec.host == "mw0001-owner"


# ---------------------------------------------------------------------------
# N-HOST POOL: config-driven membership, PHYSICAL-identity slot arithmetic.
#
# The load-bearing invariant is not "N hosts, N slots" — it is ONE GATE PER
# PHYSICAL BOX. Two SSH names for one Mac (`mw0001` and `mw0001-owner` both
# resolve to 203.0.113.10 on this installation) are one slot, not two, and the
# scheduler may only learn that LOCALLY: a box's self-reported hostname is not
# evidence (two Macs both answer `Mac-Studio.local`), while `ssh -G` expands
# Host/Match/Include/ProxyJump exactly as the dial would and never contacts the
# remote.
# ---------------------------------------------------------------------------


def _fixed_ids(mapping):
    """A resolver stub: host -> physical id (or None for unresolvable)."""
    calls = []

    def resolve(host, **_kw):
        calls.append(host)
        return mapping.get(host)

    return resolve, calls


def _spec(host, cores=16):
    return gh.TwinSpec(host, f"/ws/{host}", f"/ev/{host}", perf_cores=cores)


def test_resolve_ssh_physical_id_reads_the_destination_ssh_would_dial(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = ("user youruser\n"
                  "HostName 203.0.113.10\n"
                  "port 22\n")

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.resolve_ssh_physical_id("mw0001") == "203.0.113.10"
    assert seen["cmd"][:2] == ["ssh", "-G"], "must be the local expansion, not a dial"


def test_an_unresolvable_identity_is_none_never_a_guess(monkeypatch):
    monkeypatch.setattr(gh.time, "sleep", lambda *_a, **_k: None)

    class _Fail:
        returncode = 255
        stdout = ""

    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: _Fail())
    gh._IDENTITY_CACHE.clear()
    assert gh.resolve_ssh_physical_id("nope") is None

    def boom(*a, **k):
        raise OSError("no ssh on this box")

    monkeypatch.setattr(gh.subprocess, "run", boom)
    gh._IDENTITY_CACHE.clear()
    assert gh.resolve_ssh_physical_id("nope") is None

    def slow(*a, **k):
        raise gh.subprocess.TimeoutExpired(cmd="ssh", timeout=gh.SSH_IDENTITY_TIMEOUT_S)

    monkeypatch.setattr(gh.subprocess, "run", slow)
    gh._IDENTITY_CACHE.clear()
    assert gh.resolve_ssh_physical_id("nope") is None


def test_a_transient_local_probe_failure_is_retried_before_giving_up(monkeypatch):
    """FAIL-SAFE is preserved but not TRIGGER-HAPPY: a stable fault only becomes
    None after every attempt is spent, so a genuinely unprovable host is dropped
    while a momentary stall gets its retries. Regression guard: the old code took
    a single attempt, so any transient fork failure dropped the host outright."""
    monkeypatch.setattr(gh.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def always_timeout(*a, **k):
        calls["n"] += 1
        raise gh.subprocess.TimeoutExpired(cmd="ssh", timeout=gh.SSH_IDENTITY_TIMEOUT_S)

    monkeypatch.setattr(gh.subprocess, "run", always_timeout)
    gh._IDENTITY_CACHE.clear()
    assert gh.resolve_ssh_physical_id("nope") is None
    assert calls["n"] == gh.SSH_IDENTITY_ATTEMPTS, \
        "a transient failure must be retried up to the attempt budget, not dropped on the first"


def test_a_stable_rc0_refusal_with_no_hostname_is_not_retried(monkeypatch):
    """rc==0 but no `hostname` line is well-formed and stable — re-running it
    buys the identical answer, so it must NOT consume the retry budget."""
    monkeypatch.setattr(gh.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    class _NoHost:
        returncode = 0
        stdout = "user youruser\nport 22\n"

    def run(*a, **k):
        calls["n"] += 1
        return _NoHost()

    monkeypatch.setattr(gh.subprocess, "run", run)
    gh._IDENTITY_CACHE.clear()
    assert gh.resolve_ssh_physical_id("weird") is None
    assert calls["n"] == 1, "a stable refusal is decided on the first expansion"


def test_two_config_names_for_one_box_collapse_to_one_slot():
    """THE invariant. A pool that lists both aliases must still schedule ONE
    gate on that Mac; the earlier entry (preference order) is the survivor."""
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10", "mw0001": "203.0.113.10"})
    kept = gh.collapse_to_physical_boxes(
        (_spec("mw0001-owner"), _spec("mw0001")), resolve=resolve)
    assert [s.host for s in kept] == ["mw0001-owner"]


def test_distinct_boxes_each_keep_their_slot():
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10", "mw0002": "203.0.113.11"})
    kept = gh.collapse_to_physical_boxes(
        (_spec("mw0001-owner"), _spec("mw0002", 12)), resolve=resolve)
    assert [s.host for s in kept] == ["mw0001-owner", "mw0002"]
    assert [s.perf_cores for s in kept] == [16, 12], "per-box facts survive the collapse"


def test_a_host_with_no_provable_identity_is_dropped_not_admitted(capsys):
    """Unprovable identity cannot be distinguished from 'the box already
    gating', so it must not buy a slot. Dropping costs capacity; admitting
    costs the CPU-overload outage this scheduler exists to prevent."""
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10", "ghost": None})
    kept = gh.collapse_to_physical_boxes(
        (_spec("mw0001-owner"), _spec("ghost")), resolve=resolve)
    assert [s.host for s in kept] == ["mw0001-owner"]
    assert "ghost" in capsys.readouterr().err


def test_a_remote_that_resolves_to_this_box_is_not_a_second_slot(capsys):
    """`local` always owns a slot. A config entry that dials back to this
    machine (loopback) would hand the SAME Mac a second concurrent gate."""
    resolve, _ = _fixed_ids({"myself": "127.0.0.1", "mw0002": "203.0.113.11"})
    kept = gh.collapse_to_physical_boxes(
        (_spec("myself"), _spec("mw0002")), resolve=resolve)
    assert [s.host for s in kept] == ["mw0002"]
    assert "myself" in capsys.readouterr().err


def test_identity_is_compared_case_and_trailing_dot_insensitively():
    resolve, _ = _fixed_ids({"a": "Gate-Box.Example.Com.", "b": "gate-box.example.com"})
    kept = gh.collapse_to_physical_boxes((_spec("a"), _spec("b")), resolve=resolve)
    assert [s.host for s in kept] == ["a"]


def test_the_identity_probe_runs_once_per_host_per_pool_build():
    """One local subprocess per host, not one per comparison."""
    resolve, calls = _fixed_ids({"a": "1.1.1.1", "b": "2.2.2.2", "c": "1.1.1.1"})
    gh.collapse_to_physical_boxes(
        (_spec("a"), _spec("b"), _spec("c")), resolve=resolve)
    assert sorted(calls) == ["a", "b", "c"]


# -- the mw0001-owner regression, through the REAL retrying resolver ----------
# The tests above inject a stub resolver, so they never exercise the transient
# fork stall that actually dropped mw0001-owner. These drive `ssh -G` (mocked at
# subprocess.run) through `resolve_ssh_physical_id` -> `collapse_to_physical_boxes`
# so the retry itself is under test, end to end.


def _fake_ssh_G(host_ids, *, transient_fail=None):
    """A `subprocess.run(["ssh", "-G", host])` stand-in.

    `host_ids` maps a pool host to the HostName `ssh -G` would print for it.
    `transient_fail` maps a host to how many LEADING attempts raise
    TimeoutExpired before the good expansion returns — the exact shape of a cold
    first-fork stall on a loaded dispatcher.
    """
    state = {h: 0 for h in host_ids}
    calls = []

    def run(cmd, **kwargs):
        host = cmd[-1]
        calls.append(host)
        if state[host] < (transient_fail or {}).get(host, 0):
            state[host] += 1
            raise gh.subprocess.TimeoutExpired(cmd="ssh", timeout=gh.SSH_IDENTITY_TIMEOUT_S)

        class _P:
            returncode = 0
            stdout = f"user youruser\nhostname {host_ids[host]}\nport 22\n"

        return _P()

    return run, calls


def test_a_reachable_distinct_twin_survives_a_transient_first_probe_and_is_kept(monkeypatch):
    """THE regression, reproduced end to end. mw0001-owner is the FIRST pool entry,
    so it bears the cold-fork cost of the first `ssh` in a fresh `--once` process;
    a single transient TimeoutExpired on that probe used to drop a healthy 24-core
    box for the whole tick (pool = local + mw0002 only). The bounded retry lets
    the warm second attempt resolve it, so the pool keeps BOTH twins and the third
    gate slot is restored."""
    monkeypatch.setattr(gh.time, "sleep", lambda *_a, **_k: None)
    run, calls = _fake_ssh_G(
        {"mw0001-owner": "203.0.113.10", "mw0002": "203.0.113.11"},
        transient_fail={"mw0001-owner": 1})
    monkeypatch.setattr(gh.subprocess, "run", run)
    gh._IDENTITY_CACHE.clear()
    try:
        kept = gh.collapse_to_physical_boxes((_spec("mw0001-owner"), _spec("mw0002", 12)))
    finally:
        gh._IDENTITY_CACHE.clear()
    assert [s.host for s in kept] == ["mw0001-owner", "mw0002"], \
        "a transient cold-fork stall on the first twin must not drop it"
    assert calls.count("mw0001-owner") == 2, "exactly one retry recovered the identity"


def test_two_names_for_one_box_still_dedupe_through_the_retrying_resolver(monkeypatch):
    """The invariant the retry must NOT weaken: two aliases for 203.0.113.10
    (mw0001-owner + mw0001) collapse to ONE slot even when the first alias's probe
    stalls once. One physical box, one gate — the earlier entry survives."""
    monkeypatch.setattr(gh.time, "sleep", lambda *_a, **_k: None)
    run, _ = _fake_ssh_G(
        {"mw0001-owner": "203.0.113.10", "mw0001": "203.0.113.10"},
        transient_fail={"mw0001-owner": 1})
    monkeypatch.setattr(gh.subprocess, "run", run)
    gh._IDENTITY_CACHE.clear()
    try:
        kept = gh.collapse_to_physical_boxes((_spec("mw0001-owner"), _spec("mw0001")))
    finally:
        gh._IDENTITY_CACHE.clear()
    assert [s.host for s in kept] == ["mw0001-owner"], \
        "both names resolve to one IP -> one slot; the never-two-gates guarantee holds"


# -- config file -----------------------------------------------------------


def _write_pool(tmp_path, body):
    p = tmp_path / "gate-hosts.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_the_shipped_config_reproduces_todays_pool_exactly():
    """The default file is a description of what already ships, not a change:
    loading it must yield the same hosts/paths/cores as the built-in fallback."""
    shipped = gh.load_twin_specs(config_path=gh.DEFAULT_GATE_HOSTS_CONFIG)
    assert shipped == gh.BUILTIN_TWIN_SPECS


def test_a_new_box_is_added_by_config_alone(tmp_path):
    cfg = _write_pool(tmp_path, """
twins:
  - host: mw0001-owner
    workspace: /Users/youruser/OmniAgentOS-gate
    evidence_root: /Users/youruser/OmniAgentOS/var/gate-evidence
    perf_cores: 16
  - host: mw0003
    workspace: /Users/cloud/OmniAgentOS-gate
    evidence_root: /Users/cloud/OmniAgentOS/var/gate-evidence
    perf_cores: 10
""")
    specs = gh.load_twin_specs(config_path=cfg)
    assert [s.host for s in specs] == ["mw0001-owner", "mw0003"]
    assert specs[1].workspace == "/Users/cloud/OmniAgentOS-gate"
    assert specs[1].perf_cores == 10


def test_a_missing_or_malformed_config_falls_back_to_the_shipped_pool(tmp_path, capsys):
    assert gh.load_twin_specs(config_path=tmp_path / "absent.yaml") == gh.BUILTIN_TWIN_SPECS
    for body in (
        "twins:\n  - host: ''\n    workspace: /w\n    evidence_root: /e\n",
        "twins:\n  - host: mw\n    workspace: relative/path\n    evidence_root: /e\n",
        "twins:\n  - host: mw\n    workspace: /w\n    evidence_root: /e\n    perf_cores: 0\n",
        "twins:\n  - host: 'two words'\n    workspace: /w\n    evidence_root: /e\n",
        "twins:\n  - host: -rf\n    workspace: /w\n    evidence_root: /e\n",
        "twins: {host: mw}\n",
        "[unclosed\n",
    ):
        cfg = _write_pool(tmp_path, body)
        assert gh.load_twin_specs(config_path=cfg) == gh.BUILTIN_TWIN_SPECS, body
    assert "gate-host CONFIG:" in capsys.readouterr().err


def test_an_empty_remote_pool_is_allowed_when_the_operator_says_so(tmp_path):
    """An explicitly EMPTY `twins:` (a local-only installation) is a
    DECLARATION, not a parse failure — it must not resurrect the shipped
    remotes. Both empty spellings mean the same thing; distinguishing them
    would be a trap for whoever writes the file next."""
    for body in ("twins: null\n", "twins: []\n"):
        assert gh.load_twin_specs(config_path=_write_pool(tmp_path, body)) == (), body


def test_a_duplicate_host_name_in_config_is_listed_once(tmp_path):
    cfg = _write_pool(tmp_path, """
twins:
  - host: mw0001-owner
    workspace: /w
    evidence_root: /e
  - host: mw0001-owner
    workspace: /other
    evidence_root: /other-e
""")
    specs = gh.load_twin_specs(config_path=cfg)
    assert [s.host for s in specs] == ["mw0001-owner"]
    assert specs[0].workspace == "/w", "first wins; a later duplicate cannot repoint the box"


# -- busy arithmetic across names ------------------------------------------


def test_an_in_flight_alias_marks_the_same_physical_box_busy():
    """A gate dispatched under one name must block the OTHER name for the same
    Mac — the exact way an N-host generalization loses the invariant."""
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10", "mw0001": "203.0.113.10",
                             "mw0002": "203.0.113.11"})
    specs = (_spec("mw0001-owner"), _spec("mw0002"))
    busy = gh.busy_physical_hosts({"mw0001"}, specs=specs, resolve=resolve)
    assert busy == {"mw0001-owner"}, "the alias in flight occupies the pool entry"


def test_an_unresolvable_in_flight_name_fails_closed_to_the_whole_pool():
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10", "mw0002": "203.0.113.11"})
    specs = (_spec("mw0001-owner"), _spec("mw0002"))
    busy = gh.busy_physical_hosts({"who-knows"}, specs=specs, resolve=resolve)
    assert busy == {"mw0001-owner", "mw0002"}, \
        "an unidentifiable running gate may be on ANY box; assume all"


def test_nothing_in_flight_leaves_the_whole_pool_free():
    resolve, _ = _fixed_ids({"mw0001-owner": "203.0.113.10"})
    assert gh.busy_physical_hosts(set(), specs=(_spec("mw0001-owner"),),
                                  resolve=resolve) == set()
