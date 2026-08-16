#!/usr/bin/env python3
"""memcert cert-split rotation seeds — out-of-checkout, ratchet-rotated.

The cert split's fixture seed is the suite's only secret (DESIGN §4, §7): the
grader re-derives expected answers from the seed, so whoever can read the seed
can read the answer key. Storage rules copied from
``scripts/northstar_cert/seed_holdout.py`` (R1-012):

- the seed file lives OUTSIDE every checkout this process can name (both the
  checkout this script runs from and the checkout ``omniagentos`` was imported
  from, computed independently);
- an in-checkout path is refused with exit 2 — do not retry unchanged;
- receipts carry only ``sha256(seed)``; the raw seed is revealed only when the
  rotation retires it (retired seeds graduate to the visible dev split).

Commands:
    ensure   — create the current rotation's seed if missing; prints sha256 only
    show     — print current rotation metadata (hash, period, created); never the seed
    rotate   — retire the current seed (reveal it into retired.jsonl) and mint a new one
    seed-file — print the path of the current rotation's seed file (for run_bench)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 2

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "memcert"


def _script_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _package_root() -> Path | None:
    try:
        import omniagentos
    except ImportError:  # pragma: no cover - hard dep in production venvs
        return None
    return Path(os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))).resolve()


def _protected_roots() -> tuple[Path, ...]:
    roots = [_script_root().resolve()]
    pkg = _package_root()
    if pkg is not None:
        roots.append(pkg)
    return tuple(dict.fromkeys(roots))


def _refuse_in_checkout(path: Path) -> None:
    resolved = path.resolve()
    for root in _protected_roots():
        if resolved == root or root in resolved.parents:
            print(
                f"memcert seed_holdout: REFUSED — {resolved} is inside checkout {root}; "
                "the cert seed is the answer key and must never live in a checkout "
                "(exit 2, do not retry unchanged)",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_REFUSED)
    # Sol review MC-007: the two nameable roots are not the only checkouts on
    # a machine — refuse ANY ancestor carrying a .git dir or worktree .git
    # file ("outside every checkout" means every checkout, not just ours).
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            print(
                f"memcert seed_holdout: REFUSED — {resolved} is inside git checkout "
                f"{ancestor}; the cert seed is the answer key and must never live in a "
                "checkout (exit 2, do not retry unchanged)",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_REFUSED)


def _period_id(today: dt.date | None = None) -> str:
    d = today or dt.date.today()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _seed_path(state_dir: Path, period: str) -> Path:
    return state_dir / f"cert-seed-{period}.json"


def _sha(seed: int) -> str:
    return hashlib.sha256(str(seed).encode("utf-8")).hexdigest()


def ensure(state_dir: Path, period: str) -> dict[str, object]:
    _refuse_in_checkout(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _seed_path(state_dir, period)
    if path.exists():
        meta = json.loads(path.read_text())
        meta.pop("seed", None)
        return meta
    seed = secrets.randbits(63)
    record = {
        "period": period,
        "seed": seed,
        "seed_sha256": _sha(seed),
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scale": "S",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh)
    public = dict(record)
    public.pop("seed")
    return public


def rotate(state_dir: Path, period: str) -> dict[str, object]:
    _refuse_in_checkout(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    retired = state_dir / "retired.jsonl"
    revealed: list[dict[str, object]] = []
    for f in sorted(state_dir.glob("cert-seed-*.json")):
        rec = json.loads(f.read_text())
        rec["retired_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Reveal on retirement: retired seeds are dev-split material by design.
        with retired.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        revealed.append({"period": rec["period"], "seed_sha256": rec["seed_sha256"]})
        f.unlink()
    fresh = ensure(state_dir, period)
    return {"retired": revealed, "current": fresh}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["ensure", "show", "rotate", "seed-file"])
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--period", default=None, help="rotation period id (default: current ISO week)")
    args = ap.parse_args(argv)
    period = args.period or _period_id()

    if args.command == "ensure":
        print(json.dumps(ensure(args.state_dir, period)))
        return EXIT_OK
    if args.command == "show":
        path = _seed_path(args.state_dir, period)
        if not path.exists():
            print(json.dumps({"period": period, "exists": False}))
            return EXIT_OK
        meta = json.loads(path.read_text())
        meta.pop("seed", None)
        meta["exists"] = True
        print(json.dumps(meta))
        return EXIT_OK
    if args.command == "rotate":
        print(json.dumps(rotate(args.state_dir, period)))
        return EXIT_OK
    if args.command == "seed-file":
        _refuse_in_checkout(args.state_dir)
        print(_seed_path(args.state_dir, period))
        return EXIT_OK
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
