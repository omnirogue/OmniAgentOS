"""Offline tests for the Hypothesis Tester harness (no network, no spend)."""

from __future__ import annotations

import json

import pytest

from scripts.benchmarks.hypothesis_tester import analyze as ht_analyze
from scripts.benchmarks.hypothesis_tester import experiment as ht_experiment
from scripts.benchmarks.hypothesis_tester.memory import (
    MEMORY_CHAR_BUDGET,
    build_scripted_history,
    memory_block,
)
from scripts.benchmarks.hypothesis_tester.worlds import (
    GATE_FIELDS,
    STRATEGIES,
    GatekeeperWorld,
    StrategyWorld,
    TrapCliWorld,
    extract_json,
    make_world,
)

# ---------------------------------------------------------------------------
# worlds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["gatekeeper", "strategy", "trapcli"])
def test_worlds_are_deterministic(family):
    a, b = make_world(family, 7), make_world(family, 7)
    other = make_world(family, 8)
    assert a.tasks == b.tasks
    assert a.tasks != other.tasks
    if family == "gatekeeper":
        assert a.rule_indices == b.rule_indices
    elif family == "strategy":
        assert a.mapping == b.mapping
    else:
        assert a.quirks == b.quirks


def test_gatekeeper_worlds_calibrated():
    for seed in range(8):
        w = GatekeeperWorld(seed)
        assert 0.05 <= w.feasible_fraction <= 0.60
        for task in w.tasks:
            assert task["chance"] > 0  # pins always satisfiable


def test_gatekeeper_verify_paths():
    w = GatekeeperWorld(0)
    task = w.task(1)
    good = dict(w.feasible[0])
    good.update(task["pins"])
    # A pinned feasible config may itself violate nothing only if pins came from
    # a feasible config; find one that satisfies pins AND rules.
    candidates = [c for c in w.feasible if all(c[f] == v for f, v in task["pins"].items())]
    assert candidates
    assert w.verify(task, candidates[0]).win
    bad_pin = dict(candidates[0])
    fld, val = next(iter(task["pins"].items()))
    bad_pin[fld] = next(v for v in GATE_FIELDS[fld] if v != val)
    assert w.verify(task, bad_pin).code == "REQ"
    infeasible = [
        c
        for c in _all_configs()
        if all(c[f] == v for f, v in task["pins"].items()) and c not in w.feasible
    ]
    if infeasible:
        v = w.verify(task, infeasible[0])
        assert not v.win and v.code.startswith("P")
        assert "field '" in v.feedback
    assert w.verify(task, None).code == "MALFORMED"


def _all_configs():
    from scripts.benchmarks.hypothesis_tester.worlds import _all_gate_configs

    return _all_gate_configs()


def test_strategy_world_structure():
    w = StrategyWorld(3)
    assert len(w.mapping) == 12
    assert len(w.exceptions) == 3
    base = {"regression": "bisect", "corruption": "rollback", "flaky": "quarantine"}
    for combo, winner in w.mapping.items():
        if combo in w.exceptions:
            assert winner != base[combo[0]]
        else:
            assert winner == base[combo[0]]
    task = w.task(1)
    combo = (
        task["features"]["failure_type"],
        task["features"]["blast_radius"],
        task["features"]["repro"],
    )
    assert w.verify(task, {"strategy": w.mapping[combo]}).win
    loser = next(s for s in STRATEGIES if s != w.mapping[combo])
    assert not w.verify(task, {"strategy": loser}).win


def test_strategy_parse_normalizes():
    w = StrategyWorld(0)
    assert w.parse_action('{"strategy": "Patch Forward"}') == {"strategy": "patch-forward"}
    assert w.parse_action('{"strategy": "warp"}') is None


def test_trapcli_canonical_wins_and_quirks_fail():
    for seed in range(6):
        w = TrapCliWorld(seed)
        plain = TrapCliWorld.__new__(TrapCliWorld)
        plain.quirks = set()
        for task in w.tasks:
            canonical = w.canonical_tokens(task)
            ok = w.verify(task, {"command": " ".join(canonical), "tokens": canonical})
            assert ok.win, (seed, task, canonical)
            plain_tokens = TrapCliWorld.canonical_tokens(plain, task)
            if plain_tokens != canonical:
                v = w.verify(task, {"command": " ".join(plain_tokens), "tokens": plain_tokens})
                assert not v.win
                assert v.code.startswith("E") and v.code != "E00"


