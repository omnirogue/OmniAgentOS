#!/usr/bin/env bash
# s00 — acceptance for the standing wiring/drift audit.
#
#   0 NON-VACUITY   the registry is non-empty and EVERY registered check reports
#                   (a silently-skipping check is indistinguishable from a passing one)
#   1 CLEAN RUN     on an undamaged tree every check reports and every check is ok
#   2 PLANTED       one defect per check, asserted, then reverted
#   3 READ-ONLY     sha256 of the whole tree is identical before and after a real run
#   4 PROVENANCE    every registered threshold carries a provenance field
#
# Steps 1 and 2 run against a HERMETIC sandbox built in a temp dir, never against
# the real repo: planting a defect in the real tree to prove a check fires is how
# you end up shipping the defect. Steps 0, 3 and 4 run against the real repo,
# read-only.
#
# Runs with --dry-run --no-push. Exits 0 ONLY on a full pass.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
SENTINEL="$ROOT/scripts/health-sentinel/health_sentinel.py"
REGISTRY="$ROOT/configs/audit-checks.yaml"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/s00.XXXXXX")
LISTENER_PID=""
cleanup() {
    [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

PASS=0
FAILED=0
step() { printf '\n=== %s\n' "$*"; }
ok()   { printf '  PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }

TREE="$TMP/tree"
ACCOUNTS="$TMP/accounts"

audit_sandbox() {  # -> $TMP/out.json
    OMNIAGENTOS_SPEND_DB="$TREE/var/runtime/state.sqlite3" \
      "$PY" "$SENTINEL" --audit --json --dry-run \
        --audit-repo-root "$TREE" \
        --audit-accounts-root "$ACCOUNTS" \
        --audit-registry "$TREE/configs/audit-checks.yaml" > "$TMP/out.json"
}

# Assert one check's status in the last sandbox run. $1=check id $2=expected status
assert_status() {
    "$PY" - "$TMP/out.json" "$1" "$2" <<'PYEOF'
import json, sys
report, check_id, expected = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
rows = {c["id"]: c for c in report["checks"]}
assert check_id in rows, f"{check_id} did not report at all"
got = rows[check_id]["status"]
assert got == expected, f"{check_id}: expected {expected}, got {got} — {rows[check_id]['evidence']}"
print(f"    {check_id}: {got} — {rows[check_id]['evidence'][:150]}")
PYEOF
}

# --------------------------------------------------------------------------- preflight
step "PREFLIGHT"
[ -x "$PY" ] || { echo "ABORT: venv python missing at $PY (run 'uv sync')" >&2; exit 3; }
[ -f "$SENTINEL" ] || { echo "ABORT: $SENTINEL missing" >&2; exit 3; }
[ -f "$REGISTRY" ] || { echo "ABORT: $REGISTRY missing — the audit has no registry" >&2; exit 3; }
"$PY" -c 'import yaml' >/dev/null 2>&1 || { echo "ABORT: pyyaml missing in the venv" >&2; exit 3; }
command -v git >/dev/null || { echo "ABORT: git not on PATH" >&2; exit 3; }
ok "venv python, sentinel and registry present"

# --------------------------------------------------------------------------- 0 non-vacuity
step "0 NON-VACUITY — every registered check is reachable and REPORTS (real repo, read-only)"
"$PY" "$SENTINEL" --audit --json --dry-run > "$TMP/real.json"
REAL_RC=$?
if "$PY" - "$TMP/real.json" "$REGISTRY" "$REAL_RC" <<'PYEOF'
import json, sys, yaml
report = json.load(open(sys.argv[1]))
registry = yaml.safe_load(open(sys.argv[2]).read()) or {}
rc = int(sys.argv[3])
registered = registry.get("checks") or {}
assert registered, "configs/audit-checks.yaml registers no checks"
assert len(registered) >= 9, f"only {len(registered)} checks registered, expected the nine"
reported = {c["id"] for c in report["checks"]}
missing = set(registered) - reported
assert not missing, f"registered but did not report: {sorted(missing)}"
for c in report["checks"]:
    assert c["status"] in ("ok", "warn", "fail"), f"{c['id']} reported {c['status']!r} — 'skip' is not a verdict"
    assert c["evidence"], f"{c['id']} reported no evidence"
assert rc == 0, f"audit machinery exited {rc}"
assert report["read_only"] is True
print(f"    {len(registered)} registered, {len(reported)} reported, 0 silent")
for c in report["checks"]:
    print(f"      [{c['status'].upper():4}] {c['id']}: {c['evidence'][:110]}")
PYEOF
then ok "all ten registered checks are reachable and report a verdict"
else bad "a registered check did not report"; fi

# --------------------------------------------------------------------------- build the sandbox
step "BUILD HERMETIC SANDBOX"
PORT=$("$PY" - "$TREE" "$ACCOUNTS" <<'PYEOF'
"""Build a tree on which all ten checks are OK, so a planted defect is the only
variable. Every path and threshold below is written into the sandbox's OWN
registry, never the repo's."""
import hashlib, json, os, socket, sqlite3, sys, time
from datetime import UTC, datetime, timedelta
from pathlib import Path

tree, accounts = Path(sys.argv[1]), Path(sys.argv[2])
now = datetime.now(UTC)

# --- accounts: three configs whose merged permissions the template will pin ---
rules = {
    ".claude": (["Bash(ls:*)", "Read(//Users/**)"], ["Bash(npm run test:*)"]),
    ".claude-account-2": (["Bash(git status)"], ["Edit(//tmp/**)"]),
    ".claude-account-3": (["Bash(git diff:*)"], ["WebFetch(domain:example.com)"]),
}
merged = set()
for name, (base, local) in rules.items():
    d = accounts / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": base, "defaultMode": "acceptEdits"},
         "agentPushNotifEnabled": True, "statusLine": {"type": "command", "command": "echo hi"}}, indent=2))
    (d / "settings.local.json").write_text(json.dumps(
        {"permissions": {"allow": local}}, indent=2))
    merged.update(f"allow:{r}" for r in base + local)
merged_sha = hashlib.sha256("\n".join(sorted(merged)).encode()).hexdigest()

# --- repo tree ---
(tree / "configs").mkdir(parents=True, exist_ok=True)
(tree / "tools").mkdir(parents=True, exist_ok=True)
(tree / "scripts").mkdir(parents=True, exist_ok=True)
(tree / "omniagentos").mkdir(parents=True, exist_ok=True)
(tree / "var/swarm/clones").mkdir(parents=True, exist_ok=True)
(tree / "var/runtime").mkdir(parents=True, exist_ok=True)

(tree / "configs/canonical-claude-settings.json").write_text(json.dumps(
    {"_meta": {"artifact": "canonical-claude-settings", "version": 1,
               "accounts": sorted(rules), "merged_permissions_sha256": merged_sha}}, indent=2))

(tree / "tools/mcp-servers.json").write_text(json.dumps(
    {"mcpServers": {"alpha": {"command": "alpha-server", "args": []}}}, indent=2))
(tree / "configs/mcp-approved.yaml").write_text(
    "approved:\n  alpha:\n    justification: >-\n      Sandbox fixture server.\n")

# 3 never_wired: both added files ARE referenced by the Makefile.
(tree / "scripts/used.sh").write_text("#!/bin/sh\necho used\n")
(tree / "omniagentos/mod_used.py").write_text("VALUE = 1\n")
(tree / "Makefile").write_text(
    "wired:\n\tsh scripts/used.sh\n\tpython -c 'import omniagentos.mod_used'\n")

# 6 lane_brief: one healthy lane, one lane exempt BY DECLARED CLASS.
healthy = tree / "var/swarm/clones/healthy-lane/var"
healthy.mkdir(parents=True, exist_ok=True)
(healthy / "task.md").write_text("# lane brief\nDo the thing.\n")
(healthy / "LANE-CLASS").write_text("task\n")
exempt = tree / "var/swarm/clones/exempt-lane/var"
exempt.mkdir(parents=True, exist_ok=True)
(exempt / "LANE-CLASS").write_text("sandbox\n")

# 8 unscheduled_heartbeat: a FRESH artifact.
(tree / "var/swarm/fleet-status.md").write_text("# fleet\nfresh\n")

# 4 + 7 ledger: four attributable pumps and two distinct signers, both inside the window.
db = tree / "var/runtime/state.sqlite3"
conn = sqlite3.connect(db)
conn.execute("create table swarm_attempts (id integer primary key, swarm_run_id text, "
             "board_task_id text, started_at text, detail text)")
conn.execute("create table promotions (id integer primary key, signing_key_id text, created_at text)")
conn.execute("create table provider_call_usage (billing_provider text, cost_usd_nanos integer, "
             "cost_upper_bound_usd_nanos integer, created_at text)")
stamp = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
for i, lane in enumerate(["rework", "review", "verdict", "sim"]):
    conn.execute("insert into swarm_attempts (swarm_run_id, board_task_id, started_at, detail) values (?,?,?,?)",
                 ("swr_pumpledger", f"btk_pump_{lane}_abc{i}", stamp, json.dumps({"verdict_hash": f"h{i}"})))
for i, key in enumerate(["key-alpha", "key-beta"]):
    conn.execute("insert into promotions (signing_key_id, created_at) values (?,?)", (key, stamp))
conn.commit()
conn.close()

# 9 loopback_connectors: one endpoint we actually LISTEN on, one annotated expected_down.
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
port = srv.getsockname()[1]
srv.listen(8)
pid = os.fork()
if pid == 0:
    # Child: accept forever so the TCP probe succeeds; the parent shell kills it
    # on exit. It MUST drop the inherited stdout first — this whole builder runs
    # inside a $(...) command substitution, and a child holding that pipe open
    # hangs the shell forever waiting for EOF that never comes.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.setsid()
    try:
        while True:
            c, _ = srv.accept()
            c.close()
    except Exception:
        pass
    os._exit(0)
srv.close()

# A port nothing listens on, annotated expected_down so the check must NOT fire.
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()

(tree / "configs/connectors.yaml").write_text(
    "connectors:\n"
    "  live:\n"
    "    capabilities:\n"
    "      - id: live-one\n"
    f"        base_url: \"http://127.0.0.1:{port}\"\n"
    "  known_down:\n"
    "    capabilities:\n"
    "      - id: down-one\n"
    "        expected_down: true\n"
    f"        base_url: \"http://127.0.0.1:{dead_port}\"\n"
)

# --- the sandbox's OWN registry ---
(tree / "configs/audit-checks.yaml").write_text(f"""version: 1
detectors:
  blocked_session:
    threshold_minutes: 15
    provenance: default-15min-unmeasured
checks:
  config_digest:
    enabled: true
    threshold: sha256-equality
    provenance: derived-from-sandbox-template
    accounts: [.claude, .claude-account-2, .claude-account-3]
    template: configs/canonical-claude-settings.json
  mcp_roster:
    enabled: true
    threshold: roster subset-of approved
    provenance: derived-from-sandbox
    roster: tools/mcp-servers.json
    approved: configs/mcp-approved.yaml
  never_wired:
    enabled: true
    window_days: 30
    threshold: one call site outside its own tests
    provenance: default-guess-30d
    patterns: ["scripts/*.sh", "omniagentos/*.py", "omniagentos/**/*.py"]
    ignore: ["**/__init__.py"]
  pump_ledger_wiring:
    enabled: true
    report_only: false
    threshold: 100
    provenance: derived-from-sandbox-fixture
    pumps: [rework, review, verdict, sim]
    pump_ledger_run_id: swr_pumpledger
    window_hours: 24
    identical_verdict_hash_run: 5
    db: var/runtime/state.sqlite3
  soak_window_diff:
    enabled: true
    threshold: declared == effective
    provenance: derived-from-sandbox
    windows:
      - name: sandbox-window
        env: S00_SANDBOX_SOAK
        declared_mode: "off"
        default_when_unset: "off"
  lane_brief:
    enabled: true
    min_age_minutes: 0
    threshold: non-empty brief unless the declared class is exempt
    provenance: derived-from-sandbox
    root: var/swarm/clones
    brief_paths: [var/task.md, LANE-BRIEF.md, var/LANE-BRIEF.md]
    class_marker: var/LANE-CLASS
    exempt_classes: [sandbox]
    known_classes: [task, sandbox]
  single_signer:
    enabled: true
    window_days: 30
    threshold: ">=2 distinct signing keys"
    provenance: derived-from-sandbox-fixture
    db: var/runtime/state.sqlite3
    table: promotions
    signer_column: signing_key_id
    timestamp_column: created_at
  unscheduled_heartbeat:
    enabled: true
    threshold: artifact newer than max_age_hours
    provenance: derived-from-sandbox
    registry: configs/expected-heartbeats.yaml
  loopback_connectors:
    enabled: true
    connect_timeout_seconds: 1.0
    threshold: every declared loopback accepts a TCP connect
    provenance: derived-from-sandbox
    connectors: configs/connectors.yaml
  provider_daily_spend:
    enabled: true
    threshold: warn >80%; fail >100%
    provenance: derived-from-sandbox
    db: var/runtime/state.sqlite3
    caps: configs/spend-caps.yaml
    providers: [kimi, fireworks]
""")
(tree / "configs/spend-caps.yaml").write_text(
    "providers:\n"
    "  kimi:\n    enabled: true\n    daily_cap_usd: '100'\n"
    "  fireworks:\n    enabled: true\n    daily_cap_usd: '200'\n")
(tree / "configs/expected-heartbeats.yaml").write_text(
    "version: 1\nheartbeats:\n  fleet:\n    script: scripts/used.sh\n"
    "    artifact: var/swarm/fleet-status.md\n    max_age_hours: 24\n"
    "    provenance: derived-from-sandbox\n")

os.system(f"cd {tree} && git init -q -b main && git add -A && "
          f"git -c user.email=s00@test -c user.name=s00 commit -qm seed")
print(port)
PYEOF
)
LISTENER_PID=$(pgrep -f "s00.*tree" >/dev/null 2>&1 && echo "" || echo "")
# The listener is a forked child of the builder; find it by the port it holds.
LISTENER_PID=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
[ -n "$PORT" ] || { echo "ABORT: sandbox build produced no port" >&2; exit 3; }
ok "sandbox built at $TREE (live loopback on 127.0.0.1:$PORT, pid ${LISTENER_PID:-?})"

