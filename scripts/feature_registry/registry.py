#!/usr/bin/env python3
"""Dual-axis feature registry — B-1 of the Unified Mechanization plan.

    python3 -m scripts.feature_registry.registry generate   # write var/feature-registry/registry.json
    python3 -m scripts.feature_registry.registry check      # 0 clean / 1 drift / 3 could-not-run
    python3 -m scripts.feature_registry.registry summary    # per-feature grid + unattributed counts

TWO AXES, TWO OWNERS
--------------------
The *curated* axis is human-edited: stable ids, rename aliases, criticality,
blast radius, expected behaviour, and the selectors that say which generated
items belong to a feature. It lives in ``configs/feature-registry.yaml``.

It is a SIDECAR on purpose. ``configs/feature-health.yaml`` is listed in the
plan's DO-NOT-REBUILD register (section 2) as an owner-governed surface that may
be extended only through its own channel, so this lane reads it and never writes
it. The overlay is keyed by the feature-health feature name, and the two files
must agree exactly on that key set — a key on either side with no partner is a
hard error, because a feature that quietly exists on one axis only is precisely
the favourable-absence shape this registry is meant to expose.

The *derived* axis is generated from inputs nobody here owns either: ARCHI.json
(routes, launchd jobs, migrations), ``configs/livesim-registry.yaml``, the
migrations directory, and the durable mechanism registry. Everything generated
is either attributed to a curated feature or listed in ``unattributed`` — there
is no third, silent place for it to go.

DENOMINATORS ARE PUBLISHED, NEVER BLENDED
-----------------------------------------
Each axis publishes a count PER SOURCE. Where sources disagree (ARCHI declares
26 launchd jobs, the mechanism registry holds 58 rows) the artifact carries both
numbers plus a written reconciliation, and no averaged or otherwise blended
figure exists anywhere in it. A disagreeing axis with no reconciliation note is
a refusal, not a warning: "could not reconcile" must never render as reconciled.
An unreadable source records ``null`` and lands in ``unavailable`` — never 0,
which would read as a truthful measurement of nothing.

EXIT CODES (check)
------------------
    0   the artifact matches what its inputs would generate now
    1   drift — regenerate and commit
    3   could not run (no artifact yet, unreadable input) — never "clean"
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "feature-registry.v1"
CURATED_SCHEMA = "feature-registry-curated.v1"
GENERATOR_VERSION = "1.0.0"

ARTIFACT_REL = "var/feature-registry/registry.json"
CURATED_REL = "configs/feature-registry.yaml"
FEATURE_HEALTH_REL = "configs/feature-health.yaml"
LIVESIM_REL = "configs/livesim-registry.yaml"
ARCHI_REL = "ARCHI.json"
MIGRATIONS_REL = "omniagentos/db/migrations"
UI_PAGES_REL = "dashboard/src/app"

DEFAULT_MECHANISM_REGISTRY = (
    Path(os.environ.get("OMNIAGENTOS_MECHANISM_REGISTRY")
         or (Path.home() / "Work" / "Ops" / "mechanism-registry" / "registry.jsonl"))
)

AXES = ("routes", "launchd", "migrations", "livesim", "ui")

#: Standing explanations for the source disagreements this estate actually has.
#: A disagreeing axis missing an entry here refuses (see ``_denominator``).
RECONCILIATIONS: dict[str, str] = {
    "routes": (
        "ARCHI.json's route list and its stamp.route_count are produced by the "
        "same archdocs pass; a gap between them means the stamp is stale — "
        "re-run archdocs rather than trusting either number."
    ),
    "launchd": (
        "ARCHI.json counts the jobs this repo defines; the durable mechanism "
        "registry counts every mechanism the estate has ever registered, "
        "including retired ones and jobs owned by other repos. The gap is "
        "expected and is NOT averaged: repo-defined jobs are the denominator "
        "for this repo's coverage, the registry count is the estate's."
    ),
    "migrations": (
        "ARCHI.json's migrations array is stamped at the last archdocs run "
        "while the migrations directory is current, so the directory count "
        "leads after every new migration. The directory is authoritative for "
        "'what exists'; ARCHI is authoritative for 'what was last documented'."
    ),
    "ui": (
        "The dashboard page count is derived from the Next.js app directory; a "
        "gap against any other UI inventory means one of them is stale. The "
        "app directory is authoritative for 'what ships'."
    ),
    "livesim": (
        "configs/livesim-registry.yaml carries both a generated counts.tests "
        "field and the test list itself; a gap means the registry was "
        "hand-edited — regenerate with scripts/livesim/gen_registry.py."
    ),
}


class RegistryError(RuntimeError):
    """A refusal. Nothing is written when one of these is raised."""


# --------------------------------------------------------------------------
# input loading — every reader is read-only by construction
# --------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RegistryError(f"missing input: {path}") from None
    except json.JSONDecodeError as exc:
        raise RegistryError(f"unreadable input {path}: {exc}") from None


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RegistryError(f"missing input: {path}") from None
    except yaml.YAMLError as exc:
        raise RegistryError(f"unreadable input {path}: {exc}") from None


def load_curated(path: Path) -> dict[str, dict[str, Any]]:
    doc = _load_yaml(path)
    if not isinstance(doc, dict) or doc.get("schema") != CURATED_SCHEMA:
        raise RegistryError(
            f"{path} must declare schema: {CURATED_SCHEMA} (got "
            f"{None if not isinstance(doc, dict) else doc.get('schema')!r})")
    features = doc.get("features")
    if not isinstance(features, dict) or not features:
        raise RegistryError(f"{path}: features must be a non-empty mapping")
    return features


# --------------------------------------------------------------------------
# curated-axis validation
# --------------------------------------------------------------------------

_REQUIRED_CURATED = ("stable_id", "criticality", "blast_radius", "expected_behavior")
_CRITICALITY = frozenset({"low", "medium", "high", "critical"})
_BLAST_RADIUS = frozenset({"local", "subsystem", "estate"})


def _validate_curated(curated: dict[str, dict[str, Any]],
                      health_keys: set[str]) -> None:
    unknown = sorted(set(curated) - health_keys)
    if unknown:
        raise RegistryError(
            "curated overlay names feature(s) that do not exist in "
            f"{FEATURE_HEALTH_REL}: {', '.join(unknown)}. The overlay is keyed by "
            "the feature-health key; add the feature through the owner's channel "
            "first, or drop the overlay entry.")
    missing = sorted(health_keys - set(curated))
    if missing:
        raise RegistryError(
            "feature(s) present in "
            f"{FEATURE_HEALTH_REL} but absent from the curated overlay: "
            f"{', '.join(missing)}. Every feature carries curated metadata or "
            "the registry is silently partial.")

    seen_names: dict[str, str] = {}
    for name in curated:
        seen_names[name] = name
    for name, entry in curated.items():
        if not isinstance(entry, dict):
            raise RegistryError(f"curated feature {name}: entry is not a mapping")
        for field in _REQUIRED_CURATED:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(
                    f"curated feature {name}: {field} must be a non-empty string")
        if entry["criticality"] not in _CRITICALITY:
            raise RegistryError(
                f"curated feature {name}: criticality must be one of "
                f"{sorted(_CRITICALITY)}")
        if entry["blast_radius"] not in _BLAST_RADIUS:
            raise RegistryError(
                f"curated feature {name}: blast_radius must be one of "
                f"{sorted(_BLAST_RADIUS)}")

    # stable ids and aliases share one namespace with the feature keys, so a
    # rename can never resolve to two different features.
    namespace: dict[str, str] = {}
    for name, entry in curated.items():
        for token, kind in [(name, "feature key"), (entry["stable_id"], "stable_id")]:
            owner = namespace.get(token)
            if owner not in (None, name):
                raise RegistryError(
                    f"{kind} {token!r} on feature {name} collides with feature "
                    f"{owner}")
            namespace[token] = name
    for name, entry in curated.items():
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list) or any(
                not isinstance(a, str) or not a.strip() for a in aliases):
            raise RegistryError(
                f"curated feature {name}: aliases must be a list of non-empty strings")
        for alias in aliases:
            owner = namespace.get(alias)
            if owner not in (None, name):
                raise RegistryError(
                    f"alias {alias!r} on feature {name} collides with feature "
                    f"{owner} — one name must resolve to one feature")
            namespace[alias] = name


# --------------------------------------------------------------------------
# derived-axis attribution
# --------------------------------------------------------------------------

_KNOWN_SELECTORS = frozenset({
    "route_areas", "route_prefixes", "launchd_labels", "migration_patterns",
    "livesim_categories", "ui_paths",
})


def _selectors(entry: dict[str, Any], axis_key: str) -> list[str]:
    selectors = entry.get("selectors") or {}
    if not isinstance(selectors, dict):
        raise RegistryError("selectors must be a mapping")
    unknown = sorted(set(selectors) - _KNOWN_SELECTORS)
    if unknown:
        raise RegistryError(
            f"unknown selector key(s) {', '.join(unknown)} — a misspelled "
            "selector matches nothing and reads as 'this feature owns no such "
            f"items'. Known keys: {', '.join(sorted(_KNOWN_SELECTORS))}")
    values = selectors.get(axis_key) or []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise RegistryError(f"selectors.{axis_key} must be a list of strings")
    for value in values:
        if axis_key in ("route_prefixes", "ui_paths") and not value.strip("/ "):
            # "/" is the empty selector wearing a slash: it prefixes every
            # absolute path, so it empties the UNATTRIBUTED bucket exactly the
            # same way. Same refusal, same reason.
            raise RegistryError(
                f"selectors.{axis_key} contains a root catch-all ({value!r}) — "
                "it matches every path and would silently empty the "
                "UNATTRIBUTED bucket. Name the sub-paths this feature owns.")
        if not value.strip():
            # An empty prefix matches EVERY path at specificity 0, so one blank
            # string quietly hoovers the whole unattributed bucket into one
            # feature and makes section 13-B's "UNATTRIBUTED empty" bar look
            # met. Refuse it on every axis, not just the one where it bites.
            raise RegistryError(
                f"selectors.{axis_key} contains an empty selector — an empty "
                "selector matches everything and would silently empty the "
                "UNATTRIBUTED bucket. Remove the entry, or drop the key to say "
                "'this feature owns no items of this kind'.")
    return values


def _match(item: str, selector: str, *, prefix: bool) -> bool:
    if prefix:
        return _prefix_match(item, selector)
    return fnmatch.fnmatchcase(item, selector)


def _prefix_match(path: str, selector: str) -> bool:
    """Prefix match on SEGMENT boundaries: /agent must not claim /agents."""
    if path == selector:
        return True
    selector = selector.rstrip("/")
    return path.startswith(selector + "/")


def _attribute(items: list[str], curated: dict[str, dict[str, Any]],
               specs: list[tuple[str, bool]]) -> tuple[dict[str, list[str]],
                                                       list[str],
                                                       list[dict[str, Any]]]:
    """Award each item to at most one feature.

    The winner is the feature whose matching selector is the most SPECIFIC
    (longest); alphabetical feature name breaks a tie. Neither rule depends on
    the order features happen to appear in the YAML, so a re-sorted overlay can
    never change the attribution. Every multi-claim is recorded in ``contested``
    rather than resolved silently.
    """
    per_feature: dict[str, list[str]] = {name: [] for name in curated}
    unattributed: list[str] = []
    contested: list[dict[str, Any]] = []

    for item in items:
        claims: list[tuple[int, str]] = []
        for name, entry in curated.items():
            best = -1
            for axis_key, prefix in specs:
                for selector in _selectors(entry, axis_key):
                    if _match(item, selector, prefix=prefix):
                        best = max(best, len(selector))
            if best >= 0:
                claims.append((best, name))
        if not claims:
            unattributed.append(item)
            continue
        winner = sorted(claims, key=lambda c: (-c[0], c[1]))[0][1]
        per_feature[winner].append(item)
        if len(claims) > 1:
            contested.append({
                "item": item,
                "claimed_by": sorted(name for _, name in claims),
                "awarded_to": winner,
                "rule": "most-specific selector, alphabetical tiebreak",
            })

    return ({k: sorted(v) for k, v in per_feature.items()},
            sorted(unattributed), contested)


# --------------------------------------------------------------------------
# denominators
# --------------------------------------------------------------------------

def _denominator(axis: str, sources: dict[str, int | None],
                 reconciliations: dict[str, str]) -> dict[str, Any]:
    known = [v for v in sources.values() if v is not None]
    unavailable = sorted(k for k, v in sources.items() if v is None)
    complete = not unavailable
    # An axis whose sources did not all report has not been reconciled — it has
    # been half-observed. `agree` is therefore NULL rather than True when a
    # declared source is missing: "the one source that answered did not
    # contradict itself" must never render as "the sources agree".
    agree = (len(set(known)) <= 1) if complete else None
    entry: dict[str, Any] = {
        "sources": sources,
        "agree": agree,
        "complete": complete,
        "unavailable": unavailable,
        "vacuous": any(v == 0 for v in known) or not known,
    }
    if agree is False:
        note = (reconciliations or {}).get(axis)
        if not (isinstance(note, str) and note.strip()):
            raise RegistryError(
                f"denominator {axis}: sources disagree ({sources}) and no "
                "reconciliation note is available. Sources are reconciled in "
                "writing or the artifact is not written — an unexplained gap "
                "must never render as agreement.")
        entry["reconciliation"] = note
    return entry


def non_vacuity_problems(doc: dict[str, Any]) -> list[str]:
    """Axes whose denominators cannot support a trustworthy metric.

    Two distinct defects, both fatal to a coverage claim and neither allowed to
    hide behind the other: a denominator measured as 0, and a denominator that
    is INCOMPLETE because a declared source never reported. Round-2 review found
    that fixing "absent reads as 0" had quietly made the second case pass.
    """
    problems = []
    for axis, entry in sorted(doc.get("denominators", {}).items()):
        if entry.get("vacuous"):
            problems.append(
                f"denominator {axis} is vacuous (sources {entry['sources']}) — "
                "a metric over it proves nothing")
        elif not entry.get("complete", True):
            problems.append(
                f"denominator {axis} is incomplete: source(s) "
                f"{', '.join(entry.get('unavailable') or [])} did not report, so "
                "the axis is half-observed, not reconciled")
    return problems


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _dir_sha256(path: Path) -> str:
    """A directory's identity is its sorted file list — a new migration moves it."""
    names = sorted(p.name for p in path.iterdir() if p.is_file())
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def _display_path(path: Path, repo: Path) -> str:
    """Repo-relative where possible, else home-relative — never a host path.

    The artifact is compared across checkouts and machines; an absolute
    /Users/<someone>/... string makes two identical registries look different.
    """
    try:
        return path.resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        pass
    try:
        return "~/" + path.resolve().relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def _input_record(name: str, path: Path, count: int | None, repo: Path) -> dict[str, Any]:
    present = path.exists()
    if present and path.is_file():
        digest: str | None = _sha256(path)
    elif present and path.is_dir():
        digest = _dir_sha256(path)
    else:
        digest = None
    return {
        "name": name,
        "path": _display_path(path, repo),
        "present": present,
        "sha256": digest,
        "count": count,
    }