def test_trapcli_specific_codes():
    w = TrapCliWorld(0)
    w.quirks = {"json_first", "ack_after_force", "out_suffix", "purge_yes"}
    sync_task = {"ep": 1, "sub": "sync", "store": "depot", "force": True}
    toks = ["omnex", "sync", "depot", "--force"]
    assert w.verify(sync_task, {"command": " ".join(toks), "tokens": toks}).code == "E17"
    export_task = {"ep": 2, "sub": "export", "project": "acme", "file": "report", "json": False}
    toks = ["omnex", "export", "acme", "report"]
    assert w.verify(export_task, {"command": " ".join(toks), "tokens": toks}).code == "E23"
    purge_task = {"ep": 3, "sub": "purge", "store": "depot"}
    toks = ["omnex", "purge", "depot"]
    assert w.verify(purge_task, {"command": " ".join(toks), "tokens": toks}).code == "E52"


def test_extract_json_tolerates_noise():
    assert extract_json('prose {"a": 1} more') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extract_json("no json here") is None


# ---------------------------------------------------------------------------
# memory arms
# ---------------------------------------------------------------------------


def _records(world, n=5):
    return build_scripted_history(world, n, "test-history")


def test_memory_arms_render():
    w = make_world("gatekeeper", 1)
    records = _records(w)
    task = w.task(6)
    assert memory_block("none", "gatekeeper", records, task) == ""
    outcomes = memory_block("outcomes", "gatekeeper", records, task)
    assert "episode 1:" in outcomes and "code" in outcomes
    # The hidden-rule field name must not leak into the outcome ledger arm.
    assert "policy violation" not in outcomes
    transcripts = memory_block("transcripts", "gatekeeper", records, task)
    assert "AGENT RESPONSE" in transcripts and len(transcripts) <= MEMORY_CHAR_BUDGET + 200
    lessons = memory_block("lessons", "gatekeeper", records, task)
    assert "Consolidated lessons" in lessons
    empty = memory_block("transcripts", "gatekeeper", [], task)
    assert empty == ""


def test_memory_lessons_are_cheaper_than_transcripts():
    w = make_world("trapcli", 2)
    records = _records(w, 10)
    task = w.task(11)
    lessons = memory_block("lessons", "trapcli", records, task)
    transcripts = memory_block("transcripts", "trapcli", records, task)
    assert 0 < len(lessons) < len(transcripts)


def test_memory_placebo_grows_with_history():
    w = make_world("strategy", 1)
    shadow_world = make_world("strategy", 1001)
    shadow = build_scripted_history(shadow_world, 15, "placebo:test")
    records = _records(w, 4)
    assert memory_block("placebo", "strategy", [], w.task(1), shadow) == ""
    m4 = memory_block("placebo", "strategy", records, w.task(5), shadow)
    assert "past episode" in m4
    assert m4.count("--- past episode") == 4


def test_memory_activation_prefers_relevant_episodes():
    w = make_world("trapcli", 0)
    sync_task = next(t for t in w.tasks if t["sub"] == "sync")
    records = _records(w, 12)
    act = memory_block("activation", "trapcli", records, sync_task)
    assert act.count("--- past episode") <= 3
    sync_eps = [r.ep for r in records if r.task["sub"] == sync_task["sub"]]
    assert any(f"past episode {ep} " in act or f"past episode {ep}\n" in act for ep in sync_eps)


# ---------------------------------------------------------------------------
# runner + experiment CLI (dry-run, offline)
# ---------------------------------------------------------------------------


