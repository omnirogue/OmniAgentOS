#!/usr/bin/env bash
# enroll.sh — join this machine to the shared work queue as a worker.
# SPEC-shared-queue.md §5.2. macOS AND Linux (`uname -s` branch).
#
# Runs on the JOINING machine, from inside a clone of this repo:
#
#   git clone https://github.com/Globex/OmniAgentOS.git ~/OmniAgentOS
#   cd ~/OmniAgentOS && uv sync
#   bash scripts/workqueue/enroll.sh --primary mac-studio.local:8487 \
#        --labels build,gate --max-concurrent 3
#
# In order, and ABORTS ON THE FIRST FAILURE WITH A NAMED REMEDY:
#   1. Preflight: one probe per §5.1 requirement, no retry loops.
#   2. POST /v1/machines to register this machine.
#   3. Render + install the launchd plist (darwin) or systemd unit (linux).
#   4. Verify the machine appears via GET /v1/machines within 60 s
#      (one curl + one sleep — not a poll loop).
#
# Does NOT run any DB migration — the joining machine is stateless apart from
# its git mirrors and logs (§5.2: "Do not run `make migrate` on a joining
# machine. There is no local DB.").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONNECTIONS_ENV="${HOME}/.config/omni/connections.env"

# ---------------------------------------------------------------------------
# Defaults / flags
# ---------------------------------------------------------------------------
PRIMARY=""
LABELS="build"
MAX_CONCURRENT=2
CEILING_FRACTION=""
WQ_HOME="${HOME}/wq"
SLOTS=""
WORKER_USER="omniworker"
WORKER_USER_GIVEN=0
RESERVED_FOR=""
RESERVED_FOR_GIVEN=0

usage() {
  cat >&2 <<'EOF'
usage: enroll.sh --primary <host:port> [--labels a,b,c] [--max-concurrent N]
                  [--ceiling-fraction F] [--wq-home PATH] [--slots N]
                  [--worker-user NAME] [--reserved-for OWNER]

  --primary <host:port>   Where wq-server is reachable from THIS machine
                           (tailnet hostname/IP, or 127.0.0.1:8487 when a
                           reverse SSH tunnel already forwards it — see
                           serve.sh tunnel and docs/workqueue/RUNBOOK.md).
  --labels a,b,c           Capability labels this machine declares
                           (configs/workqueue.yaml:labels.known). Default: build.
  --max-concurrent N        Worker slots this machine offers. Default: 2.
                           Guidance: perf_cores/4, capped at 4; 1 on a box
                           that also runs a live merge gate.
  --ceiling-fraction F      Load-gate ceiling as a fraction of ncpu. Default:
                           configs/workqueue.yaml:capacity.ceiling_fraction_default (0.75).
  --wq-home PATH            Worker's git-mirror + log root. Default: ~/wq.
  --slots N                 Worker processes to launch. Default: --max-concurrent.
  --worker-user NAME        Linux-only, and only consulted when enroll.sh is
                           invoked as root: the NON-ROOT account the systemd
                           unit's acceptance-command worker runs as. Default:
                           omniworker (see grant-access.sh). When this flag is
                           NOT given, enroll.sh falls back to `logname`
                           ONLY if it succeeds and returns a non-root user;
                           otherwise it aborts rather than risk installing a
                           unit that runs submitter-supplied commands as
                           root. Ignored on darwin (the launchd job always
                           runs as the invoking GUI user, already non-root).
  --reserved-for OWNER      Reserve THIS machine's worker for one owner: it then
                           claims ONLY units whose submitted_by equals OWNER. This
                           is resource hygiene to keep other people's pool work off
                           a machine reserved for one person under COOPERATIVE use.
                           It is NOT a hard security boundary: submitted_by is
                           self-declared under the single shared pool token, so a
                           token-holder who deliberately submits AS OWNER can still
                           land work on this box — it prevents accidental
                           contention, not deliberate forging (a hard boundary
                           needs per-submitter auth tokens, out of scope). Given
                           but empty/blank ⇒ enroll.sh ABORTS before install (fail
                           closed): a broken reservation must claim NOTHING rather
                           than silently become an unrestricted worker. The owner
                           must be a username-like token ([A-Za-z0-9_-]); anything
                           else is rejected. Omit it to enroll a normal
                           unrestricted worker.
EOF
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --primary) PRIMARY="$2"; shift 2 ;;
    --labels) LABELS="$2"; shift 2 ;;
    --max-concurrent) MAX_CONCURRENT="$2"; shift 2 ;;
    --ceiling-fraction) CEILING_FRACTION="$2"; shift 2 ;;
    --wq-home) WQ_HOME="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
    --worker-user) WORKER_USER="$2"; WORKER_USER_GIVEN=1; shift 2 ;;
    --reserved-for) RESERVED_FOR="$2"; RESERVED_FOR_GIVEN=1; shift 2 ;;
    -h|--help) usage ;;
    *) echo "enroll.sh: unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$PRIMARY" ] || usage