def _mechanism_rows(path: Path) -> int | None:
    """Count PARSED JSONL objects, or None when the file is absent or corrupt.

    Counting non-blank lines would turn a corrupt registry into a confident
    number — two lines of garbage would read as two mechanisms and the standing
    reconciliation note would then explain a gap that is really a parse failure.
    """
    if not path.is_file():
        return None
    rows = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        # '#' comments are part of the format — the registry keeps its schema
        # note in the file (scripts/health-sentinel/mechanism_registry.py:78-79).
        if not text or text.startswith("#"):
            continue
        try:
            entry = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(entry, dict):
            return None
        rows += 1
    return rows


def _dir_count(path: Path, pattern: str) -> int | None:
    """None when the directory is absent — an absent source is not a zero one."""
    if not path.is_dir():
        return None
    return len(list(path.glob(pattern)))


def build_registry(repo: Path, *, now: str | None = None,
                   git_head: str | None = None,
                   mechanism_registry: Path | None = None,
                   reconciliations: dict[str, str] | None = None,
                   strict: bool = False) -> dict[str, Any]:
    repo = Path(repo)
    now = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_head = git_head if git_head is not None else _git_head(repo)
    mech_path = Path(mechanism_registry) if mechanism_registry is not None \
        else DEFAULT_MECHANISM_REGISTRY
    notes = RECONCILIATIONS if reconciliations is None else reconciliations

    archi = _load_json(repo / ARCHI_REL)
    health = _load_yaml(repo / FEATURE_HEALTH_REL)
    livesim = _load_yaml(repo / LIVESIM_REL)
    curated = load_curated(repo / CURATED_REL)

    health_features = (health or {}).get("features") or {}
    if not isinstance(health_features, dict):
        raise RegistryError(f"{FEATURE_HEALTH_REL}: features must be a mapping")
    _validate_curated(curated, set(health_features))

    # ROUTE NORMALIZATION (plan section 6 B-1: "route-normalization join key").
    # ARCHI records router-RELATIVE paths, so method+path is NOT unique: 17 of
    # the 313 entries collide across routers ('GET ' alone is 20 different
    # routers' roots). Keying on method+path silently merged those and lost the
    # losing router's routes from the registry entirely. The join key is
    # therefore (area, method, path), rendered "<METHOD> <path> [<area>]".
    route_rows = [(r[0], r[1], (r[2] if len(r) > 2 else "")) for r in archi.get("routes") or []]
    routes = [_route_key(m, p, a) for m, p, a in route_rows]
    route_areas = {_route_key(m, p, a): a for m, p, a in route_rows}
    route_paths = {_route_key(m, p, a): p for m, p, a in route_rows}
    distinct_method_path = len({(m, p) for m, p, _ in route_rows})
    launchd = [j[0] for j in archi.get("launchd_jobs") or []]
    archi_migrations = [m[1] for m in archi.get("migrations") or []]
    migrations_dir = repo / MIGRATIONS_REL
    migration_files = sorted(p.name for p in migrations_dir.glob("*.sql")) \
        if migrations_dir.is_dir() else []
    migrations_dir_count = _dir_count(migrations_dir, "*.sql")
    ui_dir = repo / UI_PAGES_REL
    ui_pages = sorted(_ui_route(p, ui_dir) for p in ui_dir.rglob("page.tsx")) \
        if ui_dir.is_dir() else []
    ui_count = len(ui_pages) if ui_dir.is_dir() else None
    livesim_tests = [t.get("id", "") for t in (livesim or {}).get("tests") or []]
    livesim_categories = {t.get("id", ""): t.get("category", "")
                          for t in (livesim or {}).get("tests") or []}

    # routes match on the ARCHI area tag as well as the path prefix; the area is
    # the mechanically produced join key, the prefix is the human-legible one.
    routes_by_feature, routes_unattributed, routes_contested = _attribute_routes(
        routes, route_areas, route_paths, curated)
    launchd_by_feature, launchd_unattributed, launchd_contested = _attribute(
        launchd, curated, [("launchd_labels", False)])
    migrations_by_feature, migrations_unattributed, migrations_contested = _attribute(
        sorted(set(archi_migrations) | set(migration_files)), curated,
        [("migration_patterns", False)])
    livesim_by_feature, livesim_unattributed, livesim_contested = _attribute_livesim(
        livesim_tests, livesim_categories, curated)
    ui_by_feature, ui_unattributed, ui_contested = _attribute(
        ui_pages, curated, [("ui_paths", True)])

    features = []
    for name in sorted(curated):
        entry = curated[name]
        derived = {
            "routes": routes_by_feature[name],
            "launchd": launchd_by_feature[name],
            "migrations": migrations_by_feature[name],
            "livesim": livesim_by_feature[name],
            "ui": ui_by_feature[name],
        }
        tiers = health_features.get(name) or {}
        features.append({
            "feature_id": name,
            "stable_id": entry["stable_id"],
            "aliases": list(entry.get("aliases") or []),
            "criticality": entry["criticality"],
            "blast_radius": entry["blast_radius"],
            "expected_behavior": entry["expected_behavior"],
            "test_paths": {tier: list(tiers.get(tier) or [])
                           for tier in ("tier1", "tier2", "tier3")},
            "derived": derived,
            "counts": {axis: len(items) for axis, items in derived.items()},
        })

    denominators = {
        "routes": _denominator("routes", {
            "archi.routes": len(routes),
            "archi.stamp.route_count": (archi.get("stamp") or {}).get("route_count"),
        }, notes),
        "launchd": _denominator("launchd", {
            "archi.launchd_jobs": len(launchd),
            "mechanism_registry": _mechanism_rows(mech_path),
        }, notes),
        "migrations": _denominator("migrations", {
            "archi.migrations": len(archi_migrations),
            "migrations_dir": migrations_dir_count,
        }, notes),
        "livesim": _denominator("livesim", {
            "livesim.counts.tests": ((livesim or {}).get("counts") or {}).get("tests"),
            "livesim.tests": len(livesim_tests),
        }, notes),
        "ui": _denominator("ui", {"dashboard_pages": ui_count}, notes),
    }

    unattributed = {
        "routes": routes_unattributed,
        "launchd": launchd_unattributed,
        "migrations": migrations_unattributed,
        "livesim": livesim_unattributed,
        "ui": ui_unattributed,
    }
    contested = sorted(
        routes_contested + launchd_contested + migrations_contested
        + livesim_contested + ui_contested,
        key=lambda c: c["item"])

    doc = {
        "schema": SCHEMA,
        "stamp": {
            "generator_version": GENERATOR_VERSION,
            "generated_at": now,
            "git_head": git_head,
            "inputs": [
                _input_record("archi", repo / ARCHI_REL, len(routes), repo),
                _input_record("feature_health", repo / FEATURE_HEALTH_REL,
                              len(health_features), repo),
                _input_record("livesim", repo / LIVESIM_REL, len(livesim_tests), repo),
                _input_record("curated", repo / CURATED_REL, len(curated), repo),
                _input_record("migrations_dir", migrations_dir,
                              migrations_dir_count, repo),
                _input_record("ui_pages", ui_dir, ui_count, repo),
                _input_record("mechanism_registry", mech_path,
                              _mechanism_rows(mech_path), repo),
            ],
        },
        "features": features,
        "route_identity": {
            "join_key": "(area, method, path) rendered '<METHOD> <path> [<area>]'",
            "why": (
                "ARCHI records router-relative paths, so method+path is not "
                "unique across routers. Both counts are published; neither is "
                "blended into the other."
            ),
            "entries": len(routes),
            "distinct_method_path": distinct_method_path,
            "collisions": len(routes) - distinct_method_path,
        },
        "denominators": denominators,
        "unattributed": unattributed,
        "contested": contested,
        "counts": {
            "features": len(features),
            "attributed": {
                axis: sum(f["counts"][axis] for f in features) for axis in AXES
            },
            "unattributed": {axis: len(items) for axis, items in unattributed.items()},
            "contested": len(contested),
        },
    }

    if strict:
        problems = non_vacuity_problems(doc)
        if problems:
            raise RegistryError("; ".join(problems))
    return doc


