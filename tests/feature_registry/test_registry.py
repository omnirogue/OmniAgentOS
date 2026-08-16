"""B-1 dual-axis feature registry — behaviour tests.

Every test runs against a SYNTHETIC repo root built in tmp_path, never the real
estate, so the assertions stay stable when ARCHI.json or the feature-health
matrix change. The real-tree smoke test at the bottom is the only one that reads
the live inputs, and it asserts shape, not counts.

Plan authority: Unified Mechanization + Feature-Verification Plan 2026-08-08 r5
FINAL, section 6 B-1 and section 13-B acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.feature_registry import registry as reg

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# synthetic fixture
# --------------------------------------------------------------------------

ARCHI = {
    "schema_version": 1,
    "routes": [
        ["GET", "/agents", "access"],
        ["GET", "/agents/{agent_id}", "access"],
        ["POST", "/goals", "goals"],
        ["GET", "/orphan", "misc"],
        # same method+path as the access route above, different router: ARCHI
        # paths are router-relative, so this really is a second route.
        ["GET", "/agents", "misc"],
    ],
    "launchd_jobs": [
        ["com.omniagentos.api", "keep-alive"],
        ["com.omniagentos.goals-tick", "every 300s"],
    ],
    "migrations": [[1, "001_init.sql"], [2, "002_goals.sql"]],
    "stamp": {"generated_at": "2026-08-08T10:53:21Z", "git_head": "deadbeef",
              "max_migration": 2, "route_count": 5},
}

FEATURE_HEALTH = {
    "schema": "feature-health-matrix.v1",
    "features": {
        "goals": {"tier1": ["tests/goals"], "tier2": [], "tier3": []},
        "access": {"tier1": ["tests/access"], "tier2": [], "tier3": []},
    },
}

LIVESIM = {
    "schema": "livesim-registry.v1",
    "counts": {"tests": 2},
    "tests": [
        {"id": "api_endpoints::test_a", "nodeid": "tests/livesim/x.py::test_a",
         "category": "api_endpoints"},
        {"id": "memory::test_b", "nodeid": "tests/livesim/y.py::test_b",
         "category": "memory"},
    ],
}

CURATED = {
    "schema": "feature-registry-curated.v1",
    "features": {
        "goals": {
            "stable_id": "feat.goals",
            "aliases": ["objectives"],
            "criticality": "high",
            "blast_radius": "estate",
            "expected_behavior": "Goal CRUD and tick collectors stay live.",
            "selectors": {
                "route_prefixes": ["/goals"],
                "launchd_labels": ["com.omniagentos.goals-*"],
                "migration_patterns": ["*goals*"],
                "livesim_categories": ["memory"],
                "ui_paths": ["/goals"],
            },
        },
        "access": {
            "stable_id": "feat.access",
            "aliases": [],
            "criticality": "medium",
            "blast_radius": "subsystem",
            "expected_behavior": "Agent access routes stay live.",
            "selectors": {
                "route_prefixes": ["/agents"],
                "launchd_labels": ["com.omniagentos.api"],
                "migration_patterns": ["*init*"],
                "livesim_categories": ["api_endpoints"],
                "ui_paths": ["/agents"],
            },
        },
    },
}


def _write(root: Path, rel: str, obj: object, *, as_yaml: bool = False) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if as_yaml:
        path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "estate"
    _write(root, "ARCHI.json", ARCHI)
    _write(root, "configs/feature-health.yaml", FEATURE_HEALTH, as_yaml=True)
    _write(root, "configs/livesim-registry.yaml", LIVESIM, as_yaml=True)
    _write(root, "configs/feature-registry.yaml", CURATED, as_yaml=True)
    migrations = root / "omniagentos/db/migrations"
    migrations.mkdir(parents=True)
    for name in ("001_init.sql", "002_goals.sql", "003_extra.sql"):
        (migrations / name).write_text("-- x\n", encoding="utf-8")
    for page in ("goals", "agents", "orphanui"):
        target = root / "dashboard/src/app" / page
        target.mkdir(parents=True)
        (target / "page.tsx").write_text("export default function P(){}\n",
                                         encoding="utf-8")
    return root


def build(repo: Path, **kw):
    kw.setdefault("now", "2026-08-08T23:00:00Z")
    kw.setdefault("git_head", "cafef00d")
    return reg.build_registry(repo, **kw)


# --------------------------------------------------------------------------
# curated axis: stable ids, aliases, owner-channel boundary
# --------------------------------------------------------------------------

def test_stable_ids_and_aliases_resolve(repo: Path) -> None:
    doc = build(repo)
    ids = {f["feature_id"]: f for f in doc["features"]}
    assert ids["goals"]["stable_id"] == "feat.goals"
    assert ids["goals"]["aliases"] == ["objectives"]
    # a renamed feature is still findable by its old name
    assert reg.resolve_feature(doc, "objectives")["feature_id"] == "goals"
    assert reg.resolve_feature(doc, "feat.goals")["feature_id"] == "goals"
    assert reg.resolve_feature(doc, "nope") is None


def test_alias_colliding_with_another_feature_is_a_hard_error(repo: Path) -> None:
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["goals"]["aliases"] = ["access"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="alias"):
        build(repo)


def test_curated_entry_for_unknown_feature_health_key_is_a_hard_error(repo: Path) -> None:
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["ghost"] = curated["features"]["goals"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="ghost"):
        build(repo)


def test_feature_health_key_missing_from_the_overlay_is_a_hard_error(repo: Path) -> None:
    curated = json.loads(json.dumps(CURATED))
    del curated["features"]["access"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="access"):
        build(repo)


def test_generation_never_writes_to_owner_governed_inputs(repo: Path) -> None:
    """Plan section 2: feature-health.yaml / ARCHI.json are owner-channel surfaces."""
    watched = ["ARCHI.json", "configs/feature-health.yaml", "configs/livesim-registry.yaml"]
    before = {p: (repo / p).read_bytes() for p in watched}
    stat_before = {p: (repo / p).stat().st_mtime_ns for p in watched}
    reg.write_registry(repo, build(repo))
    for path in watched:
        assert (repo / path).read_bytes() == before[path]
        assert (repo / path).stat().st_mtime_ns == stat_before[path]
    written = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    new = written - {*watched, "configs/feature-registry.yaml"}
    assert all(
        p.startswith("var/feature-registry/")
        or p.startswith("omniagentos/db/migrations/")
        or p.startswith("dashboard/")
        for p in new
    ), sorted(new)


# --------------------------------------------------------------------------
# derived axis: attribution and the UNATTRIBUTED bucket
# --------------------------------------------------------------------------

def test_derived_items_attribute_to_features(repo: Path) -> None:
    doc = build(repo)
    goals = reg.resolve_feature(doc, "goals")
    assert goals["derived"]["routes"] == ["POST /goals [goals]"]
    assert goals["derived"]["launchd"] == ["com.omniagentos.goals-tick"]
    assert goals["derived"]["migrations"] == ["002_goals.sql"]
    assert goals["derived"]["livesim"] == ["memory::test_b"]
    access = reg.resolve_feature(doc, "access")
    # prefix selectors are deliberately area-agnostic (an ARCHI path is
    # router-relative), so the misc router's /agents lands here too; the real
    # overlay prefers route_areas for exactly that reason.
    assert set(access["derived"]["routes"]) == {
        "GET /agents [access]", "GET /agents [misc]", "GET /agents/{agent_id} [access]"}


def test_routes_colliding_on_method_and_path_stay_distinct_items(repo: Path) -> None:
    """17 of the real tree's 313 routes share method+path across routers."""
    doc = build(repo)
    everything = [r for f in doc["features"] for r in f["derived"]["routes"]]
    everything += doc["unattributed"]["routes"]
    assert everything.count("GET /agents [access]") == 1
    assert everything.count("GET /agents [misc]") == 1
    identity = doc["route_identity"]
    assert identity["entries"] == 5
    assert identity["distinct_method_path"] == 4
    assert identity["collisions"] == 1