def _run_cli(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("HYPOTHESIS_TESTER_VAR", str(tmp_path / "ht"))
    ht_experiment.main(argv)
    return tmp_path / "ht" / "runs"


BASE = [
    "--dry-run",
    "--families",
    "strategy",
    "--arms",
    "none,outcomes,placebo",
    "--models",
    "nemo",
    "--seeds",
    "0",
    "--episodes",
    "6",
    "--workers",
    "2",
]


def test_dry_run_end_to_end_and_resume(tmp_path, monkeypatch, capsys):
    runs_dir = _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t1", *BASE])
    exp = runs_dir / "t1"
    lines = [json.loads(x) for x in (exp / "episodes.jsonl").read_text().splitlines()]
    assert len(lines) == 18  # 3 arms x 6 episodes
    assert all(line["cost_usd"] == 0 for line in lines)
    assert (exp / "config.json").exists() and (exp / "summary.json").exists()
    results = [json.loads(x) for x in (exp / "results.jsonl").read_text().splitlines()]
    assert len(results) == 3 and all(r["errors"] == 0 for r in results)
    # Resume: same id re-run adds no new episode lines.
    _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t1", *BASE])
    lines2 = (exp / "episodes.jsonl").read_text().splitlines()
    assert len(lines2) == 18
    capsys.readouterr()


def test_changed_matrix_same_exp_id_refused(tmp_path, monkeypatch, capsys):
    _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t2", *BASE])
    changed = [x.replace("none,outcomes,placebo", "none,transcripts") for x in BASE]
    with pytest.raises(SystemExit):
        _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t2", *changed])
    capsys.readouterr()


def test_analyze_dry_run(tmp_path, monkeypatch, capsys):
    runs_dir = _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t3", *BASE])
    res = ht_analyze.analyze(runs_dir / "t3", episodes_per_run=6)
    assert res["runs_analyzed"] == 3
    assert set(res["hypotheses"]) >= {
        "H1",
        "H2",
        "H2b",
        "H3",
        "H4",
        "H5",
        "H6",
        "core_hypothesis",
    }
    assert (runs_dir / "t3" / "ANALYSIS.md").exists()
    ledgers = list((tmp_path / "ht").glob("ledger-*.jsonl"))
    assert len(ledgers) == 1
    entry = json.loads(ledgers[0].read_text().splitlines()[-1])
    assert entry["exp_id"] == "t3" and "core_hypothesis" in entry
    capsys.readouterr()


def test_paired_deltas_and_bootstrap():
    runs = {
        f"{arm}-{seed}": {
            "family": "strategy",
            "seed": seed,
            "arm": arm,
            "model": "nemo",
            "invalid": False,
            "late_success": 0.8 if arm == "outcomes" else 0.4,
            "repeat_rate": 0.0,
            "mem_tokens_mean": 100.0,
        }
        for arm in ("none", "outcomes")
        for seed in (0, 1, 2)
    }
    deltas = ht_analyze.paired_deltas(runs, "outcomes")
    assert deltas == [pytest.approx(0.4)] * 3
    stat = ht_analyze.bootstrap_ci(deltas, n=500)
    assert stat["mean"] == pytest.approx(0.4)
    assert stat["ci_low"] > 0


# ---------------------------------------------------------------------------
# regression tests for the 2026-08-12 cross-lineage review findings (HT-001..009)
# ---------------------------------------------------------------------------


def test_placebo_shadow_world_always_differs():  # HT-008
    from scripts.benchmarks.hypothesis_tester.runner import pick_shadow_world

    for family in ("gatekeeper", "strategy", "trapcli"):
        for seed in range(6):
            world = make_world(family, seed)
            shadow = pick_shadow_world(family, seed, 16, world)
            assert shadow.hidden_signature() != world.hidden_signature(), (family, seed)


def test_transcript_block_carries_task_identity():  # HT-002
    w = make_world("trapcli", 0)
    records = _records(w, 6)
    block = memory_block("transcripts", "trapcli", records, w.task(7))
    for r in records[-3:]:
        assert r.task_summary in block  # the task line survives, never truncated away
    assert "documented usage" not in block  # and the man page boilerplate does not


def test_verdict_needs_min_pairs():  # HT-001
    stat = ht_analyze.bootstrap_ci([0.5], n=200)
    assert ht_analyze._verdict_gain(stat, 0.10, ht_analyze.MIN_N_FAMILY) == "INCONCLUSIVE"
    full = ht_analyze.bootstrap_ci([0.5] * 12, n=200)
    assert ht_analyze._verdict_gain(full, 0.10, ht_analyze.MIN_N_FAMILY) == "CONFIRMED"


def test_torn_tail_is_terminated_on_open(tmp_path):  # HT-003
    from scripts.benchmarks.hypothesis_tester.runner import EpisodeLog

    path = tmp_path / "episodes.jsonl"
    good = json.dumps({"run_id": "r1", "ep": 1, "gen": 1, "code": "OK", "win": True})
    path.write_text(good + "\n" + '{"run_id": "r1", "ep": 2, "gen": 1, "co')  # torn, no \n
    log = EpisodeLog(path)
    log.append({"run_id": "r1", "ep": 2, "gen": 1, "code": "OK", "win": False})
    lines = path.read_text().splitlines()
    assert json.loads(lines[-1])["ep"] == 2  # repair episode is on its own line
    gen, eps = log.completed("r1")
    assert gen == 1 and set(eps) == {1}


def test_invalid_run_reruns_as_next_generation(tmp_path):  # HT-005
    from scripts.benchmarks.hypothesis_tester.runner import EpisodeLog

    path = tmp_path / "episodes.jsonl"
    with path.open("w") as f:
        for ep in (1, 2, 3):
            f.write(json.dumps({"run_id": "r1", "ep": ep, "gen": 1, "code": "ERROR"}) + "\n")
    gen, eps = EpisodeLog(path).completed("r1")
    assert (gen, eps) == (2, {})  # >2 errors: full rerun, nothing reused


def test_client_redacts_key_material():  # HT-006
    from scripts.benchmarks.hypothesis_tester.client import OpenRouterClient

    c = OpenRouterClient.__new__(OpenRouterClient)
    c.api_key = "sk-or-v1-super-secret-value-123456"
    echoed = f'{{"error": "bad request, auth was Bearer {c.api_key}"}} sk-abcdefghij999'
    out = c._redact(echoed)
    assert "super-secret" not in out and "sk-abcdefghij999" not in out
    assert "[redacted-key]" in out


def test_attempts_accounting(monkeypatch):  # HT-007
    import urllib.error

    from scripts.benchmarks.hypothesis_tester import client as ht_client

    c = ht_client.OpenRouterClient.__new__(ht_client.OpenRouterClient)
    c.api_key, c.timeout = "sk-test", 1
    spec = ht_client.MODELS["nemo"]
    sleeps: list[float] = []
    monkeypatch.setattr(ht_client.time, "sleep", sleeps.append)

    def raise_400(*a, **k):
        raise urllib.error.HTTPError("u", 400, "bad", None, None)

    monkeypatch.setattr(ht_client.urllib.request, "urlopen", raise_400)
    r = c.chat(spec, [])
    assert not r.ok and r.attempts == 1 and r.status == 400 and sleeps == []

    def raise_429(*a, **k):
        raise urllib.error.HTTPError("u", 429, "slow down", None, None)

    monkeypatch.setattr(ht_client.urllib.request, "urlopen", raise_429)
    r = c.chat(spec, [])
    assert not r.ok and r.attempts == 5 and len(sleeps) == 4  # no trailing sleep


def test_pin_feedback_stable_across_log_replay():  # HT-009
    w = GatekeeperWorld(0)
    task = {"ep": 1, "pins": {"scope": "wide", "cache": "off"}, "chance": 0.5}
    action = {k: v[0] for k, v in GATE_FIELDS.items()}
    action.update({"scope": "narrow", "cache": "on"})
    v1 = w.verify(task, action)
    replayed = json.loads(json.dumps(task, sort_keys=True))
    v2 = w.verify(replayed, action)
    assert v1.feedback == v2.feedback == v1.feedback
    assert v1.code == "REQ"


# ---------------------------------------------------------------------------
# round-2 reopenings (HT-001b / HT-003b / HT-004b)
# ---------------------------------------------------------------------------


def _ratio_run(arm, seed, tokens, family="strategy", model="nemo"):
    return {
        "family": family,
        "seed": seed,
        "arm": arm,
        "model": model,
        "invalid": False,
        "late_success": 0.5,
        "repeat_rate": 0.0,
        "mem_tokens_mean": tokens,
    }


def test_token_ratio_is_paired_and_nan_guarded():  # HT-001b
    runs = {}
    for seed in range(24):
        runs[f"lessons-{seed}"] = _ratio_run("lessons", seed, 100.0)
        runs[f"transcripts-{seed}"] = _ratio_run("transcripts", seed, 400.0)
    # Unmatched giant-token lessons runs must not drag the ratio: no baseline pair.
    for seed in range(100, 110):
        runs[f"lessons-{seed}"] = _ratio_run("lessons", seed, 99999.0)
    assert ht_analyze.token_ratio(runs, "lessons") == pytest.approx(0.25)
    # A token-only orphan pair (late_success None on both sides) is ineligible
    # for the accuracy contrast and must not skew the ratio either (HT-001b r3).
    for arm, tokens in (("lessons", 1.0), ("transcripts", 999999.0)):
        orphan = _ratio_run(arm, 200, tokens)
        orphan["late_success"] = None
        runs[f"{arm}-orphan"] = orphan
    assert ht_analyze.token_ratio(runs, "lessons") == pytest.approx(0.25)
    # Below the pooled minimum the ratio is NaN and H3/H5 stay INCONCLUSIVE.
    few = {k: v for k, v in runs.items() if v["seed"] < 5}
    import math

    assert math.isnan(ht_analyze.token_ratio(few, "lessons"))
    verdicts = ht_analyze.hypotheses(few)
    assert verdicts["H3"]["verdict"] == "INCONCLUSIVE"
    assert verdicts["H5"]["verdict"] == "INCONCLUSIVE"


def test_torn_multibyte_tail_recovers(tmp_path):  # HT-003b
    from scripts.benchmarks.hypothesis_tester.runner import EpisodeLog

    path = tmp_path / "episodes.jsonl"
    good = json.dumps({"run_id": "r1", "ep": 1, "gen": 1, "code": "OK", "win": True})
    torn = '{"run_id": "r1", "ep": 2, "note": "café'.encode()[:-1]  # split é
    path.write_bytes(good.encode() + b"\n" + torn)
    log = EpisodeLog(path)  # must not raise
    log.append({"run_id": "r1", "ep": 2, "gen": 1, "code": "OK", "win": False})
    assert json.loads(path.read_text(errors="replace").splitlines()[-1])["ep"] == 2
    gen, eps = log.completed("r1")
    assert gen == 1 and set(eps) == {1}


def test_analyze_refuses_stale_instrument(tmp_path, monkeypatch, capsys):  # HT-004b
    runs_dir = _run_cli(tmp_path, monkeypatch, ["run", "--exp-id", "t5", *BASE])
    cfg_path = runs_dir / "t5" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["code_sha"] = "0" * 16  # simulate: instrument changed since the run
    cfg_path.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit, match="allow_stale_instrument"):
        _run_cli(tmp_path, monkeypatch, ["analyze", "--exp-id", "t5", *BASE])
    # The guard lives in analyze() itself: the direct entry point refuses too,
    # before any artifact or ledger write (HT-004b round 3).
    with pytest.raises(RuntimeError, match="allow_stale_instrument"):
        ht_analyze.analyze(runs_dir / "t5", episodes_per_run=6)
    assert not (runs_dir / "t5" / "analysis.json").exists()
    # A hand-built Namespace without the flag gets the refusal, never an
    # AttributeError (HT-010).
    import argparse

    with pytest.raises(SystemExit, match="allow_stale_instrument"):
        ht_experiment.cmd_analyze(argparse.Namespace(exp_id="t5", episodes=6))
    _run_cli(
        tmp_path, monkeypatch, ["analyze", "--exp-id", "t5", "--allow-stale-instrument", *BASE]
    )
    res = json.loads((runs_dir / "t5" / "analysis.json").read_text())
    assert res["run_code_sha"] == "0" * 16
    assert res["analyzed_with_code_sha"] == ht_experiment._code_sha()
    ledger = json.loads(next((tmp_path / "ht").glob("ledger-*.jsonl")).read_text().splitlines()[-1])
    assert ledger["analyzed_with_code_sha"] == ht_experiment._code_sha()
    capsys.readouterr()


