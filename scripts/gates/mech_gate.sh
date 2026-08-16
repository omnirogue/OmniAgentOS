#!/bin/bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT_DIR"
FAILED=()
PYTEST_OUTPUT=$(mktemp)
trap 'rm -f "$PYTEST_OUTPUT"' EXIT
# Ruff and mypy are REQUIRED tools, invoked as modules of the selected
# interpreter ("$python_bin" -m ruff / -m mypy) — never resolved via shell
# PATH. If either tool is not importable by that interpreter the gate fails
# CLOSED: a missing tool is never reported as a zero-finding pass.

# Interpreter selection: an explicit OMNIAGENTOS_PYTHON is authoritative and
# must be usable. Only an unset override may fall back to the project venv or
# PATH python3.12/python3.
if [[ -n "${OMNIAGENTOS_PYTHON+x}" ]]; then
  python_bin="${OMNIAGENTOS_PYTHON}"
elif [[ -x .venv/bin/python ]]; then
  python_bin=.venv/bin/python
else
  python_bin=$(command -v python3.12 || command -v python3 || true)
fi

python_probe=
if [[ -z "$python_bin" || ! -x "$python_bin" ]] \
  || ! python_probe=$("$python_bin" -c \
    'import sys; sys.stdout.write("omniagentos-python-ok")' 2>/dev/null) \
  || [[ "$python_probe" != "omniagentos-python-ok" ]]; then
  if [[ -n "${OMNIAGENTOS_PYTHON+x}" ]]; then
    echo "FAIL: explicit OMNIAGENTOS_PYTHON is not an executable, usable Python" >&2
    echo "FAILED"
    echo "- invalid explicit OMNIAGENTOS_PYTHON"
  else
    echo "FAIL: no executable, usable Python interpreter found" >&2
    echo "FAILED"
    echo "- no usable Python interpreter"
  fi
  exit 1
fi

# --- MCP roster re-accretion check ------------------------------------------
# .mcp.json is the file the agent runtime actually loads, so it is what this
# check reads. Adding a server must be a reviewed TWO-file change (roster +
# configs/mcp-approved.yaml), and a server whose ${VAR} resolves to empty is
# dead by configuration -- that is the shape that produced ~93k
# mcp_server_failed events. Fails CLOSED.
#
# It used to read tools/mcp-servers.json, on the premise -- stated in this
# comment and in configs/mcp-approved.yaml's header -- that .mcp.json was a
# tracked symlink to it. That premise stopped holding at 00000000 (2026-08-02),
# which replaced the symlink with a regular file for an unrelated reason. The
# check kept passing on a 2-server file while the loaded one held 11, the exact
# roster this control exists to prevent. So it now ALSO asserts its own premise:
# two rosters that disagree is itself a refusal, because the reviewed one is
# then not necessarily the loaded one. A control that names its assumption in a
# comment and never tests it outlives that assumption silently.
check_mcp_roster() {
  local roster="${1:-}" mirror="${2:-tools/mcp-servers.json}"
  # DEFAULT resolution only. .mcp.json is what the runtime loads, so it is what
  # gets reviewed -- but a tree carrying ONLY the mirror still HAS a roster, and
  # "roster not found: .mcp.json" is the wrong diagnosis for a tree that is
  # merely still on the older layout. Check the roster it has instead. Naming a
  # roster EXPLICITLY and having it be absent stays an error, not a fallback and
  # not a skip: that is the standalone contract documented below.
  if [[ -z "$roster" ]]; then
    if [[ ! -f .mcp.json && -f "$mirror" ]]; then roster="$mirror"; else roster=".mcp.json"; fi
  fi
  "$python_bin" - "$roster" configs/mcp-approved.yaml "$mirror" <<'PY'
import json, os, re, sys
try:
    import yaml
except ImportError:
    sys.exit("mcp-roster: pyyaml not importable (required tool missing)")
roster_path, approved_path = sys.argv[1], sys.argv[2]

# Keep in sync with tests/acceptance/s12_s19_environment.sh check 6. A one-word
# justification is not a review, so the GATE must reject what the acceptance
# test rejects -- otherwise the gate is the weaker of the two and a server can
# be added past it with justification "x".
MIN_JUSTIFICATION = 20

def _load(path, loader, what):
    # A missing, unreadable or malformed input fails CLOSED with a readable
    # reason -- never a raw traceback, and never a silent pass.
    try:
        with open(path) as fh:
            data = loader(fh)
    except FileNotFoundError:
        sys.exit(f"mcp-roster: {what} not found: {path}")
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        sys.exit(f"mcp-roster: {what} is malformed ({path}): {exc}")
    except OSError as exc:
        sys.exit(f"mcp-roster: {what} unreadable ({path}): {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        sys.exit(f"mcp-roster: {what} is not a mapping ({path})")
    return data

servers = _load(roster_path, json.load, "roster").get("mcpServers") or {}
approved = _load(approved_path, yaml.safe_load, "approved list").get("approved") or {}
bad = []

# Premise assertion. This check is only meaningful if the roster it reads is the
# roster the runtime loads. When a second roster file exists, the two must agree
# -- otherwise a reviewed file can pass while a divergent one is what ships.
# Compared by parsed content, not bytes: formatting is not a finding.
mirror_path = sys.argv[3] if len(sys.argv) > 3 else ""
if mirror_path and os.path.exists(mirror_path) and os.path.realpath(mirror_path) != os.path.realpath(roster_path):
    mirror = _load(mirror_path, json.load, "mirror roster").get("mcpServers") or {}
    if mirror != servers:
        only_loaded = sorted(set(servers) - set(mirror))
        only_mirror = sorted(set(mirror) - set(servers))
        detail = []
        if only_loaded:
            detail.append(f"only in {roster_path}: {', '.join(only_loaded)}")
        if only_mirror:
            detail.append(f"only in {mirror_path}: {', '.join(only_mirror)}")
        if not detail:
            detail.append("same server names, differing definitions")
        sys.exit(
            f"mcp-roster: {roster_path} and {mirror_path} disagree, so the reviewed roster is "
            f"not necessarily the loaded one ({'; '.join(detail)})"
        )

def judge(name, spec, allowed, where):
    """Hold one server spec to the three rules. Appends to `bad`."""
    entry = allowed.get(name)
    if entry is None:
        bad.append(f"{where}{name}: absent from {approved_path} (adding a server is a reviewed two-file change)")
    else:
        just = str((entry or {}).get("justification") or "").strip()
        if not just:
            bad.append(f"{where}{name}: approved with no justification")
        elif len(just) < MIN_JUSTIFICATION:
            bad.append(f"{where}{name}: justification is trivial ({len(just)} < {MIN_JUSTIFICATION} chars)")
    for var in sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(spec)))):
        if not os.environ.get(var, "").strip():
            bad.append(f"{where}{name}: ${{{var}}} resolves to empty")