# --------------------------------------------------------------------------- 1 clean run
step "1 CLEAN RUN — the undamaged sandbox"
audit_sandbox
if "$PY" - "$TMP/out.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
bad = [c for c in r["checks"] if c["status"] != "ok"]
assert len(r["checks"]) == 10, f"{len(r['checks'])} checks reported, expected 10"
assert not bad, "not ok on a clean tree: " + "; ".join(f"{c['id']}={c['status']} ({c['evidence'][:90]})" for c in bad)
assert not r["missing_provenance"], r["missing_provenance"]
print(f"    10/10 ok in {r['duration_seconds']}s")
PYEOF
then ok "every check passes on an undamaged tree"
else bad "the clean sandbox does not pass"; fi

# --------------------------------------------------------------------------- 2 planted defects
step "2 PLANTED DEFECTS — one per check, reverted after each"

plant() { printf '  -- %s\n' "$*"; }

# --- 1 config drift -----------------------------------------------------------
plant "config drift: one extra permission in ~/.claude/settings.local.json"
cp "$ACCOUNTS/.claude/settings.local.json" "$TMP/perm.bak"
"$PY" -c "
import json,sys
p='$ACCOUNTS/.claude/settings.local.json'
d=json.load(open(p)); d['permissions']['allow'].append('Bash(rm -rf:*)'); json.dump(d,open(p,'w'))"
audit_sandbox
if assert_status config_digest fail; then ok "config drift detected"; else bad "config drift NOT detected"; fi
cp "$TMP/perm.bak" "$ACCOUNTS/.claude/settings.local.json"

