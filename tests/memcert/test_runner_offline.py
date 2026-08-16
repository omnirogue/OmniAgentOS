"""Decisive tests for scripts/memcert/run_bench.py (DESIGN §12 runner CLI).

Hermetic: mock adapter only, no network, no wall-clock dependence in any
graded content, deterministic under fixed seeds, tmp_path only. The module is
loaded from its file path (matching tests/scripts/test_prompt_ab_runner.py
and tests/memcert/test_arms.py / test_grade.py in this same lane).

``scripts/memcert/gen.py`` (the fixture-world generator) is owned by another
lane of this same devtask and may not exist at any given point in this
lane's history, so every test constructs a tiny fake world by hand and
injects it via ``run_bench.run(worlds=...)`` rather than relying on that
import. The context builder is similarly hand-written and injected
(``context_builder=...``) to keep this file's fixtures decoupled from
``scripts/memcert/arms.py``'s exact on-disk session format (that module has
its own test file, ``test_arms.py``). ``scripts/memcert/grade.py`` -- the
real grading/statistics module -- IS exercised directly: it is run_bench's
auto-detected default grader/summarizer, so every test above that doesn't
pass ``grader_fn=``/``summarizer_fn=`` is a genuine integration test against
it (see ``test_default_fallback_grader_and_summarizer`` below for the
defensive path exercised when grade.py is unavailable).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _load(name: str, rel: str):
    path = Path(__file__).parents[2] / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: run_bench.py declares its own dataclasses under
    # `from __future__ import annotations` (PEP 563), and dataclass field-type
    # resolution on 3.12 looks the defining module up via `sys.modules[cls.__module__]`
    # -- a module loaded only via spec_from_file_location isn't there yet.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("memcert_run_bench", "scripts/memcert/run_bench.py")
REPORT = _load("memcert_report", "scripts/memcert/report.py")
core = RUNNER.core  # run_bench's own resolved `core` module (see its ImportError fallback)


# --------------------------------------------------------------------------
# fixture world: 6 hand-written items across 5 axes, incl. one abstain item
# (E1) and one params/action item (H1); 2 hand-written arms (none/fullhistory).
# --------------------------------------------------------------------------


def _make_items() -> list:
    return [
        core.Item(
            item_id="A1",
            axis="A",
            level=1,
            split="dev",
            question="What is the alpha project budget?",
            answer_spec=core.AnswerSpec(kind="exact", value="42"),
            cluster_id="w1",
        ),
        core.Item(
            item_id="A2",
            axis="A",
            level=2,
            split="dev",
            question="What is the beta project budget?",
            answer_spec=core.AnswerSpec(kind="exact", value="99", aliases=("ninety nine",)),
            cluster_id="w1",
        ),
        core.Item(
            item_id="B1",
            axis="B",
            level=1,
            split="dev",
            question="Which two sessions mention the rollout?",
            answer_spec=core.AnswerSpec(kind="set", value=["session-1", "session-2"]),
            cluster_id="w1",
        ),
        core.Item(
            item_id="C1",
            axis="C",
            level=1,
            split="dev",
            question="In what order did the deploy stages run?",
            answer_spec=core.AnswerSpec(kind="ordered", value=["staging", "prod", "rollback"]),
            cluster_id="w1",
        ),
        core.Item(
            item_id="E1",
            axis="E",
            level=1,
            split="dev",
            question="What is the office fax number?",
            answer_spec=core.AnswerSpec(kind="abstain", value=None),
            cluster_id="w1",
        ),
        core.Item(
            item_id="H1",
            axis="H",
            level=1,
            split="dev",
            question="Emit the deploy tool call for prod with 3 replicas.",
            answer_spec=core.AnswerSpec(
                kind="params", value={"tool": "deploy", "args": {"env": "prod", "replicas": 3}}
            ),
            cluster_id="w1",
        ),
    ]


TRANSCRIPT_TEXT = (
    "[2026-01-01T00:00:00Z] user: the alpha project budget is 42 dollars.\n"
    "[2026-01-01T00:05:00Z] user: session-1 and session-2 both mention the rollout.\n"
    "[2026-01-02T00:00:00Z] user: deploy stages ran staging, then prod, then rollback.\n"
    "[2026-01-03T00:00:00Z] user: deploy prod with 3 replicas using the deploy tool.\n"
)


class FakeWorld:
    """Minimal stand-in for gen.py's World (write_fixtures + items(split))."""

    def __init__(self, items: list, transcript_text: str = TRANSCRIPT_TEXT) -> None:
        self._items = items
        self._transcript_text = transcript_text

    def items(self, split: str) -> list:
        return [i for i in self._items if i.split == split]

    def write_fixtures(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sessions.jsonl").write_text(self._transcript_text)
        (out_dir / "items.json").write_text(
            json.dumps([i.public_json() for i in self._items], indent=2)
        )
        (out_dir / "CANARY.txt").write_text(core.canary_line("fixture-run-uuid") + "\n")


def _context_builder(arm: str, world_dir: Path, item, budget_tokens: int, rng) -> core.ArmContext:
    if arm == "none":
        return core.ArmContext(arm=arm, context_block="", meta={"arm": "none"})
    if arm == "fullhistory":
        transcript = (world_dir / "sessions.jsonl").read_text()
        return core.ArmContext(arm=arm, context_block=transcript, meta={"arm": "fullhistory"})
    raise ValueError(f"unhandled test arm: {arm}")


def _default_run_kwargs(out_dir: Path, **overrides):
    world = FakeWorld(_make_items())
    kwargs = dict(
        models=["mock-a", "mock-b"],
        arms=["none", "fullhistory"],
        axes=["A", "B", "C", "E", "H"],
        trials=2,
        split="dev",
        seeds=[7],
        scale="S",
        out_dir=out_dir,
        adapter="mock",
        budget_tokens=2000,
        max_workers=4,
        wall_ms=5000,
        worlds={7: world},
        context_builder=_context_builder,
    )
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------
# (1) end-to-end: results.jsonl rows = items * arms * models * trials, all graded
# --------------------------------------------------------------------------


def test_end_to_end_produces_one_graded_row_per_combination(tmp_path: Path) -> None:
    out_dir = tmp_path / "run1"
    result = RUNNER.run(**_default_run_kwargs(out_dir))

    n_items = 6
    n_arms = 2
    n_models = 2
    n_trials = 2
    expected = n_items * n_arms * n_models * n_trials

    assert len(result.rows) == expected

    lines = (out_dir / "results.jsonl").read_text().splitlines()
    assert len(lines) == expected

    for row in result.rows:
        assert row["verdict"] is not None
        assert "score" in row
        if row["error"] is None:
            assert row["score"] is not None
            assert row["verdict"] in {
                "correct",
                "wrong",
                "abstain_correct",
                "abstain_miss",
                "stale",
                "partial",
            }

    assert result.exit_code == RUNNER.EXIT_OK


def test_mock_oracle_always_abstains(tmp_path: Path) -> None:
    out_dir = tmp_path / "oracle"
    kwargs = _default_run_kwargs(
        out_dir,
        models=["mock-oracle"],
        arms=["none"],
        trials=1,
    )
    result = RUNNER.run(**kwargs)
    by_item = {row["item_id"]: row for row in result.rows}
    # The abstain item is correctly abstained...
    assert by_item["E1"]["verdict"] == "abstain_correct"
    assert by_item["E1"]["score"] == 1.0
    # ...but every answerable item is an abstain-miss (oracle cannot see answers).
    for item_id in ("A1", "A2", "B1", "C1"):
        assert by_item[item_id]["verdict"] == "abstain_miss"
        assert by_item[item_id]["score"] == 0.0
    assert by_item["H1"]["verdict"] == "abstain_miss"


# --------------------------------------------------------------------------
# (2) summary.json has per-axis cells with CIs
# --------------------------------------------------------------------------


def test_summary_has_per_axis_arm_model_cells_with_ci(tmp_path: Path) -> None:
    out_dir = tmp_path / "run2"
    result = RUNNER.run(**_default_run_kwargs(out_dir))
    axes = result.summary["axes"]  # flat "axis/arm/model" -> entry (grade.py's own shape)
    axis_letters = {entry["axis"] for entry in axes.values()}
    assert axis_letters == {"A", "B", "C", "E", "H"}

    cell = axes["A/none/mock-a"]
    for key in ("axis", "arm", "model", "mean", "ci_lo", "ci_hi", "n_rows", "n_items", "pass_k", "verdicts", "cost_usd"):
        assert key in cell
    assert cell["n_rows"] == 4  # 2 A-axis items (A1,A2) * 2 trials
    assert cell["n_items"] == 2
    assert cell["ci_lo"] <= cell["mean"] <= cell["ci_hi"]

    summary_on_disk = json.loads((out_dir / "summary.json").read_text())
    assert summary_on_disk["axes"]["A/none/mock-a"]["n_rows"] == 4
    assert summary_on_disk["manifest"]["canary"].startswith(f"MEMCERT-CANARY {core.SUITE_GUID}:")


# --------------------------------------------------------------------------
# (3) SUMMARY.md renders
# --------------------------------------------------------------------------


def test_summary_md_renders(tmp_path: Path) -> None:
    out_dir = tmp_path / "run3"
    result = RUNNER.run(**_default_run_kwargs(out_dir))
    md = REPORT.render_summary_md(result.summary)
    assert "# memcert run" in md
    for axis in ("A", "B", "C", "E", "H"):
        assert f"## MEM-{axis}" in md
    assert "none" in md and "fullhistory" in md
    assert "mock-a" in md and "mock-b" in md


# --------------------------------------------------------------------------
# (4) junit written with correct testcase count and failure entries under an
#     impossible bar (1.1)
# --------------------------------------------------------------------------


def test_junit_testcase_count_and_impossible_bar_failures(tmp_path: Path) -> None:
    out_dir = tmp_path / "run4"
    result = RUNNER.run(**_default_run_kwargs(out_dir))
    axes = result.summary["axes"]  # flat "axis/arm/model" -> entry
    n_cells = len(axes)

    junit_path = out_dir / "junit.xml"
    bars = {entry["axis"]: 1.1 for entry in axes.values()}
    REPORT.write_junit(result.summary, junit_path, bars=bars)

    tree = ET.parse(junit_path)
    suite = tree.getroot().find("testsuite")
    assert suite is not None
    testcases = suite.findall("testcase")
    assert len(testcases) == n_cells
    assert int(suite.get("tests")) == n_cells
    # every cell's best possible mean is 1.0 < bar 1.1 -> every testcase fails
    failures = [tc for tc in testcases if tc.find("failure") is not None]
    assert len(failures) == n_cells
    assert int(suite.get("failures")) == n_cells
    for name in (tc.get("name") for tc in testcases):
        assert name.startswith("MEM-")
        assert "--" in name


def test_junit_no_bars_means_no_failures(tmp_path: Path) -> None:
    out_dir = tmp_path / "run4b"
    result = RUNNER.run(**_default_run_kwargs(out_dir))
    junit_path = out_dir / "junit.xml"
    REPORT.write_junit(result.summary, junit_path, bars=None)
    tree = ET.parse(junit_path)
    suite = tree.getroot().find("testsuite")
    assert int(suite.get("failures")) == 0


def test_run_exit_code_1_when_bar_unmet(tmp_path: Path) -> None:
    out_dir = tmp_path / "run4c"
    result = RUNNER.run(**_default_run_kwargs(out_dir, bars={"A": 1.1}))
    assert result.exit_code == RUNNER.EXIT_BAR_FAILED


# --------------------------------------------------------------------------
# (5) determinism: same --out refuses; a fresh --out reproduces identical scores
# --------------------------------------------------------------------------


def test_unchanged_input_refuses_same_out_dir(tmp_path: Path) -> None:
    out_dir = tmp_path / "run5"
    first = RUNNER.run(**_default_run_kwargs(out_dir))
    assert first.exit_code == RUNNER.EXIT_OK
    assert not first.refused

    second = RUNNER.run(**_default_run_kwargs(out_dir))
    assert second.exit_code == RUNNER.EXIT_REFUSED
    assert second.refused
    assert second.summary is None

    # the original summary.json was not clobbered
    on_disk = json.loads((out_dir / "summary.json").read_text())
    assert on_disk == first.summary


def test_fresh_out_dir_reproduces_identical_scores(tmp_path: Path) -> None:
    run_a = RUNNER.run(**_default_run_kwargs(tmp_path / "run5a"))
    run_b = RUNNER.run(**_default_run_kwargs(tmp_path / "run5b"))

    def _key(row):
        return (row["item_id"], row["arm"], row["model"], row["trial"])

    rows_a = {_key(r): r for r in run_a.rows}
    rows_b = {_key(r): r for r in run_b.rows}
    assert set(rows_a) == set(rows_b)
    for key, row_a in rows_a.items():
        row_b = rows_b[key]
        assert row_a["raw_answer"] == row_b["raw_answer"]
        assert row_a["verdict"] == row_b["verdict"]
        assert row_a["score"] == row_b["score"]

    # axis-level means are identical too (same bootstrap CI seed derivation)
    assert run_a.summary["axes"] == run_b.summary["axes"]


# --------------------------------------------------------------------------
# (6) leak guard
# --------------------------------------------------------------------------


def test_leak_guard_raises_on_direct_system_prompt_leak() -> None:
    spec = core.AnswerSpec(kind="exact", value="super-secret-42")
    with pytest.raises(RUNNER.LeakGuardError):
        RUNNER.assert_no_answer_leak(
            "some system prefix ... super-secret-42 ... suffix", spec
        )


def test_leak_guard_allows_context_block_containing_the_answer() -> None:
    # The USER half (context_block) may legitimately contain the answer via a
    # transcript-based arm; only the harness-authored SYSTEM half is guarded.
    spec = core.AnswerSpec(kind="exact", value="42")
    item = core.Item(
        item_id="A1",
        axis="A",
        level=1,
        split="dev",
        question="What is the budget?",
        answer_spec=spec,
        cluster_id="w1",
    )
    context = core.ArmContext(
        arm="fullhistory", context_block="the alpha project budget is 42 dollars.", meta={}
    )
    system, user = RUNNER.build_messages(item, context, trial=0)
    RUNNER.assert_no_answer_leak(system, spec)  # must not raise
    assert "42" in user


def test_leak_guard_fires_when_the_answer_reaches_the_system_prompt() -> None:
    """The leak guard is the defense-in-depth backstop behind MC-001 redaction:
    even though arms can no longer OBTAIN the answer (they receive a redacted
    item — see test_context_builder_never_receives_the_answer_spec), any path
    that lands the answer value in the SYSTEM prompt must still abort the run.
    Tested directly on assert_no_answer_leak against the REAL spec."""
    spec = core.AnswerSpec(kind="exact", value="session-archive-vault")
    leaked_system = "You are a grader.\n(hint: the answer is session-archive-vault)"
    with pytest.raises(RUNNER.LeakGuardError):
        RUNNER.assert_no_answer_leak(leaked_system, spec)


def test_leak_guard_allows_a_clean_system_prompt() -> None:
    spec = core.AnswerSpec(kind="exact", value="session-archive-vault")
    RUNNER.assert_no_answer_leak("You are a grader. Answer only from memory.", spec)


# --------------------------------------------------------------------------
# (7) parked-terminal path: auth error marks the pair parked, no more retries
# --------------------------------------------------------------------------


def test_terminal_auth_error_parks_pair_and_stops_calling_adapter(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def broken_adapter(model, system, user, wall_ms, **kwargs):
        calls.append((kwargs.get("item_id"), kwargs.get("trial")))
        raise RuntimeError("401 Unauthorized: invalid api key for this model")

    out_dir = tmp_path / "run7"
    kwargs = _default_run_kwargs(
        out_dir,
        models=["broken-model"],
        arms=["none"],
        axes=["A"],  # 2 items (A1, A2)
        trials=2,  # -> 4 total tasks for this single (arm, model) pair
        max_workers=1,  # deterministic ordering: no cross-task race on `parked`
        adapter_fn=broken_adapter,
        backoffs=(0.0, 0.0),
    )
    result = RUNNER.run(**kwargs)

    assert len(result.rows) == 4
    # exactly ONE call ever reached the adapter: the first task hit a
    # terminal (auth) error on its FIRST attempt (no retry storm), parked the
    # pair, and every subsequent task short-circuited before calling in.
    assert len(calls) == 1
    assert "none:broken-model" in result.summary["parked_pairs"]

    parked_rows = [r for r in result.rows if "parked:" in (r.get("error") or "")]
    assert len(parked_rows) == 3
    for row in result.rows:
        assert row["error"]  # every row in this run is an error row
    assert result.exit_code == RUNNER.EXIT_INSTRUMENT_FAILURE


def test_non_terminal_error_retries_up_to_max_attempts(tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky_adapter(model, system, user, wall_ms, **kwargs):
        calls.append(1)
        return {"text": "", "cost_usd": None, "tokens_in": None, "tokens_out": None,
                "error": "transient network hiccup"}

    out_dir = tmp_path / "run7b"
    kwargs = _default_run_kwargs(
        out_dir,
        models=["flaky-model"],
        arms=["none"],
        axes=["A"],
        trials=1,
        max_workers=1,
        adapter_fn=flaky_adapter,
        backoffs=(0.0, 0.0),
        max_attempts=3,
    )
    result = RUNNER.run(**kwargs)
    # 2 A-axis items * 1 trial = 2 tasks, each retried to exhaustion (3 attempts)
    assert len(calls) == 2 * 3
    assert all(row["error"] == "transient network hiccup" for row in result.rows)
    assert result.summary["parked_pairs"] == []


# --------------------------------------------------------------------------
# defensive fallback: grader/summarizer used when scripts/memcert/grade.py is
# unavailable (dead in THIS environment since grade.py landed, but real code
# this repo's Makefile/CLI path can hit if grade.py is ever missing/broken)
# --------------------------------------------------------------------------


def test_default_fallback_grader_and_summarizer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(RUNNER, "grade_mod", None)
    out_dir = tmp_path / "run8"
    kwargs = _default_run_kwargs(
        out_dir,
        models=["mock-a"],
        arms=["none"],
        axes=["A", "E"],
        trials=2,
    )
    result = RUNNER.run(**kwargs)
    assert result.exit_code == RUNNER.EXIT_OK
    assert len(result.rows) == 3 * 1 * 1 * 2  # 3 items (A1,A2,E1) * 1 arm * 1 model * 2 trials
    for row in result.rows:
        assert row["verdict"] in {"correct", "wrong", "abstain_correct", "abstain_miss"}
        assert row["score"] is not None

    axes = result.summary["axes"]
    axis_letters = {entry["axis"] for entry in axes.values()}
    assert axis_letters == {"A", "E"}
    cell = axes["A/none/mock-a"]
    for key in ("mean", "ci_lo", "ci_hi", "n_rows", "n_items", "n_trials", "pass_k", "verdicts", "cost_usd"):
        assert key in cell
    assert cell["n_rows"] == 4
    assert cell["n_items"] == 2


def test_default_grade_rows_and_summarize_directly() -> None:
    """Unit-level check of the fallback functions themselves (bypassing run())."""
    items = {i.item_id: i for i in _make_items()}
    rows = [
        {"item_id": "A1", "axis": "A", "arm": "none", "model": "m", "trial": 0,
         "raw_answer": "ANSWER: 42", "cluster_id": "w1"},
        {"item_id": "A1", "axis": "A", "arm": "none", "model": "m", "trial": 1,
         "raw_answer": "ANSWER: wrong", "cluster_id": "w1"},
        {"item_id": "E1", "axis": "E", "arm": "none", "model": "m", "trial": 0,
         "raw_answer": "ANSWER: UNKNOWN", "cluster_id": "w1"},
    ]
    graded = RUNNER._default_grade_rows(items, rows)
    by_trial = {(r["item_id"], r["trial"]): r for r in graded}
    assert by_trial[("A1", 0)]["verdict"] == "correct"
    assert by_trial[("A1", 0)]["score"] == 1.0
    assert by_trial[("A1", 1)]["verdict"] == "wrong"
    assert by_trial[("E1", 0)]["verdict"] == "abstain_correct"

    summary = RUNNER._default_summarize(graded, bars={"A": 0.5}, k=1)
    assert summary["A/none/m"]["mean"] == 0.25  # (1.0 + -0.5) / 2
    assert summary["A/none/m"]["n_rows"] == 2
    assert summary["A/none/m"]["pass_k"] is False  # 0.25 < bar 0.5
    assert summary["E/none/m"]["mean"] == 1.0
