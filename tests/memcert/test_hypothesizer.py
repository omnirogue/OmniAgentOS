"""Decisive tests for scripts/memcert/hypothesizer.py (DESIGN §8).

Hermetic: no network, tmp_path only, deterministic. The module is loaded via
importlib from its file path WITH sys.modules registration (py3.12 dataclass
trap — same _load idiom as tests/memcert/test_core.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hyp_mod = _load("memcert_hypothesizer_test", REPO_ROOT / "scripts" / "memcert" / "hypothesizer.py")


# ---------------------------------------------------------------------------
# fixture-construction helpers
# ---------------------------------------------------------------------------


def make_cell(axis: str, arm: str, mean: float, model: str = "m1", n_items: int = 8) -> dict:
    return {
        "axis": axis,
        "arm": arm,
        "model": model,
        "mean": mean,
        "ci_lo": mean,
        "ci_hi": mean,
        "n_rows": n_items * 2,
        "n_items": n_items,
        "n_trials": 2,
        "pass_k": None,
        "verdicts": {},
    }


def make_rows(cells: list[dict], n: int = 4, n_errors: int = 0) -> list[dict]:
    rows = []
    for cell in cells:
        for i in range(n):
            rows.append(
                {
                    "item_id": f"{cell['axis']}-{i:02d}",
                    "axis": cell["axis"],
                    "arm": cell["arm"],
                    "model": cell["model"],
                    "trial": 0,
                    "score": cell["mean"] + (0.1 if i % 2 else -0.1),
                    "error": None,
                    "cluster_id": f"w{i % 2}",
                }
            )
    for j in range(n_errors):
        rows.append(
            {
                "item_id": f"E-{j:02d}",
                "axis": "A",
                "arm": "transcript",
                "model": "m1",
                "trial": 0,
                "score": None,
                "error": "boom: adapter exploded",
                "cluster_id": "w0",
            }
        )
    return rows


def write_run(run_dir: Path, cells: list[dict], rows: list[dict], run_uuid: str = "uuid-1") -> Path:
    axes = sorted({c["axis"] for c in cells})
    summary = {
        "run_id": run_dir.name,
        "manifest": {
            "axes": axes,
            "arms": sorted({c["arm"] for c in cells}),
            "models": sorted({c["model"] for c in cells}),
            "seeds": [5],
            "trials": 2,
            "split": "dev",
            "adapter": "mock",
            "budget_tokens": 24000,
            "run_uuid": run_uuid,
        },
        "axes": {f"{c['axis']}/{c['arm']}/{c['model']}": c for c in cells},
        "row_count": len(rows),
        "error_count": sum(1 for r in rows if r.get("error")),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary))
    (run_dir / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return run_dir


def write_ab_dir(
    run_dir: Path, arm: str, axis_scores: dict[str, float], model: str = "m1", n_items: int = 8
) -> Path:
    rows = []
    for axis, score in axis_scores.items():
        for i in range(n_items):
            rows.append(
                {
                    "item_id": f"{axis}-{i:02d}",
                    "axis": axis,
                    "arm": arm,
                    "model": model,
                    "trial": 0,
                    "score": score,
                    "error": None,
                    "cluster_id": f"w{i % 4}",
                }
            )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (run_dir / "summary.json").write_text(json.dumps({"run_id": run_dir.name}))
    return run_dir


def standard_cells() -> list[dict]:
    # Weakest arm-of-interest cell is A/transcript (0.2); the floor arm `none`
    # is lower (-0.3) but is a control and must never be picked.
    return [
        make_cell("A", "none", -0.3),
        make_cell("A", "transcript", 0.2),
        make_cell("B", "none", -0.1),
        make_cell("B", "transcript", 0.8),
    ]


def run_hypothesizer(latest: Path, tmp_path: Path, **kwargs):
    kwargs.setdefault("state_dir", tmp_path / "state")
    kwargs.setdefault("loopqueue_root", tmp_path / "loopqueue")
    kwargs.setdefault("env", {})
    # These fixtures use small synthetic A/B sets; production keeps the 20-pair
    # floor (MC-005). The floor itself is covered by test_ab_evidence_*.
    kwargs.setdefault("min_pairs", 1)
    return hyp_mod.run(latest_run=latest, **kwargs)


def state_records(state_dir: Path) -> dict[str, dict]:
    out = {}
    if state_dir.is_dir():
        for p in state_dir.glob("*.json"):
            if p.name != "last_processed.json":
                out[p.stem] = json.loads(p.read_text())
    return out


# ---------------------------------------------------------------------------
# (1) weakest-axis pick is deterministic and skips control arms
# ---------------------------------------------------------------------------


def test_pick_weakest_cell_deterministic_and_skips_controls() -> None:
    cells = {f"{c['axis']}/{c['arm']}/{c['model']}": c for c in standard_cells()}
    picked = hyp_mod.pick_weakest_cell(cells)
    assert picked is not None
    assert (picked["axis"], picked["arm"]) == ("A", "transcript")  # not the lower `none` floor

    # tie on mean -> resolved by sorted (axis, arm, model), independent of dict order
    tie = {
        "B/rag/m1": make_cell("B", "rag", 0.2),
        "A/transcript/m1": make_cell("A", "transcript", 0.2),
    }
    for permutation in (tie, dict(reversed(list(tie.items())))):
        picked = hyp_mod.pick_weakest_cell(permutation)
        assert (picked["axis"], picked["arm"]) == ("A", "transcript")


# ---------------------------------------------------------------------------
# (2) pre-registration file is written before any test run
# ---------------------------------------------------------------------------


def test_preregistration_written_with_expected_fields(tmp_path: Path, capsys) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    result = run_hypothesizer(latest, tmp_path)  # no A/B dirs: registration only

    assert result.exit_code == 0
    assert result.action == "registered"
    records = state_records(tmp_path / "state")
    assert result.hypothesis_id in records
    rec = records[result.hypothesis_id]
    assert rec["state"] == "proposed"
    assert rec["kind"] == "perf"
    assert rec["axis"] == "A"
    assert rec["arm_control"] == "transcript"
    assert rec["expected_direction"] == "+"
    assert rec["registered_at_run"] == "uuid-1"
    # first playbook entry: raise budget_tokens (24000 -> 48000)
    assert rec["param_change"] == {"budget_tokens": 48000}
    assert "mde_hint" in rec and rec["mde_hint"] is not None
    # the exact A/B run_bench commands are pre-registered and emitted
    for name in ("control", "candidate"):
        assert any("run_bench.py" in part for part in rec["commands"][name])
    assert "--budget-tokens" in rec["commands"]["candidate"]
    emitted = capsys.readouterr().out
    assert "run_bench.py" in emitted


# ---------------------------------------------------------------------------
# (3) confirmation requires a significant delta in the registered direction
# ---------------------------------------------------------------------------


def test_confirmed_on_significant_delta_in_registered_direction(tmp_path: Path) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": 0.5})

    result = run_hypothesizer(latest, tmp_path, control_run=ctrl, candidate_run=cand)

    assert result.exit_code == 0
    assert result.action == "confirmed"
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["state"] == "confirmed"
    assert rec["evaluation"]["confirmed"] is True
    assert rec["evaluation"]["target"]["delta"] == 1.5
    assert rec["evaluation"]["target"]["significant"] is True
    # transitions recorded: proposed -> testing -> confirmed
    states = [t["to"] for t in rec["transitions"]]
    assert states == ["testing", "confirmed"]
    # default (unarmed) filing = outbox envelope with measured evidence
    outbox = tmp_path / "state" / "outbox" / f"{result.hypothesis_id}.json"
    assert outbox.exists()
    env = json.loads(outbox.read_text())
    assert env["kind"] == "proposal"
    assert env["paths"], "proposal must carry non-empty paths"
    assert env["payload"]["falsifier"] == "revert change, delta disappears"
    assert env["payload"]["measured"]["delta"] == 1.5
    assert env["payload"]["measured"]["before_mean"] == -0.5
    assert env["payload"]["measured"]["after_mean"] == 1.0
    assert env["payload"]["measured"]["p"] is not None
    assert env["payload"]["receipts"]
    assert env["id"].startswith("sha256:")


def test_wrong_direction_delta_is_disproved(tmp_path: Path) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    # significant delta but NEGATIVE: candidate is worse on the target axis
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": 1.0, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": -0.5, "B": 0.5})

    result = run_hypothesizer(latest, tmp_path, control_run=ctrl, candidate_run=cand)

    assert result.exit_code == 0
    assert result.action == "disproved"
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["state"] == "disproved"
    assert not (tmp_path / "state" / "outbox").exists()


# ---------------------------------------------------------------------------
# (4) a significant regression on a non-target axis blocks confirmation
# ---------------------------------------------------------------------------


def test_non_target_axis_regression_blocks_confirmation(tmp_path: Path) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 1.0})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": -1.0})

    result = run_hypothesizer(latest, tmp_path, control_run=ctrl, candidate_run=cand)

    assert result.exit_code == 0
    assert result.action == "disproved"
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["state"] == "disproved"
    evaluation = rec["evaluation"]
    assert evaluation["target"]["significant"] is True  # the win itself was real...
    assert evaluation["regressions"] and evaluation["regressions"][0]["axis"] == "B"
    assert evaluation["confirmed"] is False  # ...but the regression vetoes it
    assert not (tmp_path / "state" / "outbox").exists()


# ---------------------------------------------------------------------------
# (5) two-key arming: file_proposal subprocess NEVER runs on one key alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "env"),
    [(True, {}), (False, {"MEMCERT_LIVE": "1"}), (False, {})],
    ids=["flag-only", "env-only", "neither"],
)
def test_two_key_arming_never_spawns_subprocess_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool, env: dict
) -> None:
    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("subprocess.run must NEVER be invoked without both keys")

    monkeypatch.setattr(hyp_mod.subprocess, "run", _forbidden)
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": 0.5})

    result = run_hypothesizer(
        latest, tmp_path, control_run=ctrl, candidate_run=cand, live=live, env=env
    )

    assert result.exit_code == 0
    assert result.action == "confirmed"
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["filing"] == "outbox"
    assert (tmp_path / "state" / "outbox" / f"{result.hypothesis_id}.json").exists()


def test_two_key_arming_invokes_file_proposal_when_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _recorder(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hyp_mod.subprocess, "run", _recorder)
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": 0.5})

    result = run_hypothesizer(
        latest,
        tmp_path,
        control_run=ctrl,
        candidate_run=cand,
        live=True,
        env={"MEMCERT_LIVE": "1"},
    )

    assert result.exit_code == 0
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["filing"] == "filed_live"
    assert len(calls) == 1
    assert any("file_proposal.py" in part for part in calls[0])
    # armed filing goes through the sanctioned writer, not a direct outbox write
    assert not (tmp_path / "state" / "outbox" / f"{result.hypothesis_id}.json").exists()


# ---------------------------------------------------------------------------
# (6) backpressure: >max-pending queued proposals skips filing
# ---------------------------------------------------------------------------


def test_backpressure_skips_filing_above_max_pending(tmp_path: Path) -> None:
    proposals = tmp_path / "loopqueue" / "proposals"
    proposals.mkdir(parents=True)
    for i in range(16):  # 16 > default max-pending of 15
        (proposals / f"fake-{i:02d}.json").write_text("{}")
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": 0.5})

    result = run_hypothesizer(latest, tmp_path, control_run=ctrl, candidate_run=cand)

    assert result.exit_code == 0
    assert result.action == "confirmed"  # the science happened; only filing was skipped
    rec = state_records(tmp_path / "state")[result.hypothesis_id]
    assert rec["filing"] == "skipped_backpressure"
    assert not (tmp_path / "state" / "outbox").exists()


def test_dedup_skips_filing_when_id_already_outboxed(tmp_path: Path) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    ctrl = write_ab_dir(tmp_path / "ab-ctrl", "transcript", {"A": -0.5, "B": 0.5})
    cand = write_ab_dir(tmp_path / "ab-cand", "transcript", {"A": 1.0, "B": 0.5})
    # pre-plant the outbox file for the id the playbook will generate
    cells = {f"{c['axis']}/{c['arm']}/{c['model']}": c for c in standard_cells()}
    cell = hyp_mod.pick_weakest_cell(cells)
    manifest = json.loads((latest / "summary.json").read_text())["manifest"]
    hyp_id = hyp_mod.playbook_candidates(cell, manifest)[0]["id"]
    outbox = tmp_path / "state" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / f"{hyp_id}.json").write_text("{}")

    result = run_hypothesizer(latest, tmp_path, control_run=ctrl, candidate_run=cand)

    assert result.exit_code == 0
    assert result.hypothesis_id == hyp_id
    rec = state_records(tmp_path / "state")[hyp_id]
    assert rec["filing"] == "skipped_dedup"
    assert (outbox / f"{hyp_id}.json").read_text() == "{}"  # never overwritten


# ---------------------------------------------------------------------------
# (7) unchanged input: a second call on the same run exits 2
# ---------------------------------------------------------------------------


def test_second_call_with_same_run_is_unchanged_input_exit_2(tmp_path: Path) -> None:
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    first = run_hypothesizer(latest, tmp_path)
    assert first.exit_code == 0

    second = run_hypothesizer(latest, tmp_path)
    assert second.exit_code == 2
    assert second.action == "unchanged_input"

    # a DIFFERENT run (new run_uuid) is new input and processes normally
    other = write_run(
        tmp_path / "run2", standard_cells(), make_rows(standard_cells()), run_uuid="uuid-2"
    )
    third = run_hypothesizer(other, tmp_path)
    assert third.exit_code == 0


def test_exec_resumes_pending_hypothesis_instead_of_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--exec on an already-processed run tests the PENDING hypothesis (changed
    action, not an unchanged-input retry) — the daily cadence's second leg."""
    latest = write_run(tmp_path / "run1", standard_cells(), make_rows(standard_cells()))
    first = run_hypothesizer(latest, tmp_path)
    assert first.exit_code == 0 and first.action == "registered"

    def _fake_run_bench(argv, **kwargs):  # noqa: ANN001, ANN003
        out = Path(argv[argv.index("--out") + 1])
        arm = argv[argv.index("--arms") + 1]
        score = 1.0 if out.name == "candidate" else -0.5
        write_ab_dir(out, arm, {"A": score, "B": 0.5})
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hyp_mod.subprocess, "run", _fake_run_bench)
    second = run_hypothesizer(latest, tmp_path, exec_ab=True)

    assert second.exit_code == 0
    assert second.action == "confirmed"
    assert second.hypothesis_id == first.hypothesis_id  # resumed, not re-registered
    rec = state_records(tmp_path / "state")[first.hypothesis_id]
    assert rec["state"] == "confirmed"
    assert rec["ab_exit_codes"] == {"control": 0, "candidate": 0}