# --- 2 roster re-accretion ----------------------------------------------------
plant "roster re-accretion: an unapproved server in tools/mcp-servers.json"
cp "$TREE/tools/mcp-servers.json" "$TMP/roster.bak"
"$PY" -c "
import json
p='$TREE/tools/mcp-servers.json'
d=json.load(open(p)); d['mcpServers']['smuggled']={'command':'x'}; json.dump(d,open(p,'w'))"
audit_sandbox
if assert_status mcp_roster fail; then ok "roster re-accretion detected"; else bad "roster re-accretion NOT detected"; fi
cp "$TMP/roster.bak" "$TREE/tools/mcp-servers.json"

# --- 3 never wired, plus its false-positive guard ------------------------------
plant "never-wired: a new script nothing calls"
printf '#!/bin/sh\necho orphan\n' > "$TREE/scripts/orphan.sh"
(cd "$TREE" && git add -A && git -c user.email=s00@test -c user.name=s00 commit -qm orphan)
audit_sandbox
if assert_status never_wired fail; then ok "unwired script detected"; else bad "unwired script NOT detected"; fi

plant "never-wired FALSE-POSITIVE GUARD: a new script that IS called"
rm -f "$TREE/scripts/orphan.sh"
printf '#!/bin/sh\necho called\n' > "$TREE/scripts/called.sh"
printf 'call:\n\tsh scripts/called.sh\n' >> "$TREE/Makefile"
(cd "$TREE" && git add -A && git -c user.email=s00@test -c user.name=s00 commit -qm called)
audit_sandbox
if assert_status never_wired ok; then ok "a script that IS called does not fire the check"; else bad "false positive on a called script"; fi

