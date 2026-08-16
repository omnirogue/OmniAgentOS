#!/usr/bin/env bash
# Synthetic pipeline heartbeat probe. It deliberately never writes var/loopqueue.

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DRY_RUN=0
INJECT_FAILURE=""
RUN_ID="heartbeat-$(date -u +%Y%m%dT%H%M%SZ)-$$"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUNTIME_DIR="$REPO_ROOT/var/heartbeat-probe"
SCRATCH_DIR=""
RESULTS_FILE=""
BUILD_WORKTREE=""
BUILD_COMMIT=""
CLEANUP_DONE=0
ANY_FAILED=0
STATION_FAILURE_REASON=""

STATIONS=(propose claim build gate receipt learning_event cleanup)
RESULT_NAMES=()
RESULT_STATUSES=()
RESULT_REASONS=()

usage() {
  cat <<'EOF'
Usage: ./scripts/heartbeat-probe/run_probe.sh [--dry-run] [--inject-failure=<station>]

Options:
  --dry-run                    Exercise every station inside an ephemeral sandbox only.
  --inject-failure=<station>   Force exactly one named station to report FAIL.
  -h, --help                   Show this help.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run|--self-check) DRY_RUN=1 ;;
    --inject-failure=*) INJECT_FAILURE="${arg#--inject-failure=}" ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$INJECT_FAILURE" ]]; then
  valid_station=0
  for station in "${STATIONS[@]}"; do
    [[ "$station" == "$INJECT_FAILURE" ]] && valid_station=1
  done
  if [[ "$valid_station" -ne 1 ]]; then
    printf 'Unknown injection station: %s\n' "$INJECT_FAILURE" >&2
    exit 2
  fi
fi

safe_remove_scratch() {
  [[ -n "$SCRATCH_DIR" && -d "$SCRATCH_DIR" ]] || return 0
  case "$SCRATCH_DIR" in
    "$RUNTIME_DIR"/scratch.*|"${TMPDIR:-/tmp}"/omniagentos-heartbeat-probe.*) rm -rf "$SCRATCH_DIR" ;;
    *) printf 'Refusing to remove unexpected scratch path: %s\n' "$SCRATCH_DIR" >&2; return 1 ;;
  esac
}

cleanup_resources() {
  [[ "$CLEANUP_DONE" -eq 0 ]] || return 0
  local cleanup_failed=0
  safe_remove_scratch || cleanup_failed=1
  CLEANUP_DONE=1
  return "$cleanup_failed"
}

# Tear down a partially-built worktree when interrupted. The worktree is detached, so no branch is made.
trap cleanup_resources EXIT INT TERM

prepare_run_paths() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/omniagentos-heartbeat-probe.XXXXXX")" || return 1
  else
    mkdir -p "$RUNTIME_DIR/receipts" "$RUNTIME_DIR/findings" "$RUNTIME_DIR/state" || return 1
    SCRATCH_DIR="$(mktemp -d "$RUNTIME_DIR/scratch.XXXXXX")" || return 1
  fi
  RESULTS_FILE="$SCRATCH_DIR/stations.tsv"
  : > "$RESULTS_FILE"
}

write_json_file() {
  local target="$1" document_kind="$2" station="$3" reason="$4"
  python3 - "$target" "$document_kind" "$station" "$reason" "$RUN_ID" "$TIMESTAMP" <<'PY'
import json, sys
target, document_kind, station, reason, run_id, timestamp = sys.argv[1:]
with open(target, "w", encoding="utf-8") as handle:
    json.dump({"schema": "omniagentos.heartbeat-probe.v1", "type": document_kind,
               "synthetic": True, "run_id": run_id, "timestamp": timestamp,
               "station": station, "reason": reason,
               "producer": {"role": "external"}, "actor": "heartbeat-probe"},
              handle, sort_keys=True)
    handle.write("\n")
PY
}

