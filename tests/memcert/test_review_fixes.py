"""Decisive tests for the 2026-08-12 Class-A review fixes (Grok second lens).

BLOCKER-1: hedged answers must not launder through abstention detection —
verdict priority is correct > stale > abstain > wrong, and an explicit
ANSWER: line asserting a value overrides hedge phrasing.
BLOCKER-2: cert-split run artifacts never carry the raw seed.
SHOULD-FIX-3: degenerate all-abstain cells fail their bar.
SHOULD-FIX-7: hypothesizer confirmation requires CI AND McNemar p <= 0.01.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("memcert_core_rf", REPO_ROOT / "scripts" / "memcert" / "core.py")
sys.modules["core"] = core
rb = _load("memcert_run_bench_rf", REPO_ROOT / "scripts" / "memcert" / "run_bench.py")


# --- BLOCKER-1: hedge laundering ------------------------------------------


def test_hedged_reply_containing_expected_value_is_correct() -> None:
    spec = core.AnswerSpec(kind="exact", value="Gukugipa")
    verdict, score = core.grade_item(
        spec, "I cannot be fully certain, but the archives appear to be on Gukugipa."
    )
    assert (verdict, score) == ("correct", 1.0)


def test_hedged_reply_containing_stale_value_is_stale() -> None:
    spec = core.AnswerSpec(kind="exact", value="Fridays", stale_values=("Mondays",))
    verdict, score = core.grade_item(
        spec, "I don't have full information, but it seems deploys run on Mondays."
    )
    assert (verdict, score) == ("stale", -1.0)


def test_answer_line_assertion_overrides_hedge_phrasing() -> None:
    spec = core.AnswerSpec(kind="exact", value="Gukugipa")
    verdict, score = core.grade_item(
        spec, "I cannot determine this reliably.\nANSWER: Voheparo"
    )
    assert (verdict, score) == ("wrong", -0.5)


def test_abstain_item_with_asserted_value_is_wrong() -> None:
    spec = core.AnswerSpec(kind="abstain", value=core.ABSTAIN_TOKEN)
    verdict, score = core.grade_item(
        spec, "I am not sure the context specifies it.\nANSWER: Voheparo"
    )
    assert (verdict, score) == ("wrong", -0.5)


def test_pure_refusal_still_grades_abstention() -> None:
    spec = core.AnswerSpec(kind="exact", value="Gukugipa")
    verdict, score = core.grade_item(spec, "I don't have access to that information.")
    assert (verdict, score) == ("abstain_miss", 0.0)


# --- BLOCKER-2: cert artifacts carry no raw seed ---------------------------


class _TinyWorld:
    def __init__(self, seed: int, split: str) -> None:
        self.seed = seed
        self.split = split

    def items(self):
        return [
            core.Item(
                item_id=f"MEM-A1-01-x{self.split}",
                axis="A",
                level=1,
                split=self.split,
                question="Which machine holds the archives?",
                answer_spec=core.AnswerSpec(kind="exact", value="Gukugipa"),
                cluster_id="world-x",
            )
        ]

    def write_fixtures(self, out_dir, run_uuid=None):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "items.jsonl").write_text("{}\n")


def _run(split: str, seed: int, out: Path):
    return rb.run(
        models=["mock-m1"],
        arms=["none"],
        axes=["A"],
        trials=1,
        split=split,
        seeds=[seed],
        scale="S",
        out_dir=out,
        adapter="mock",
        budget_tokens=200,
        max_workers=1,
        worlds={seed: _TinyWorld(seed, split)},
    )


def test_cert_run_artifacts_never_carry_the_raw_seed(tmp_path: Path) -> None:
    seed = 987654321987
    _run("cert", seed, tmp_path / "certrun")
    blob = ""
    for f in sorted((tmp_path / "certrun").rglob("*")):
        if f.is_file():
            blob += f.name + "\n" + f.read_text(errors="ignore")
    assert str(seed) not in blob


def test_dev_run_keeps_raw_seed_for_debuggability(tmp_path: Path) -> None:
    seed = 987654321987
    _run("dev", seed, tmp_path / "devrun")
    manifest = json.loads((tmp_path / "devrun" / "summary.json").read_text())["manifest"]
    assert manifest["seeds"] == [seed]


# --- SHOULD-FIX-3: degenerate all-abstain cells fail their bar --------------


def test_all_abstain_cell_fails_a_zero_bar(tmp_path: Path) -> None:
    result = rb.run(
        models=["mock-oracle"],  # mock-oracle always abstains
        arms=["none"],
        axes=["A"],
        trials=1,
        split="dev",
        seeds=[7],
        scale="S",
        out_dir=tmp_path / "degen",
        adapter="mock",
        budget_tokens=200,
        max_workers=1,
        worlds={7: _TinyWorld(7, "dev")},
        bars={"A": 0.0},
        k_trials=1,
    )
    assert result.exit_code == rb.EXIT_BAR_FAILED


# --- Sol review round 2 -----------------------------------------------------


def test_context_builder_never_receives_the_answer_spec(tmp_path: Path) -> None:
    """MC-001: the arm callback must get a redacted item (no answer value)."""
    seen: list = []

    def spy_builder(arm, wdir, item, budget, rng):
        seen.append(item.answer_spec.value)
        return core.ArmContext(arm=arm, context_block="", meta={})

    rb.run(
        models=["mock-m1"], arms=["none"], axes=["A"], trials=1, split="dev",
        seeds=[3], scale="S", out_dir=tmp_path / "redact", adapter="mock",
        budget_tokens=200, max_workers=1, worlds={3: _TinyWorld(3, "dev")},
        context_builder=spy_builder,
    )
    assert seen and all(v == "" for v in seen), f"answer leaked to arm: {seen}"


class _ErrWorld(_TinyWorld):
    def items(self):
        base = super().items()
        # add an easy-correct sibling so a partial-error run has a green row
        return base + [
            core.Item(
                item_id=f"MEM-A1-02-x{self.split}", axis="A", level=1, split=self.split,
                question="easy", answer_spec=core.AnswerSpec(kind="exact", value="EASY"),
                cluster_id="world-x",
            )
        ]


def test_partial_adapter_errors_block_green(tmp_path: Path) -> None:
    """MC-003: one erroring row + one correct row must not certify at bar 1.0."""
    calls = {"n": 0}

    def flaky_adapter(model, system, user, wall_ms, **kw):
        calls["n"] += 1
        if "easy" in user.lower():
            return {"text": "ANSWER: EASY", "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                    "error": None}
        return {"text": "", "cost_usd": None, "tokens_in": None, "tokens_out": None,
                "error": "timeout"}

    result = rb.run(
        models=["mock-m1"], arms=["none"], axes=["A"], trials=1, split="dev",
        seeds=[4], scale="S", out_dir=tmp_path / "flaky", adapter="mock",
        budget_tokens=200, max_workers=1, worlds={4: _ErrWorld(4, "dev")},
        adapter_fn=flaky_adapter, bars={"A": 1.0}, k_trials=1,
    )
    assert result.exit_code in (rb.EXIT_BAR_FAILED, rb.EXIT_INSTRUMENT_FAILURE)


def test_seed_holdout_refuses_any_git_ancestor(tmp_path: Path) -> None:
    """MC-007: a state dir under an UNRELATED checkout is refused."""
    import subprocess
    fake_checkout = tmp_path / "other-repo"
    (fake_checkout / ".git").mkdir(parents=True)
    inside = fake_checkout / "state"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "memcert" / "seed_holdout.py"),
         "ensure", "--state-dir", str(inside)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stderr
    assert not inside.exists()