# ---------------------------------------------------------------------------
# (8) instrument-health guard: error rows > 20% -> exit 70, no hypothesis
# ---------------------------------------------------------------------------


def test_error_rows_over_20_percent_exit_70(tmp_path: Path) -> None:
    cells = standard_cells()
    rows = make_rows(cells, n=4, n_errors=7)  # 7 of 23 rows errored (~30%)
    latest = write_run(tmp_path / "run1", cells, rows)

    result = run_hypothesizer(latest, tmp_path)

    assert result.exit_code == 70
    assert result.action == "instrument_failure"
    assert state_records(tmp_path / "state") == {}  # never hypothesize on instrument failure

    # a rerun of the SAME (broken) run is NOT exit 2: 70 never marks processed
    again = run_hypothesizer(latest, tmp_path)
    assert again.exit_code == 70


# ---------------------------------------------------------------------------
# (9) saturation -> suite-improvement hypothesis emitted
# ---------------------------------------------------------------------------


def test_saturated_axis_emits_suite_improvement_hypothesis(tmp_path: Path) -> None:
    cells = [
        make_cell("A", "transcript", 0.99),
        make_cell("A", "rag", 1.0),
        make_cell("B", "transcript", 0.99),
        make_cell("B", "rag", 0.985),
    ]
    latest = write_run(tmp_path / "run1", cells, make_rows(cells))

    result = run_hypothesizer(latest, tmp_path)

    assert result.exit_code == 0
    assert result.action == "nothing_to_do"  # every axis saturated: no perf cell left
    suites = [r for r in state_records(tmp_path / "state").values() if r.get("kind") == "suite"]
    assert sorted(r["axis"] for r in suites) == ["A", "B"]
    assert all(r["state"] == "proposed" for r in suites)
    assert all("saturated" in r["note"] for r in suites)
    assert all(r["filing"] == "outbox" for r in suites)
    outbox_files = sorted((tmp_path / "state" / "outbox").glob("*.json"))
    assert len(outbox_files) == 2
    env = json.loads(outbox_files[0].read_text())
    assert env["kind"] == "proposal"
    assert "saturated" in env["title"]
    assert env["paths"]


def test_mixed_saturation_still_registers_perf_hypothesis_on_weak_axis(tmp_path: Path) -> None:
    cells = [
        make_cell("A", "transcript", 0.99),
        make_cell("A", "rag", 1.0),
        make_cell("B", "transcript", 0.3),
        make_cell("B", "rag", 0.6),
    ]
    latest = write_run(tmp_path / "run1", cells, make_rows(cells))

    result = run_hypothesizer(latest, tmp_path)

    assert result.exit_code == 0
    assert result.action == "registered"
    records = state_records(tmp_path / "state")
    perf = records[result.hypothesis_id]
    assert perf["kind"] == "perf"
    assert perf["axis"] == "B"  # the saturated axis A is excluded from the weakest pick
    suites = [r for r in records.values() if r.get("kind") == "suite"]
    assert [r["axis"] for r in suites] == ["A"]