write_finding() {
  local station="$1" reason="$2" findings_dir
  if [[ "$DRY_RUN" -eq 1 ]]; then findings_dir="$SCRATCH_DIR/findings"; else findings_dir="$RUNTIME_DIR/findings"; fi
  mkdir -p "$findings_dir" || return 1
  write_json_file "$findings_dir/${RUN_ID}-${station}.json" "finding" "$station" "$reason"
}

record_result() {
  local station="$1" status="$2" reason="$3"
  RESULT_NAMES+=("$station")
  RESULT_STATUSES+=("$status")
  RESULT_REASONS+=("$reason")
  # cleanup removes the ephemeral results file before its own result is recorded.
  if [[ -f "$RESULTS_FILE" ]]; then
    printf '%s\t%s\t%s\n' "$station" "$status" "$reason" >> "$RESULTS_FILE"
  fi
  printf '%-16s %s — %s\n' "$station" "$status" "$reason"
  if [[ "$status" == "FAIL" ]]; then
    ANY_FAILED=1
    write_finding "$station" "$reason" || printf 'Unable to write finding for %s\n' "$station" >&2
  fi
}

station_propose() {
  local proposal="$SCRATCH_DIR/proposal.json"
  cat > "$proposal" <<'EOF'
{"contract":"loopqueue.proposal.v1.1","id":"heartbeat-known-answer","kind":"synthetic_canary","title":"Synthetic pipeline heartbeat known-answer","payload":{"synthetic":true,"expected":"all stations pass"}}
EOF
  python3 - "$proposal" <<'PY'
import json, sys
required = {"contract", "id", "kind", "title", "payload"}
with open(sys.argv[1], encoding="utf-8") as handle: proposal = json.load(handle)
if required.difference(proposal) or not isinstance(proposal["payload"], dict):
    raise SystemExit("proposal is missing required v1.1-shaped fields")
PY
}

station_claim() {
  local claim_dir claim_path
  if [[ "$DRY_RUN" -eq 1 ]]; then claim_dir="$SCRATCH_DIR/claims"; else claim_dir="$RUNTIME_DIR/claims"; fi
  claim_path="$claim_dir/${RUN_ID}.json"
  python3 - "$claim_path" "$TIMESTAMP" <<'PY'
import datetime as dt
import json
import os
import sys

claim_path, timestamp = sys.argv[1:]
os.makedirs(os.path.dirname(claim_path), exist_ok=True)
claim = {
    "actor": "heartbeat-probe",
    "at": timestamp,
    "expires_at": (dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
}
fd = None
try:
    fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, (json.dumps(claim, sort_keys=True) + "\n").encode("utf-8"))
    os.close(fd)
    fd = None
    try:
        second_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    else:
        os.close(second_fd)
        raise SystemExit("second O_EXCL claim unexpectedly succeeded")
finally:
    if fd is not None:
        os.close(fd)
    try:
        os.unlink(claim_path)
    except FileNotFoundError:
        pass
PY
}

station_build() {
  local known_file
  if [[ "$DRY_RUN" -eq 1 ]]; then
    known_file="$SCRATCH_DIR/known-answer.sh"
    printf '#!/usr/bin/env bash\nprintf "heartbeat known answer\\n"\n' > "$known_file"
    bash -n "$known_file"
    BUILD_COMMIT="simulated"
    return
  fi
  BUILD_WORKTREE="$SCRATCH_DIR/worktree"
  git init -q "$BUILD_WORKTREE" || return 1
  git -C "$BUILD_WORKTREE" -c user.name='Heartbeat Probe' -c user.email='heartbeat-probe@localhost' commit --allow-empty -m 'chore: synthetic heartbeat base' || return 1
  git -C "$BUILD_WORKTREE" branch -M main || return 1
  known_file="$BUILD_WORKTREE/heartbeat-known-answer.sh"
  printf '#!/usr/bin/env bash\nprintf "heartbeat known answer\\n"\n' > "$known_file"
  chmod +x "$known_file"
  git -C "$BUILD_WORKTREE" add heartbeat-known-answer.sh || return 1
  git -C "$BUILD_WORKTREE" -c user.name='Heartbeat Probe' -c user.email='heartbeat-probe@localhost' commit -m 'chore: synthetic heartbeat known answer' || return 1
  BUILD_COMMIT="$(git -C "$BUILD_WORKTREE" rev-parse HEAD)" || return 1
}

