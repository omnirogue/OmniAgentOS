#!/usr/bin/env bash
# Acceptance: the ambient environment every CLI starts in (S12/S19).
#
# Asserts the two halves of the determinism guarantee:
#   A. ~/.zshenv is the ONE place four ambient variables are defined, and the
#      git ceiling really stops sessions below $HOME from resolving ~/.git.
#   B. the roster the runtime LOADS (.mcp.json) is a pruned SUBSET of a reviewed
#      approved list, enforced by a gate that actually fails when the roster
#      re-accretes -- and tools/mcp-servers.json, while it still exists as a
#      second copy, agrees with it.
#
# `set -u` (not -e): every check runs so a single failure does not hide the
# rest. Exit 0 only on a full pass.
set -u

# Defaults unchanged: on the acceptance machine these resolve exactly as before.
# The overrides exist so section 7 -- the "gate ACTUALLY fails" probes -- can be
# run against another checkout. Until now the hardcoded REPO made this whole
# file exit at preflight anywhere else, which is how 7a and 7b could quietly
# become tautologies: nobody outside one Mac was able to run them and look.
HOME_DIR=${S12_HOME_DIR:-/Users/youruser}
REPO=${S12_REPO:-/Users/youruser/OmniAgentOS}
ZSHENV="$HOME_DIR/.zshenv"
EXPECT_DB="$REPO/var/runtime/state.sqlite3"
EXPECT_PP="$REPO"

PASS=0
FAIL=0
PROBE_DIR=""
TMP_DIR=""

cleanup() {
  [ -n "$PROBE_DIR" ] && [ -d "$PROBE_DIR" ] && rm -rf "$PROBE_DIR"
  [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR"
  return 0
}
trap cleanup EXIT INT TERM

ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; [ $# -gt 1 ] && printf '        %s\n' "$2"; return 0; }

# --- preflight: fail fast and loudly on anything we depend on ---------------
preflight_fail() { printf 'PREFLIGHT FAIL: %s\n' "$1" >&2; exit 1; }
[ -d "$REPO" ]        || preflight_fail "repo not found: $REPO"
[ -r "$ZSHENV" ]      || preflight_fail "not readable: $ZSHENV"
[ -x /bin/zsh ]       || preflight_fail "/bin/zsh is not executable"
command -v git >/dev/null 2>&1 || preflight_fail "git not on PATH"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ]          || preflight_fail "repo interpreter missing: $PY"
cd "$REPO"            || preflight_fail "cannot cd to $REPO"

PROBE_DIR=$(mktemp -d "$HOME_DIR/.s12probe.XXXXXX") || preflight_fail "cannot create probe dir under \$HOME"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/s12tmp.XXXXXX") || preflight_fail "cannot create temp dir"

printf '=== S12/S19 ambient environment acceptance ===\n\n'

# ---------------------------------------------------------------------------
# 1r. THE CEILING WORKS the way an agent-launched CLI actually starts.
#     `env -i HOME=... /bin/zsh -c` is the real shape: a stripped environment
#     where the ONLY thing that can set the ceiling is ~/.zshenv itself.
# ---------------------------------------------------------------------------
out_1r=$(env -i HOME="$HOME_DIR" /bin/zsh -c "cd '$PROBE_DIR' && git rev-parse --show-toplevel" 2>&1)
case "$out_1r" in
  *"not a git repository"*) ok "1r ceiling: a dir under \$HOME resolves no repo in a stripped zsh" ;;
  *) bad "1r ceiling: expected 'not a git repository'" "got: $out_1r" ;;
esac

# ---------------------------------------------------------------------------
# 2r. NON-VACUITY. The same probe with the ceiling explicitly unset must STILL
#     find /Users/youruser. Without this, 1r starts passing for free the day
#     someone deletes ~/.git by hand, and the test stops meaning anything.
# ---------------------------------------------------------------------------
out_2r=$(env -i HOME="$HOME_DIR" /bin/zsh -c \
  "unset GIT_CEILING_DIRECTORIES; cd '$PROBE_DIR' && git rev-parse --show-toplevel" 2>&1)
if [ "$out_2r" = "$HOME_DIR" ]; then
  ok "2r non-vacuity: without the ceiling the same probe still resolves $HOME_DIR"
else
  bad "2r non-vacuity: expected '$HOME_DIR' (the ambient repo must still exist)" "got: $out_2r"
fi

