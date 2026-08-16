"""Contract tests for `scripts/testlanes/duration_store.py`.

This module shipped untested -- which the impact analysis itself flagged, since no test file
referenced it and therefore no lane could ever have covered a change to it. The properties
below are the ones its own docstring promises, and the one the repo's standing doctrine
requires (a rate over an empty set is undefined, never 1.0).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.testlanes.duration_store import (
    EWMA_ALPHA,
    duration_of,
    ingest_junit,
    known_fraction,
    load_store,
    lpt_plan,
    save_store,
    update,
)


def _junit(path: Path, cases: dict[str, float]) -> Path:
    suite = ET.Element("testsuite", {"name": "stub"})
    for node_id, seconds in cases.items():
        classname, _, name = node_id.rpartition("::")
        ET.SubElement(
            suite, "testcase", {"classname": classname, "name": name, "time": str(seconds)}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
    return path


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------
def test_first_sample_is_taken_verbatim(tmp_path: Path) -> None:
    store: dict = {}
    n = ingest_junit(store, _junit(tmp_path / "a.xml", {"tests/x.py::test_a": 2.0}))
    assert n == 1
    assert store["tests/x.py::test_a"] == {"ewma": 2.0, "last": 2.0, "n": 1}


def test_second_sample_is_folded_as_an_ewma_not_overwritten(tmp_path: Path) -> None:
    store: dict = {}
    ingest_junit(store, _junit(tmp_path / "a.xml", {"tests/x.py::test_a": 2.0}))
    ingest_junit(store, _junit(tmp_path / "b.xml", {"tests/x.py::test_a": 12.0}))
    entry = store["tests/x.py::test_a"]
    assert entry["last"] == 12.0
    assert entry["n"] == 2
    assert entry["ewma"] == pytest.approx(EWMA_ALPHA * 12.0 + (1 - EWMA_ALPHA) * 2.0)
    assert 2.0 < entry["ewma"] < 12.0, "an EWMA must sit between the samples it averages"


def test_a_missing_or_corrupt_report_ingests_nothing_rather_than_raising(tmp_path: Path) -> None:
    store: dict = {}
    assert ingest_junit(store, tmp_path / "absent.xml") == 0
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuite><testc", encoding="utf-8")
    assert ingest_junit(store, broken) == 0
    assert store == {}


def test_update_round_trips_through_disk(tmp_path: Path) -> None:
    store_path = tmp_path / "store.json"
    update([_junit(tmp_path / "a.xml", {"tests/x.py::test_a": 1.5})], store_path)
    assert load_store(store_path)["tests/x.py::test_a"]["last"] == 1.5


def test_a_corrupt_store_degrades_to_empty_instead_of_breaking_the_lane(tmp_path: Path) -> None:
    store_path = tmp_path / "store.json"
    store_path.write_text("{not json", encoding="utf-8")
    assert load_store(store_path) == {}


# --------------------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------------------
def test_lpt_balances_shards_better_than_round_robin(tmp_path: Path) -> None:
    store = {f"t{i}": {"ewma": float(d), "last": float(d), "n": 3} for i, d in
             enumerate([10, 9, 8, 7, 6, 5, 4, 3])}
    nodes = list(store)
    shards = lpt_plan(nodes, 2, store)
    assert sorted(n for shard in shards for n in shard) == sorted(nodes)
    totals = [sum(duration_of(store, n, 0.0) for n in shard) for shard in shards]
    assert max(totals) - min(totals) <= 1.0, f"LPT left shards unbalanced: {totals}"


def test_an_unknown_test_is_costed_at_the_median_never_at_zero() -> None:
    """'Unmeasured cost is not zero cost' -- treating an unknown test as free is what makes
    a shard plan silently lopsided."""
    store = {"known-a": {"ewma": 10.0}, "known-b": {"ewma": 10.0}, "known-c": {"ewma": 10.0}}
    assert duration_of(store, "never-seen", 7.5) == 7.5

    nodes = ["known-a", "known-b", "known-c", "unknown-1", "unknown-2", "unknown-3"]
    shards = lpt_plan(nodes, 3, store)
    # if unknowns were costed at 0, LPT would pile all three of them onto one shard.
    assert all(len(shard) == 2 for shard in shards), shards


def test_plan_never_loses_or_duplicates_a_test() -> None:
    nodes = [f"t{i}" for i in range(17)]
    shards = lpt_plan(nodes, 4, {})
    flat = [n for shard in shards for n in shard]
    assert sorted(flat) == sorted(nodes)
    assert len(flat) == len(set(flat))


def test_zero_shards_is_refused() -> None:
    with pytest.raises(ValueError):
        lpt_plan(["a"], 0, {})


# --------------------------------------------------------------------------------------
# the empty-set doctrine
# --------------------------------------------------------------------------------------
def test_known_fraction_over_an_empty_set_is_none_not_one() -> None:
    """Standing operator doctrine: a rate over an empty denominator is undefined. A run that
    planned nothing must not report "100% measured"."""
    assert known_fraction([], {}) is None
    assert known_fraction([], {"a": {"ewma": 1.0}}) is None


def test_known_fraction_counts_only_tests_with_history() -> None:
    store = {"a": {"ewma": 1.0}, "b": {"ewma": 2.0}}
    assert known_fraction(["a", "b"], store) == 1.0
    assert known_fraction(["a", "x"], store) == 0.5
    assert known_fraction(["x", "y"], store) == 0.0


def test_save_store_creates_its_parent_directory(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "deeper" / "store.json"
    save_store({"a": {"ewma": 1.0, "last": 1.0, "n": 1}}, dest)
    assert load_store(dest)["a"]["ewma"] == 1.0