def _ui_route(page: Path, root: Path) -> str:
    """A Next.js app-router page path: dashboard/src/app/goals/page.tsx -> /goals."""
    rel = page.parent.relative_to(root).as_posix()
    return "/" if rel == "." else "/" + rel


def _route_key(method: str, path: str, area: str) -> str:
    """The route join key: router area disambiguates a relative method+path."""
    return f"{method} {path or '/'} [{area}]"


def _attribute_routes(items: list[str], areas: dict[str, str],
                      paths: dict[str, str],
                      curated: dict[str, dict[str, Any]]):
    """Routes match on path prefix OR on the ARCHI area tag."""
    per_feature: dict[str, list[str]] = {name: [] for name in curated}
    unattributed: list[str] = []
    contested: list[dict[str, Any]] = []
    for item in dict.fromkeys(items):
        area = areas.get(item, "")
        path = paths.get(item, "")
        claims: list[tuple[int, str]] = []
        for name, entry in curated.items():
            best = -1
            for selector in _selectors(entry, "route_prefixes"):
                if _prefix_match(path, selector):
                    best = max(best, len(selector))
            for selector in _selectors(entry, "route_areas"):
                if area and fnmatch.fnmatchcase(area, selector):
                    best = max(best, len(selector))
            if best >= 0:
                claims.append((best, name))
        if not claims:
            unattributed.append(item)
            continue
        winner = sorted(claims, key=lambda c: (-c[0], c[1]))[0][1]
        per_feature[winner].append(item)
        if len(claims) > 1:
            contested.append({
                "item": item,
                "claimed_by": sorted(name for _, name in claims),
                "awarded_to": winner,
                "rule": "most-specific selector, alphabetical tiebreak",
            })
    return ({k: sorted(v) for k, v in per_feature.items()},
            sorted(unattributed), contested)