# ---------------------------------------------------------------------------
# 3. All three vars present with CORRECT values in a fresh non-interactive zsh,
#    and OMNIAGENTOS_DB absolute.
# ---------------------------------------------------------------------------
vals=$(env -i HOME="$HOME_DIR" /bin/zsh -c 'printf "%s\n%s\n%s\n" "$OMNIAGENTOS_DB" "$PYTHONPATH" "$SSL_CERT_FILE"' 2>&1)
got_db=$(printf '%s\n' "$vals" | sed -n '1p')
got_pp=$(printf '%s\n' "$vals" | sed -n '2p')
got_ssl=$(printf '%s\n' "$vals" | sed -n '3p')

if [ "$got_db" = "$EXPECT_DB" ]; then
  ok "3a OMNIAGENTOS_DB matches the launchd/launch-env value"
else
  bad "3a OMNIAGENTOS_DB expected '$EXPECT_DB'" "got: '$got_db'"
fi
case "$got_db" in
  /*) ok "3b OMNIAGENTOS_DB is absolute" ;;
  *)  bad "3b OMNIAGENTOS_DB is not absolute" "got: '$got_db'" ;;
esac
case ":$got_pp:" in
  *":$EXPECT_PP:"*) ok "3c PYTHONPATH contains the repo root" ;;
  *) bad "3c PYTHONPATH does not contain '$EXPECT_PP'" "got: '$got_pp'" ;;
esac
if [ -n "$got_ssl" ] && [ -r "$got_ssl" ]; then
  ok "3d SSL_CERT_FILE is set and readable"
else
  bad "3d SSL_CERT_FILE unset or unreadable (a dangling bundle breaks TLS)" "got: '$got_ssl'"
fi

# ---------------------------------------------------------------------------
# 4. ~/.zshenv parses, and a backup exists so the edit is one command to undo.
# ---------------------------------------------------------------------------
if zsh -n "$ZSHENV" 2>/dev/null; then
  ok "4a zsh -n $ZSHENV exits 0"
else
  bad "4a zsh -n $ZSHENV did not exit 0"
fi
bak_count=$(find "$HOME_DIR" -maxdepth 1 -name '.zshenv.bak.*' -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "${bak_count:-0}" -ge 1 ]; then
  ok "4b a ~/.zshenv.bak.* rollback copy exists ($bak_count found)"
else
  bad "4b no ~/.zshenv.bak.* backup found"
fi

# ---------------------------------------------------------------------------
# 5. The OWNED artifact is .mcp.json -- the file the runtime LOADS -- and while
#    tools/mcp-servers.json still exists as a second copy the two must agree.
#
#    This asserted `readlink .mcp.json = tools/mcp-servers.json` until 2026-08-07.
#    00000000 (2026-08-02) deliberately replaced that symlink with a regular file
#    ("make certification offline and path-local"), so the step had been taking
#    its `bad` branch on every run since -- red for a premise the owner retired,
#    which is noise, not protection. Worse, once the symlink was gone this shape
#    could no longer detect the thing that actually matters: the two files
#    drifting apart (they now hold 11 servers vs 2). Assert THAT instead, which
#    is what scripts/gates/mech_gate.sh --check-mcp-roster and the mcp_roster
#    audit already refuse on. Do not restore the readlink form without first
#    restoring the symlink.
# ---------------------------------------------------------------------------
if [ ! -f "$REPO/.mcp.json" ]; then
  bad "5a .mcp.json (the loaded roster) is missing" "expected a tracked file at $REPO/.mcp.json"
elif [ -L "$REPO/.mcp.json" ]; then
  ok "5a .mcp.json resolves to a roster (symlink -> $(readlink "$REPO/.mcp.json"))"
else
  ok "5a .mcp.json is present as the loaded roster"
fi

if [ ! -f "$REPO/tools/mcp-servers.json" ]; then
  ok "5b no second roster copy to disagree with .mcp.json"
elif cmp -s "$REPO/.mcp.json" "$REPO/tools/mcp-servers.json"; then
  ok "5b .mcp.json and tools/mcp-servers.json agree"
else
  bad "5b .mcp.json and tools/mcp-servers.json disagree -- the reviewed roster is not the loaded one" \
    "$(printf 'loaded=%s server(s) mirror=%s server(s); edit both identically until the owner picks one' \
      "$(grep -c '"command"' "$REPO/.mcp.json" 2>/dev/null || echo '?')" \
      "$(grep -c '"command"' "$REPO/tools/mcp-servers.json" 2>/dev/null || echo '?')")"
fi

# ---------------------------------------------------------------------------
# 6. Roster is pruned AND the approved list is non-trivial: every server is
#    approved WITH a justification, and no ${VAR} resolves to empty.
#
#    Reads .mcp.json -- the file the runtime LOADS. This read tools/mcp-servers.json
#    until 2026-08-07, on step 5's premise that the two were the same file. Step 5
#    has been failing since 00000000 (2026-08-02) broke exactly that, and this
#    check went on approving the 2-server mirror while 11 servers shipped.
# ---------------------------------------------------------------------------
subset_out=$("$PY" - "$REPO/.mcp.json" "$REPO/configs/mcp-approved.yaml" <<'PY' 2>&1
import json, os, re, sys
roster_path, approved_path = sys.argv[1], sys.argv[2]
try:
    import yaml
except ImportError:
    sys.exit("pyyaml not importable")
servers = (json.load(open(roster_path)) or {}).get("mcpServers") or {}
approved = (yaml.safe_load(open(approved_path)) or {}).get("approved") or {}
problems = []
# An EMPTY default roster is the intended state as of 2026-08-13: it loads in
# every session, so it holds nothing that cannot justify that cost on every
# launch, and nothing currently clears that bar. `approved` is empty for the
# same reason. Asserting either was non-empty encoded the older assumption and
# started failing the moment the trim landed.
#
# The non-emptiness requirement MOVES to the profiles, which is where servers
# actually live now -- otherwise this check goes vacuous and a tree with no
# reachable server at all would pass.
profile_dir = os.path.join(os.path.dirname(approved_path), "toolbroker", "mcp-profiles")
if not os.path.isdir(profile_dir):
    problems.append("no profile directory: no MCP server is reachable at all")
else:
    profiles = sorted(f for f in os.listdir(profile_dir) if f.endswith(".json"))
    if not profiles:
        problems.append("profile directory declares no profiles")
    profile_approved = (yaml.safe_load(open(approved_path)) or {}).get("profile_approved") or {}
    allowed = dict(approved)
    allowed.update(profile_approved)
    for fname in profiles:
        pservers = (json.load(open(os.path.join(profile_dir, fname))) or {}).get("mcpServers") or {}
        if not pservers:
            problems.append(f"profile {fname}: declares no servers")
        for pname, pspec in sorted(pservers.items()):
            entry = allowed.get(pname)
            if entry is None:
                problems.append(f"profile {fname}: {pname}: not approved")
                continue
            if len(str((entry or {}).get("justification") or "").strip()) < 20:
                problems.append(f"profile {fname}: {pname}: justification missing or trivial")
        for dead in ("tavily", "brave-search"):
            if dead in pservers:
                problems.append(f"profile {fname}: {dead}: dead-by-configuration server is back")
for name, spec in sorted(servers.items()):
    entry = approved.get(name)
    if entry is None:
        problems.append(f"{name}: not approved")
        continue
    just = str((entry or {}).get("justification") or "").strip()
    if len(just) < 20:
        problems.append(f"{name}: justification missing or trivial")
    for var in sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(spec)))):
        if not os.environ.get(var, "").strip():
            problems.append(f"{name}: ${{{var}}} resolves to empty")
for dead in ("tavily", "brave-search"):
    if dead in servers:
        problems.append(f"{dead}: dead-by-configuration server is back in the roster")
if problems:
    sys.exit("; ".join(problems))
print(f"{len(servers)} default server(s) + profiles, all approved with justifications")
PY
); rc_subset=$?
if [ $rc_subset -eq 0 ]; then
  ok "6 roster is a justified subset of configs/mcp-approved.yaml ($subset_out)"
else
  bad "6 roster/approved-list check failed" "$subset_out"
fi

# ---------------------------------------------------------------------------
# 7. THE RE-ACCRETION GATE ACTUALLY FAILS. A gate that cannot be shown to fail
#    is not a gate. Inject into a COPY, assert non-zero, restore, assert zero.
# ---------------------------------------------------------------------------
GATE="$REPO/scripts/gates/mech_gate.sh"
if [ ! -x "$GATE" ] && [ ! -r "$GATE" ]; then
  bad "7 mech_gate.sh not found at $GATE"
else
  bad_roster="$TMP_DIR/roster_unapproved.json"
  var_roster="$TMP_DIR/roster_emptyvar.json"
  # The probe fixtures are SYNTHETIC, not derived from the repo's roster.
  #
  # They used to be built by copying tools/mcp-servers.json and mutating its
  # first server: `name = sorted(d2["mcpServers"])[0]`. That raises IndexError
  # the moment the roster is empty, so the fixture was never written and 7b/7d
  # failed with "roster not found" -- reporting a gate defect when the real
  # cause was the probe having nothing to copy. The default roster became empty
  # by design on 2026-08-13, which is what exposed it.
  #
  # These checks exercise the GATE, so they must not depend on the roster's
  # contents at all. A probe whose fixture comes from the thing under test can
  # only ever be as trustworthy as that thing.
  "$PY" - "$bad_roster" "$var_roster" <<'PY'
import json, sys
unapproved, emptyvar = sys.argv[1], sys.argv[2]
json.dump(
    {"mcpServers": {"s12-unapproved-probe": {"command": "true", "args": []}}},
    open(unapproved, "w"),
    indent=2,
)
json.dump(
    {
        "mcpServers": {
            "s12-emptyvar-probe": {
                "command": "true",
                "args": [],
                "env": {"S12_PROBE_KEY": "${S12_DEFINITELY_UNSET_KEY}"},
            }
        }
    },
    open(emptyvar, "w"),
    indent=2,
)
PY

  # 7a and 7b name the probe file as BOTH roster and mirror, and assert the
  # refusal REASON, not just a non-zero status. A probe roster in $TMP_DIR always
  # disagrees with the repo's tools/mcp-servers.json, so with the mirror left
  # implicit the divergence assertion refuses FIRST and these two exit non-zero
  # without ever reaching the approval or ${VAR} logic they exist to prove --
  # passing while demonstrating nothing. rc alone cannot tell those apart.
  gate_out=$(bash "$GATE" --check-mcp-roster "$bad_roster" "$bad_roster" 2>&1); rc_bad=$?
  case "$gate_out" in
    *"s12-unapproved-probe: absent from"*) probe_reason=ok ;;
    *) probe_reason=no ;;
  esac
  if [ $rc_bad -ne 0 ] && [ "$probe_reason" = ok ]; then
    ok "7a gate REFUSES an unapproved server, naming it (rc=$rc_bad)"
  elif [ $rc_bad -ne 0 ]; then
    bad "7a gate refused, but NOT for the unapproved server -- probe proves nothing" "$gate_out"
  else
    bad "7a gate ACCEPTED an unapproved server -- the gate does not gate" "$gate_out"
  fi

  gate_out=$(env -u S12_DEFINITELY_UNSET_KEY bash "$GATE" --check-mcp-roster "$var_roster" "$var_roster" 2>&1); rc_var=$?
  case "$gate_out" in
    *'${S12_DEFINITELY_UNSET_KEY} resolves to empty'*) probe_reason=ok ;;
    *) probe_reason=no ;;
  esac
  if [ $rc_var -ne 0 ] && [ "$probe_reason" = ok ]; then
    ok "7b gate REFUSES a \${VAR} that resolves to empty, naming it (rc=$rc_var)"
  elif [ $rc_var -ne 0 ]; then
    bad "7b gate refused, but NOT for the empty \${VAR} -- probe proves nothing" "$gate_out"
  else
    bad "7b gate ACCEPTED an empty-resolving \${VAR}" "$gate_out"
  fi

  gate_out=$(bash "$GATE" --check-mcp-roster 2>&1); rc_good=$?
  if [ $rc_good -eq 0 ]; then
    ok "7c gate PASSES the restored real roster (rc=0)"
  else
    bad "7c gate refused the real roster -- it fails closed on a clean tree" "$gate_out"
  fi

  # 7d proves the divergence assertion itself can fail. It is the newest half of
  # the control and the only one no other probe exercises: 7a/7b now suppress it
  # by naming their own mirror, and 7c only shows the composite verdict.
  gate_out=$(bash "$GATE" --check-mcp-roster "$bad_roster" "$var_roster" 2>&1); rc_div=$?
  case "$gate_out" in
    *disagree*) probe_reason=ok ;;
    *) probe_reason=no ;;
  esac
  if [ $rc_div -ne 0 ] && [ "$probe_reason" = ok ]; then
    ok "7d gate REFUSES two rosters that disagree (rc=$rc_div)"
  else
    bad "7d gate did not refuse a divergent roster pair -- the premise is unasserted" "$gate_out"
  fi
fi

# ---------------------------------------------------------------------------
# 8. The Gemini crew's skill path resolves to a real directory.
# ---------------------------------------------------------------------------
SKILL_LINK=/Users/youruser/.gemini/skills/scraper
if [ -d "$SKILL_LINK/" ]; then
  ok "8 $SKILL_LINK resolves to an existing directory"
else
  bad "8 $SKILL_LINK does not resolve to a directory"
fi

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
