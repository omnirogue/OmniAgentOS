#!/bin/bash
# S18 acceptance — artifact-truthful reflection status + corrected agent watchdog.
#
# Asserts, in order:
#   a1   the reflection WRITER calls the shared classifier: a run whose briefing
#        is zero bytes does NOT persist a success status, and no writer in the
#        package still assigns a bare success literal to report_status
#   a1b  the reverse direction: a run WITH a non-empty briefing does persist ok
#   a2   exactly ONE row per run under ONE id scheme (no refr_/ref_ pair)
#   a3   classify_settlement is three-valued, and an ungateable outcome is
#        EXCLUDED from the acceptance floor rather than counted against it
#   c1   etime arithmetic is base-10 for every shape ps emits on macOS
#   d1   a live interactive S+ process on a controlling tty is never flagged,
#        while a synthetic real flatline still is
#   b1   configs/launchd-approved.yaml parses, is non-empty, is fully populated,
#        and does not contain the watchdog label that is still under observation
#   z    NO ACTUATION was added anywhere in the owned paths
#
# `set -e` is deliberately NOT used: this is a test runner and every assertion
# must be evaluated even after one fails. Exit status is 0 only on a full pass.
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
PY="${REPO}/.venv/bin/python"
WATCHDOG="${REPO}/scripts/gates/agent_watchdog.sh"
MANIFEST="${REPO}/configs/launchd-approved.yaml"
REFLECTION="${REPO}/omniagentos/reflection"

PASS=0
FAIL=0

ok() {
  printf '  PASS  %s\n' "$1"
  PASS=$((PASS + 1))
}

bad() {
  printf '  FAIL  %s\n' "$1"
  FAIL=$((FAIL + 1))
}

die() {
  printf '\nPREFLIGHT FAILED: %s\n' "$1" >&2
  printf 'Nothing was asserted. Fix the above and re-run.\n' >&2
  exit 2
}

section() { printf '\n== %s ==\n' "$1"; }

# --------------------------------------------------------------------------
# Preflight — fail fast and loudly, before a single assertion is claimed.
# --------------------------------------------------------------------------
section "preflight"
[[ -x "$PY" ]] || die "python interpreter not found or not executable: $PY (run 'make sync')"
[[ -f "$WATCHDOG" ]] || die "missing $WATCHDOG"
[[ -x "$WATCHDOG" ]] || die "$WATCHDOG is not executable (chmod +x)"
[[ -f "$MANIFEST" ]] || die "missing $MANIFEST"
[[ -d "$REFLECTION" ]] || die "missing $REFLECTION"
[[ -f "${REFLECTION}/settlement.py" ]] || die "missing shared classifier ${REFLECTION}/settlement.py"
bash -n "$WATCHDOG" || die "$WATCHDOG does not parse"
"$PY" -c 'import yaml' 2>/dev/null || die "pyyaml unavailable in $PY"
command -v ps >/dev/null 2>&1 || die "ps(1) unavailable"
command -v script >/dev/null 2>&1 || die "script(1) unavailable (needed to spawn a real pty)"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "$REPO is not a git repository"
printf '  preflight ok (repo=%s)\n' "$REPO"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/s18-acceptance-XXXXXX") || die "cannot create work dir"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------
# Python driver. Writes JSON to stdout; every scenario is hermetic (temp DB,
# temp repo root, temp vault) so running twice equals running once.
# --------------------------------------------------------------------------
cat > "${WORK}/driver.py" <<'PY'
"""S18 acceptance driver. Not a library — invoked only by the acceptance script."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ["S18_REPO"])

from omniagentos.db.migrate import migrate  # noqa: E402
from omniagentos.reflection import harvest, runner  # noqa: E402
from omniagentos.reflection.settlement import (  # noqa: E402
    Settlement,
    acceptance_floor,
    classify_settlement,
)

HARVEST_CONFIG = """
window_hours: 36
caps:
  per_source_bytes: 1000
  per_source_tokens: 500
  total_tokens: 2000
adapters:
  claude: false
  gemini: false
  kimi: false
  codex: false
  grok: false