def test_unclaimed_items_land_in_the_unattributed_bucket(repo: Path) -> None:
    doc = build(repo)
    assert doc["unattributed"]["routes"] == ["GET /orphan [misc]"]
    # 003_extra.sql exists on disk, is claimed by no selector, and must be visible
    assert "003_extra.sql" in doc["unattributed"]["migrations"]
    assert doc["counts"]["unattributed"]["routes"] == 1


def test_an_item_claimed_by_two_features_is_recorded_as_contested_not_silently_dropped(
    repo: Path,
) -> None:
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["access"]["selectors"]["route_prefixes"].append("/goals")
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    doc = build(repo)
    contested = {c["item"]: c for c in doc["contested"]}
    assert "POST /goals [goals]" in contested
    assert contested["POST /goals [goals]"]["claimed_by"] == ["access", "goals"]
    # equally specific selectors ("/goals" both) -> alphabetical tiebreak, so the
    # award never depends on YAML ordering
    assert contested["POST /goals [goals]"]["awarded_to"] == "access"
    # first-match-wins, and the loser does not also carry it
    assert reg.resolve_feature(doc, "goals")["derived"]["routes"] == []


# --------------------------------------------------------------------------
# denominators: published per source, reconciled, never averaged
# --------------------------------------------------------------------------

