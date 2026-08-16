#!/usr/bin/env python3
"""Select and run the gates that apply to a branch, and record whether they earned it.

WHY A RUNNER AND NOT JUST MORE CHECKS IN merge-gate.sh
------------------------------------------------------
`merge-gate.sh` is fourteen hardcoded checks in a flat script. That works until the suite
grows, and then two things go wrong at once: every lane pays for every check, and nobody can
tell which checks have ever caught anything. A suite that is slow and unmeasured gets routed
around — which is precisely what happened to `merge-gate.sh` itself, whose `signed-receipt`
check has refused every candidate since it was added because no producer has ever existed.

So this does three things a flat list cannot:

  SELECT   by surface, so a docs lane does not pay for a security scan
  RECORD   every run to var/gates/efficacy.jsonl, so promotion and retirement are measured
  REPORT   which gates have never fired, so the library can be pruned rather than accreted

THE RULE IT ENFORCES ABOVE ALL OTHERS
A gate that cannot run is a REFUSAL. Not a skip, not a warning. If the script is missing,
un-executable, or errors out, this exits non-zero. An absent witness is never a pass, and a
gate suite that silently skips broken gates is worse than no suite because it reports green.

    gate-runner.py <branch> [base]      # run the applicable gates
    gate-runner.py --efficacy           # what has each gate actually caught?
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "configs" / "gates.yaml"
EFFICACY = REPO / "var" / "gates" / "efficacy.jsonl"


def _load_registry() -> dict:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("REFUSED: pyyaml unavailable — cannot read the gate registry", file=sys.stderr)
        raise SystemExit(2) from None
    if not REGISTRY.exists():
        print(f"REFUSED: no gate registry at {REGISTRY}", file=sys.stderr)
        raise SystemExit(2)
    return yaml.safe_load(REGISTRY.read_text()) or {}


def _changed_files(base: str, branch: str) -> list[str]:
    p = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{branch}"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    if p.returncode != 0:
        # Cannot see the diff => cannot choose gates => refuse. Guessing a surface here
        # would silently downgrade a security lane to the default set.
        print(f"REFUSED: cannot diff {base}...{branch}", file=sys.stderr)
        raise SystemExit(2)
    return p.stdout.split()


def _surface(files: list[str], reg: dict) -> str:
    """Pick the surface with the STRONGEST gate set that any file demands.

    The first version returned the first surface with any matching file. Because `docs`
    matched on a single `devtasks/` path, a 66-file merge containing production Python was
    gated as `docs` — which runs `forbidden-paths` only. Five unwired public symbols reached
    main through that hole (`omniagentos/integration/config.py:100,246`,
    `verdicts.py:97,135,141`), found afterwards by running the reachability gate by hand.

    So `docs` is now a CEILING, not a match: it applies only when EVERY changed file is
    documentation. Anything else falls to at least `default`, which includes reachability.
    A weakening surface must be earned by the whole diff; a strengthening one needs only a
    single file, because one policy file among forty docs is still a security change.
    """
    paths = reg.get("surface_paths", {}) or {}
    doc_prefixes = paths.get("docs", []) or []

    # Strengthening surfaces first — any single matching file is enough.
    for surface, prefixes in paths.items():
        if surface == "docs":
            continue
        if any(f.startswith(pre) for pre in prefixes for f in files):
            return surface

    # `docs` only if the ENTIRE diff is documentation. One production file revokes it.
    if files and doc_prefixes and all(
        any(f.startswith(pre) for pre in doc_prefixes) for f in files
    ):
        return "docs"
    return "default"


def _record(gate_id: str, branch: str, outcome: str, seconds: float, detail: str = "") -> None:
    EFFICACY.parent.mkdir(parents=True, exist_ok=True)
    with EFFICACY.open("a") as fh:
        fh.write(json.dumps({
            "gate": gate_id, "branch": branch, "outcome": outcome,
            "seconds": round(seconds, 2), "detail": detail[:300],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }) + "\n")


def _efficacy_report() -> int:
    """Which gates have earned their place, and which should be retired."""
    if not EFFICACY.exists():
        print("no runs recorded yet")
        return 0
    rows = [json.loads(x) for x in EFFICACY.read_text().splitlines() if x.strip()]
    reg = _load_registry()
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by.setdefault(r["gate"], {"runs": 0, "refused": 0, "passed": 0, "broken": 0})
        d["runs"] += 1
        d[{"refused": "refused", "passed": "passed"}.get(r["outcome"], "broken")] += 1

    print(f"{'gate':<24} {'runs':>5} {'refused':>8} {'broken':>7}   verdict")
    for g in reg.get("gates", []) or []:
        if g.get("status") != "active":
            continue
        gid = g["id"]
        d = by.get(gid, {"runs": 0, "refused": 0, "broken": 0})
        limit = int(g.get("retire_after_clean_runs") or 0)
        if d["broken"]:
            v = "BROKEN — gate could not run; fix or retire"
        elif d["runs"] and d["refused"] == 0 and limit and d["runs"] >= limit:
            v = f"RETIRE — {d['runs']} runs, never fired"
        elif d["refused"]:
            v = f"earning it — caught {d['refused']}"
        else:
            v = f"too early ({d['runs']}/{limit or '?'} clean runs)"
        print(f"{gid:<24} {d['runs']:>5} {d['refused']:>8} {d['broken']:>7}   {v}")

    cands = [g for g in (reg.get("gates") or []) if g.get("status") == "candidate"]
    if cands:
        print("\ncandidates — observed, not yet wired (promotion threshold is 3 instances):")
        for g in cands:
            n = len(g.get("promoted_from") or [])
            flag = "AT THRESHOLD" if n >= 3 else f"{n}/3"
            print(f"  {g['id']:<24} {flag:<14} {g.get('blocked_on','')[:80]}")
    return 0


def main() -> int:
    if "--efficacy" in sys.argv:
        return _efficacy_report()

    branch = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    reg = _load_registry()

    files = _changed_files(base, branch)
    surface = _surface(files, reg)
    wanted = (reg.get("surfaces", {}) or {}).get(surface, [])
    active = {g["id"]: g for g in (reg.get("gates") or []) if g.get("status") == "active"}

    print(f"gate-runner: {branch} vs {base} — {len(files)} file(s), surface={surface}")
    print(f"  gates: {', '.join(wanted) or '(none)'}")

    failures: list[str] = []
    for gid in wanted:
        g = active.get(gid)
        if g is None:
            # Named in a surface but not active. That is a registry error, and a registry
            # error means the suite is not what it claims to be — refuse.
            print(f"  {gid:<22} REFUSED — named for surface {surface!r} but not an active gate")
            failures.append(gid)
            _record(gid, branch, "broken", 0.0, "not an active gate")
            continue

        script = REPO / str(g.get("script") or "")
        if not script.exists():
            print(f"  {gid:<22} REFUSED — script missing: {g.get('script')}")
            failures.append(gid)
            _record(gid, branch, "broken", 0.0, "script missing")
            continue

        argv = str(g.get("args") or "").format(branch=branch, base=base).split()
        cmd = ([str(REPO / ".venv/bin/python")] if script.suffix == ".py" else ["bash"]) + [str(script), *argv]
        t0 = time.time()
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
        dt = time.time() - t0

        if p.returncode == 0:
            print(f"  {gid:<22} ok ({dt:.1f}s)")
            _record(gid, branch, "passed", dt)
        elif p.returncode == 2:
            # By convention 2 means the gate could not run. Unknown is a refusal.
            print(f"  {gid:<22} REFUSED — could not run (rc=2); unknown is never a pass")
            failures.append(gid)
            _record(gid, branch, "broken", dt, (p.stdout + p.stderr)[-300:])
        else:
            detail = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-3:]
            print(f"  {gid:<22} REFUSED ({dt:.1f}s)")
            for ln in detail:
                print(f"      {ln.strip()[:110]}")
            failures.append(gid)
            _record(gid, branch, "refused", dt, " | ".join(detail))

    if failures:
        print(f"\nGATES: REFUSED ({len(failures)}) — {', '.join(failures)}")
        return 1
    print("\nGATES: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
