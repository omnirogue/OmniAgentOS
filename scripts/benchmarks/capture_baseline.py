"""CLI: capture an A/B benchmark run for one arm over the frozen fixture set.

    uv run python -m scripts.benchmarks.capture_baseline --arm b0
    uv run python -m scripts.benchmarks.capture_baseline --arm mock --label smoke
    uv run python -m scripts.benchmarks.capture_baseline --arm b0 \
        --only fx_002_bugfix_failing_test --replicates 3

Writes ``var/benchmarks/results.jsonl`` (raw) and ``var/benchmarks/baseline.db``
(queryable), and prints a summary table. ``--append-ledger`` additionally writes
real RunManifests through ``omniagentos.wrapper.durable.finalize_manifest`` into
a benchmark-scoped ledger/vault/db — off by default so a capture never touches
the operator's live control-plane database.

Fixture-set integrity is enforced before anything runs: if the corpus digest
does not match ``tests/benchmarks/frozen_digests.json`` the capture refuses,
because results from a changed corpus are not comparable to earlier baselines.

Additionally, a dirty or moving tree refuses the capture, and full provenance is
written to ``var/benchmarks/provenance/<capture_id>.json`` to ensure exact code-state
attribution of benchmark measurements.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.harnesses.envhash import env_hash
from scripts.benchmarks import observe, provenance
from scripts.benchmarks.fixtures import (
    FIXTURES_DIR,
    FROZEN_DIGESTS_PATH,
    REPO_ROOT,
    Fixture,
    fixture_set_digest,
    load_fixtures,
)
from scripts.benchmarks.runner import (
    DEFAULT_WORKSPACE_ROOT,
    SYNTHETIC_ARMS,
    run_fixture,
)
from scripts.benchmarks.store import DEFAULT_DIR, BenchStore, append_jsonl


def load_frozen_digests(path: str | Path = FROZEN_DIGESTS_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def verify_frozen(fixtures: list[Fixture], frozen: dict[str, Any]) -> list[str]:
    """Return human-readable drift descriptions; empty means the corpus is intact."""
    problems: list[str] = []
    expected: dict[str, str] = frozen.get("fixtures") or {}
    for fixture in fixtures:
        want = expected.get(fixture.id)
        got = fixture.digest()
        if want is None:
            problems.append(f"{fixture.id}: not in frozen_digests.json (new fixture)")
        elif want != got:
            problems.append(f"{fixture.id}: digest {got[:12]} != frozen {want[:12]}")
    for fixture_id in expected:
        if fixture_id not in {f.id for f in fixtures}:
            problems.append(f"{fixture_id}: frozen fixture is missing from disk")
    return problems


def _append_ledger(record: dict[str, Any], fixture: Fixture, root: Path) -> str | None:
    """Best-effort RunManifest projection into a benchmark-scoped ledger."""
    from omniagentos.contracts import RunManifest
    from omniagentos.wrapper.durable import finalize_manifest

    try:
        finalize_manifest(
            RunManifest.model_validate(record["manifest"]),
            task_title=f"bench {fixture.id}",
            task_input={"prompt": fixture.prompt, "fixture": fixture.id},
            output_text=record.get("output_text") or "",
            ledger_dir=str(root / "ledger"),
            vault_dir=str(root / "vault"),
            db_path=str(root / "ledger.db"),
        )
    except Exception as exc:  # noqa: BLE001 -- never lose a capture over a projection
        return f"{type(exc).__name__}: {exc}"
    return None


def _print_summary(store: BenchStore, capture_id: str) -> dict[str, Any]:
    summary = store.capture_summary(capture_id) or {}
    rows = store.per_fixture(capture_id)
    width = max((len(r["fixture_id"]) for r in rows), default=10)
    print("")
    print(f"capture {capture_id}  arm={summary.get('arm')}  model={summary.get('model')}")
    header = (
        f"{'fixture'.ljust(width)}  pass   wall_s   tokens     cost_usd  tools  "
        "undecl  reads  canary"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{str(row['fixture_id']).ljust(width)}  "
            f"{row['passed']}/{row['runs']}   "
            f"{row['avg_wall_s'] or 0:>6}  "
            f"{row['total_tokens'] or 0:>7}  "
            f"{row['cost_usd'] or 0:>10}  "
            f"{row['tool_calls'] or 0:>5}  "
            f"{row['undeclared_changes'] or 0:>6}  "
            f"{row['undeclared_read_attempts'] or 0:>5}  "
            f"{row['canary_trips'] or 0:>6}"
        )
    print("-" * len(header))
    print(
        f"TOTAL: {summary.get('passed') or 0}/{summary.get('runs') or 0} passed "
        f"({(summary.get('pass_rate') or 0) * 100:.1f}%)  "
        f"avg_wall={summary.get('avg_wall_s')}s  "
        f"tokens={summary.get('total_tokens')}  cost=${summary.get('total_cost_usd')}  "
        f"tool_calls={summary.get('tool_calls')}  "
        f"undeclared_changes={summary.get('undeclared_changes')}  "
        f"undeclared_reads={summary.get('undeclared_read_attempts')}  "
        f"outside_workspace={summary.get('outside_workspace_attempts')}  "
        f"canary_trips={summary.get('canary_trips')}"
    )
    return {"summary": summary, "per_fixture": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.benchmarks.capture_baseline",
        description="Capture an A/B benchmark for one arm over the frozen fixture set.",
    )
    parser.add_argument(
        "--arm",
        required=True,
        help=(
            "b0 | b1 | champion | grok (real arms) or oracle | noop | mock "
            "(synthetic controls: ceiling, floor, and a no-op adapter)"
        ),
    )
    parser.add_argument("--label", default="", help="Free-text label for this capture")
    parser.add_argument("--notes", default="", help="Free-text notes stored with the capture")
    parser.add_argument("--model", default=None, help="Model override passed to the arm")
    parser.add_argument("--only", default="", help="Comma-separated fixture ids to run")
    parser.add_argument("--replicates", type=int, default=1, help="Replicates per fixture")
    parser.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT))
    parser.add_argument(
        "--append-ledger",
        action="store_true",
        help="Also write RunManifests to a benchmark-scoped ledger/vault/db",
    )
    parser.add_argument(
        "--allow-unfrozen",
        action="store_true",
        help="Run even when the fixture corpus does not match frozen_digests.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    fixtures = load_fixtures(args.fixtures_dir, only=only or None)
    if not fixtures:
        print("error: no fixtures matched", file=sys.stderr)
        return 2
    if args.replicates < 1:
        print("error: --replicates must be >= 1", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    capture_id = new_id("cap")
    try:
        prov, git_before = provenance.collect(
            capture_id=capture_id,
            repo_root=REPO_ROOT,
            model_id=args.model,
        )
    except provenance.DirtyTreeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except provenance.ProvenanceError as exc:
        print(f"error: provenance unavailable: {exc}", file=sys.stderr)
        return 4

    provenance.write_sidecar(prov, out_dir)
    print(f"provenance commit={prov.commit_sha[:12]} env={prov.environment_hash()} "
          f"sandbox={prov.sandbox_mode} lock={prov.lock_digest[:12]}", flush=True)

    problems = verify_frozen(load_fixtures(args.fixtures_dir), load_frozen_digests())
    if problems:
        for problem in problems:
            print(f"fixture-set drift: {problem}", file=sys.stderr)
        if not args.allow_unfrozen:
            print(
                "error: fixture corpus is not the frozen one; results would not be "
                "comparable. Re-freeze deliberately or pass --allow-unfrozen.",
                file=sys.stderr,
            )
            return 3

    arm = args.arm.lower().strip()
    corpus_digest = fixture_set_digest(load_fixtures(args.fixtures_dir))

    store = BenchStore(out_dir / "baseline.db")
    store.start_capture(
        capture_id=capture_id,
        arm=arm,
        label=args.label,
        model=args.model,
        repo_rev=observe.repo_rev(REPO_ROOT),
        env_hash=env_hash(),
        fixture_set_digest=corpus_digest,
        fixture_count=len(fixtures),
        replicates=args.replicates,
        dry_run=(arm in SYNTHETIC_ARMS),
        notes=args.notes,
    )
    print(
        f"capture {capture_id} arm={arm} fixtures={len(fixtures)} "
        f"replicates={args.replicates} corpus={corpus_digest[:12]} started={utc_now_iso()}",
        flush=True,
    )

    records: list[dict[str, Any]] = []
    for fixture in fixtures:
        for replicate in range(args.replicates):
            print(f"  -> {fixture.id} r{replicate} ...", flush=True)
            record = run_fixture(
                fixture,
                arm,
                capture_id=capture_id,
                workspace_root=args.workspace_root,
                replicate=replicate,
                model=args.model,
            )
            if args.append_ledger:
                error = _append_ledger(record, fixture, out_dir)
                if error:
                    record["ledger_error"] = error
            record["provenance_environment_hash"] = prov.environment_hash()
            record["provenance_commit_sha"] = prov.commit_sha
            store.record_run(record)
            append_jsonl(out_dir / "results.jsonl", [record])
            records.append(record)
            usage = record["usage"]
            print(
                f"     {'PASS' if record['success'] else 'FAIL'} "
                f"wall={record['wall_ms'] / 1000:.1f}s "
                f"tokens={(usage.get('input_tokens') or 0) + (usage.get('output_tokens') or 0)} "
                f"cost=${usage.get('cost_usd') or 0:.4f} "
                f"tools={(record['governance']['access'] or {}).get('tool_calls')} "
                f"undeclared={len(record['governance']['undeclared_changes'])}",
                flush=True,
            )

    try:
        provenance.verify_unmoved(git_before, REPO_ROOT)
    except provenance.MovedTreeError as exc:
        provenance.write_sidecar(
            replace(prov, valid=False, abort_reason=str(exc),
                    head_verified_at=utc_now_iso()),
            out_dir,
        )
        store.close()
        print(f"error: {exc}", file=sys.stderr)
        print("error: this capture is INVALID and must not be compared.", file=sys.stderr)
        return 5
    provenance.write_sidecar(replace(prov, head_verified_at=utc_now_iso()), out_dir)

    store.finish_capture(capture_id)
    _print_summary(store, capture_id)
    store.close()
    print(f"\nrecords: {out_dir / 'results.jsonl'}\ndatabase: {out_dir / 'baseline.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
