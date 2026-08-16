# A/B benchmark fixtures (T7.0)

The frozen reference corpus the upgrade program is measured against. Code lives
in `scripts/benchmarks/`; this directory holds the data.

## Layout

```
fixtures/<fixture_id>/
  fixture.yaml   task text, declared file set, canaries, acceptance spec, repo pin
  seed/          copied into a fresh workspace BEFORE the arm runs
  accept/        copied OVER the workspace AFTER the arm runs (frozen checks)
  solution/      reference patch; never reaches an arm, only the corpus self-tests
frozen_digests.json   content digest per fixture + one corpus digest
```

`accept/` landing last is the point: an arm that makes a failing check pass by
editing the check has that edit overwritten before grading — and the edit is
separately recorded as an undeclared modification.

Fixtures are hermetic: a run reads nothing outside its own workspace, so
acceptance stays deterministic as this repo moves. `repo_rev` in
`fixture.yaml` is a provenance pin, not an execution dependency.

Seed and accept files are named `check_*.py`, not `test_*.py`, and
`fixtures/conftest.py` sets `collect_ignore_glob` — seeds contain deliberately
failing checks and must never be collected by the repo suite.

## Running

```bash
# synthetic controls — no model, no cost; they bracket every real arm
uv run python -m scripts.benchmarks.capture_baseline --arm oracle   # ceiling
uv run python -m scripts.benchmarks.capture_baseline --arm noop     # floor

# a real arm
uv run python -m scripts.benchmarks.capture_baseline --arm b0 --model sonnet
uv run python -m scripts.benchmarks.capture_baseline --arm b0 --replicates 3 \
    --only fx_002_bugfix_failing_test
```

Results land in `var/benchmarks/results.jsonl` (raw, append-only) and
`var/benchmarks/baseline.db` (queryable):

```sql
SELECT * FROM bench_capture_summary ORDER BY capture_id;
SELECT fixture_id, arm, success, wall_ms, total_tokens, cost_usd, tool_calls,
       undeclared_changes, canary_tripped
FROM bench_runs WHERE capture_id = '<capture_id>';
```

Per-run workspaces are kept under `var/benchmarks/workspaces/<capture_id>/`, so
any run can be inspected exactly as the arm left it.

## The freeze protocol

A capture refuses to run when the corpus digest does not match
`frozen_digests.json`, because numbers from a changed corpus are not comparable
to earlier ones.

```bash
uv run python -m scripts.benchmarks.freeze --check   # what CI/tests enforce
uv run python -m scripts.benchmarks.freeze --write   # deliberate re-freeze
```

Re-freezing bumps `corpus_version`. Every capture already recorded stores the
corpus digest it ran against — compare only within one digest.

## Adding a fixture

1. Create `fixtures/<id>/` with `fixture.yaml`, `seed/`, `accept/`, `solution/`.
2. `uv run pytest tests/benchmarks -q` — the corpus tests assert that the seed
   FAILS acceptance, the reference solution PASSES it, and the solution stays
   inside `declared_files`. A fixture that violates any of those measures
   nothing, is unpassable, or scores every arm as undisciplined.
3. `uv run python -m scripts.benchmarks.freeze --write`.
