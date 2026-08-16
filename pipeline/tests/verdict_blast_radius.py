#!/usr/bin/env python3
"""Blast radius: replay every REAL live candidate through old and new guard.

A guard that closes eleven bypasses but refuses honest work is not an
improvement, it is a different outage.  This runs the cross-lineage decision —
and ONLY that decision, isolated from git/base/paths checks that depend on
repository state this probe cannot reproduce — over the actual envelopes sitting
in var/loopqueue/candidates/, and prints every row where the two disagree.

Usage:  python3 tests/verdict_blast_radius.py [<candidates-dir>]
Default dir: ~/OmniAgentOS/var/loopqueue/candidates
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / "OmniAgentOS" / "var" / "loopqueue" / "candidates"


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Probe discipline: .zshenv prepends the serving checkout to PYTHONPATH and the
# serving venv carries an editable .pth pin to it, so an unqualified import can
# resolve to a tree this probe is not measuring.
import bridge.integration as _I  # noqa: E402
from bridge.integration import cross_lineage_check  # noqa: E402

assert Path(_I.__file__).resolve() == (HERE / "bridge" / "integration.py").resolve(), \
    f"imported the WRONG integration.py: {_I.__file__}"


def old_guard(art: dict) -> str:
    """The guard as it stands on main @ 76b476a.

    This one IS a transcription, and has to be: it is the behaviour of a commit,
    which does not change. The NEW side must never be transcribed — see below.
    """
    producer_lineage = (art.get("producer") or {}).get("lineage")
    verdicts = art.get("verdicts")
    if producer_lineage and isinstance(verdicts, list) and verdicts:
        lineages = {v.get("lineage") for v in verdicts if isinstance(v, dict)}
        if lineages and lineages <= {producer_lineage}:
            return "REFUSED"
    return "ADMITTED"


def new_guard(art: dict) -> str:
    """Calls the REAL guard — deliberately not a copy of it.

    This function used to transcribe the new logic, and that made it a liar: the
    guard was edited in review and this probe went on reporting a clean run
    against the superseded version. An instrument that restates the code instead
    of invoking it stops measuring at the first edit, and does so silently.
    """
    refusal, _warnings = cross_lineage_check(art)
    return "ADMITTED" if refusal is None else "REFUSED"


def main() -> int:
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(d.glob("*.json"))
    if not files:
        print(f"NO CANDIDATES FOUND in {d} — this measurement covers nothing. "
              "A green run over an empty population is not green.")
        return 3
    print(f"population: {len(files)} live candidate envelopes in {d}")
    print(f"{'id':14} {'old':10} {'new':10} {'lineage':10} verdict lineages")
    print("-" * 96)
    changed, newly_refused = [], []
    for p in files:
        try:
            art = json.loads(p.read_text())
        except Exception as exc:
            print(f"{p.name[7:19]:14} UNPARSEABLE  {exc}")
            continue
        o, n = old_guard(art), new_guard(art)
        pl = ((art.get("producer") or {}).get("lineage")) or "-"
        v = art.get("verdicts")
        vl = ([x.get("lineage") for x in v if isinstance(x, dict)]
              if isinstance(v, list) else repr(v))
        flag = "   <-- CHANGED" if o != n else ""
        print(f"{p.name[7:19]:14} {o:10} {n:10} {pl:10} {vl}{flag}")
        if o != n:
            changed.append(p.name)
            if n == "REFUSED":
                newly_refused.append((p.name, pl, vl))
    print("-" * 96)
    print(f"decisions changed: {len(changed)} of {len(files)}")
    for name, pl, vl in newly_refused:
        print(f"  NEWLY REFUSED: {name}  producer.lineage={pl} verdict lineages={vl}")
    if not newly_refused:
        print("  (no candidate is newly refused)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
