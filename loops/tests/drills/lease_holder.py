"""Take one loop-instance lease in a REAL separate process and report the verdict.

The lease is arbitration between OS processes (launchd firing a second tick over
a slow first one). A threading test would exercise a different primitive
entirely, so the concurrency tests drive this script instead.

Protocol
--------
The process spins until ``--barrier`` exists, then calls ``lease.acquire`` and
prints ONE json line: ``{"pid": …, "acquired": true|false, "holder": {...}}``.
Spinning on a shared file is what makes N racers contend in the same
millisecond; staggered ``Popen`` calls would serialise the very window under
test.

With ``--ready`` it touches that path once the lease is held, so a test can wait
for "the lease is definitely taken" without sleeping. With ``--hold`` it keeps
the lease for that many seconds before releasing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omniagentos_loops import lease  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--barrier", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--hold", type=float, default=0.0)
    parser.add_argument("--ready", default="")
    args = parser.parse_args(argv)

    barrier = Path(args.barrier)
    deadline = time.time() + 60.0
    while not barrier.exists() and time.time() < deadline:
        time.sleep(0.002)

    try:
        held = lease.acquire(args.template, args.instance)
    except lease.LeaseHeld as exc:
        print(json.dumps({"pid": os.getpid(), "acquired": False, "holder": exc.holder}), flush=True)
        return 0

    if args.ready:
        Path(args.ready).touch()
    print(json.dumps({"pid": os.getpid(), "acquired": True}), flush=True)
    try:
        time.sleep(args.hold)
    finally:
        lease.release(held)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