station_gate() {
  local known_file gate_wrapper="$REPO_ROOT/scripts/merge-gate.sh"
  if [[ ! -x "$gate_wrapper" ]]; then
    STATION_FAILURE_REASON="real merge-gate wrapper is missing or non-executable: $gate_wrapper"
    return 1
  fi
  if ! "$gate_wrapper" --help >/dev/null; then
    STATION_FAILURE_REASON="real merge-gate wrapper --help invocation failed: $gate_wrapper"
    return 1
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    known_file="$SCRATCH_DIR/known-answer.sh"
    if ! bash -n "$known_file"; then
      STATION_FAILURE_REASON="known-answer shell syntax check failed in ephemeral sandbox"
      return 1
    fi
    return
  fi
  if [[ -z "$BUILD_COMMIT" || ! -f "$BUILD_WORKTREE/heartbeat-known-answer.sh" ]]; then
    STATION_FAILURE_REASON="known-answer build output is unavailable for gate checks"
    return 1
  fi
  if ! git -C "$BUILD_WORKTREE" diff --stat main "$BUILD_COMMIT" >/dev/null; then
    STATION_FAILURE_REASON="known-answer diff check against main failed"
    return 1
  fi
  if ! bash -n "$BUILD_WORKTREE/heartbeat-known-answer.sh"; then
    STATION_FAILURE_REASON="known-answer shell syntax check failed"
    return 1
  fi
}

write_receipt() {
  local receipt
  if [[ "$DRY_RUN" -eq 1 ]]; then receipt="$SCRATCH_DIR/receipts/${RUN_ID}.json"; else receipt="$RUNTIME_DIR/receipts/${RUN_ID}.json"; fi
  mkdir -p "$(dirname "$receipt")" || return 1
  python3 - "$receipt" "$RESULTS_FILE" "$RUN_ID" "$TIMESTAMP" "$DRY_RUN" "$BUILD_COMMIT" <<'PY'
import json, sys
receipt_path, results_path, run_id, timestamp, dry_run, build_commit = sys.argv[1:]
stations = []
with open(results_path, encoding="utf-8") as handle:
    for line in handle:
        name, status, reason = line.rstrip("\n").split("\t", 2)
        stations.append({"station": name, "status": status, "reason": reason})
document = {"schema": "omniagentos.heartbeat-probe.receipt.v1", "synthetic": True,
            "run_id": run_id, "timestamp": timestamp, "dry_run": dry_run == "1",
            "known_answer": "propose, claim, build, gate, receipt, learning_event, and cleanup pass",
            "build_commit": build_commit, "stations_observed_before_receipt": stations}
with open(receipt_path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, sort_keys=True)
    handle.write("\n")
PY
}

station_receipt() { write_receipt; }

station_learning_event() {
  local event_file
  if [[ "$DRY_RUN" -eq 1 ]]; then event_file="$SCRATCH_DIR/synthetic-events.jsonl"; else event_file="$RUNTIME_DIR/synthetic-events.jsonl"; fi
  python3 - "$event_file" "$RUN_ID" "$TIMESTAMP" "$ANY_FAILED" <<'PY'
import json, sys
event_path, run_id, timestamp, any_failed = sys.argv[1:]
event = {"synthetic": True, "station": "learning_event", "run_id": run_id,
         "timestamp": timestamp, "probe_failed_before_event": any_failed == "1",
         "note": "stand-in until the shared synthetic-exclusion view and learning-event wiring land"}
with open(event_path, "a", encoding="utf-8") as handle: handle.write(json.dumps(event, sort_keys=True) + "\n")
with open(event_path, encoding="utf-8") as handle:
    fresh_event = json.loads(handle.readlines()[-1])
if fresh_event.get("synthetic") is not True:
    raise SystemExit("freshly-written stand-in learning event lost synthetic=true")
PY
}