def test_denominators_publish_every_source_separately(repo: Path) -> None:
    doc = build(repo)
    migrations = doc["denominators"]["migrations"]
    assert migrations["sources"] == {"archi.migrations": 2, "migrations_dir": 3}
    assert migrations["agree"] is False
    assert migrations["reconciliation"], "a disagreeing denominator must state why"
    routes = doc["denominators"]["routes"]
    assert routes["sources"] == {"archi.routes": 5, "archi.stamp.route_count": 5}
    assert routes["agree"] is True


def test_disagreeing_sources_are_never_averaged_or_blended(repo: Path) -> None:
    doc = build(repo)
    for axis in doc["denominators"].values():
        assert "average" not in axis and "value" not in axis, axis
        for count in axis["sources"].values():
            assert count is None or isinstance(count, int)


def test_unreadable_source_is_unavailable_not_zero(repo: Path) -> None:
    doc = build(repo, mechanism_registry=repo / "does-not-exist.jsonl")
    launchd = doc["denominators"]["launchd"]
    assert launchd["sources"]["mechanism_registry"] is None
    assert launchd["unavailable"] == ["mechanism_registry"]
    stamp = {i["name"]: i for i in doc["stamp"]["inputs"]}
    assert stamp["mechanism_registry"]["present"] is False
    assert stamp["mechanism_registry"]["sha256"] is None


def test_a_disagreeing_denominator_without_a_reconciliation_note_refuses(repo: Path) -> None:
    """Favourable-absence guard: 'could not reconcile' must not read as reconciled."""
    with pytest.raises(reg.RegistryError, match="reconciliation"):
        build(repo, reconciliations={})


def test_zero_denominator_fails_non_vacuity(repo: Path) -> None:
    """Plan section 13-B: metrics non-vacuous (denominators > 0 proven)."""
    archi = json.loads(json.dumps(ARCHI))
    archi["routes"] = []
    archi["stamp"]["route_count"] = 0
    _write(repo, "ARCHI.json", archi)
    doc = build(repo)
    assert doc["denominators"]["routes"]["vacuous"] is True
    problems = reg.non_vacuity_problems(doc)
    assert any("routes" in p for p in problems)
    with pytest.raises(reg.RegistryError, match="vacuous"):
        build(repo, strict=True)


# --------------------------------------------------------------------------
# stamping, determinism, drift
# --------------------------------------------------------------------------