for name, spec in sorted(servers.items()):
    judge(name, spec, approved, "")

# Opt-in profiles. Without this loop the nine servers trimmed from the default
# roster on 2026-08-13 would sit in configs/toolbroker/mcp-profiles/*.json,
# outside every control that reads the three fixed paths above -- so the
# accretion this gate exists to stop would recur one directory over while the
# gate kept printing OK. A profile server is opt-in, so it may ALSO be approved
# under the lower `profile_approved` bar; it still has to be named and justified.
profile_approved = _load(approved_path, yaml.safe_load, "approved list").get("profile_approved") or {}
profile_allowed = dict(approved)
profile_allowed.update(profile_approved)
profile_dir = os.path.join(os.path.dirname(approved_path), "toolbroker", "mcp-profiles")
profile_count = 0
if os.path.isdir(profile_dir):
    for fname in sorted(os.listdir(profile_dir)):
        if not fname.endswith(".json"):
            continue
        ppath = os.path.join(profile_dir, fname)
        pservers = _load(ppath, json.load, f"profile {fname}").get("mcpServers") or {}
        if not pservers:
            bad.append(f"profile {fname}: declares no servers")
        profile_count += 1
        for name, spec in sorted(pservers.items()):
            judge(name, spec, profile_allowed, f"profile {fname}: ")

# Vacuity guard. Once the default roster is legitimately EMPTY, "roster subset-of
# approved" is satisfied by a tree in which NO MCP server is reachable at all --
# delete configs/toolbroker/mcp-profiles/ and this check goes green while the
# entire capability surface has silently vanished. Verified: before this guard,
# removing the profile directory gave gate=0 and audit=ok.
#
# Conditioned on the roster being empty so that older trees, which carry servers
# in .mcp.json and no profiles at all, are unaffected.
if not servers and profile_count == 0:
    bad.append(
        "default roster is empty AND no profiles exist, so no MCP server is reachable at all "
        f"(expected profile files in {profile_dir})"
    )