[ -n "$SLOTS" ] || SLOTS="$MAX_CONCURRENT"
[ -n "$CEILING_FRACTION" ] || CEILING_FRACTION="0.75"

abort() {
  echo "" >&2
  echo "enroll.sh: ABORT — $1" >&2
  echo "  remedy: $2" >&2
  exit 1
}

step() { echo "enroll.sh: [$1] $2" >&2; }

# ---------------------------------------------------------------------------
# Reserved worker (worker-declared, fail closed). --reserved-for OWNER makes
# this box claim ONLY OWNER's units — resource hygiene under COOPERATIVE use,
# NOT a security boundary (submitted_by is self-declared under the shared pool
# token, so a token-holder who submits AS OWNER can still land work here). Given
# but blank ⇒ abort: a broken reservation must claim NOTHING, never silently
# become an unrestricted worker.
# ---------------------------------------------------------------------------
RESERVED_FOR_ARG=""
if [ "$RESERVED_FOR_GIVEN" -eq 1 ]; then
  RESERVED_FOR_TRIMMED="$(printf '%s' "$RESERVED_FOR" | tr -d '[:space:]')"
  [ -n "$RESERVED_FOR_TRIMMED" ] \
    || abort "--reserved-for was given but is empty/whitespace" "pass a real owner (e.g. --reserved-for owner) so this box claims only that owner's units, or drop the flag to enroll a normal unrestricted worker. A blank reservation is refused rather than silently made unrestricted (fail closed)."
  # OWNER is substituted into a `bash -lc "... ${RESERVED_FOR_ARG}"` ExecStart
  # STRING (and sed-substituted into the unit with '#' as the delimiter), so a
  # shell metacharacter here is command injection into the generated daemon unit
  # and a '#' collides with the sed delimiter. An owner is a username-like token,
  # so restrict it to a safe allowlist and abort on anything else (fail closed).
  case "$RESERVED_FOR" in
    *[!A-Za-z0-9_-]*)
      abort "--reserved-for OWNER '${RESERVED_FOR}' contains characters outside [A-Za-z0-9_-]" "an owner is a username-like token; drop the shell metacharacters (spaces, ';', '#', '\"', backticks, \$() etc.). This is refused rather than substituted into the daemon unit's ExecStart, where it would be command injection (fail closed)."
      ;;
  esac
  RESERVED_FOR_ARG="--reserved-for ${RESERVED_FOR}"
  echo "enroll.sh: reserved worker — this box will claim ONLY units submitted_by='${RESERVED_FOR}' (resource hygiene, cooperative use; NOT a hard boundary — submitted_by is self-declared under the shared token)." >&2
fi

OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Darwin) OS_LABEL="darwin" ;;
  Linux)  OS_LABEL="linux" ;;
  *) abort "unsupported OS: $OS_NAME" "enroll.sh only supports macOS (Darwin) and Linux." ;;
esac

# ---------------------------------------------------------------------------
# 1. Preflight — §5.1, one probe each, abort on first failure
# ---------------------------------------------------------------------------

step 1 "preflight: OS/arch"
ARCH="$(uname -m)"
echo "enroll.sh: OS=${OS_NAME} ARCH=${ARCH}" >&2