def test_stamp_records_every_input_with_a_hash_and_a_count(repo: Path) -> None:
    doc = build(repo)
    stamp = doc["stamp"]
    assert stamp["generated_at"] == "2026-08-08T23:00:00Z"
    assert stamp["git_head"] == "cafef00d"
    assert stamp["generator_version"] == reg.GENERATOR_VERSION
    names = {i["name"] for i in stamp["inputs"]}
    assert {"archi", "feature_health", "livesim", "curated"} <= names
    for entry in stamp["inputs"]:
        if entry["present"]:
            assert len(entry["sha256"]) == 64


def test_generation_is_deterministic_apart_from_the_timestamp(repo: Path) -> None:
    first = build(repo, now="2026-08-08T23:00:00Z")
    second = build(repo, now="2026-08-08T23:59:59Z")
    assert first != second
    assert reg.comparable(first) == reg.comparable(second)


def test_check_is_clean_after_generate_and_reports_drift_after_an_input_changes(
    repo: Path,
) -> None:
    reg.write_registry(repo, build(repo))
    code, reasons = reg.check_drift(repo, now="2026-08-09T00:00:00Z", git_head="cafef00d")
    assert (code, reasons) == (0, [])

    archi = json.loads(json.dumps(ARCHI))
    archi["routes"].append(["GET", "/goals/{goal_id}", "goals"])
    _write(repo, "ARCHI.json", archi)
    code, reasons = reg.check_drift(repo, now="2026-08-09T00:00:00Z", git_head="cafef00d")
    assert code == 1
    assert reasons and any("routes" in r or "drift" in r for r in reasons)


def test_check_without_a_generated_artifact_cannot_run_rather_than_passing(repo: Path) -> None:
    code, reasons = reg.check_drift(repo)
    assert code == 3
    assert reasons


def test_written_artifact_is_valid_json_with_the_declared_schema(repo: Path) -> None:
    path = reg.write_registry(repo, build(repo))
    assert path == repo / "var/feature-registry/registry.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == reg.SCHEMA


# --------------------------------------------------------------------------
# real-tree smoke: shape only, never counts
# --------------------------------------------------------------------------

def test_real_tree_generates_and_covers_every_feature_health_feature() -> None:
    doc = build(REPO_ROOT)
    health = yaml.safe_load((REPO_ROOT / "configs/feature-health.yaml").read_text())
    assert {f["feature_id"] for f in doc["features"]} == set(health["features"])
    assert doc["denominators"]["routes"]["sources"]["archi.routes"] > 0
    assert isinstance(doc["unattributed"]["routes"], list)


# --------------------------------------------------------------------------
# regressions from the grok-critic round-1 review
# --------------------------------------------------------------------------

@pytest.mark.parametrize("axis", ["route_prefixes", "route_areas", "launchd_labels",
                                  "migration_patterns", "livesim_categories",
                                  "ui_paths"])
def test_an_empty_selector_is_refused_on_every_axis(repo: Path, axis: str) -> None:
    """An empty selector matches everything and would fake an empty bucket."""
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["goals"]["selectors"][axis] = [""]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="empty selector|root catch-all"):
        build(repo)


def test_a_root_catch_all_prefix_is_refused(repo: Path) -> None:
    """"/" is the empty selector wearing a slash — it empties the bucket too."""
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["jira" if "jira" in curated["features"] else "goals"]
    curated["features"]["goals"]["selectors"]["ui_paths"] = ["/"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="root catch-all"):
        build(repo)