if bad:
    sys.exit("mcp-roster: " + "; ".join(bad))
print(f"mcp-roster: {len(servers)} server(s) approved; {profile_count} profile(s) checked")
PY
}

# Standalone mode so the check is testable without the full gate. Arg 3 names
# the mirror to compare against: a probe roster written to a scratch directory
# ALWAYS disagrees with the repo's tools/mcp-servers.json, so a probe that means
# to exercise the approval or ${VAR} logic must say which mirror it is asserting
# a premise about -- otherwise the premise assertion refuses first and the probe
# proves nothing while still exiting non-zero.
if [[ "${1:-}" == "--check-mcp-roster" ]]; then
  check_mcp_roster "${2:-}" "${3:-}"
  exit $?
fi

# A tree with NEITHER .mcp.json NOR tools/mcp-servers.json configures no MCP
# servers at all, so there is nothing that can re-accrete -- skip rather than
# fail a tree the check does not apply to. A roster that EXISTS without configs/mcp-approved.yaml
# still FAILS: that is the control itself being removed, which is the exact
# thing this gate is here to catch. The standalone --check-mcp-roster mode above
# is unaffected -- naming a roster explicitly and having it be absent is an
# error, not a skip.
if [[ -f .mcp.json || -f tools/mcp-servers.json ]]; then
  if ! check_mcp_roster >/dev/null; then FAILED+=("mcp roster not a subset of configs/mcp-approved.yaml"); fi
fi

ruff_baseline=$(tr -d '[:space:]' < scripts/gates/ruff-baseline.txt)
if "$python_bin" -m ruff --version >/dev/null 2>&1; then
  # Count FINDINGS via the JSON report, never output lines: a CLEAN run prints
  # "All checks passed!" — one line — so the old line-count failed the gate
  # exactly when the tree finally went clean. Exit status 0 and Ruff's
  # diagnostic-bearing status 1 are countable; tool-error statuses and
  # unparseable output fail CLOSED.
  ruff_json=$("$python_bin" -m ruff check . --output-format json 2>/dev/null) \
    && ruff_rc=0 || ruff_rc=$?
  if (( ruff_rc > 1 )); then
    ruff_count=999999
  else
    ruff_count=$(printf '%s' "$ruff_json" \
      | "$python_bin" -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null) \
      || ruff_count=999999
  fi
  if (( ruff_count > ruff_baseline )); then FAILED+=("ruff check ($ruff_count > $ruff_baseline)"); fi
else
  echo "FAIL: ruff is not importable by $python_bin (required tool missing)" >&2
  ruff_count=999999
  FAILED+=("ruff not importable by $python_bin (required tool missing)")
fi
mypy_baseline=$(tr -d '[:space:]' < scripts/gates/mypy-baseline.txt)
if "$python_bin" -m mypy --version >/dev/null 2>&1; then
  # Count only actual error diagnostics (": error:" lines) — notes and other
  # output lines must not inflate the count. A mypy crash or usage error
  # (exit code > 1) fails CLOSED instead of reporting a bogus zero.
  mypy_output=$("$python_bin" -m mypy omniagentos --no-error-summary 2>&1) \
    && mypy_rc=0 || mypy_rc=$?
  if (( mypy_rc > 1 )); then
    mypy_count=999999
  else
    mypy_count=$(printf '%s\n' "$mypy_output" | grep -c ': error:' || true)
  fi
  if (( mypy_count > mypy_baseline )); then FAILED+=("mypy check ($mypy_count > $mypy_baseline)"); fi
else
  echo "FAIL: mypy is not importable by $python_bin (required tool missing)" >&2
  mypy_count=999999
  FAILED+=("mypy not importable by $python_bin (required tool missing)")
fi

if ! "$python_bin" -m pytest \
  tests/certification tests/wiring tests/swarm/test_spawn_integrations.py -q \
  >"$PYTEST_OUTPUT" 2>&1; then
  FAILED+=("pytest quick set")
fi

if ((${#FAILED[@]})); then
  echo "FAILED"
  for gate in "${FAILED[@]}"; do echo "- $gate"; done
  if [[ " ${FAILED[*]} " == *" pytest quick set "* ]]; then
    tail -n 20 "$PYTEST_OUTPUT"
  fi
  exit 1
fi
echo "PASSED: ruff=$ruff_count mypy=$mypy_count pytest=ok"