# worker.py bounds each agent run with gtimeout (darwin) / timeout (linux)
# per SPEC §7 step 5; this just confirms the binary the worker will shell
# out to actually exists on this box before it joins the pool.
if [ "$OS_LABEL" = "darwin" ]; then
  step 1 "preflight: Homebrew coreutils (gtimeout)"
  command -v gtimeout >/dev/null 2>&1 \
    || abort "gtimeout not found" "brew install coreutils"
else
  step 1 "preflight: coreutils timeout"
  command -v timeout >/dev/null 2>&1 \
    || abort "timeout not found" "install GNU coreutils (apt install coreutils / yum install coreutils)"
fi

step 1 "preflight: uv + Python 3.12"
command -v uv >/dev/null 2>&1 \
  || abort "uv not found on PATH" "install uv: https://docs.astral.sh/uv/getting-started/installation/"
uv python list 2>/dev/null | grep -q '3\.12' \
  || abort "Python 3.12 not registered with uv" "run: uv python install 3.12"

step 1 "preflight: git >= 2.39"
command -v git >/dev/null 2>&1 || abort "git not found" "install git"
GIT_VERSION="$(git --version | awk '{print $3}')"
GIT_MAJOR="$(echo "$GIT_VERSION" | cut -d. -f1)"
GIT_MINOR="$(echo "$GIT_VERSION" | cut -d. -f2)"
if [ "$GIT_MAJOR" -lt 2 ] || { [ "$GIT_MAJOR" -eq 2 ] && [ "$GIT_MINOR" -lt 39 ]; }; then
  abort "git ${GIT_VERSION} is older than 2.39" "brew upgrade git (darwin) / apt/yum upgrade git (linux)"
fi

step 1 "preflight: repo venv"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || abort "no venv at ${PYTHON_BIN}" "cd ${REPO_ROOT} && uv sync"

step 1 "preflight: connections.env present, mode 600"
[ -f "$CONNECTIONS_ENV" ] \
  || abort "${CONNECTIONS_ENV} missing" "run scripts/workqueue/mint-token.sh on the primary, then copy WQ_TOKEN onto this machine's connections.env (never copy the primary's full connections.env — it holds unrelated production secrets, see docs/workqueue/ACCESS.md)"
ENV_MODE="$(stat -f '%Lp' "$CONNECTIONS_ENV" 2>/dev/null || stat -c '%a' "$CONNECTIONS_ENV" 2>/dev/null || echo '')"
if [ "$ENV_MODE" != "600" ]; then
  chmod 600 "$CONNECTIONS_ENV" \
    || abort "cannot chmod 600 ${CONNECTIONS_ENV} (mode was ${ENV_MODE:-unknown})" "chmod 600 ${CONNECTIONS_ENV}"
fi
grep -q '^WQ_TOKEN=' "$CONNECTIONS_ENV" \
  || abort "WQ_TOKEN not set in ${CONNECTIONS_ENV}" "mint it on the primary (scripts/workqueue/mint-token.sh) and copy the WQ_TOKEN=<hex> line into this file"

set -a
# shellcheck disable=SC1090
source "$CONNECTIONS_ENV"
set +a
[ -n "${WQ_TOKEN:-}" ] || abort "WQ_TOKEN sourced empty" "re-mint with scripts/workqueue/mint-token.sh"

step 1 "preflight: network route to primary on :8487"
# SECURITY: the bearer token must never appear on argv (readable via `ps` by
# any local user for the process lifetime) — fed to curl through -K stdin
# config instead of -H on the command line.
if ! curl -sf -m 5 -K - "http://${PRIMARY}/v1/health" >/dev/null <<CURLCFG
header = "Authorization: Bearer ${WQ_TOKEN}"
CURLCFG
then
  abort "cannot reach http://${PRIMARY}/v1/health" "confirm the primary's wq-server is running (serve.sh run/install) and that this machine has a route: tailnet membership, or an active reverse tunnel (serve.sh tunnel --host <this-machine>) run FROM the primary. If PRIMARY is 127.0.0.1:8487, the tunnel must already be up."