"""


def _stub(brief_path: Path):
    class Stub:
        @staticmethod
        def harvest_evidence(date_str=None, run_id=None):
            return {"date": date_str, "sources": []}

        @staticmethod
        def run_propose(date_str=None, db_path=None):
            return {"proposals": [], "dropped_proposals": []}

        @staticmethod
        def auto_apply_eligible(db_path=None, accepted_fingerprints=None):
            return []

        @staticmethod
        def generate_reflection_report(date_str=None, db_path=None, vault_dir=None):
            return str(brief_path)

    return Stub


def _rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM reflection_runs")]
    finally:
        conn.close()


def cmd_loop(tmp: Path, mode: str) -> dict:
    """Run the REAL writer against a briefing fixture and report what persisted."""
    db = tmp / "s18.sqlite3"
    migrate(str(db))
    brief = tmp / "briefing.md"
    if mode == "empty":
        brief.write_bytes(b"")
    else:
        brief.write_text("# Reflection Morning Report - fixture\n\nreal content\n", encoding="utf-8")

    stub = _stub(brief)
    with (
        mock.patch.object(runner, "default_db_path", lambda: str(db)),
        mock.patch.object(runner, "get_harvester", lambda: stub),
        mock.patch.object(runner, "get_proposer", lambda: stub),
        mock.patch.object(runner, "get_applier", lambda: stub),
        mock.patch.object(runner, "get_reporter", lambda: stub),
    ):
        run_id = runner.run_reflection_loop(observe_only=True)

    rows = _rows(db)
    row = next(r for r in rows if r["id"] == run_id)
    return {
        "run_id": run_id,
        "briefing_bytes": brief.stat().st_size,
        "report_status": row["report_status"],
        "run_status": row["status"],
        "row_count": len(rows),
        "ids": [r["id"] for r in rows],
    }


def cmd_onerow(tmp: Path) -> dict:
    """Drive the runner with the REAL harvester — the historical two-writer path."""
    db = tmp / "s18.sqlite3"
    migrate(str(db))
    (tmp / "configs").mkdir(parents=True, exist_ok=True)
    (tmp / "configs" / "reflection.yaml").write_text(HARVEST_CONFIG, encoding="utf-8")
    brief = tmp / "briefing.md"
    brief.write_text("# briefing\n", encoding="utf-8")

    stub = _stub(brief)
    with (
        mock.patch.object(runner, "default_db_path", lambda: str(db)),
        mock.patch.object(harvest, "default_db_path", lambda: str(db)),
        mock.patch.object(harvest, "default_vault_dir", lambda: str(tmp / "vault")),
        mock.patch.object(harvest, "_repo_root", lambda: tmp),
        mock.patch.object(runner, "get_harvester", lambda: harvest),
        mock.patch.object(runner, "get_proposer", lambda: stub),
        mock.patch.object(runner, "get_applier", lambda: stub),
        mock.patch.object(runner, "get_reporter", lambda: stub),
    ):
        run_id = runner.run_reflection_loop(observe_only=True)

    rows = _rows(db)
    ids = [r["id"] for r in rows]
    return {
        "run_id": run_id,
        "row_count": len(rows),
        "ids": ids,
        "prefixes": sorted({i.split("_", 1)[0] for i in ids}),
        "legacy_ref_ids": [i for i in ids if i.startswith("ref_")],
        "harvest_status": rows[0]["harvest_status"] if rows else None,
    }


def cmd_settlement(tmp: Path) -> dict:
    empty = tmp / "empty.txt"
    empty.write_bytes(b"")
    full = tmp / "full.txt"
    full.write_text("x", encoding="utf-8")
    missing = tmp / "missing.txt"

    floor_with_ungateable = acceptance_floor(
        [Settlement.OK, Settlement.OK, Settlement.UNGATEABLE], 1.0
    )
    floor_with_failure = acceptance_floor([Settlement.OK, Settlement.OK, Settlement.FAILED], 1.0)
    naive_ratio = 2 / 3  # what it would be if ungateable were counted against

    return {
        "members": sorted(m.value for m in Settlement),
        "member_count": len(list(Settlement)),
        "empty_file": classify_settlement(empty).value,
        "full_file": classify_settlement(full).value,
        "missing_file": classify_settlement(missing).value,
        "missing_required": classify_settlement(missing, required=True).value,
        "raised": classify_settlement(error=RuntimeError("boom")).value,
        "no_evidence": classify_settlement(evidence=False).value,
        "unknown": classify_settlement().value,
        "floor_ungateable": {
            "ok": floor_with_ungateable.ok,
            "failed": floor_with_ungateable.failed,
            "ungateable": floor_with_ungateable.ungateable,
            "gateable": floor_with_ungateable.gateable,
            "ratio": floor_with_ungateable.ratio,
            "meets": floor_with_ungateable.meets,
        },
        "floor_failure_meets": floor_with_failure.meets,
        "naive_ratio_would_be": naive_ratio,
    }


def cmd_manifest(_tmp: Path) -> dict:
    import yaml

    path = Path(os.environ["S18_REPO"]) / "configs" / "launchd-approved.yaml"
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    entries = (data or {}).get("approved") or []
    incomplete = [
        e
        for e in entries
        if not isinstance(e, dict)
        or not str(e.get("label") or "").strip()
        or not str(e.get("reason") or "").strip()
        or not str(e.get("approved_on") or "").strip()
    ]
    return {
        "parsed": isinstance(data, dict),
        "count": len(entries),
        "incomplete": len(incomplete),
        "labels": [e.get("label") for e in entries if isinstance(e, dict)],
    }


def main() -> int:
    cmd = sys.argv[1]
    tmp = Path(sys.argv[2])
    arg = sys.argv[3] if len(sys.argv) > 3 else ""
    tmp.mkdir(parents=True, exist_ok=True)
    if cmd == "loop":
        out = cmd_loop(tmp, arg)
    elif cmd == "onerow":
        out = cmd_onerow(tmp)
    elif cmd == "settlement":
        out = cmd_settlement(tmp)
    elif cmd == "manifest":
        out = cmd_manifest(tmp)
    else:
        raise SystemExit(f"unknown driver command: {cmd}")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

export S18_REPO="$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

jget() { "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())[sys.argv[1]])' "$1"; }

# --------------------------------------------------------------------------
# a1 — the WRITER is wired to the classifier
# --------------------------------------------------------------------------
section "a1 — zero-byte briefing must not persist a success status"
A1_JSON=$("$PY" "${WORK}/driver.py" loop "${WORK}/a1" empty 2>"${WORK}/a1.err")
A1_RC=$?
if [[ $A1_RC -ne 0 || -z "$A1_JSON" ]]; then
  bad "writer run against a zero-byte briefing crashed (rc=$A1_RC)"
  sed -n '1,25p' "${WORK}/a1.err" >&2
else
  A1_BYTES=$(printf '%s' "$A1_JSON" | jget briefing_bytes)
  A1_REPORT=$(printf '%s' "$A1_JSON" | jget report_status)
  A1_ROWS=$(printf '%s' "$A1_JSON" | jget row_count)
  if [[ "$A1_BYTES" != "0" ]]; then
    bad "fixture briefing was not zero bytes (got ${A1_BYTES})"
  elif [[ -z "$A1_REPORT" || "$A1_REPORT" == "None" ]]; then
    bad "no report status was persisted at all"
  elif [[ "$A1_REPORT" == "ok" ]]; then
    bad "report_status persisted as 'ok' over a ZERO-BYTE briefing — the status is still lying"
  else
    ok "zero-byte briefing persisted report_status='${A1_REPORT}' (not a success)"
  fi
  if [[ "$A1_ROWS" == "1" ]]; then
    ok "the run wrote exactly one row"
  else
    bad "the run wrote ${A1_ROWS} rows (expected 1)"
  fi
fi

section "a1 — no unconditional success literal survives in the writer package"
LITERAL_HITS=$(
  grep -REn --include='*.py' \
    "report_status[[:space:]]*=[[:space:]]*[\"']ok[\"']|[\"'](harvest|propose|validate|apply|report)[\"'][[:space:]]*,[[:space:]]*[\"']ok[\"']" \
    "$REFLECTION" 2>/dev/null
)
if [[ -n "$LITERAL_HITS" ]]; then
  bad "unconditional success literal still assigned to a stage status:"
  printf '%s\n' "$LITERAL_HITS" | sed 's/^/        /'
else
  ok "no unconditional 'ok' assignment to a stage status remains in the package"
fi

if grep -REn --include='*.py' 'classify_settlement' "$REFLECTION" >/dev/null 2>&1; then
  CALLERS=$(grep -rlE --include='*.py' 'classify_settlement\(' "$REFLECTION" | grep -v '/settlement.py$' | sort)
  CALLER_COUNT=$(printf '%s\n' "$CALLERS" | grep -c . )
  if [[ "$CALLER_COUNT" -ge 4 ]]; then
    ok "classify_settlement is CALLED from ${CALLER_COUNT} writer modules, not merely defined"
  else
    bad "classify_settlement is called from only ${CALLER_COUNT} module(s); expected >= 4"
    printf '%s\n' "$CALLERS" | sed 's/^/        /'
  fi
else
  bad "classify_settlement is not present in the reflection package"
fi

# --------------------------------------------------------------------------
# a1b — the reverse direction
# --------------------------------------------------------------------------
section "a1b — a non-empty briefing must persist a success status"
A1B_JSON=$("$PY" "${WORK}/driver.py" loop "${WORK}/a1b" full 2>"${WORK}/a1b.err")
A1B_RC=$?
if [[ $A1B_RC -ne 0 || -z "$A1B_JSON" ]]; then
  bad "writer run against a non-empty briefing crashed"
  sed -n '1,25p' "${WORK}/a1b.err" >&2
else
  A1B_BYTES=$(printf '%s' "$A1B_JSON" | jget briefing_bytes)
  A1B_REPORT=$(printf '%s' "$A1B_JSON" | jget report_status)
  if [[ "$A1B_BYTES" -le 0 ]]; then
    bad "fixture briefing was empty (${A1B_BYTES} bytes) — scenario is invalid"
  elif [[ "$A1B_REPORT" == "ok" ]]; then
    ok "non-empty briefing (${A1B_BYTES} bytes) persisted report_status='ok'"
  else
    bad "non-empty briefing persisted report_status='${A1B_REPORT}' (expected 'ok')"
  fi
fi

# --------------------------------------------------------------------------
# a2 — one row per run, one id scheme
# --------------------------------------------------------------------------
section "a2 — exactly one row per run under one id scheme"
A2_JSON=$("$PY" "${WORK}/driver.py" onerow "${WORK}/a2" 2>"${WORK}/a2.err")
A2_RC=$?
if [[ $A2_RC -ne 0 || -z "$A2_JSON" ]]; then
  bad "runner+real-harvester run crashed"
  sed -n '1,40p' "${WORK}/a2.err" >&2
else
  A2_ROWS=$(printf '%s' "$A2_JSON" | jget row_count)
  A2_PREFIXES=$(printf '%s' "$A2_JSON" | jget prefixes)
  A2_LEGACY=$(printf '%s' "$A2_JSON" | jget legacy_ref_ids)
  if [[ "$A2_ROWS" == "1" ]]; then
    ok "one full run (runner + real harvester) wrote exactly 1 row"
  else
    bad "one full run wrote ${A2_ROWS} rows — the refr_/ref_ pair is back"
  fi
  if [[ "$A2_PREFIXES" == "['refr']" ]]; then
    ok "one id scheme in play: ${A2_PREFIXES}"
  else
    bad "more than one id scheme in play: ${A2_PREFIXES}"
  fi
  if [[ "$A2_LEGACY" == "[]" ]]; then
    ok "no legacy ref_ ids were minted"
  else
    bad "legacy ref_ ids were minted: ${A2_LEGACY}"
  fi
fi

if grep -REn --include='*.py' 'new_id\([\"'"'"']ref[\"'"'"']\)' "$REFLECTION" >/dev/null 2>&1; then
  bad "a second id scheme is still minted somewhere in the package"
else
  ok "no second id scheme is minted anywhere in the package"
fi

# --------------------------------------------------------------------------
# a3 — three-valued enum, ungateable excluded from the floor
# --------------------------------------------------------------------------
section "a3 — three-valued settlement, ungateable excluded from the acceptance floor"
A3_JSON=$("$PY" "${WORK}/driver.py" settlement "${WORK}/a3" 2>"${WORK}/a3.err")
A3_RC=$?
if [[ $A3_RC -ne 0 || -z "$A3_JSON" ]]; then
  bad "settlement scenario crashed"
  sed -n '1,25p' "${WORK}/a3.err" >&2
else
  A3_MEMBERS=$(printf '%s' "$A3_JSON" | jget members)
  A3_COUNT=$(printf '%s' "$A3_JSON" | jget member_count)
  A3_EMPTY=$(printf '%s' "$A3_JSON" | jget empty_file)
  A3_FULL=$(printf '%s' "$A3_JSON" | jget full_file)
  A3_RAISED=$(printf '%s' "$A3_JSON" | jget raised)
  A3_FLOOR_MEETS=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["floor_ungateable"]["meets"])' <<< "$A3_JSON")
  A3_FLOOR_GATEABLE=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["floor_ungateable"]["gateable"])' <<< "$A3_JSON")
  A3_FLOOR_UNGATEABLE=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["floor_ungateable"]["ungateable"])' <<< "$A3_JSON")
  A3_FLOOR_RATIO=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["floor_ungateable"]["ratio"])' <<< "$A3_JSON")
  A3_FAILMEETS=$(printf '%s' "$A3_JSON" | jget floor_failure_meets)

  if [[ "$A3_COUNT" == "3" ]]; then
    ok "settlement enum is three-valued: ${A3_MEMBERS}"
  else
    bad "settlement enum has ${A3_COUNT} members (expected 3): ${A3_MEMBERS}"
  fi
  [[ "$A3_FULL" == "ok" ]] && ok "non-empty artifact settles ok" || bad "non-empty artifact settled '${A3_FULL}'"
  [[ "$A3_EMPTY" == "ungateable" ]] && ok "zero-byte artifact settles ungateable" || bad "zero-byte artifact settled '${A3_EMPTY}'"
  [[ "$A3_RAISED" == "failed" ]] && ok "a raised stage settles failed" || bad "a raised stage settled '${A3_RAISED}'"

  if [[ "$A3_FLOOR_UNGATEABLE" == "1" && "$A3_FLOOR_GATEABLE" == "2" && "$A3_FLOOR_RATIO" == "1.0" && "$A3_FLOOR_MEETS" == "True" ]]; then
    ok "ungateable is EXCLUDED from the floor (gateable=2, ratio=1.0, meets=True; naive scoring would have given 2/3)"
  else
    bad "ungateable was not excluded from the floor (gateable=${A3_FLOOR_GATEABLE}, ungateable=${A3_FLOOR_UNGATEABLE}, ratio=${A3_FLOOR_RATIO}, meets=${A3_FLOOR_MEETS})"
  fi
  if [[ "$A3_FAILMEETS" == "False" ]]; then
    ok "a genuine failure still fails the same floor (the floor is not vacuous)"
  else
    bad "a genuine failure did not fail the floor (meets=${A3_FAILMEETS})"
  fi
fi

# --------------------------------------------------------------------------
# c1 — base-10 elapsed-time arithmetic for every etime shape
# --------------------------------------------------------------------------
section "c1 — etime arithmetic is base-10 for every shape ps emits"
etime_case() {
  local etime=$1 expect=$2 out rc err
  out=$(bash "$WATCHDOG" --etime-seconds "$etime" 2>"${WORK}/etime.err")
  rc=$?
  err=$(cat "${WORK}/etime.err")
  if [[ $rc -ne 0 ]]; then
    bad "etime '${etime}' exited ${rc} (stderr: ${err})"
    return
  fi
  if printf '%s' "$err" | grep -qi 'value too great for base'; then
    bad "etime '${etime}' produced a base-parsing error: ${err}"
    return
  fi
  if [[ -n "$err" ]]; then
    bad "etime '${etime}' wrote to stderr: ${err}"
    return
  fi
  if [[ "$out" != "$expect" ]]; then
    bad "etime '${etime}' computed ${out}, expected ${expect}"
    return
  fi
  ok "etime '${etime}' -> ${out}s"
}

etime_case "59" 59                      # SS
etime_case "08:09" 489                  # MM:SS with leading-zero (octal trap)
etime_case "00:00:08" 8                 # HH:MM:SS, every component zero-padded
etime_case "1-02:03:04" 93784           # DD-HH:MM:SS
etime_case "09" 9                       # bare octal-invalid second
etime_case "00:00" 0                    # MM:SS zero
etime_case "09:08:07" 32887             # HH:MM:SS all octal-invalid
etime_case "02-03:01:07" 183667         # multi-day

# --------------------------------------------------------------------------
# d1 — the flatline predicate vs a live interactive S+ process
# --------------------------------------------------------------------------
section "d1 — a live interactive S+ process on a controlling tty is never flagged"
cat > "${WORK}/s18_idle_probe.sh" <<'IDLE'
#!/bin/bash
sleep 21
IDLE
chmod +x "${WORK}/s18_idle_probe.sh"
# A real pty, so the state/tty under test are the ones ps actually reports.
# The probe exits on its own in ~21s; nothing is ever signalled.
(script -q /dev/null "${WORK}/s18_idle_probe.sh" >/dev/null 2>&1 &)

PROBE_PID=""
PROBE_STATE=""
PROBE_TTY=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  read -r PROBE_PID PROBE_STATE PROBE_TTY <<< "$(
    ps -axo pid=,state=,tty=,command= |
      awk '/s18_idle_probe/ && $2 ~ /\+/ && $3 != "??" { print $1, $2, $3; exit }'
  )"
  [[ -n "${PROBE_STATE:-}" ]] && break
  sleep 0.5
done

if [[ -z "${PROBE_STATE:-}" ]]; then
  bad "could not spawn a live interactive process on a controlling tty (pty allocation failed)"
else
  printf '  probe: pid=%s state=%s tty=%s (real ps values)\n' "$PROBE_PID" "$PROBE_STATE" "$PROBE_TTY"
  # Pair the REAL state/tty with an age that would otherwise trip the flatline
  # predicate — this is precisely the shape that produced the false positives.
  LIVE_KIND=$(bash "$WATCHDOG" --classify 0.0 0.0 "1-02:03:04" "$PROBE_STATE" "$PROBE_STATE" "$PROBE_TTY")
  if [[ -z "$LIVE_KIND" ]]; then
    ok "live interactive process (state=${PROBE_STATE} tty=${PROBE_TTY}, age 1d02h, 0.0% CPU) is NOT flagged"
  else
    bad "live interactive process was flagged as '${LIVE_KIND}'"
  fi
  LIVE_ANSWER=$(bash "$WATCHDOG" --live-interactive "$PROBE_STATE" "$PROBE_TTY")
  if [[ "$LIVE_ANSWER" == "yes" ]]; then
    ok "the predicate recognises the spawned process as interactive"
  else
    bad "the predicate did not recognise the spawned process as interactive (${LIVE_ANSWER})"
  fi
fi

DEAD_KIND=$(bash "$WATCHDOG" --classify 0.0 0.0 "1-02:03:04" "S" "S" "??")
if [[ "$DEAD_KIND" == "flatline" ]]; then
  ok "a synthetic real flatline (0.0% CPU, age 1d02h, no controlling tty) IS still flagged"
else
  bad "a synthetic real flatline was classified as '${DEAD_KIND}' (expected flatline)"
fi

SPIN_KIND=$(bash "$WATCHDOG" --classify 99.4 98.7 "00:20:00" "R+" "R+" "ttys999")
if [[ "$SPIN_KIND" == "spinner" ]]; then
  ok "a foreground R+ spinner is still flagged (the exclusion is narrow, not blanket)"
else
  bad "a foreground R+ spinner was classified as '${SPIN_KIND}' (expected spinner)"
fi

YOUNG_KIND=$(bash "$WATCHDOG" --classify 0.0 0.0 "08:09" "S" "S" "??")
if [[ -z "$YOUNG_KIND" ]]; then
  ok "an idle process younger than 15 minutes is not flagged"
else
  bad "a 489-second-old idle process was flagged as '${YOUNG_KIND}'"
fi

# --------------------------------------------------------------------------
# b1 — the recorded approved launchd set
# --------------------------------------------------------------------------
section "b1 — configs/launchd-approved.yaml is a real, complete, non-vacuous record"
B1_JSON=$("$PY" "${WORK}/driver.py" manifest "${WORK}/b1" 2>"${WORK}/b1.err")
B1_RC=$?
if [[ $B1_RC -ne 0 || -z "$B1_JSON" ]]; then
  bad "manifest did not parse as YAML"
  sed -n '1,25p' "${WORK}/b1.err" >&2
else
  B1_COUNT=$(printf '%s' "$B1_JSON" | jget count)
  B1_INCOMPLETE=$(printf '%s' "$B1_JSON" | jget incomplete)
  ok "manifest parses as YAML"
  if [[ "$B1_COUNT" -gt 0 ]]; then
    ok "manifest is non-empty (${B1_COUNT} approved labels) — the gate is not vacuous"
  else
    bad "manifest has no approved entries; a gate that passes on an empty manifest asserts nothing"
  fi
  if [[ "$B1_INCOMPLETE" == "0" ]]; then
    ok "every entry carries label + reason + approved_on"
  else
    bad "${B1_INCOMPLETE} entr(ies) are missing label, reason or approved_on"
  fi
fi

if grep -F -q 'agent-watchdog' "$MANIFEST"; then
  bad "'agent-watchdog' appears in the manifest before its observe window has run clean"
else
  ok "'agent-watchdog' is absent from the manifest (observe window not yet served)"
fi

# --------------------------------------------------------------------------
# z — no actuation
# --------------------------------------------------------------------------
section "z — no actuation anywhere in the owned paths"
# Tokens are assembled from fragments so this file itself contains none of them
# verbatim and can therefore be scanned alongside everything else.
T_LOAD="launch""ctl"
T_KILL="ki""ll "
T_SIG="SIG""TERM"
T_PKILL="pki""ll"
ACT_RE="${T_LOAD}|${T_KILL}|${T_SIG}|${T_PKILL}"

OWNED=(
  "omniagentos/reflection"
  "scripts/gates/agent_watchdog.sh"
  "configs/launchd-approved.yaml"
  "tests/acceptance/s18_status_and_watchdog.sh"
)

# Full-content scan: never vacuous, and stays meaningful after the work is
# committed (a diff-only scan silently passes once HEAD moves).
CONTENT_HITS=$(
  cd "$REPO" &&
    grep -REn --exclude-dir=__pycache__ --binary-files=without-match \
      "$ACT_RE" "${OWNED[@]}" 2>/dev/null
)
if [[ -n "$CONTENT_HITS" ]]; then
  bad "actuation token present in an owned path:"
  printf '%s\n' "$CONTENT_HITS" | sed 's/^/        /'
else
  ok "no actuation token in any owned path (content scan)"
fi

DIFF_ADDED=$(
  {
    git -C "$REPO" diff HEAD -- "${OWNED[@]}" 2>/dev/null
    git -C "$REPO" ls-files --others --exclude-standard -- "${OWNED[@]}" 2>/dev/null |
      while IFS= read -r f; do sed 's/^/+/' "${REPO}/${f}" 2>/dev/null; done
  } | grep '^+' | grep -v '^+++'
)
DIFF_HITS=$(printf '%s\n' "$DIFF_ADDED" | grep -E "$ACT_RE")
if [[ -n "$DIFF_HITS" ]]; then
  bad "actuation token added by the diff:"
  printf '%s\n' "$DIFF_HITS" | sed 's/^/        /'
else
  ok "no actuation token added by the diff"
fi

# --------------------------------------------------------------------------
section "result"
printf '  passed: %d\n  failed: %d\n' "$PASS" "$FAIL"
if [[ $FAIL -eq 0 && $PASS -gt 0 ]]; then
  printf '\nS18 ACCEPTANCE: PASS\n'
  exit 0
fi
printf '\nS18 ACCEPTANCE: FAIL\n'
exit 1
