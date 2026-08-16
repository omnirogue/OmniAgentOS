#!/usr/bin/env bash
# s23 — acceptance for the blocked-session detector.
#
# Seven steps, in the order that matters:
#   1 RETRODICT           the real 111.2-minute block is flagged, from history
#   2 FALSE-POSITIVE      not one turn_duration-preceded gap is flagged
#   3 BACKGROUND          pendingBackgroundAgentCount>0 excludes; flipped to 0 does not
#   4 PAYLOAD             exactly six keys, and no transcript body in any of them
#   5 DEDUP               two sweeps over the same fixture produce one alert
#   6 COST                the full sweep over all three stores finishes under 5s
#   7 ISOLATION           the sentinel's own 1800s plist is untouched; the new one is 300
#
# Runs with --dry-run --no-push. Exits 0 ONLY on a full pass. Nothing here ever
# passes --arm-push, loads a launchd label, or writes outside a temp directory.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
SENTINEL="$ROOT/scripts/health-sentinel/health_sentinel.py"
STORE3="$HOME/.claude-account-3"
T3="$STORE3/projects/-Users-youruser/8279631d-fe14-4104-8f45-5622be450bbd.jsonl"
FIXTURE_STORE="$ROOT/tests/fixtures/blocked-sessions/store"

SID_BG="11111111-1111-4111-8111-111111111111"
SID_OK="22222222-2222-4222-8222-222222222222"
SID_END="33333333-3333-4333-8333-333333333333"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/s23.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAILED=0
step() { printf '\n=== %s\n' "$*"; }
ok()   { printf '  PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }

# --------------------------------------------------------------------------- preflight
# Fail fast and loudly: every assertion below depends on ambient state, and a
# test that silently skips because a store moved is worse than no test.
step "PREFLIGHT"
[ -x "$PY" ] || { echo "ABORT: venv python missing at $PY (run 'uv sync')" >&2; exit 3; }
[ -f "$SENTINEL" ] || { echo "ABORT: $SENTINEL missing" >&2; exit 3; }
[ -d "$STORE3/projects" ] || { echo "ABORT: $STORE3/projects missing — step 2 needs the real store" >&2; exit 3; }
[ -f "$T3" ] || { echo "ABORT: retrodiction transcript missing: $T3" >&2; exit 3; }
[ -d "$FIXTURE_STORE/projects" ] || { echo "ABORT: fixture store missing: $FIXTURE_STORE" >&2; exit 3; }
"$PY" -c 'import yaml' >/dev/null 2>&1 || { echo "ABORT: pyyaml missing in the venv" >&2; exit 3; }
ok "venv python, sentinel, real store, retrodiction transcript and fixtures all present"

# --------------------------------------------------------------------------- 1 retrodict
step "1 RETRODICT — account-3 session 8279631d at its ExitPlanMode record"
"$PY" "$SENTINEL" --watch-blocked --json --dry-run \
    --replay "$T3" --replay-at-record ExitPlanMode > "$TMP/replay.json"
if "$PY" - "$TMP/replay.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get("verdict") or {}
assert d.get("blocked") is True, f"expected blocked=True, got {d.get('blocked')}: {v.get('reason')}"
m = d.get("minutes_blocked")
assert m is not None and 108.0 <= m <= 114.0, f"expected minutes_blocked ~111, got {m}"
assert v.get("tool_name") == "ExitPlanMode", v.get("tool_name")
assert (d.get("alert") or {}).get("sessionId", "").startswith("8279631d"), d.get("alert")
print(f"    minutes_blocked={m} tool={v['tool_name']} pbac={v['pending_background_agents']} "
      f"supersession={v['human_input_supersession']}")
PYEOF
then ok "the real 111.2-minute ExitPlanMode block is flagged from history"
else bad "retrodiction did not flag session 8279631d"; fi

# --------------------------------------------------------------------------- 2 false positives
step "2 FALSE-POSITIVE GUARD — every gap >15m in 3 days of ~/.claude-account-3"
"$PY" "$SENTINEL" --watch-blocked --json --dry-run --gap-scan \
    --store "$STORE3" --window-days 3 --gap-minutes 15 > "$TMP/gaps.json"
if "$PY" - "$TMP/gaps.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
total = d["total_gaps"]
td = d["turn_duration_preceded"]
flagged = d["flagged"]
# Non-vacuity: if the window suddenly contains almost no long gaps, this guard
# proves nothing and must say so rather than pass.
assert total >= 20, f"only {total} gaps >15m in the window — guard is vacuous"
assert td >= 15, f"only {td} turn_duration-preceded gaps — guard is vacuous"
leaked = [g for g in flagged if g["preceding_subtype"] == "turn_duration"]
assert not leaked, f"{len(leaked)} turn_duration-preceded gap(s) were flagged: {leaked[:2]}"
assert all(g["preceding_type"] == "assistant" and g["tool_name"] for g in flagged), \
    "a flagged gap is not preceded by an assistant tool_use"
print(f"    {total} gaps >15m; {td} preceded by turn_duration; "
      f"{d['assistant_tool_use_preceded']} by assistant/tool_use; {len(flagged)} flagged")
print(f"    measured p90 of assistant/tool_use gaps = "
      f"{d['assistant_tool_use_gap_p90_minutes']}m over {d['assistant_tool_use_gap_samples']} samples")
PYEOF
then ok "zero turn_duration-preceded gaps flagged; only unanswered tool_use gaps are"
else bad "a turn_duration-preceded gap leaked into the flagged set"; fi

# --------------------------------------------------------------------------- 3 background exclusion
step "3 BACKGROUND EXCLUSION — pendingBackgroundAgentCount 3 vs the same fixture at 0"
cp -R "$FIXTURE_STORE" "$TMP/store-bg"
"$PY" "$SENTINEL" --watch-blocked --json --dry-run \
    --store "$TMP/store-bg" --window-days 1 --liveness assume-live > "$TMP/bg-before.json"

# Flip the ONE declared field, nothing else.
"$PY" - "$TMP/store-bg/projects/-Users-fixture/$SID_BG.jsonl" <<'PYEOF'
import json, sys
path = sys.argv[1]
lines = []
for line in open(path):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if r.get("subtype") == "turn_duration":
        assert r["pendingBackgroundAgentCount"] == 3, r.get("pendingBackgroundAgentCount")
        r["pendingBackgroundAgentCount"] = 0
    lines.append(json.dumps(r))
open(path, "w").write("\n".join(lines) + "\n")
PYEOF
"$PY" "$SENTINEL" --watch-blocked --json --dry-run \
    --store "$TMP/store-bg" --window-days 1 --liveness assume-live > "$TMP/bg-after.json"

if "$PY" - "$TMP/bg-before.json" "$TMP/bg-after.json" "$SID_BG" "$SID_OK" "$SID_END" <<'PYEOF'
import json, sys
before, after, sid_bg, sid_ok, sid_end = sys.argv[1:6]

def blocked_ids(path):
    d = json.load(open(path))
    return {e["alert"]["sessionId"] for e in d["blocked"]}

b, a = blocked_ids(before), blocked_ids(after)
assert sid_bg not in b, "a session with pendingBackgroundAgentCount=3 was flagged (it is WORKING)"
assert sid_ok in b, "the reference blocked shape was NOT flagged"
assert sid_end not in b, "a session whose turn ENDED (last record turn_duration) was flagged"
assert sid_bg in a, "flipping pendingBackgroundAgentCount to 0 did not make the same fixture blocked"
print(f"    pbac=3 -> not flagged; pbac=0 -> flagged; turn-ended -> not flagged")
PYEOF
then ok "the declared field alone decides; flipping it flips the verdict"
else bad "background-agent exclusion did not behave as declared"; fi

# --------------------------------------------------------------------------- 4 payload
step "4 PAYLOAD — exactly six keys and not one byte of transcript body"
if "$PY" - "$TMP/bg-after.json" "$FIXTURE_STORE/projects/-Users-fixture" <<'PYEOF'
import json, re, sys
from pathlib import Path

report = json.load(open(sys.argv[1]))
transcripts = Path(sys.argv[2])
ALLOWED = {"sessionId", "account", "cwd", "gitBranch", "tool_name", "minutes_blocked"}
assert report["blocked"], "nothing was flagged — the payload assertion would be vacuous"

# Every distinctive long token that appears anywhere in the transcript BODIES.
body_tokens = set()
for path in sorted(transcripts.glob("*.jsonl")):
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        msg = rec.get("message") or {}
        for block in msg.get("content") or []:
            blob = json.dumps(block)
            body_tokens.update(t for t in re.findall(r"[^\s\"',]{40,}", blob))
assert body_tokens, "fixtures carry no long body token — the leak assertion would be vacuous"

for entry in report["blocked"]:
    alert = entry["alert"]
    assert set(alert) == ALLOWED, f"payload key set is {sorted(alert)}, must be {sorted(ALLOWED)}"
    blob = json.dumps(alert)
    assert len(blob) <= 512, f"payload is {len(blob)} bytes; an alert must stay glanceable"
    leaked = [t for t in body_tokens if t in blob]
    assert not leaked, f"transcript body leaked into the alert: {leaked[:1]}"
    assert re.fullmatch(r"[0-9a-fA-F-]{8,64}", alert["sessionId"]), alert["sessionId"]
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", alert["tool_name"]), alert["tool_name"]
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alert["account"]), alert["account"]
    assert re.fullmatch(r"[A-Za-z0-9._/-]{0,128}", alert["gitBranch"]), alert["gitBranch"]
    assert alert["cwd"].startswith("/") and not re.search(r"\s", alert["cwd"]), alert["cwd"]
    assert isinstance(alert["minutes_blocked"], (int, float)), alert["minutes_blocked"]