fi

step 1 "preflight: SSH key trusted by primary (best-effort, informational only)"
PRIMARY_HOST="${PRIMARY%%:*}"
if [ -n "$PRIMARY_HOST" ] && [ "$PRIMARY_HOST" != "127.0.0.1" ] && [ "$PRIMARY_HOST" != "localhost" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$PRIMARY_HOST" true >/dev/null 2>&1; then
    echo "enroll.sh: SSH to ${PRIMARY_HOST} OK" >&2
  else
    echo "enroll.sh: WARNING — SSH to ${PRIMARY_HOST} did not succeed. This is only required for the enrollment preflight and for the primary's reverse tunnel (if this machine is not on the tailnet); the HTTP health check above already proved queue reachability, so enrollment continues." >&2
  fi
fi

step 1 "preflight: agent CLIs for declared labels"
IFS=',' read -ra LABEL_ARR <<< "$LABELS"
for lbl in "${LABEL_ARR[@]}"; do
  case "$lbl" in
    agent-codex) command -v codex >/dev/null 2>&1 || abort "label 'agent-codex' declared but codex CLI not found" "install/auth the codex CLI, or drop 'agent-codex' from --labels" ;;
    agent-claude) command -v claude >/dev/null 2>&1 || abort "label 'agent-claude' declared but claude CLI not found" "install/auth the claude CLI, or drop 'agent-claude' from --labels" ;;
    agent-grok) command -v grok >/dev/null 2>&1 || abort "label 'agent-grok' declared but grok CLI not found" "install/auth the grok CLI, or drop 'agent-grok' from --labels" ;;
  esac
done

step 1 "preflight: bare mirror of this repo"
mkdir -p "${WQ_HOME}/repos" "${WQ_HOME}/logs"
REPO_SLUG="$(basename "$REPO_ROOT")"
MIRROR_PATH="${WQ_HOME}/repos/${REPO_SLUG}.git"
if [ ! -d "$MIRROR_PATH" ]; then
  ORIGIN_URL="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  [ -n "$ORIGIN_URL" ] || abort "no 'origin' remote on ${REPO_ROOT} and no existing mirror at ${MIRROR_PATH}" "git -C ${REPO_ROOT} remote add origin <url>, or pre-create the mirror yourself: git clone --mirror <repo_url> ${MIRROR_PATH}"
  git clone --mirror "$ORIGIN_URL" "$MIRROR_PATH" \
    || abort "git clone --mirror ${ORIGIN_URL} failed" "check network access to the git host and retry manually"
fi

step 1 "preflight: disk space (5G floor, ENOSPC killed jobs silently before — MACHINE-FLEET-PLAN.md §6)"
AVAIL_KB="$(df -Pk "$WQ_HOME" | tail -1 | awk '{print $4}')"
if [ "$AVAIL_KB" -lt 5242880 ]; then
  abort "less than 5G free at ${WQ_HOME} (${AVAIL_KB}KB available)" "free disk space before enrolling — a starved worker fails jobs as instrument-error, not candidate-defect, but it still stalls the pool"
fi

echo "enroll.sh: preflight OK" >&2

# ---------------------------------------------------------------------------
# 2. Register with the primary — POST /v1/machines
# ---------------------------------------------------------------------------

step 2 "detecting machine identity and capacity"
if [ "$OS_LABEL" = "darwin" ]; then
  MACHINE_ID="$(scutil --get LocalHostName)"
  HOSTNAME_FULL="$(hostname)"
  NCPU="$(sysctl -n hw.ncpu)"
  PERF_CORES="$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || echo "$NCPU")"
  MEM_BYTES="$(sysctl -n hw.memsize)"
  MEM_GB="$(awk -v b="$MEM_BYTES" 'BEGIN{printf "%.1f", b/1073741824}')"
else
  MACHINE_ID="$(hostname -s)"
  HOSTNAME_FULL="$(hostname -f 2>/dev/null || hostname)"
  NCPU="$(nproc)"
  PERF_CORES="$NCPU"
  MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
  MEM_GB="$(awk -v k="$MEM_KB" 'BEGIN{printf "%.1f", k/1048576}')"
