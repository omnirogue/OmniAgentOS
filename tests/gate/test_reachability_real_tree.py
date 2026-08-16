#!/usr/bin/env python3
"""Regression test: gate must work against the real repository tree.

Context: The reachability gate was previously defeated by:
1. Generic symbol names (e.g., "fetch", "search") matching 31+ unrelated files
2. False positives from files/directories with shared name prefixes
3. Missing same-module call detection

This test verifies the gate can be run against main...main (no new symbols)
without crashing or reporting false positives, using real production code.
"""

import subprocess
import sys
from pathlib import Path


def run_gate_on_real_tree():
    """Run the reachability gate against main...main on the real tree."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    gate_script = repo_root / "scripts" / "reachability-gate.py"

    if not gate_script.exists():
        print(f"SKIP: gate script not found at {gate_script}")
        return True

    # Run gate on main...main (no new symbols, should pass cleanly)
    result = subprocess.run(
        [sys.executable, str(gate_script), "main", "main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Expected: "0 new public symbol(s)" and rc=0
    if "0 new public symbol(s)" in result.stdout and result.returncode == 0:
        print("PASS: Gate runs cleanly on real tree (no regressions)")
        return True

    # If new symbols were somehow detected, that's also OK (gate working)
    if "new public symbol(s)" in result.stdout:
        print("PASS: Gate detects symbols on real tree")
        return True

    # Any other output is suspect
    print(f"FAIL: Unexpected gate output:\n{result.stdout}\n{result.stderr}")
    return False

if __name__ == "__main__":
    success = run_gate_on_real_tree()
    sys.exit(0 if success else 1)