# ---------------------------------------------------------------------------
# recall arm (M7, DESIGN.md §10 round 5)
# ---------------------------------------------------------------------------


def _run_history(family, seed, n, arm_rng_tag="hist"):
    world = make_world(family, seed)
    return world, build_scripted_history(world, n, f"{arm_rng_tag}:{family}:{seed}")


def test_recall_gatekeeper_surfaces_matching_accepted_config():
    world, records = _run_history("gatekeeper", 3, 12)
    wins = [r for r in records if r.win]
    if not wins:
        pytest.skip("scripted history drew no gatekeeper win at this seed")
    win = wins[-1]
    task = {"ep": 13, "pins": dict(list(win.action.items())[:1]), "chance": 1.0}
    text = memory_block("recall", "gatekeeper", records, task)
    assert "ACCEPTED" in text
    assert json.dumps(win.action, sort_keys=True) in text
    # And the surfaced config really passes the world's rules.
    assert world.verify(task, win.action).win


def test_recall_strategy_exact_profile_win_is_directive():
    world, records = _run_history("strategy", 1, 16)
    wins = [r for r in records if r.win]
    if not wins:
        pytest.skip("scripted history drew no strategy win at this seed")
    win = wins[-1]
    text = memory_block("recall", "strategy", records, win.task)
    assert "KNOWN CORRECT for this exact incident profile" in text
    assert win.action_summary in text