fi
[ -n "$MACHINE_ID" ] || abort "could not determine machine_id" "on darwin: scutil --get LocalHostName must return a name (System Settings > General > Sharing > Local hostname); on linux: hostname -s must return a name"

AGENT_VERSION="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")"

# Ensure the os label is present so configs/workqueue.yaml:labels.known claim
# routing can distinguish darwin/linux workers.
HAS_OS_LABEL=0
for lbl in "${LABEL_ARR[@]}"; do
  [ "$lbl" = "$OS_LABEL" ] && HAS_OS_LABEL=1
done
if [ "$HAS_OS_LABEL" -eq 0 ]; then
  LABEL_ARR+=("$OS_LABEL")
fi

ENROLL_JSON="$(python3 - "$MACHINE_ID" "$HOSTNAME_FULL" "$OS_LABEL" "$MAX_CONCURRENT" "$AGENT_VERSION" "$NCPU" "$PERF_CORES" "$MEM_GB" "$CEILING_FRACTION" "${LABEL_ARR[@]}" <<'PYEOF'
import json, sys
(machine_id, hostname, os_name, max_concurrent, agent_version,
 ncpu, perf_cores, mem_gb, ceiling_fraction, *labels) = sys.argv[1:]
print(json.dumps({
    "machine_id": machine_id,
    "hostname": hostname,
    "os": os_name,
    "labels": labels,
    "max_concurrent": int(max_concurrent),
    "agent_version": agent_version,
    "ncpu": int(ncpu),
    "perf_cores": int(perf_cores),
    "mem_gb": float(mem_gb),
    "ceiling_fraction": float(ceiling_fraction),
}))
PYEOF
)"

step 2 "POST /v1/machines (${MACHINE_ID}, labels=${LABEL_ARR[*]}, max_concurrent=${MAX_CONCURRENT})"
# A predictable /tmp/<name>.$$ path is world-readable and its name is
# guessable from the PID — use mktemp and remove it on exit (trap) so a
# world-readable temp file can't leak the enrollment response.
ENROLL_RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/enroll-response.XXXXXXXX.json")"
trap 'rm -f "$ENROLL_RESPONSE_FILE"' EXIT
# SECURITY: bearer token off argv — see the preflight curl call above.
HTTP_CODE="$(curl -sS -o "$ENROLL_RESPONSE_FILE" -w '%{http_code}' \
  -X POST "http://${PRIMARY}/v1/machines" \
  -H "Content-Type: application/json" \
  -d "$ENROLL_JSON" \
  -K - <<CURLCFG
header = "Authorization: Bearer ${WQ_TOKEN}"
CURLCFG
)" || abort "POST /v1/machines failed to connect" "confirm wq-server is reachable at http://${PRIMARY} (same check as the preflight health probe)"

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; then
  BODY="$(cat "$ENROLL_RESPONSE_FILE" 2>/dev/null || echo '<no body>')"
  abort "POST /v1/machines returned HTTP ${HTTP_CODE}: ${BODY}" "check WQ_TOKEN matches the primary's, and that the JSON body matches contract.schema.json:machine_enroll"
fi
echo "enroll.sh: registered ${MACHINE_ID}" >&2

# ---------------------------------------------------------------------------
# 3. Install the worker service
# ---------------------------------------------------------------------------

LOG_DIR="${WQ_HOME}/logs"
mkdir -p "$LOG_DIR"

