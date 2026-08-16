"""Contract tests for `scripts/testlanes/impacted.py`, the test-dev/test-pr impact analysis.

Every assertion here corresponds to a defect that shipped in the first version of this
module and was caught in review:

* it mapped `omniagentos/<pkg>/**` to `tests/<pkg>/` and nothing else, so a change to
  `omniagentos/db/store.py` selected only `tests/db` even though 131 test files elsewhere
  import `SqliteStore`;
* it selected `tests/doctrine` and let the runner hand it to `pytest -n 8`, whose revert
  harness then raced itself across workers and left the checkout dirty;
* it reported changed files it could not explain as a note, not as a reason to distrust
  the lane.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from scripts.testlanes.impacted import (
    DEFERRED_TEST_PREFIXES,
    SERIAL_TEST_PREFIXES,
    ImpactGraph,
    _extract,
    _string_literals,
    build_index,
    impacted,
    index_version,
    module_name,
    partition,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def graph() -> ImpactGraph:
    # use_cache=False: the assertions below are about what the analysis computes from the
    # tree as it is now, not about whatever a previous run left in var/.
    return ImpactGraph(build_index(use_cache=False))


# --------------------------------------------------------------------------------------
# module-name / import resolution
# --------------------------------------------------------------------------------------
def test_module_name_maps_packages_and_modules() -> None:
    assert module_name("omniagentos/db/store.py") == "omniagentos.db.store"
    assert module_name("omniagentos/db/__init__.py") == "omniagentos.db"


def test_package_init_prefixes_are_edges_too(graph: ImpactGraph) -> None:
    """`import a.b.c` executes `a/__init__.py` and `a/b/__init__.py`. Resolving only the
    longest prefix left package __init__ files with zero dependents, which is how
    `scripts/testlanes/__init__.py` came out as an unexplainable change."""
    targets = graph._resolve_module("scripts.testlanes.impacted")
    assert "scripts/testlanes/impacted.py" in targets
    assert "scripts/testlanes/__init__.py" in targets
    assert "scripts/__init__.py" in targets


def test_docstrings_are_not_dependencies() -> None:
    """A file named in prose is not a file the module depends on. Without this exclusion one
    sentence in `omniagentos/api/eventbus.py`'s docstring ("see the Makefile") made a
    `Makefile` edit select 380 of 808 test files through eventbus's importers."""
    src = '"""Prose mentioning the Makefile and TESTING.md."""\nP = "configs/real.toml"\n'
    literals = _string_literals(ast.parse(src))
    assert "configs/real.toml" in literals
    assert not any("Makefile" in lit for lit in literals)

    _names, tokens = _extract("omniagentos/x.py", src)
    assert "configs/real.toml" in tokens
    assert "Makefile" not in tokens
    assert "TESTING.md" not in tokens


def test_dynamic_import_strings_become_edges() -> None:
    src = 'import importlib\nm = importlib.import_module("omniagentos.db.store")\n'
    names, _tokens = _extract("tests/x/test_y.py", src)
    assert "omniagentos.db.store" in names


def test_relative_imports_resolve_against_the_containing_package() -> None:
    src = "from . import store\nfrom ..context import capsule\n"
    names, _tokens = _extract("omniagentos/db/api.py", src)
    assert "omniagentos.db.store" in names
    assert "omniagentos.context.capsule" in names


# --------------------------------------------------------------------------------------
# the regression the reviewer named: cross-package reach
# --------------------------------------------------------------------------------------
def test_store_change_reaches_tests_outside_its_own_directory(graph: ImpactGraph) -> None:
    """The exact case a directory mirror cannot see: `omniagentos/db/store.py` is imported
    all over the suite, so its impact set must be far larger than `tests/db/`.

    Asserted on the END-TO-END result, not on the graph object: what matters is what the
    lane would actually select. The rejected version of this module selected `tests/db` and
    nothing else here, while 131 test files outside `tests/db` import `SqliteStore` directly
    and many more reach it transitively.
    """
    store = "omniagentos/db/store.py"
    assert store in graph.entries, "the index no longer covers omniagentos/db/store.py"

    result = impacted(changed_override=[store])
    selected = set(result["impacted"]) | set(result["serial"]) | set(result["deferred"])
    outside = {f for f in selected if not f.startswith("tests/db/")}
    assert len(outside) > 100, (
        "the impact analysis for omniagentos/db/store.py reached only "
        f"{len(outside)} test file(s) outside tests/db -- the directory-mirror regression "
        "is back and cross-package impact is being missed"
    )