def test_recall_trapcli_adapts_names_mechanically():
    from scripts.benchmarks.hypothesis_tester.memory import _trapcli_adapt

    win_task = {"sub": "sync", "store": "depot", "force": True}
    adapted = _trapcli_adapt(win_task, "omnex sync @depot --force --ack", {"sub": "sync", "store": "vault9", "force": True})
    assert adapted == "omnex sync @vault9 --force --ack"
    # Casing decoration carries over (upper_label quirk shape).
    tag_task = {"sub": "tag", "item": "item17", "label": "urgent"}
    adapted2 = _trapcli_adapt(tag_task, "omnex tag item17 URGENT", {"sub": "tag", "item": "node4", "label": "stale"})
    assert adapted2 == "omnex tag node4 STALE"


def test_recall_trapcli_same_shape_win_is_adapted_and_correct():
    world = make_world("trapcli", 0)
    # Fabricate a genuine win record for some task, then ask recall for a later
    # task of the same shape and check the adapted command verifies as a WIN.
    by_shape = {}
    for ep in range(1, 17):
        t = world.task(ep)
        by_shape.setdefault((t["sub"], bool(t.get("json")), bool(t.get("force"))), []).append(t)
    pair = next((v for v in by_shape.values() if len(v) >= 2), None)
    if pair is None:
        pytest.skip("no repeated trapcli shape at this seed")
    past, cur = pair[0], pair[1]
    tokens = world.canonical_tokens(past)
    cmd = " ".join(tokens)
    from scripts.benchmarks.hypothesis_tester.memory import EpisodeRecord

    rec = EpisodeRecord(
        ep=past["ep"], task=past, task_summary=world.task_summary(past),
        task_text=world.render_task(past), response_text=json.dumps({"command": cmd}),
        action={"command": cmd, "tokens": tokens}, action_summary=cmd,
        parse_ok=True, win=True, code="OK", feedback="command executed (code OK)",
    )
    text = memory_block("recall", "trapcli", [rec], cur)
    assert "submit EXACTLY" in text
    import re as _re

    m = _re.search(r"submit EXACTLY: `([^`]+)`", text)
    assert m, text
    adapted = m.group(1)
    action = world.parse_action(json.dumps({"command": adapted}))
    assert world.verify(cur, action).win, (adapted, world.canonical_tokens(cur))