if [ "$OS_LABEL" = "darwin" ]; then
  step 3 "installing launchd LaunchAgent"
  LABEL="com.omniagentos.wq-worker"
  PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
  TEMPLATE="${SCRIPT_DIR}/wq-worker.plist.template"
  [ -f "$TEMPLATE" ] || abort "missing template ${TEMPLATE}" "re-clone the repo; scripts/workqueue/wq-worker.plist.template must ship with enroll.sh"
  mkdir -p "$(dirname "$PLIST_DEST")"
  sed \
    -e "s#__LABEL__#${LABEL}#g" \
    -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" \
    -e "s#__REPO_ROOT__#${REPO_ROOT}#g" \
    -e "s#__MACHINE_ID__#${MACHINE_ID}#g" \
    -e "s#__SERVER_URL__#http://${PRIMARY}#g" \
    -e "s#__SLOTS__#${SLOTS}#g" \
    -e "s#__WQ_HOME__#${WQ_HOME}#g" \
    -e "s#__CONNECTIONS_ENV__#${CONNECTIONS_ENV}#g" \
    -e "s#__LOG_DIR__#${LOG_DIR}#g" \
    -e "s#__RESERVED_FOR_ARG__#${RESERVED_FOR_ARG}#g" \
    "$TEMPLATE" > "$PLIST_DEST"
  launchctl bootout "gui/$(id -u)" "$PLIST_DEST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" \
    || abort "launchctl bootstrap failed for ${PLIST_DEST}" "launchctl print gui/$(id -u)/${LABEL} for detail; a common cause is a stale bootout — retry: launchctl bootout gui/\$(id -u) ${PLIST_DEST}"
  launchctl enable "gui/$(id -u)/${LABEL}"
  echo "enroll.sh: launchd job ${LABEL} bootstrapped from ${PLIST_DEST}" >&2
else
  step 3 "installing systemd unit"
  TEMPLATE="${SCRIPT_DIR}/wq-worker.service.template"
  [ -f "$TEMPLATE" ] || abort "missing template ${TEMPLATE}" "re-clone the repo; scripts/workqueue/wq-worker.service.template must ship with enroll.sh"
  if [ "$(id -u)" -eq 0 ]; then
    UNIT_DEST="/etc/systemd/system/wq-worker.service"
    # SECURITY: the systemd unit runs submitter-provided acceptance commands
    # (SPEC §7) — it must NEVER run as root. Resolve, in order: an explicit
    # --worker-user; else `logname` IF it succeeds AND is non-root (logname
    # fails under a headless `ssh root@host bash enroll.sh ...` provisioning
    # run — no controlling tty — which is exactly the case that used to
    # silently fall back to `echo root` and hand every bearer-token holder
    # root RCE). No code path below may emit User=root.
    if [ "$WORKER_USER_GIVEN" -eq 1 ]; then
      RESOLVED_WORKER_USER="$WORKER_USER"
    else
      RESOLVED_WORKER_USER="$(logname 2>/dev/null || true)"
      if [ -z "$RESOLVED_WORKER_USER" ] || [ "$RESOLVED_WORKER_USER" = "root" ]; then
        abort "no --worker-user given and \`logname\` failed or returned root (the normal case for a headless 'ssh root@host bash enroll.sh ...' provisioning run) — refusing to install a systemd unit that would run acceptance commands as root" "create a non-root worker user first: sudo scripts/workqueue/grant-access.sh --create-worker-account, then re-run enroll.sh with --worker-user omniworker"
      fi
    fi
    [ -n "$RESOLVED_WORKER_USER" ] && [ "$RESOLVED_WORKER_USER" != "root" ] \
      || abort "resolved worker user is empty or root ('${RESOLVED_WORKER_USER:-<empty>}')" "pass a non-root --worker-user, e.g. --worker-user omniworker"
    id -u "$RESOLVED_WORKER_USER" >/dev/null 2>&1 \
      || abort "worker user '${RESOLVED_WORKER_USER}' does not exist on this box" "create it first: sudo scripts/workqueue/grant-access.sh --create-worker-account --user ${RESOLVED_WORKER_USER}"
    USER_DIRECTIVE="User=${RESOLVED_WORKER_USER}"
    WANTED_BY="multi-user.target"
    SYSTEMCTL="systemctl"
  else
    UNIT_DEST="${HOME}/.config/systemd/user/wq-worker.service"
    mkdir -p "$(dirname "$UNIT_DEST")"
    USER_DIRECTIVE=""
    WANTED_BY="default.target"
    SYSTEMCTL="systemctl --user"
  fi
  if [ -n "$USER_DIRECTIVE" ]; then
    sed \
      -e "s#__MACHINE_ID__#${MACHINE_ID}#g" \
      -e "s#__REPO_ROOT__#${REPO_ROOT}#g" \
      -e "s#__USER_DIRECTIVE_LINE__#${USER_DIRECTIVE}#g" \
      -e "s#__CONNECTIONS_ENV__#${CONNECTIONS_ENV}#g" \
      -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" \
      -e "s#__SERVER_URL__#http://${PRIMARY}#g" \
      -e "s#__SLOTS__#${SLOTS}#g" \
      -e "s#__WQ_HOME__#${WQ_HOME}#g" \
      -e "s#__LOG_DIR__#${LOG_DIR}#g" \
      -e "s#__WANTED_BY__#${WANTED_BY}#g" \
      -e "s#__RESERVED_FOR_ARG__#${RESERVED_FOR_ARG}#g" \
      "$TEMPLATE" > "$UNIT_DEST"
  else
    sed \
      -e "s#__MACHINE_ID__#${MACHINE_ID}#g" \
      -e "s#__REPO_ROOT__#${REPO_ROOT}#g" \
      -e "/__USER_DIRECTIVE_LINE__/d" \
      -e "s#__CONNECTIONS_ENV__#${CONNECTIONS_ENV}#g" \
      -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" \
      -e "s#__SERVER_URL__#http://${PRIMARY}#g" \
      -e "s#__SLOTS__#${SLOTS}#g" \
      -e "s#__WQ_HOME__#${WQ_HOME}#g" \
      -e "s#__LOG_DIR__#${LOG_DIR}#g" \
      -e "s#__WANTED_BY__#${WANTED_BY}#g" \
      -e "s#__RESERVED_FOR_ARG__#${RESERVED_FOR_ARG}#g" \
      "$TEMPLATE" > "$UNIT_DEST"
  fi
  if [ "$(id -u)" -ne 0 ]; then
    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || echo "enroll.sh: WARNING — could not enable-linger for $(id -un); the user unit will stop at logout unless an admin runs: sudo loginctl enable-linger $(id -un)" >&2
  fi
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL enable --now wq-worker \
    || abort "$SYSTEMCTL enable --now wq-worker failed" "$SYSTEMCTL status wq-worker for detail"
  echo "enroll.sh: systemd unit installed at ${UNIT_DEST} and started" >&2