def test_a_leaf_change_does_not_select_the_whole_suite(graph: ImpactGraph) -> None:
    """The other failure mode: an analysis that selects everything is not an analysis. A
    change to this very test file must select a small set."""
    result = impacted(changed_override=["tests/scripts/test_lane_impact_analysis.py"])
    assert result["selected_test_files"] < result["total_test_files"] // 4
    assert "tests/scripts/test_lane_impact_analysis.py" in result["impacted"]


# --------------------------------------------------------------------------------------
# xdist safety partition
# --------------------------------------------------------------------------------------
def test_doctrine_is_partitioned_into_the_serial_bucket() -> None:
    parallel, serial, deferred = partition(
        [
            "tests/db/test_store.py",
            "tests/doctrine/test_self.py",
            "tests/longhaul/test_api.py",
        ]
    )
    assert parallel == ["tests/db/test_store.py"]
    assert serial == ["tests/doctrine/test_self.py"]
    assert deferred == ["tests/longhaul/test_api.py"]


def test_selecting_doctrine_never_puts_it_in_the_parallel_bucket() -> None:
    """End-to-end through the real analysis: a change under tests/doctrine must come back
    as serial-only. Handing this harness to xdist mutates shared fixture files from several
    workers at once."""
    result = impacted(changed_override=["tests/doctrine/revert.py"])
    assert result["serial"], "tests/doctrine change produced no serial selection at all"
    assert all(n.startswith(SERIAL_TEST_PREFIXES) for n in result["serial"])
    assert not any(n.startswith(SERIAL_TEST_PREFIXES) for n in result["impacted"])


def test_quarantined_suites_are_reported_not_silently_dropped() -> None:
    result = impacted(changed_override=["tests/longhaul/test_api.py"])
    assert "tests/longhaul/test_api.py" in result["deferred"]
    assert not any(n.startswith(DEFERRED_TEST_PREFIXES) for n in result["impacted"])


# --------------------------------------------------------------------------------------
# honesty rules
# --------------------------------------------------------------------------------------
# These two paths are assembled at runtime on purpose. The analysis indexes string
# literals, so writing the path out in full here would make THIS file a referencing file
# and hand the "nothing references it" test an edge -- which is a nice demonstration that
# the data-reference edge works, and a useless test.
_UNREFERENCED_CONFIG = "configs/" + "zz-unreferenced-" + "fixture" + ".toml"
_UNREFERENCED_DOC = "docs/" + "zz-unreferenced-" + "note" + ".md"


def test_an_unexplainable_change_forces_a_full_run() -> None:
    result = impacted(changed_override=[_UNREFERENCED_CONFIG])
    assert result["unresolved"] == [_UNREFERENCED_CONFIG]
    assert result["full_required"] is True
    assert result["unresolved_input"] is True
    assert result["closure_too_broad"] is False
    assert result["full_reasons"], "full_required must always come with a stated reason"


def test_a_doc_no_test_reads_is_recorded_but_does_not_force_full() -> None:
    result = impacted(changed_override=[_UNREFERENCED_DOC])
    assert result["no_test_edge"] == [_UNREFERENCED_DOC]
    assert result["unresolved"] == []
    assert result["full_required"] is False


def test_a_doc_that_tests_actually_read_is_an_edge() -> None:
    """`tests/scripts/test_lane_marker_superset.py` reads the Makefile; a Makefile change
    must therefore select it. Data files are dependencies too."""
    result = impacted(changed_override=["Makefile"])
    assert "tests/scripts/test_lane_marker_superset.py" in result["impacted"]
    assert result["no_test_edge"] == []


def test_a_broad_change_escalates_instead_of_pretending() -> None:
    result = impacted(changed_override=["omniagentos/db/store.py"])
    if result["selected_fraction"] is not None and result["selected_fraction"] >= 0.60:
        assert result["full_required"] is True
        assert result["closure_too_broad"] is True
        assert any("impact closure selects" in r for r in result["full_reasons"])
    else:
        # still must be far wider than the old tests/db-only answer
        assert result["selected_test_files"] > 50


