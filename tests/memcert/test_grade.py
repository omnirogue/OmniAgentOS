"""Decisive tests for scripts/memcert/grade.py (DESIGN §6 scoring algebra + stats).

Hermetic: no network, no wall clock, deterministic (fixed boot seeds), tmp_path only.
The module is loaded from its file path, matching tests/scripts/test_prompt_ab_runner.py.
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
from pathlib import Path


def _load_grade():
    path = Path(__file__).parents[2] / "scripts" / "memcert" / "grade.py"
    spec = importlib.util.spec_from_file_location("memcert_grade", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GRADE = _load_grade()


def _item(item_id: str, kind: str, value, *, axis: str = "A", stale=(), cluster: str = "w1"):
    return GRADE.Item(
        item_id=item_id,
        axis=axis,
        level=1,
        split="dev",
        question="q?",
        answer_spec=GRADE.AnswerSpec(kind=kind, value=value, stale_values=tuple(stale)),
        cluster_id=cluster,
    )


def _row(item_id: str, raw: str, *, arm: str = "sys", model: str = "m1", trial: int = 0):
    return {"item_id": item_id, "arm": arm, "model": model, "trial": trial, "raw_answer": raw}


# ---------------------------------------------------------------------------
# (1) grade_rows verdict paths for every kind


def test_grade_rows_every_verdict_path() -> None:
    items = {
        it.item_id: it
        for it in [
            _item("i-exact", "exact", "blue vault"),
            _item("i-abstain", "abstain", None, axis="E"),
            _item("i-stale", "exact", "plan v2", axis="F", stale=("plan v1",)),
            _item("i-set", "set", ["alpha", "beta"], axis="B"),
            _item("i-ordered", "ordered", ["first", "second"], axis="C"),
            _item(
                "i-params",
                "params",
                {"tool": "deploy", "args": {"env": "prod", "region": "us"}},
                axis="H",
            ),
        ]
    }

    def one(item_id: str, raw: str):
        [r] = GRADE.grade_rows(items, [_row(item_id, raw)])
        return r["verdict"], r["score"]

    # exact: correct / wrong / abstain-on-answerable
    assert one("i-exact", "ANSWER: The Blue Vault") == ("correct", 1.0)
    assert one("i-exact", "ANSWER: red vault") == ("wrong", -0.5)
    assert one("i-exact", "ANSWER: UNKNOWN") == ("abstain_miss", 0.0)
    # abstain axis: correct abstention = 1.0; a guess = wrong
    assert one("i-abstain", "ANSWER: UNKNOWN") == ("abstain_correct", 1.0)
    assert one("i-abstain", "ANSWER: something plausible") == ("wrong", -0.5)
    # stale retracted value = -1.0 (MEM-F); the current value = correct
    assert one("i-stale", "ANSWER: plan v1") == ("stale", -1.0)
    assert one("i-stale", "ANSWER: plan v2") == ("correct", 1.0)
    # set / ordered
    assert one("i-set", "ANSWER: beta, alpha") == ("correct", 1.0)
    assert one("i-ordered", "ANSWER: second, first") == ("wrong", -0.5)
    assert one("i-ordered", "ANSWER: first, second") == ("correct", 1.0)
    # params full match / partial = 0.5*F1 / wrong
    full = json.dumps({"tool": "deploy", "args": {"env": "prod", "region": "us"}})
    assert one("i-params", full) == ("correct", 1.0)
    partial = json.dumps({"tool": "deploy", "args": {"env": "prod", "region": "eu"}})
    verdict, score = one("i-params", partial)
    # tp=1 of exp=2/got=2 -> precision=recall=F1=0.5 -> score = 0.5*0.5 = 0.25
    assert (verdict, score) == ("partial", 0.25)
    bad = json.dumps({"tool": "destroy", "args": {"env": "prod"}})
    assert one("i-params", bad) == ("wrong", -0.5)


def test_grade_rows_enriches_axis_and_cluster_and_rejects_unknown_items() -> None:
    items = {"i-exact": _item("i-exact", "exact", "x", axis="D", cluster="w9")}
    raw = _row("i-exact", "ANSWER: x")
    [r] = GRADE.grade_rows(items, [raw])
    assert (r["axis"], r["level"], r["cluster_id"]) == ("D", 1, "w9")
    assert "verdict" not in raw and "score" not in raw  # input rows not mutated
    try:
        GRADE.grade_rows(items, [_row("i-ghost", "ANSWER: x")])
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown item_id must raise, not silently skip")


# ---------------------------------------------------------------------------
# (2) summarize: hand-computed mean, deterministic bootstrap, CI contains mean


def _scored(item_id, score, *, axis="A", arm="sys", model="m1", trial=0, cluster="w1"):
    return {
        "item_id": item_id,
        "axis": axis,
        "arm": arm,
        "model": model,
        "trial": trial,
        "cluster_id": cluster,
        "verdict": "correct" if score == 1.0 else "wrong",
        "score": score,
    }


def test_summarize_hand_computed_mean_and_deterministic_ci() -> None:
    rows = [
        _scored("i1", 1.0, cluster="w1"),
        _scored("i2", -0.5, cluster="w2"),
        _scored("i3", 1.0, cluster="w3"),
        _scored("i4", 0.0, cluster="w4"),
    ]
    s1 = GRADE.summarize(rows, boot_seed=7, n_boot=400)
    s2 = GRADE.summarize(rows, boot_seed=7, n_boot=400)
    assert s1 == s2  # bit-for-bit deterministic for a fixed boot_seed
    entry = s1["A/sys/m1"]
    assert entry["mean"] == 0.375  # (1 - 0.5 + 1 + 0) / 4
    assert entry["n_rows"] == 4 and entry["n_items"] == 4 and entry["n_trials"] == 1
    assert entry["ci_lo"] <= entry["mean"] <= entry["ci_hi"]
    assert entry["verdicts"] == {"correct": 2, "wrong": 2}
    assert entry["pass_k"] is None  # no bars/k given


# ---------------------------------------------------------------------------
# (3) pass^k semantics


def _trial_rows(trial_means: list[float]) -> list[dict]:
    rows = []
    for trial, mean in enumerate(trial_means):
        for i in range(2):
            rows.append(_scored(f"i{i}", mean, trial=trial, cluster=f"w{i}"))
    return rows


def test_pass_k_requires_every_trial_over_the_bar() -> None:
    bars = {"A": 0.5}
    below = GRADE.summarize(_trial_rows([1.0, 1.0, 0.0]), bars=bars, k=3, n_boot=50)
    assert below["A/sys/m1"]["pass_k"] is False  # one trial below the bar
    above = GRADE.summarize(_trial_rows([1.0, 0.75, 1.0]), bars=bars, k=3, n_boot=50)
    assert above["A/sys/m1"]["pass_k"] is True
    # fewer than k observed trials means pass^k was NOT MEASURED (None, not
    # False): a 1- or 2-trial dev run must not read as a reliability FAILURE
    # (Sol review MC-002). A False is reserved for k trials where one fell
    # below the bar.
    short = GRADE.summarize(_trial_rows([1.0, 1.0]), bars=bars, k=3, n_boot=50)
    assert short["A/sys/m1"]["pass_k"] is None
    # an axis with no bar is not evaluable
    other = GRADE.summarize(_trial_rows([1.0, 1.0, 1.0]), bars={"B": 0.5}, k=3, n_boot=50)
    assert other["A/sys/m1"]["pass_k"] is None


# ---------------------------------------------------------------------------
# (4) paired_delta: delta, McNemar counts, exact p


def test_paired_delta_b_beats_a_and_mcnemar_exact() -> None:
    rows_a, rows_b = [], []
    # 6 items where only B succeeds, 1 where only A succeeds, 1 concordant success.
    for i in range(6):
        rows_a.append(_scored(f"i{i}", 0.0, arm="a", cluster=f"w{i}"))
        rows_b.append(_scored(f"i{i}", 1.0, arm="b", cluster=f"w{i}"))
    rows_a.append(_scored("i6", 1.0, arm="a", cluster="w6"))
    rows_b.append(_scored("i6", 0.0, arm="b", cluster="w6"))
    rows_a.append(_scored("i7", 1.0, arm="a", cluster="w7"))
    rows_b.append(_scored("i7", 1.0, arm="b", cluster="w7"))

    res = GRADE.paired_delta(rows_a, rows_b, boot_seed=3, n_boot=500)
    assert res["n_pairs"] == 8
    assert res["delta"] == 0.625  # (6*1 - 1 + 0) / 8
    assert res["delta"] > 0
    assert res["mcnemar_b"] == 1 and res["mcnemar_c"] == 6
    # exact: p = 2*(C(7,0)+C(7,1))*0.5^7 = 0.125
    assert res["mcnemar_p"] == 0.125
    assert res["mcnemar_p"] == GRADE.mcnemar_exact(1, 6)
    assert res["ci_lo"] <= res["delta"] <= res["ci_hi"]

    # determinism of the paired bootstrap
    assert res == GRADE.paired_delta(rows_a, rows_b, boot_seed=3, n_boot=500)


def test_mcnemar_exact_edge_values() -> None:
    assert GRADE.mcnemar_exact(0, 0) == 1.0
    assert GRADE.mcnemar_exact(3, 3) == 1.0  # symmetric counts cap at 1.0
    # b=0, c=5 -> 2*C(5,0)*0.5^5 = 0.0625
    assert GRADE.mcnemar_exact(0, 5) == 0.0625


# ---------------------------------------------------------------------------
# (5) clustering matters: opposite-effect clusters widen the CI vs naive rows


def _naive_row_bootstrap_ci(scores: list[float], seed: int, n_boot: int) -> tuple[float, float]:
    """Local naive helper: resample individual rows as if independent."""
    rng = random.Random(seed)
    n = len(scores)
    stats = sorted(
        sum(scores[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)
    )

    def pct(q: float) -> float:
        idx = q * (n_boot - 1)
        lo, hi = math.floor(idx), math.ceil(idx)
        frac = idx - lo
        return stats[lo] * (1 - frac) + stats[hi] * frac

    return pct(0.025), pct(0.975)


def test_cluster_ci_wider_than_naive_when_clusters_disagree() -> None:
    rows = [_scored(f"a{i}", 1.0, cluster="w1") for i in range(10)]
    rows += [_scored(f"b{i}", -0.5, cluster="w2") for i in range(10)]
    entry = GRADE.summarize(rows, boot_seed=11, n_boot=500)["A/sys/m1"]
    cluster_width = entry["ci_hi"] - entry["ci_lo"]
    naive_lo, naive_hi = _naive_row_bootstrap_ci([r["score"] for r in rows], 11, 500)
    naive_width = naive_hi - naive_lo
    assert cluster_width > naive_width
    # two clusters flipping between all-1.0 and all=-0.5 span nearly the whole range
    assert cluster_width > 1.0


# ---------------------------------------------------------------------------
# (6) degenerate inputs never crash


def test_degenerate_inputs_do_not_crash() -> None:
    assert GRADE.grade_rows({}, []) == []
    assert GRADE.summarize([]) == {}
    res = GRADE.paired_delta([], [])
    assert res == {
        "n_pairs": 0,
        "delta": None,
        "ci_lo": None,
        "ci_hi": None,
        "significant": False,
        "mcnemar_b": 0,
        "mcnemar_c": 0,
        "mcnemar_p": None,
    }
    assert GRADE.mde_hint(0, 1.0) is None
    assert GRADE.mde_hint(-3, 1.0) is None
    # non-overlapping pairs are ignored, not crashed on
    only_a = [_scored("i1", 1.0, arm="a")]
    only_b = [_scored("i2", 1.0, arm="b")]
    assert GRADE.paired_delta(only_a, only_b)["n_pairs"] == 0
    # single-cluster group: CI degenerates to the mean but never divides by zero
    single = GRADE.summarize([_scored("i1", 1.0)], n_boot=20)["A/sys/m1"]
    assert single["ci_lo"] == single["ci_hi"] == single["mean"] == 1.0


def test_mde_hint_value() -> None:
    # 2.8 * 0.5 / sqrt(25) = 0.28
    assert GRADE.mde_hint(25, 0.5) == 0.28