fi

# ---------------------------------------------------------------------------
# 4. Verify — ONE curl + sleep, not a poll loop
# ---------------------------------------------------------------------------

step 4 "verifying ${MACHINE_ID} appears in the pool (waiting 10s for the first machine_beat)"
sleep 10
# SECURITY: bearer token off argv — see the preflight curl call above.
VERIFY_JSON="$(curl -sf -m 10 -K - "http://${PRIMARY}/v1/machines" <<CURLCFG || true
header = "Authorization: Bearer ${WQ_TOKEN}"
CURLCFG
)"
if echo "$VERIFY_JSON" | python3 -c "
import json, sys
try:
    machines = json.load(sys.stdin)
except Exception:
    sys.exit(1)
ids = [m.get('machine_id') for m in machines] if isinstance(machines, list) else []
sys.exit(0 if '${MACHINE_ID}' in ids else 1)
" 2>/dev/null; then
  echo "enroll.sh: OK — ${MACHINE_ID} is enrolled and visible via GET /v1/machines." >&2
else
  echo "enroll.sh: WARNING — ${MACHINE_ID} was registered (step 2 succeeded) but did not appear in GET /v1/machines within 10s. This does not mean enrollment failed — machine_beat may just be slow on a busy worker start. Check manually (token via stdin, never -H, so it never lands in ps): printf 'header = \"Authorization: Bearer %s\"\n' \"\$WQ_TOKEN\" | curl -sf -K - http://${PRIMARY}/v1/machines | python3 -m json.tool" >&2
fi

echo "enroll.sh: done. Worker for ${MACHINE_ID} is running (${SLOTS} slots, labels: ${LABEL_ARR[*]})." >&2
