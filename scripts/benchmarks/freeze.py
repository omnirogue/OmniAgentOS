"""Freeze the fixture corpus: write ``tests/benchmarks/frozen_digests.json``.

    uv run python -m scripts.benchmarks.freeze --check    # CI / test use
    uv run python -m scripts.benchmarks.freeze --write    # deliberate re-freeze

Re-freezing is a decision, never a side effect: every baseline already captured
is keyed to a corpus digest, and a changed corpus makes those numbers
incomparable. ``--write`` bumps ``corpus_version`` so the break is visible in
the file itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from scripts.benchmarks.fixtures import (
    FIXTURES_DIR,
    FROZEN_DIGESTS_PATH,
    REPO_ROOT,
    fixture_set_digest,
    load_fixtures,
)
from scripts.benchmarks.observe import repo_rev


def build_manifest(fixtures_dir: str | Path = FIXTURES_DIR) -> dict[str, Any]:
    fixtures = load_fixtures(fixtures_dir)
    return {
        "corpus_version": 1,
        "frozen_at": utc_now_iso(),
        "frozen_at_repo_rev": repo_rev(REPO_ROOT),
        "set_digest": fixture_set_digest(fixtures),
        "fixtures": {f.id: f.digest() for f in fixtures},
        "meta": {
            f.id: {"kind": f.kind, "tier": f.tier, "declared_files": list(f.declared_files)}
            for f in fixtures
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmarks.freeze")
    parser.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    parser.add_argument("--out", default=str(FROZEN_DIGESTS_PATH))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Verify without writing")
    group.add_argument("--write", action="store_true", help="(Re-)freeze the corpus")
    args = parser.parse_args(argv)

    manifest = build_manifest(args.fixtures_dir)
    out = Path(args.out)

    if args.check:
        if not out.is_file():
            print(f"error: {out} does not exist; run --write", file=sys.stderr)
            return 3
        current = json.loads(out.read_text(encoding="utf-8"))
        if current.get("set_digest") != manifest["set_digest"]:
            print(
                f"error: corpus digest {manifest['set_digest'][:12]} != frozen "
                f"{str(current.get('set_digest'))[:12]}",
                file=sys.stderr,
            )
            for fixture_id, want in (current.get("fixtures") or {}).items():
                got = manifest["fixtures"].get(fixture_id)
                if got != want:
                    print(f"  {fixture_id}: {want[:12]} -> {got if got else 'MISSING'}")
            return 3
        print(f"ok: corpus matches frozen digest {manifest['set_digest'][:12]}")
        return 0

    previous = {}
    if out.is_file():
        previous = json.loads(out.read_text(encoding="utf-8"))
        manifest["corpus_version"] = int(previous.get("corpus_version") or 0) + 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"wrote {out} corpus_version={manifest['corpus_version']} "
        f"set_digest={manifest['set_digest'][:12]} fixtures={len(manifest['fixtures'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