print(f"    {len(report['blocked'])} alert(s), 6 keys each, "
      f"checked against {len(body_tokens)} distinctive body token(s)")
PYEOF
then ok "alerts carry exactly the six allowed keys and no transcript content"
else bad "payload discipline violated"; fi

# --------------------------------------------------------------------------- 5 dedup
step "5 DEDUP — two consecutive sweeps over the same fixture"
cp -R "$FIXTURE_STORE" "$TMP/store-dedup"
LOG="$TMP/alerts.jsonl"
: > "$LOG"
"$PY" "$SENTINEL" --watch-blocked --json --store "$TMP/store-dedup" --window-days 1 \
    --liveness assume-live --alert-log "$LOG" > "$TMP/dedup1.json"
"$PY" "$SENTINEL" --watch-blocked --json --store "$TMP/store-dedup" --window-days 1 \
    --liveness assume-live --alert-log "$LOG" > "$TMP/dedup2.json"
if "$PY" - "$TMP/dedup1.json" "$TMP/dedup2.json" "$LOG" "$SID_OK" <<'PYEOF'
import json, sys
one, two, log, sid = sys.argv[1:5]
d1, d2 = json.load(open(one)), json.load(open(two))
rows = [json.loads(l) for l in open(log) if l.strip()]
mine = [r for r in rows if r["sessionId"] == sid]
assert len(d1["dispatch"]["emitted"]) >= 1, "first sweep emitted nothing"
assert not d2["dispatch"]["emitted"], f"second sweep re-emitted {d2['dispatch']['emitted']}"
assert d2["dispatch"]["suppressed"], "second sweep did not record a suppression"
assert len(mine) == 1, f"alert log holds {len(mine)} rows for {sid}, expected exactly 1"
assert set(mine[0]) == {"sessionId", "tool_use_id", "notified_at"}, sorted(mine[0])
assert d1["push_armed"] is False and d2["push_armed"] is False, "push was armed during a test"
print(f"    sweep1 emitted {len(d1['dispatch']['emitted'])}, sweep2 emitted 0 "
      f"({len(d2['dispatch']['suppressed'])} suppressed); log holds 1 row for {sid[:8]}")
