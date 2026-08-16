# Feature registry — the dual-axis feature inventory (B-1)

Plan authority: `~/.omniagentos/ops/Plans/Unified-Mechanization-Feature-Verification-Plan-2026-08-08.md`
section 6 (workstream B), section 13-B (acceptance), section 2 (owner channels).

The registry answers one question mechanically: **what features does this estate
have, and which generated things belong to each one?** Everything else on the
feature axis (B-2 liveness, B-3 priority, B-5 test-gap, B-7 failure-to-feature,
D-1 merge mapping) joins on the ids this file defines.

## The two axes

| Axis | Where | Who edits it |
|---|---|---|
| Curated | `configs/feature-registry.yaml` | humans: stable id, aliases, criticality, blast radius, expected behaviour, selectors |
| Derived | `var/feature-registry/registry.json` | `scripts/feature_registry/registry.py`, from ARCHI.json, the migrations directory, `configs/livesim-registry.yaml`, the dashboard app directory and the mechanism registry |

Derived axes: `routes`, `launchd`, `migrations`, `livesim`, `ui`.

`configs/feature-health.yaml` is the feature-name authority and is **owner
governed** (plan section 2). This lane reads it and never writes it, and the
curated overlay is keyed by the same feature names. If either file gains or
loses a feature the generator refuses until the other agrees — a feature that
exists on one axis only is the failure this registry exists to expose.

## Commands

```
.venv/bin/python -m scripts.feature_registry.registry summary    # per-feature grid + denominators
.venv/bin/python -m scripts.feature_registry.registry generate   # write the artifact
.venv/bin/python -m scripts.feature_registry.registry check      # 0 clean / 1 drift / 3 could-not-run
```

`check` regenerates in memory and compares against the artifact, ignoring
`stamp.generated_at` and `stamp.git_head` — both move without any input moving.
Everything else, including every input's sha256, is compared, so drift means the
INPUTS changed. A missing artifact is exit 3, never a pass.

The artifact lives under `var/`, which is gitignored: it is a runtime product
regenerated from committed inputs, exactly like the feature-health ledger.
Promoting it to a committed artifact would be a `.gitignore` change and is
deliberately out of this lane.

## Route normalization — the join key

ARCHI records **router-relative** paths, so method+path is not unique: 17 of the
313 route entries collide across routers (`GET ''` alone is 20 different
routers' roots). The join key is therefore `(area, method, path)`, rendered
`<METHOD> <path> [<area>]`. The artifact publishes `route_identity` with the
entry count, the distinct method+path count and the collision count, so the gap
is visible rather than resolved by silently dropping the losing router's routes.

## Attribution rules

Selectors live per feature in the curated overlay:

| Selector | Matches |
|---|---|
| `route_areas` | fnmatch over ARCHI's route area tag — the mechanically produced key, preferred |
| `route_prefixes` | literal prefix over the router-relative path; deliberately area-agnostic, so use it only where the path is unambiguous |
| `launchd_labels` | fnmatch over ARCHI launchd job labels |
| `migration_patterns` | fnmatch over migration filenames (ARCHI list ∪ migrations directory) |
| `livesim_categories` | fnmatch over LiveSim registry categories |
| `ui_paths` | segment-boundary prefix over dashboard app-router page paths (`dashboard/src/app/goals/page.tsx` -> `/goals`) |

Prefix selectors match on SEGMENT boundaries, so `/agent` never claims `/agents`.
A root catch-all (`/`) is refused on the path axes for the same reason an empty
string is: it prefixes every path and empties the bucket.
An EMPTY selector string is refused on every axis: it would match everything at
specificity 0 and silently empty the UNATTRIBUTED bucket, faking section 13-B's
acceptance bar. An unknown selector key is refused too — a misspelled key matches
nothing and reads as "this feature owns no items of that kind".

An item claimed by two features is awarded to the **most specific** selector
(longest match) with an alphabetical tiebreak — neither rule depends on YAML
ordering — and every multi-claim is published in `contested`. An item claimed by
nobody is published in `unattributed`. There is no third, silent destination.

Section 13-B's bar is an EMPTY unattributed bucket. This lane publishes the
bucket and its counts; emptying it is overlay work that follows, and until then
the number is the honest measure of how far the curated axis is from complete.

## Denominators

Every axis publishes a count per source with `agree`, `unavailable` and
`vacuous` flags:

- Sources that disagree carry a written `reconciliation`. A disagreeing axis
  with no reconciliation is a **refusal** — "could not reconcile" must never
  render as reconciled.
- An unreadable or ABSENT source records `null` and appears in `unavailable`. It
  never records 0, which would read as a truthful measurement of nothing. This
  covers a missing migrations directory, a missing dashboard directory, and a
  mechanism registry that is absent or fails to parse. `#` comment lines are
  part of the mechanism registry's format and are not counted as mechanisms
  (counting raw lines reported 58 where there are 49 mechanisms).
- An axis where any declared source did not report is `complete: false` and its
  `agree` is **null**, never true: one source that did not contradict itself is
  half-observed, not reconciled. `generate --strict` refuses an incomplete axis
  for the same reason it refuses a vacuous one.
- A present source measuring 0 sets `vacuous`, and `generate --strict` refuses:
  a coverage metric over an empty denominator proves nothing (section 13-B,
  "metrics non-vacuous").

`stamp.inputs[].path` is recorded repo-relative, or `~`-relative for host files,
never as an absolute `/Users/...` path — two identical registries generated on
different machines must compare equal.

The standing reconciliations (routes, launchd, migrations, livesim) are in
`RECONCILIATIONS` in the module, next to the code that enforces them.

## Tests

`tests/feature_registry/test_registry.py` — synthetic-repo behaviour tests for
alias resolution, overlay/feature-health key agreement, attribution, the
unattributed bucket, contested awards, route collisions, denominator publication
and refusals, determinism, and the drift check; plus one real-tree smoke test
that asserts shape, never counts.