def test_a_misspelled_selector_key_is_refused(repo: Path) -> None:
    """A typo'd key matches nothing and reads as 'this feature owns none'."""
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["goals"]["selectors"]["route_prefix"] = ["/goals"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    with pytest.raises(reg.RegistryError, match="unknown selector"):
        build(repo)


def test_route_prefixes_match_on_segment_boundaries(repo: Path) -> None:
    """/agent must not claim /agents."""
    curated = json.loads(json.dumps(CURATED))
    curated["features"]["access"]["selectors"]["route_prefixes"] = ["/agent"]
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    doc = build(repo)
    assert reg.resolve_feature(doc, "access")["derived"]["routes"] == []
    assert "GET /agents [access]" in doc["unattributed"]["routes"]


def test_missing_migrations_directory_is_unavailable_not_zero(repo: Path) -> None:
    import shutil
    shutil.rmtree(repo / "omniagentos/db/migrations")
    doc = build(repo)
    migrations = doc["denominators"]["migrations"]
    assert migrations["sources"]["migrations_dir"] is None
    assert migrations["unavailable"] == ["migrations_dir"]
    assert migrations["vacuous"] is False
    # HALF-OBSERVED IS NOT RECONCILED (grok-critic round 2): one source that
    # did not contradict itself must never render as "the sources agree", and
    # --strict must refuse an axis a declared source never reported to.
    assert migrations["complete"] is False
    assert migrations["agree"] is None
    assert any("incomplete" in p for p in reg.non_vacuity_problems(doc))
    with pytest.raises(reg.RegistryError, match="incomplete"):
        build(repo, strict=True)
    stamp = {i["name"]: i for i in doc["stamp"]["inputs"]}
    assert stamp["migrations_dir"]["present"] is False
    assert stamp["migrations_dir"]["count"] is None


def test_corrupt_mechanism_registry_is_unavailable_not_a_line_count(repo: Path) -> None:
    bad = repo / "mech.jsonl"
    bad.write_text("not-json\n\nnot-json-2\n", encoding="utf-8")
    doc = build(repo, mechanism_registry=bad)
    launchd = doc["denominators"]["launchd"]
    assert launchd["sources"]["mechanism_registry"] is None
    assert "mechanism_registry" in launchd["unavailable"]


def test_stamp_paths_are_never_host_absolute(repo: Path) -> None:
    doc = build(repo, mechanism_registry=Path.home() / "Work/Ops/mechanism-registry/registry.jsonl")
    for entry in doc["stamp"]["inputs"]:
        assert not entry["path"].startswith("/"), entry
        assert "/Users/" not in entry["path"], entry


def test_livesim_items_land_in_the_unattributed_bucket_too(repo: Path) -> None:
    curated = json.loads(json.dumps(CURATED))
    for name in curated["features"]:
        curated["features"][name]["selectors"]["livesim_categories"] = []
    _write(repo, "configs/feature-registry.yaml", curated, as_yaml=True)
    doc = build(repo)
    assert doc["unattributed"]["livesim"] == ["api_endpoints::test_a", "memory::test_b"]
    assert doc["counts"]["unattributed"]["livesim"] == 2


def test_ui_pages_are_a_derived_axis_with_their_own_bucket(repo: Path) -> None:
    doc = build(repo)
    assert reg.resolve_feature(doc, "goals")["derived"]["ui"] == ["/goals"]
    assert reg.resolve_feature(doc, "access")["derived"]["ui"] == ["/agents"]
    assert doc["unattributed"]["ui"] == ["/orphanui"]
    assert doc["denominators"]["ui"]["sources"]["dashboard_pages"] == 3


def test_missing_dashboard_directory_is_unavailable_not_zero(repo: Path) -> None:
    import shutil
    shutil.rmtree(repo / "dashboard")
    doc = build(repo)
    ui = doc["denominators"]["ui"]
    assert ui["sources"]["dashboard_pages"] is None
    assert ui["unavailable"] == ["dashboard_pages"]
    # its only source is unavailable, so the axis cannot support a metric at
    # all — flagged vacuous AND unavailable, which is different from, and never
    # confusable with, a truthfully measured zero.
    assert ui["vacuous"] is True


def test_mechanism_registry_comment_lines_are_not_counted_as_mechanisms(repo: Path) -> None:
    """The registry keeps its schema note in the file; '#' lines are format."""
    mech = repo / "mech.jsonl"
    mech.write_text(
        "# schema note\n"
        '{"id": "a"}\n'
        "\n"
        '{"id": "b"}\n', encoding="utf-8")
    doc = build(repo, mechanism_registry=mech)
    assert doc["denominators"]["launchd"]["sources"]["mechanism_registry"] == 2
