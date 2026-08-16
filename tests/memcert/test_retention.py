"""MEM-J retention carrier tests (deterministic, DESIGN-v2.md §5).

Drives ``scripts/memcert/retention.py`` with two synthetic runs and asserts
the paired-regression detector's contract: a real per-item drop on one axis is
flagged, a stable run is not, an empty pairing is VOID (surfaced, never green
coverage), and the CLI exit codes carry the verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.memcert import retention


def _row(
    item_id: str, axis: str, score: float, trial: int = 0, arm: str = "system",
    model: str = "m1", cluster: str = "world-w42",
) -> dict:
    return {
        "item_id": item_id,
        "axis": axis,
        "arm": arm,
        "model": model,
        "trial": trial,
        "score": score,
        "cluster_id": cluster,
    }


def _run(scores_by_axis: dict[str, list[float]], **kw: str) -> list[dict]:
    rows = []
    for axis, scores in scores_by_axis.items():
        for i, s in enumerate(scores):
            for trial in (0, 1, 2):
                rows.append(_row(f"MEM-{axis}1-{i:02d}-w42", axis, s, trial=trial, **kw))
    return rows


BASE = {"A": [1.0] * 8, "B": [1.0, 1.0, 1.0, 1.0, -0.5, 1.0, 1.0, 1.0]}


def test_stable_run_reports_no_regression() -> None:
    prev = _run(BASE)
    curr = _run(BASE)
    report = retention.retention_report(prev, curr, arm="system", model="m1")
    assert report["regressed_axes"] == []
    assert report["overall"]["n_pairs"] == 48  # 16 items x 3 trials
    assert report["overall"]["delta"] == 0.0


def test_axis_drop_is_flagged_and_localized() -> None:
    prev = _run(BASE)
    dropped = dict(BASE)
    dropped["B"] = [-0.5] * 8  # every B item now wrong; A untouched
    curr = _run(dropped)
    report = retention.retention_report(prev, curr, arm="system", model="m1")
    assert report["regressed_axes"] == ["B"]
    b = report["per_axis"]["B"]
    assert b["delta"] < 0 and b["ci_hi"] is not None and b["ci_hi"] < 0
    a = report["per_axis"]["A"]
    assert a["delta"] == 0.0


def test_stable_slice_cannot_mask_a_regressed_one() -> None:
    # codex-critic CR-001: with two arms in one run, unqualified (item_id,
    # trial) pairing collapses rows last-wins and a stable system_legacy slice
    # could hide a real system regression. Slice-qualified pairing must flag it.
    prev = _run(BASE, arm="system") + _run(BASE, arm="system_legacy")
    curr = _run({**BASE, "B": [-0.5] * 8}, arm="system") + _run(BASE, arm="system_legacy")
    report = retention.retention_report(prev, curr)  # no slice filter: the default path
    assert "B" in report["regressed_axes"]
    # And the reversed row order gives the identical verdict (order-independence).
    report_rev = retention.retention_report(list(reversed(prev)), list(reversed(curr)))
    assert report_rev["regressed_axes"] == report["regressed_axes"]
    assert report_rev["overall"]["delta"] == report["overall"]["delta"]


def test_missing_slice_is_void_not_green(tmp_path: Path) -> None:
    # codex-critic CR-001-R2: a slice measured in the previous run but ABSENT
    # from the current one must exit VOID (2) even though other slices pair.
    prev = _run(BASE, arm="system") + _run(BASE, arm="system_legacy")
    curr = _run(BASE, arm="system_legacy")  # the system slice vanished
    report = retention.retention_report(prev, curr)
    assert report["missing_slices"] == ["system/m1"]
    assert report["overall"]["n_pairs"] > 0  # legacy still pairs — not enough

    prev_dir, curr_dir = tmp_path / "prev", tmp_path / "curr"
    for d, data in ((prev_dir, prev), (curr_dir, curr)):
        d.mkdir()
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")
    assert retention.main(["--prev", str(prev_dir), "--curr", str(curr_dir)]) == 2


def test_pair_key_parity_catches_every_grain_of_lost_coverage(tmp_path: Path) -> None:
    # The structural closure (codex-critic CR-009-R4 + CR-010): coverage loss
    # at ANY grain — a per-slice axis cell, or a single item/trial — voids the
    # comparison, because parity is checked on the finest grain (the pair key)
    # and every coarser grain is subsumed.
    # CR-009-R4 shape: system/B vanishes while system_legacy/B survives.
    prev = _run(BASE, arm="system") + _run(BASE, arm="system_legacy")
    curr = _run({"A": BASE["A"]}, arm="system") + _run(BASE, arm="system_legacy")
    report = retention.retention_report(prev, curr)
    assert report["lost_pairs"]["count"] == 24  # 8 B items x 3 trials
    prev_dir, curr_dir = tmp_path / "p1", tmp_path / "c1"
    for d, data in ((prev_dir, prev), (curr_dir, curr)):
        d.mkdir()
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")
    assert retention.main(["--prev", str(prev_dir), "--curr", str(curr_dir)]) == 2

    # CR-010 shape: ONE item's trials vanish inside a fully-surviving axis.
    prev2 = _run(BASE)
    lost_item = "MEM-A1-03-w42"
    curr2 = [r for r in prev2 if r["item_id"] != lost_item]
    report2 = retention.retention_report(prev2, curr2, arm="system", model="m1")
    assert report2["lost_pairs"]["count"] == 3  # the item's 3 trials
    assert report2["void_axes"] == [] and report2["missing_slices"] == []
    p2, c2 = tmp_path / "p2", tmp_path / "c2"
    for d, data in ((p2, prev2), (c2, curr2)):
        d.mkdir()
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")
    assert retention.main(["--prev", str(p2), "--curr", str(c2)]) == 2


def test_vanished_axis_within_surviving_slice_is_void(tmp_path: Path) -> None:
    # codex-critic CR-009: axis B vanishes from the current run while axis A
    # (same slice) stays stable — slice parity can't see it; axis parity must.
    prev = _run(BASE)
    curr = _run({"A": BASE["A"]})  # every axis-B row gone
    report = retention.retention_report(prev, curr, arm="system", model="m1")
    assert report["void_axes"] == ["B"]
    assert report["missing_slices"] == []
    assert report["regressed_axes"] == []

    prev_dir, curr_dir = tmp_path / "prev", tmp_path / "curr"
    for d, data in ((prev_dir, prev), (curr_dir, curr)):
        d.mkdir()
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")
    assert retention.main(["--prev", str(prev_dir), "--curr", str(curr_dir)]) == 2


def test_empty_pairing_is_void_not_green() -> None:
    prev = _run(BASE, arm="system")
    curr = _run(BASE, arm="system_legacy")  # disjoint slice -> zero pairs
    report = retention.retention_report(prev, curr, arm="system", model="m1")
    assert report["overall"]["n_pairs"] == 0
    assert report["regressed_axes"] == []  # no evidence, no alarm — but VOID


def test_duplicate_identities_nan_scores_and_moved_payloads_are_void(
    tmp_path: Path,
) -> None:
    # codex-critic CR-010-R5: (a) duplicate measurement identities make
    # pairing order-dependent — refuse; (b) a NaN score is corruption, not a
    # zero delta — refuse; (c) a key silently moving axis or cluster changes
    # what the statistics mean — parity on the full identity catches it.
    prev = _run(BASE)
    dup = prev + [prev[0]]
    report = retention.retention_report(dup, prev, arm="system", model="m1")
    assert report["duplicate_keys"] == 1

    nan_curr = [dict(r) for r in prev]
    nan_curr[0]["score"] = float("nan")
    report2 = retention.retention_report(prev, nan_curr, arm="system", model="m1")
    assert report2["invalid_scores"] == 1

    moved = [dict(r) for r in prev]
    moved[0]["cluster_id"] = "world-w99"  # bootstrap grouping silently remapped
    report3 = retention.retention_report(prev, moved, arm="system", model="m1")
    assert report3["lost_pairs"]["count"] == 1

    for name, curr in (("dup", None), ("nan", nan_curr), ("moved", moved)):
        p, c = tmp_path / f"p-{name}", tmp_path / f"c-{name}"
        p.mkdir()
        c.mkdir()
        prev_data = dup if name == "dup" else prev
        curr_data = prev if name == "dup" else curr
        (p / "results.jsonl").write_text("\n".join(json.dumps(r) for r in prev_data) + "\n")
        (c / "results.jsonl").write_text("\n".join(json.dumps(r) for r in curr_data) + "\n")
        assert retention.main(["--prev", str(p), "--curr", str(c)]) == 2, name


def test_load_rows_skips_junk_lines(tmp_path: Path) -> None:
    p = tmp_path / "results.jsonl"
    rows = _run(BASE)
    lines = [json.dumps(rows[0]), "not-json", json.dumps({"no": "score"}), json.dumps(rows[1])]
    p.write_text("\n".join(lines) + "\n")
    assert len(retention.load_rows(p)) == 2
    assert len(retention.load_rows(tmp_path)) == 2  # dir form finds results.jsonl


def test_cli_exit_codes(tmp_path: Path) -> None:
    prev_dir = tmp_path / "prev"
    curr_ok = tmp_path / "curr_ok"
    curr_bad = tmp_path / "curr_bad"
    for d, data in (
        (prev_dir, _run(BASE)),
        (curr_ok, _run(BASE)),
        (curr_bad, _run({**BASE, "B": [-0.5] * 8})),
    ):
        d.mkdir()
        (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")

    assert retention.main(["--prev", str(prev_dir), "--curr", str(curr_ok)]) == 0
    out = tmp_path / "report.json"
    rc = retention.main(
        ["--prev", str(prev_dir), "--curr", str(curr_bad), "--out", str(out)]
    )
    assert rc == 1
    assert json.loads(out.read_text())["regressed_axes"] == ["B"]
    assert retention.main(["--prev", str(tmp_path / "absent"), "--curr", str(curr_ok)]) == 2
    # VOID (zero matched pairs) is exit 2, never green — a comparison that
    # compared nothing must not certify anything (gemini-critic F5).
    rc_void = retention.main(
        ["--prev", str(prev_dir), "--curr", str(curr_ok), "--arm", "no-such-arm"]
    )
    assert rc_void == 2