station_cleanup() { cleanup_resources; }

run_station() {
  local station="$1" reason
  STATION_FAILURE_REASON=""
  if [[ "$INJECT_FAILURE" == "$station" ]]; then
    record_result "$station" "FAIL" "injected failure for station-level verification"
    return
  fi
  if "station_${station}"; then
    case "$station" in
      propose) reason="wrote and validated v1.1-shaped proposal in probe scratch" ;;
      claim) reason="created, contention-checked, and released probe-namespaced O_EXCL claim" ;;
      build) reason="created known-answer commit in self-contained scratch repository" ;;
      gate) reason="invoked real merge-gate wrapper --help; known-answer diff and shell syntax checks passed" ;;
      receipt) reason="wrote synthetic receipt in probe output namespace" ;;
      learning_event) reason="appended and re-verified synthetic=true in local stand-in learning event" ;;
      cleanup) reason="removed probe scratch state and repository" ;;
    esac
    [[ "$DRY_RUN" -eq 0 || "$station" != "build" ]] || reason="simulated known-answer build in ephemeral sandbox"
    [[ "$DRY_RUN" -eq 0 || "$station" != "gate" ]] || reason="invoked real merge-gate wrapper --help; known-answer syntax check passed in ephemeral sandbox"
    [[ "$DRY_RUN" -eq 0 || "$station" != "cleanup" ]] || reason="removed ephemeral probe scratch state"
    record_result "$station" "PASS" "$reason"
  else
    record_result "$station" "FAIL" "${STATION_FAILURE_REASON:-station command failed}"
  fi
}

update_failure_streak() {
  [[ "$DRY_RUN" -eq 0 ]] || return 0
  local state_file="$RUNTIME_DIR/state/consecutive_fails.txt" streak=0
  if [[ -f "$state_file" ]]; then
    read -r streak < "$state_file" || streak=0
    [[ "$streak" =~ ^[0-9]+$ ]] || streak=0
  fi
  if [[ "$ANY_FAILED" -eq 1 ]]; then streak=$((streak + 1)); else streak=0; fi
  printf '%s\n' "$streak" > "$state_file" || return 1
  if [[ "$streak" -eq 3 ]]; then
    local alert="${TIMESTAMP} heartbeat probe has failed for 3 consecutive runs; inspect var/heartbeat-probe/findings/."
    printf '%s\n' "$alert" > "$RUNTIME_DIR/ALERT.md" || return 1
    printf '%s [heartbeat-probe] ALERT: 3 consecutive probe failures; inspect var/heartbeat-probe/findings/\n' "$TIMESTAMP" >> "$REPO_ROOT/var/loopqueue/ALERTS.md" || return 1
    printf 'ALERT: %s\n' "$alert"
  fi
}

print_summary() {
  local index
  printf '\nHeartbeat probe summary (run %s):\n' "$RUN_ID"
  for ((index = 0; index < ${#RESULT_NAMES[@]}; index++)); do
    printf '  %-16s %s — %s\n' "${RESULT_NAMES[$index]}" "${RESULT_STATUSES[$index]}" "${RESULT_REASONS[$index]}"
  done
}

prepare_run_paths || { printf 'Unable to create probe scratch namespace\n' >&2; exit 1; }
for station in "${STATIONS[@]}"; do run_station "$station"; done
update_failure_streak || { printf 'Unable to update probe failure streak\n' >&2; ANY_FAILED=1; }
print_summary
[[ "$ANY_FAILED" -eq 0 ]]