def test_the_two_escalation_triggers_are_reported_separately() -> None:
    """A lane has to be able to tell "my analysis has a hole" (a coverage problem) from
    "most of the suite is impacted" (a cost decision). test-dev continues loudly on the
    first and escalates on the second; collapsing them into one flag makes that impossible.
    """
    hole = impacted(changed_override=[_UNREFERENCED_CONFIG])
    assert (hole["unresolved_input"], hole["closure_too_broad"]) == (True, False)

    broad = impacted(changed_override=["omniagentos/db/store.py"])
    assert (broad["unresolved_input"], broad["closure_too_broad"]) == (False, True)

    narrow = impacted(changed_override=["omniagentos/scope/policy.py"])
    assert (narrow["unresolved_input"], narrow["closure_too_broad"]) == (False, False)
    assert narrow["full_required"] is False


def test_selected_fraction_over_an_empty_change_set_is_not_a_number() -> None:
    """An empty change set selects nothing. The fraction is a real 0/total here, but the
    lane must not claim `full_required` off an empty diff either."""
    result = impacted(changed_override=[])
    assert result["changed"] == []
    assert result["selected_test_files"] == 0
    assert result["full_required"] is False


def test_the_index_cache_is_invalidated_when_a_file_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache is keyed on (size, mtime_ns). Prove a stale entry cannot survive an edit,
    because a cache that silently serves old imports would produce false negatives -- the
    one answer an impact analysis may never give."""
    from scripts.testlanes import impacted as mod

    rel = "scripts/testlanes/impacted.py"
    target = ROOT / rel
    st = target.stat()
    fresh = build_index(use_cache=False)["entries"][rel]
    assert fresh["size"] == st.st_size
    assert fresh["mtime_ns"] == st.st_mtime_ns
    assert fresh["imports"], "the file really does import things"

    # A cache entry whose (size, mtime_ns) no longer match must be re-parsed, not served.
    poisoned = {rel: {"size": st.st_size, "mtime_ns": st.st_mtime_ns - 1, "imports": [],
                      "tokens": []}}
    monkeypatch.setattr(mod, "_load_cache", lambda: poisoned)
    monkeypatch.setattr(mod, "_save_cache", lambda entries: None)
    rebuilt = build_index(use_cache=True)
    assert rebuilt["entries"][rel]["imports"] == fresh["imports"], (
        "a stale cache entry was served instead of re-parsing the changed file"
    )

    # ... and a matching entry IS served (otherwise the cache buys nothing).
    good = {rel: {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                  "imports": ["sentinel.module"], "tokens": []}}
    monkeypatch.setattr(mod, "_load_cache", lambda: good)
    served = build_index(use_cache=True)
    assert served["entries"][rel]["imports"] == ["sentinel.module"]


def test_the_cache_is_keyed_on_the_extractor_source(tmp_path: Path) -> None:
    """A hand-maintained version integer is a footgun: this module's extractor was changed
    (docstrings excluded, package-prefix edges added) WITHOUT bumping the literal, so stale
    caches kept answering with the old edge set and two measurements of the same repo
    disagreed by 88 test files. The key is now derived from the extractor's own source.
    """
    from scripts.testlanes import impacted as mod

    version = index_version()
    assert len(version) == 16

    # a cache file stamped with any other version is discarded wholesale
    payload = {"version": "not-this-extractor", "entries": {"a.py": {"size": 1, "mtime_ns": 1}}}
    stale = tmp_path / "impact-index.json"
    stale.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch_path = mod._INDEX_PATH
    try:
        mod._INDEX_PATH = stale
        assert mod._load_cache() == {}
        payload["version"] = version
        stale.write_text(json.dumps(payload), encoding="utf-8")
        assert mod._load_cache() == payload["entries"]
    finally:
        mod._INDEX_PATH = monkeypatch_path


def test_the_extractor_version_changes_when_the_extractor_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of deriving the key: edit the extraction logic, get a new key."""
    from scripts.testlanes import impacted as mod

    before = index_version()
    monkeypatch.setattr(mod, "_PATHTOKEN_RE", re.compile(r"totally-different-pattern"))
    assert index_version() != before