def test_recall_is_deterministic_and_budgeted():
    for family in ("gatekeeper", "strategy", "trapcli"):
        world, records = _run_history(family, 2, 15)
        task = world.task(16)
        a = memory_block("recall", family, records, task)
        b = memory_block("recall", family, records, task)
        assert a == b
        assert len(a) <= MEMORY_CHAR_BUDGET
        assert memory_block("recall", family, [], task) == ""


def test_recall_trapcli_rule_extraction_bootstraps_other_subcommands():
    """A win on one subcommand teaches rules that solve OTHER subcommands,
    provided the current task's quirks were all observed in some win."""
    import json as _json

    from scripts.benchmarks.hypothesis_tester.memory import (
        EpisodeRecord,
        _trapcli_apply_rules,
        _trapcli_rules,
    )

    for seed in range(6):
        world = make_world("trapcli", seed)
        # Wins on every episode 1..8 as fuel; test rule application on 9..16.
        wins = []
        for ep in range(1, 9):
            t = world.task(ep)
            tokens = world.canonical_tokens(t)
            cmd = " ".join(tokens)
            wins.append(EpisodeRecord(
                ep=ep, task=t, task_summary=world.task_summary(t),
                task_text=world.render_task(t), response_text=_json.dumps({"command": cmd}),
                action={"command": cmd, "tokens": tokens}, action_summary=cmd,
                parse_ok=True, win=True, code="OK", feedback="command executed (code OK)",
            ))
        rules = _trapcli_rules(wins)
        observed_kinds = {r[0].split(":")[0] for r in rules}
        for ep in range(9, 17):
            cur = world.task(ep)
            built = _trapcli_apply_rules(cur, rules)
            action = world.parse_action(_json.dumps({"command": built}))
            verdict = world.verify(cur, action)
            # With 8 wins covering all 4 subcommands, every active quirk that
            # can appear in the current task has been observed, so the built
            # command must verify as a WIN.
            covered = all(
                (q not in world.quirks) or (kind in observed_kinds)
                for q, kind in (
                    ("at_prefix", "at_prefix"), ("out_suffix", "out_suffix"),
                    ("upper_label", "upper"), ("json_first", "json_first"),
                    ("ack_after_force", "after"), ("purge_yes", "end"),
                )
            )
            if covered:
                assert verdict.win, (seed, ep, built, world.canonical_tokens(cur), sorted(world.quirks))