PYEOF
then ok "one alert per (sessionId, tool_use id); the second sweep is deduped"
else bad "de-duplication failed"; fi

# --------------------------------------------------------------------------- 6 cost
step "6 COST — full sweep over all three live stores"
START=$("$PY" -c 'import time; print(time.time())')
"$PY" "$SENTINEL" --watch-blocked --json --dry-run --include-subagents \
    --store "$HOME/.claude" --store "$HOME/.claude-account-2" --store "$HOME/.claude-account-3" \
    > "$TMP/cost.json"
END=$("$PY" -c 'import time; print(time.time())')
if "$PY" - "$TMP/cost.json" "$START" "$END" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
wall = float(sys.argv[3]) - float(sys.argv[2])
assert d["scanned"] >= 100, f"only {d['scanned']} transcripts scanned — the budget claim is vacuous"
assert wall < 5.0, f"sweep took {wall:.2f}s wall (budget 5s)"
assert d["duration_seconds"] < 5.0, f"sweep took {d['duration_seconds']}s internal (budget 5s)"
print(f"    scanned {d['scanned']} transcripts in {d['duration_seconds']}s internal / {wall:.2f}s wall")
PYEOF
then ok "full three-store sweep inside the 5s budget"
else bad "sweep exceeded its cost budget"; fi

# --------------------------------------------------------------------------- 7 isolation
step "7 ISOLATION — two labels, two SLOs, one untouched"
HS_PLIST="$ROOT/var/launchd/rendered/com.omniagentos.health-sentinel.plist"
BD_PLIST="$ROOT/var/launchd/rendered/com.omniagentos.blocked-session-detector.plist"
if "$PY" - "$HS_PLIST" "$BD_PLIST" <<'PYEOF'
import plistlib, sys
hs, bd = sys.argv[1], sys.argv[2]
a = plistlib.load(open(hs, "rb"))
b = plistlib.load(open(bd, "rb"))
assert a["StartInterval"] == 1800, f"health-sentinel StartInterval is {a['StartInterval']}, must stay 1800"
assert b["StartInterval"] == 300, f"blocked-session-detector StartInterval is {b['StartInterval']}, must be 300"
assert a["Label"] != b["Label"], "the two jobs share a label"
assert b["Label"] == "com.omniagentos.blocked-session-detector", b["Label"]
print(f"    {a['Label']} @{a['StartInterval']}s ; {b['Label']} @{b['StartInterval']}s")
PYEOF
then ok "health-sentinel still 1800s; the detector is its own 300s label"
else bad "launchd isolation broken"; fi

if [ -f "$HOME/Library/LaunchAgents/com.omniagentos.blocked-session-detector.plist" ]; then
    printf '  NOTE  detector plist IS installed in ~/Library/LaunchAgents (armed by an operator)\n'
else
    printf '  NOTE  detector plist is NOT installed — DISARMED, as shipped (see ARM.md)\n'
fi

# --------------------------------------------------------------------------- verdict
printf '\n=== s23_blocked_detector: %d passed, %d failed\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