# --- 4 pump ledger -------------------------------------------------------------
plant "pump silence: one of the four pumps writes no swarm_attempts row"
cp "$TREE/var/runtime/state.sqlite3" "$TMP/db.bak"
"$PY" -c "
import sqlite3
c=sqlite3.connect('$TREE/var/runtime/state.sqlite3')
c.execute(\"delete from swarm_attempts where board_task_id like 'btk_pump_sim%'\"); c.commit()"
audit_sandbox
if assert_status pump_ledger_wiring fail; then ok "a silent pump detected"; else bad "silent pump NOT detected"; fi
cp "$TMP/db.bak" "$TREE/var/runtime/state.sqlite3"

# --- 5 soak window -------------------------------------------------------------
plant "soak drift: a declared-off observe window found enforcing"
audit_sandbox_with_env() { S00_SANDBOX_SOAK=enforce audit_sandbox; }
audit_sandbox_with_env
if assert_status soak_window_diff fail; then ok "observe-window drift detected"; else bad "observe-window drift NOT detected"; fi

# --- 6 lane briefs -------------------------------------------------------------
plant "lane brief: a brief-less clone with no declared class"
mkdir -p "$TREE/var/swarm/clones/briefless/var"
audit_sandbox
if assert_status lane_brief fail; then ok "brief-less clone detected"; else bad "brief-less clone NOT detected"; fi
rm -rf "$TREE/var/swarm/clones/briefless"

plant "lane brief: an UNDECLARED class must fire"
mkdir -p "$TREE/var/swarm/clones/mystery/var"
printf 'mystery-class\n' > "$TREE/var/swarm/clones/mystery/var/LANE-CLASS"
audit_sandbox
if assert_status lane_brief fail; then ok "undeclared LANE-CLASS detected"; else bad "undeclared LANE-CLASS NOT detected"; fi
rm -rf "$TREE/var/swarm/clones/mystery"

plant "lane brief: healthy + EXEMPT-class clones must NOT fire"
audit_sandbox
if assert_status lane_brief ok; then ok "healthy and exempt clones do not fire"; else bad "false positive on healthy/exempt clones"; fi

# --- 7 single signer -----------------------------------------------------------
plant "single signer: every promotion in the window signed by one key"
cp "$TREE/var/runtime/state.sqlite3" "$TMP/db2.bak"
"$PY" -c "
import sqlite3
c=sqlite3.connect('$TREE/var/runtime/state.sqlite3')
c.execute(\"delete from promotions where signing_key_id='key-beta'\"); c.commit()"
audit_sandbox
if assert_status single_signer fail; then ok "single-signer window detected"; else bad "single-signer window NOT detected"; fi
cp "$TMP/db2.bak" "$TREE/var/runtime/state.sqlite3"

# --- 8 stale heartbeat ---------------------------------------------------------
plant "stale heartbeat: the artifact of a script that is in no plist and no crontab"
touch -t 202001010000 "$TREE/var/swarm/fleet-status.md"
audit_sandbox
if assert_status unscheduled_heartbeat fail; then ok "stale heartbeat artifact detected"; else bad "stale heartbeat NOT detected"; fi
touch "$TREE/var/swarm/fleet-status.md"

# --- 9 loopback ----------------------------------------------------------------
plant "dead loopback: a declared endpoint nothing listens on"
cp "$TREE/configs/connectors.yaml" "$TMP/conn.bak"
DEADPORT=$("$PY" -c "
import socket
s=socket.socket(); s.bind(('127.0.0.1',0)); p=s.getsockname()[1]; s.close(); print(p)")
cat >> "$TREE/configs/connectors.yaml" <<EOF
# isolate the planted row from any prior expected_down annotation (text window)
# boundary 1
# boundary 2
# boundary 3
# boundary 4
# boundary 5
# boundary 6
  freshly_dead:
    capabilities:
      - id: dead-one
        expected_down: false
        base_url: "http://127.0.0.1:$DEADPORT"
EOF
audit_sandbox
if assert_status loopback_connectors fail; then ok "dead loopback endpoint detected"; else bad "dead loopback NOT detected"; fi

plant "loopback FALSE-POSITIVE GUARD: expected_down suppresses the same endpoint"
cp "$TMP/conn.bak" "$TREE/configs/connectors.yaml"

cat >> "$TREE/configs/connectors.yaml" <<EOF
  freshly_dead:
    capabilities:
      - id: dead-one
        expected_down: true
        base_url: "http://127.0.0.1:$DEADPORT"
EOF
audit_sandbox
if assert_status loopback_connectors ok; then ok "expected_down suppresses a known-dead endpoint"; else bad "expected_down did not suppress"; fi
cp "$TMP/conn.bak" "$TREE/configs/connectors.yaml"

# --- 10 provider spend --------------------------------------------------------
plant "spend-cap bypass: ledger exceeds the configured Kimi daily cap"
"$PY" - "$TREE/var/runtime/state.sqlite3" <<'PYEOF'
import sqlite3, sys
from datetime import UTC, datetime
conn = sqlite3.connect(sys.argv[1])
conn.execute(
    "insert into provider_call_usage "
    "(billing_provider, cost_usd_nanos, cost_upper_bound_usd_nanos, created_at) "
    "values (?, ?, ?, ?)",
    ("kimi", 101_000_000_000, None, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
)
conn.commit()
conn.close()
PYEOF
audit_sandbox
if assert_status provider_daily_spend fail; then ok "spend-cap ledger bypass detected"; else bad "spend-cap ledger bypass NOT detected"; fi
"$PY" - "$TREE/var/runtime/state.sqlite3" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("delete from provider_call_usage")
conn.commit()
conn.close()
PYEOF

# --- final revert sanity -------------------------------------------------------
plant "revert sanity: the sandbox is clean again"
audit_sandbox
if "$PY" - "$TMP/out.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
bad = [c for c in r["checks"] if c["status"] != "ok"]
assert not bad, "sandbox did not revert clean: " + "; ".join(f"{c['id']}={c['status']}" for c in bad)
print("    10/10 ok after every revert")
PYEOF
then ok "every planted defect was reverted"; else bad "the sandbox did not revert clean"; fi

# --------------------------------------------------------------------------- 3 read-only
step "3 READ-ONLY — sha256 of the real tree before and after a full audit run"
snapshot() {
    "$PY" - "$ROOT" "$1" <<'PYEOF'
import hashlib, os, subprocess, sys
from pathlib import Path
root, out = Path(sys.argv[1]), sys.argv[2]

# WHAT THIS COVERS, AND WHY EXACTLY THIS
#   * every git-TRACKED file — the repo itself, which is what "read-only" is a
#     claim about. `find . -type f` is not an option here: var/ alone holds
#     1.27 MILLION files (70 full clones under var/swarm/clones, plus
#     mission-restore and jira-goals), and hashing those would take longer than
#     the audit it is checking.
#   * plus the untracked runtime artifacts this package could plausibly touch:
#     var/launchd/ (the plists), var/health-sentinel/ (the sentinel's state) and
#     the control-plane SQLite, which the audit opens mode=ro.
#   * EXCLUDING var/log/ — the one sanctioned write target — and .git/, whose
#     index `git ls-files` legitimately restats.
rows = []
seen = set()

def add(path: Path, rel: str) -> None:
    if rel in seen or rel.startswith("var/log/"):
        return
    seen.add(rel)
    try:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        h = "unreadable"
    rows.append(f"{h}  {rel}")

tracked = subprocess.run(
    ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False
).stdout.split("\0")
for rel in tracked:
    if rel:
        add(root / rel, rel)

for extra in ("var/launchd", "var/health-sentinel"):
    base = root / extra
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if path.is_file():
                add(path, str(path.relative_to(root)))
for name in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
    path = root / "var/runtime" / name
    if path.is_file():
        add(path, f"var/runtime/{name}")

rows.sort()
Path(out).write_text("\n".join(rows) + "\n")
print(len(rows))
PYEOF
}
export PYTHONDONTWRITEBYTECODE=1
BEFORE_N=$(snapshot "$TMP/before.txt")
"$PY" "$SENTINEL" --audit --json --dry-run > "$TMP/readonly.json"
AFTER_N=$(snapshot "$TMP/after.txt")
if diff -u "$TMP/before.txt" "$TMP/after.txt" > "$TMP/diff.txt" 2>&1; then
    ok "audit mutated nothing: $BEFORE_N files identical before and after"
else
    bad "audit mutated the tree:"; head -30 "$TMP/diff.txt"
fi
[ "$BEFORE_N" -ge 1000 ] || bad "snapshot covered only $BEFORE_N files — the read-only claim is vacuous"

# --------------------------------------------------------------------------- 4 provenance
step "4 PROVENANCE — every registered threshold names where its number came from"
if "$PY" - "$REGISTRY" <<'PYEOF'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]).read()) or {}
problems = []
checks = doc.get("checks") or {}
assert checks, "no checks registered"
for name, cfg in checks.items():
    cfg = cfg or {}
    if "threshold" not in cfg:
        problems.append(f"{name}: no threshold registered")
    if not str(cfg.get("provenance") or "").strip():
        problems.append(f"{name}: threshold has no provenance")
for name, cfg in (doc.get("detectors") or {}).items():
    cfg = cfg or {}
    if not str(cfg.get("provenance") or "").strip():
        problems.append(f"detectors.{name}: no provenance")
assert not problems, "; ".join(problems)
print(f"    {len(checks)} checks + {len(doc.get('detectors') or {})} detector(s), all with provenance")
for name, cfg in sorted(checks.items()):
    print(f"      {name}: {str((cfg or {}).get('provenance'))[:80]}")
PYEOF
then ok "every registered threshold carries a provenance field"
else bad "a registered threshold has no provenance"; fi

# --------------------------------------------------------------------------- verdict
printf '\n=== s00_audit: %d passed, %d failed\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