def test_worlds_extend_past_16_with_identical_prefix():  # round 9
    for family in ("gatekeeper", "strategy", "trapcli"):
        short, long = make_world(family, 3, 16), make_world(family, 3, 32)
        assert len(long.tasks) == 32
        assert long.tasks[:16] == short.tasks
        assert short.hidden_signature() == long.hidden_signature()


def test_corrupt_arm_flips_every_verdict():  # round 11, counterfactual D
    from scripts.benchmarks.hypothesis_tester.memory import _SUCCESS_FEEDBACK

    for family in ("gatekeeper", "strategy", "trapcli"):
        world, records = _run_history(family, 4, 12)
        text = memory_block("corrupt", family, records, world.task(13))
        assert text == memory_block("corrupt", family, records, world.task(13))
        shown = [r for r in records[-6:]]
        for r in shown:
            if r.win:
                assert _SUCCESS_FEEDBACK[family] not in text.split(f"episode {r.ep} ---")[1].split("---")[0]
        # A flipped success string appears for at least one shown loss.
        if any(not r.win for r in shown):
            assert _SUCCESS_FEEDBACK[family] in text
        assert memory_block("corrupt", family, [], world.task(13)) == ""


def test_donor_records_feed_memory_not_own(tmp_path):  # round 11, cross-model
    import json as _json

    from scripts.benchmarks.hypothesis_tester.runner import (
        load_donor_records,
    )

    log_path = tmp_path / "episodes.jsonl"
    world = make_world("strategy", 0)
    lines = []
    for ep in (1, 2, 3):
        t = world.task(ep)
        lines.append({
            "run_id": "strategy-s0-recall-nemo", "ep": ep, "gen": 1,
            "task": t, "task_summary": world.task_summary(t),
            "task_text": world.render_task(t), "response": '{"strategy": "bisect"}',
            "action": {"strategy": "bisect"}, "action_summary": "bisect",
            "parse_ok": True, "win": ep == 2, "code": "WIN" if ep == 2 else "LOSS",
            "feedback": "STRATEGY SUCCEEDED (code WIN)" if ep == 2 else "STRATEGY FAILED (code LOSS)",
        })
    log_path.write_text("".join(_json.dumps(x) + "\n" for x in lines))
    recs = load_donor_records(log_path, "strategy-s0-recall-nemo")
    assert [r.ep for r in recs] == [1, 2, 3]
    assert load_donor_records(log_path, "strategy-s0-recall-gptoss") == []
    # Aligned prefix semantics: at episode 3 the consumer sees donor eps 1-2.
    visible = [r for r in recs if r.ep < 3]
    text = memory_block("recall", "strategy", visible, world.task(3))
    assert text  # donor history renders like own history