def _attribute_livesim(items: list[str], categories: dict[str, str],
                       curated: dict[str, dict[str, Any]]):
    per_feature: dict[str, list[str]] = {name: [] for name in curated}
    unattributed: list[str] = []
    contested: list[dict[str, Any]] = []
    for item in items:
        category = categories.get(item, "")
        claims: list[tuple[int, str]] = []
        for name, entry in curated.items():
            best = -1
            for selector in _selectors(entry, "livesim_categories"):
                if category and fnmatch.fnmatchcase(category, selector):
                    best = max(best, len(selector))
            if best >= 0:
                claims.append((best, name))
        if not claims:
            unattributed.append(item)
            continue
        winner = sorted(claims, key=lambda c: (-c[0], c[1]))[0][1]
        per_feature[winner].append(item)
        if len(claims) > 1:
            contested.append({
                "item": item,
                "claimed_by": sorted(name for _, name in claims),
                "awarded_to": winner,
                "rule": "most-specific selector, alphabetical tiebreak",
            })
    return ({k: sorted(v) for k, v in per_feature.items()},
            sorted(unattributed), contested)


# --------------------------------------------------------------------------
# lookup, comparison, drift
# --------------------------------------------------------------------------

def resolve_feature(doc: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Resolve a feature by current key, stable id, or a rename alias."""
    for feature in doc.get("features", []):
        if key == feature["feature_id"] or key == feature["stable_id"] \
                or key in (feature.get("aliases") or []):
            return feature
    return None


def comparable(doc: dict[str, Any]) -> dict[str, Any]:
    """The artifact minus the fields that change without an input changing.

    ``generated_at`` and ``git_head`` move on every run and every commit; input
    hashes and every derived value stay. Drift therefore means the INPUTS moved,
    which is the only thing worth failing on.
    """
    trimmed = copy.deepcopy(doc)
    stamp = trimmed.get("stamp")
    if isinstance(stamp, dict):
        stamp.pop("generated_at", None)
        stamp.pop("git_head", None)
    return trimmed


def _diff_reasons(current: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in sorted(set(current) | set(expected)):
        if current.get(key) == expected.get(key):
            continue
        if key in ("denominators", "unattributed", "counts") and isinstance(
                expected.get(key), dict) and isinstance(current.get(key), dict):
            for axis in sorted(set(current[key]) | set(expected[key])):
                if current[key].get(axis) != expected[key].get(axis):
                    reasons.append(f"drift in {key}.{axis}")
        elif key == "features":
            reasons.append("drift in features (attribution or curated metadata changed)")
        elif key == "stamp":
            reasons.append("drift in stamp.inputs (an input file changed on disk)")
        else:
            reasons.append(f"drift in {key}")
    return reasons


def check_drift(repo: Path, *, now: str | None = None, git_head: str | None = None,
                **kw: Any) -> tuple[int, list[str]]:
    repo = Path(repo)
    artifact = repo / ARTIFACT_REL
    if not artifact.is_file():
        return 3, [f"no artifact at {ARTIFACT_REL} — run `generate` first; "
                   "a missing artifact is not a clean one"]
    try:
        on_disk = json.loads(artifact.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return 3, [f"{ARTIFACT_REL} is not readable JSON: {exc}"]
    try:
        fresh = build_registry(repo, now=now, git_head=git_head, **kw)
    except RegistryError as exc:
        return 3, [str(exc)]
    reasons = _diff_reasons(comparable(on_disk), comparable(fresh))
    return (1, reasons) if reasons else (0, [])


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def write_registry(repo: Path, doc: dict[str, Any]) -> Path:
    path = Path(repo) / ARTIFACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, path)
    return path


def _git_head(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _summary(doc: dict[str, Any]) -> str:
    lines = [f"feature-registry {doc['stamp']['generated_at']} "
             f"(generator {doc['stamp']['generator_version']}, "
             f"git {doc['stamp']['git_head']})", ""]
    lines.append(f"{'feature':<18}{'crit':<10}" + "".join(
        f"{axis:>12}" for axis in AXES))
    for feature in doc["features"]:
        counts = feature["counts"]
        lines.append(f"{feature['feature_id']:<18}{feature['criticality']:<10}"
                     + "".join(f"{counts[axis]:>12}" for axis in AXES))
    lines.append("")
    for axis in AXES:
        entry = doc["denominators"][axis]
        state = ("agree" if entry["agree"]
                 else ("DISAGREE" if entry["agree"] is False else "INCOMPLETE"))
        lines.append(f"{axis:<12}{state:<10}{entry['sources']}")
        if entry.get("reconciliation"):
            lines.append(f"{'':<12}reconciliation: {entry['reconciliation']}")
        if entry["unavailable"]:
            lines.append(f"{'':<12}unavailable: {', '.join(entry['unavailable'])}")
    lines.append("")
    lines.append(f"unattributed: {doc['counts']['unattributed']}")
    lines.append(f"contested:    {doc['counts']['contested']}")
    problems = non_vacuity_problems(doc)
    for problem in problems:
        lines.append(f"VACUOUS: {problem}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("generate", "check", "summary"))
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--strict", action="store_true",
                        help="refuse to write when any denominator is vacuous")
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    if args.command == "check":
        code, reasons = check_drift(repo)
        for reason in reasons:
            print(reason, file=sys.stderr)
        if code == 0:
            print("registry matches its inputs")
        return code

    try:
        doc = build_registry(repo, strict=args.strict)
    except RegistryError as exc:
        print(f"REFUSE[3] {exc}", file=sys.stderr)
        return 3

    if args.command == "generate":
        path = write_registry(repo, doc)
        print(f"wrote {path} ({doc['counts']['features']} features, "
              f"unattributed {doc['counts']['unattributed']})")
        return 0

    print(_summary(doc))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
