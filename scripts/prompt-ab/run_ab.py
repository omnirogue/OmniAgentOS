#!/usr/bin/env python3
"""Prompt A/B replay harness — mechanical grading only, repeatable by any agent.

Usage (from repo root):
    python3 scripts/prompt-ab/run_ab.py                # run every scenario
    python3 scripts/prompt-ab/run_ab.py <scenario-id>  # run one

Scenarios: scripts/prompt-ab/scenarios/*.json (committed — this IS the prompt
failure-replay corpus; each scenario encodes a real measured failure).
Results:   var/prompt-ab/runs/<UTC-stamp>/results.jsonl + summary.json
Ledger:    var/prompt-ab/ledger-YYYYMM.jsonl (append-only, one line per
           scenario verdict, with sha256 digests of both prompt arms so a
           promotion is always traceable to the exact texts compared).

Scenario schema:
{
  "id": str,                       # unique, kebab-case
  "runner": "claude" | "codex",
  "model": str,
  "effort": "low"|"medium"|"high"|"xhigh",   # MUST match the role's production tier
  "role_id": str,                  # ROLE-REGISTRY.yaml id this tests (or MISSING:<id>)
  "failure_ref": str,              # fingerprint/ledger row the scenario replays
  "arms": {"control": <path-or-inline-text>, "candidate": <path-or-inline-text>},
  "input": str,
  "grading": [ {"type": "json_valid_keys", "keys": [...]}
             | {"type": "regex_must", "pattern": ...}
             | {"type": "regex_forbid", "pattern": ...}
             | {"type": "enum_field", "field": ..., "allowed": [...]} ],
  "trials": int                    # default 3
}
Promotion rule (small-N, strict): candidate PROMOTES only if it strictly beats
control AND passes every trial. Ties/partials keep control. Grading is always
mechanical — never an LLM judge (estate doctrine: green suites are non-evidence;
a grader you cannot replay is not evidence either).
"""
import glob
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import UTC, datetime

REPO = pathlib.Path(__file__).resolve().parents[2]
SCEN_DIR = REPO / "scripts" / "prompt-ab" / "scenarios"
VAR = REPO / "var" / "prompt-ab"


def load_arm(v):
    p = pathlib.Path(os.path.expanduser(v))
    return p.read_text() if len(v) < 300 and p.exists() else v


def call_claude(model, system, task, effort):
    # -p single turn; effort rides the invoking profile unless the CLI grows a flag.
    # cwd MUST be outside any repo: claude -p injects the cwd's project CLAUDE.md,
    # which contaminates arms (measured 2026-08-08: repo git -C rule leaked into a
    # control arm and masked the candidate's effect).
    neutral = pathlib.Path("/private/tmp/prompt-ab-neutral")
    neutral.mkdir(exist_ok=True)
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system, "--max-turns", "1", task]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL, cwd=str(neutral))
    return r.stdout


def call_codex(model, system, task, effort):
    prompt = f"SYSTEM INSTRUCTIONS (these govern your behavior):\n{system}\n\nTASK:\n{task}"
    cmd = ["codex", "exec", "--model", model, "-c", f"model_reasoning_effort={effort}",
           "--sandbox", "read-only", prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=420,
                       stdin=subprocess.DEVNULL, cwd=str(VAR))
    return r.stdout


def extract_json(text):
    for m in reversed(list(re.finditer(r"\{", text))):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[m.start():i + 1])
                    except Exception:
                        break
    return None


def grade(output, criteria):
    fails = []
    for c in criteria:
        t = c["type"]
        if t == "json_valid_keys":
            obj = extract_json(output)
            if not isinstance(obj, dict) or not all(k in obj for k in c["keys"]):
                fails.append(f"json_valid_keys:{c['keys']}")
        elif t == "regex_must":
            if not re.search(c["pattern"], output, re.I | re.S):
                fails.append(f"must:{c['pattern'][:40]}")
        elif t == "regex_forbid":
            if re.search(c["pattern"], output, re.I | re.S):
                fails.append(f"forbid:{c['pattern'][:40]}")
        elif t == "enum_field":
            obj = extract_json(output) or {}
            if obj.get(c["field"]) not in c["allowed"]:
                fails.append(f"enum:{c['field']}")
    return fails


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    scenarios = []
    for p in sorted(glob.glob(str(SCEN_DIR / "*.json"))):
        data = json.load(open(p))
        scenarios.extend(data if isinstance(data, list) else [data])
    if only:
        scenarios = [s for s in scenarios if s["id"] == only]
    if not scenarios:
        print("no scenarios matched")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = VAR / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    results = open(run_dir / "results.jsonl", "a")
    ledger = open(VAR / f"ledger-{stamp[:6]}.jsonl", "a")

    summary = {}
    for s in scenarios:
        n = s.get("trials", 3)
        effort = s.get("effort", "medium")
        res, digests = {}, {}
        for arm, spec in s["arms"].items():
            system = load_arm(spec)
            digests[arm] = hashlib.sha256(system.encode()).hexdigest()[:16]
            passes = 0
            for t in range(n):
                try:
                    fn = call_claude if s["runner"] == "claude" else call_codex
                    out = fn(s["model"], system, s["input"], effort)
                except subprocess.TimeoutExpired:
                    out = "<TIMEOUT>"
                fails = grade(out, s["grading"])
                ok = not fails
                passes += ok
                results.write(json.dumps({"scenario": s["id"], "arm": arm, "trial": t,
                                          "pass": ok, "fails": fails,
                                          "out_head": out[:400]}) + "\n")
                results.flush()
                print(f"[{s['id']}] {arm} {t}: {'PASS' if ok else 'FAIL ' + str(fails)}", flush=True)
            res[arm] = passes
        verdict = ("PROMOTE" if res.get("candidate", 0) > res.get("control", 0)
                   and res.get("candidate", 0) == n else "KEEP-CONTROL")
        summary[s["id"]] = {**res, "trials": n, "verdict": verdict}
        ledger.write(json.dumps({
            "schema": "prompt-ab.v1", "at": stamp, "scenario": s["id"],
            "role_id": s.get("role_id"), "failure_ref": s.get("failure_ref"),
            "runner": s["runner"], "model": s["model"], "effort": effort,
            "control_pass": res.get("control"), "candidate_pass": res.get("candidate"),
            "trials": n, "verdict": verdict,
            "control_digest": digests.get("control"), "candidate_digest": digests.get("candidate"),
            "run_dir": str(run_dir),
        }) + "\n")
        ledger.flush()
        print(f"== {s['id']}: control {res.get('control')}/{n} candidate {res.get('candidate')}/{n} -> {verdict}", flush=True)

    json.dump(summary, open(run_dir / "summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
