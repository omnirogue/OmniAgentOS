#!/usr/bin/env bash
# Launch OmniAgentOS as a separate, product-scoped process group.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# launchd and non-interactive shells do not inherit the operator's connector
# credentials.  Source the names-and-values vault only into this process tree;
# neither this script nor the supervisor prints credential values.  This must
# precede launch-env.sh so every supervised child sees the same environment.
if [ -f "$HOME/.config/omni/connections.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.config/omni/connections.env"
  set +a
fi

# Parse args before sourcing launch-env.sh
SIM_MODE_ARG=""
SIM_CAMPAIGN_ARG=""
POSITIONAL_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --simulate)
      SIM_MODE_ARG=1
      shift
      ;;
    --campaign=*)
      SIM_MODE_ARG=1
      SIM_CAMPAIGN_ARG="${1#*=}"
      shift
      ;;
    --campaign)
      if [ $# -lt 2 ]; then
        echo "Error: --campaign requires an argument" >&2
        exit 2
      fi
      SIM_MODE_ARG=1
      SIM_CAMPAIGN_ARG="$2"
      shift 2
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ -n "${OMNIAGENTOS_SIM_MODE:-}" ] && [ "${OMNIAGENTOS_SIM_MODE}" != "1" ]; then
  echo "FATAL(sim-isolation): OMNIAGENTOS_SIM_MODE='${OMNIAGENTOS_SIM_MODE}' is invalid; set it exactly to '1' for simulation or unset it for production." >&2
  exit 1
fi

if [ -n "$SIM_MODE_ARG" ]; then
  export OMNIAGENTOS_SIM_MODE=1
fi
if [ -n "$SIM_CAMPAIGN_ARG" ]; then
  export OMNIAGENTOS_SIM_CAMPAIGN="$SIM_CAMPAIGN_ARG"
fi

if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then
  # SIM_ROOT is an operator-selected input, not a derived state path. Keep it
  # in this shell only; launch-env's refusal loop must still reject an
  # inherited exported SIM_ROOT on direct re-entry.
  _omni_sim_root_override="${OMNIAGENTOS_SIM_ROOT:-}"
  unset OMNIAGENTOS_SIM_ROOT OMNIAGENTOS_PROJECT_BASES
  if [ -z "${OMNIAGENTOS_SIM_PORTS_EXPLICIT+x}" ]; then
    if [ "${OMNIAGENTOS_SIM_ENV_LOADED:-}" = "1" ]; then
      # These are inherited derived ports from a prior launcher invocation.
      export OMNIAGENTOS_SIM_PORTS_EXPLICIT=0
    elif [ -n "${OMNIAGENTOS_API_PORT:-}" ] || [ -n "${OMNIAGENTOS_DASH_PORT:-}" ]; then
      export OMNIAGENTOS_SIM_PORTS_EXPLICIT=1
    else
      export OMNIAGENTOS_SIM_PORTS_EXPLICIT=0
    fi
  fi

  # A supervised child is an explicit fresh entry. Remove the parent's
  # campaign-derived state so launch-env can rebuild and validate it.
  if [ -n "$SIM_MODE_ARG" ]; then
    unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED \
      OMNIAGENTOS_SIM_ENV_NONCE OMNIAGENTOS_DB OMNIAGENTOS_VAR \
      OMNIAGENTOS_VAR_DIR OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR \
      OMNIAGENTOS_SIM_CAMPAIGN_ROOT OMNIAGENTOS_SWARM_DIR \
      OMNIAGENTOS_MEMORIES_DIR OMNIAGENTOS_TRANSCRIPTS_ROOT
    if [ "${OMNIAGENTOS_SIM_PORTS_EXPLICIT:-}" != "1" ]; then
      unset OMNIAGENTOS_API_PORT OMNIAGENTOS_DASH_PORT
    fi
  fi

  # Do not turn an inherited loaded marker into a skip. Initial ambient sim
  # starts with that marker must reach launch-env and be refused; explicit
  # re-entry above is the only path that clears it.
  if [ "${OMNIAGENTOS_SIM_ENV_LOADED:-}" != "1" ]; then
    _omni_sim_launcher_nonce="$(uuidgen 2>/dev/null || printf '%s-%s-%s' "$(date +%s)" "$$" "${RANDOM:-0}")"
    export OMNIAGENTOS_SIM_ENV_NONCE="$_omni_sim_launcher_nonce"
  fi
fi

if [ ${#POSITIONAL_ARGS[@]} -gt 0 ]; then
  set -- "${POSITIONAL_ARGS[@]}"
else
  set --
fi

# Canonical OMNIAGENTOS_DB + runtime env (idempotent; never clobbers presets).
# shellcheck source=scripts/launch-env.sh
. "$ROOT/scripts/launch-env.sh"

# fd budget for the fleet (docs/architecture/concurrency-ramp.md): a launchd or
# stock shell inherits a 256 soft limit ≈ 32 sessions, and the failure mode is
# a confusing mid-spawn EMFILE, not a queued run. Raise-only — an already-high
# interactive limit is never lowered.
_fd_soft="$(ulimit -Sn 2>/dev/null || echo unlimited)"
if [ "$_fd_soft" != "unlimited" ] && [ "$_fd_soft" -lt 8192 ] 2>/dev/null; then
  ulimit -Sn 8192 2>/dev/null || echo "WARN: fd soft limit stuck at $_fd_soft;" \
    "the fleet is capped at ~$(((_fd_soft - 128) / 4)) concurrent sessions" >&2
fi

PYBIN="${OMNIAGENTOS_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYBIN" ]; then
  echo "FATAL: no executable project Python at $PYBIN" >&2
  exit 1
fi

_canonical_path() {
  "$PYBIN" - "$1" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

RUNTIME_ROOT=$(_canonical_path "${OMNIAGENTOS_VAR_DIR:-$ROOT/var/runtime}")
export OMNIAGENTOS_VAR_DIR="$RUNTIME_ROOT"
export OMNIAGENTOS_VAR=$(_canonical_path "${OMNIAGENTOS_VAR:-$RUNTIME_ROOT}")
export OMNIAGENTOS_DB=$(_canonical_path "${OMNIAGENTOS_DB:-$RUNTIME_ROOT/state.sqlite3}")
export OMNIAGENTOS_LEDGER_DIR=$(_canonical_path "${OMNIAGENTOS_LEDGER_DIR:-$RUNTIME_ROOT/ledger}")
export OMNIAGENTOS_VAULT_DIR=$(_canonical_path "${OMNIAGENTOS_VAULT_DIR:-$RUNTIME_ROOT/vault}")
export OMNIAGENTOS_API_PORT="${OMNIAGENTOS_API_PORT:-8485}"
export OMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-3003}"
# The Caddy trusted-hop front door (LS-003 / D-1). Operators and LiveSim browse
# THIS port; :3003 is reachable only by Caddy and refuses everything else.
#
# DERIVED from the dashboard port rather than fixed, so it follows simulation
# mode. `launch-env.sh` gives a `--simulate` campaign its own OMNIAGENTOS_DASH_PORT
# precisely so a sim fleet cannot collide with the live one; a hardcoded 3013
# would have reintroduced that collision on the front door alone, and the
# symptom (caddy fails to bind, the whole sim fleet tears down) would have
# pointed nowhere near this line. Default dashboard 3003 => 3013.
export OMNIAGENTOS_CADDY_PORT="${OMNIAGENTOS_CADDY_PORT:-$((OMNIAGENTOS_DASH_PORT + 10))}"
# The browser opens EventSource connections DIRECTLY to the API (:8485), so the
# API's CORS allowlist is a carrier of the same "which origin is the dashboard"
# fact as the hop boundary. Moving operators to the Caddy port would otherwise
# silence every live event stream with a CORS refusal while the page itself
# looked fine. Exported for the `api` child; appended, never clobbering.
if [ -n "${OMNIAGENTOS_CORS_EXTRA_PORTS:-}" ]; then
  export OMNIAGENTOS_CORS_EXTRA_PORTS="${OMNIAGENTOS_CORS_EXTRA_PORTS},$OMNIAGENTOS_CADDY_PORT"
else
  export OMNIAGENTOS_CORS_EXTRA_PORTS="$OMNIAGENTOS_CADDY_PORT"
fi
export OMNIAGENTOS_SCOPE_LOCKS="${OMNIAGENTOS_SCOPE_LOCKS:-shadow}"
export OMNIAGENTOS_SWARM_EXECUTE=1
export OMNIAGENTOS_FAST_DISPATCH=1
export OMNIAGENTOS_SWARM_TARGET_CAP=10
export OMNIAGENTOS_GRAPH_RUNTIME=1
export OMNIAGENTOS_ROUTER_SHADOW=1
export OMNIAGENTOS_ACCOUNT_POOL=1
# Operator-approved project bases (os.pathsep-separated). The old default
# (~/OmniAgentOS) is a deleted path — this repo and ~/work are the real bases.
if [ "${OMNIAGENTOS_SIM_MODE:-}" != "1" ]; then
  export OMNIAGENTOS_PROJECT_BASES="${OMNIAGENTOS_PROJECT_BASES:-$HOME/OmniAgentOS:$HOME/work}"
fi
PID_FILE=$(_canonical_path "${OMNIAGENTOS_SUPERVISOR_PID_FILE:-$RUNTIME_ROOT/supervisor.json}")
HEALTH_TIMEOUT="${OMNIAGENTOS_HEALTH_TIMEOUT_SECONDS:-30}"
STOP_TIMEOUT="${OMNIAGENTOS_STOP_TIMEOUT_SECONDS:-10}"

# Containment must be filesystem-true (inode ancestry), not string-prefix.
# Delegate to the same bottom-layer primitive used by the API and scope code;
# never fork this security boundary back into the launcher.
_path_inside() {
  "$PYBIN" -m omniagentos.path_containment "$1" "$2"
}

if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then
  # Refuse an inherited OMNIAGENTOS_SUPERVISOR_PID_FILE in sim mode.
  if [ -n "${OMNIAGENTOS_SUPERVISOR_PID_FILE:-}" ]; then
    echo "FATAL(sim-isolation): inherited OMNIAGENTOS_SUPERVISOR_PID_FILE=${OMNIAGENTOS_SUPERVISOR_PID_FILE} from the ambient environment; simulation mode requires OMNIAGENTOS_SUPERVISOR_PID_FILE to resolve under the campaign root ${OMNIAGENTOS_SIM_CAMPAIGN_ROOT}. Unset OMNIAGENTOS_SUPERVISOR_PID_FILE (e.g. 'env -u OMNIAGENTOS_SUPERVISOR_PID_FILE ...') and re-run." >&2
    exit 1
  fi

  CANON_CAMPAIGN_ROOT=$(_canonical_path "$OMNIAGENTOS_SIM_CAMPAIGN_ROOT")

  # Campaigns must live in the external simulation root. The historical
  # $ROOT/var waiver is intentionally gone; migrate those campaigns first.
  if _path_inside "$CANON_CAMPAIGN_ROOT" "$ROOT"; then
    echo "FATAL: runtime state path is source-controlled: $CANON_CAMPAIGN_ROOT; migrate the campaign under ${OMNIAGENTOS_SIM_ROOT} (or an explicit external SIM_ROOT)" >&2
    exit 1
  fi

  if [ -d "$HOME/OmniAgentOS" ] && _path_inside "$CANON_CAMPAIGN_ROOT" "$HOME/OmniAgentOS"; then
    echo "FATAL: runtime state path is inside the sibling product ~/OmniAgentOS: $CANON_CAMPAIGN_ROOT" >&2
    exit 1
  fi

  # ONLY after both refusals pass, create the campaign root so it has a real inode.
  mkdir -p "$CANON_CAMPAIGN_ROOT"

  # Require each derived variable to be inside the campaign root.
  for var_name in OMNIAGENTOS_VAR_DIR OMNIAGENTOS_VAR OMNIAGENTOS_DB OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR PID_FILE; do
    var_val="${!var_name}"
    if ! _path_inside "$var_val" "$CANON_CAMPAIGN_ROOT"; then
      echo "FATAL(sim-isolation): ${var_name}=${var_val} escapes the campaign root ${CANON_CAMPAIGN_ROOT}" >&2
      exit 1
    fi
  done
else
  # Generic production mode verification.
  for runtime_path in \
    "$RUNTIME_ROOT" "$OMNIAGENTOS_VAR" "$OMNIAGENTOS_DB" \
    "$OMNIAGENTOS_LEDGER_DIR" "$OMNIAGENTOS_VAULT_DIR" "$PID_FILE"
  do
    if _path_inside "$runtime_path" "$ROOT/var"; then
      :
    elif _path_inside "$runtime_path" "$ROOT"; then
      echo "FATAL: runtime state path is source-controlled: $runtime_path" >&2
      exit 1
    fi

    # Always (sim or production) refuse any runtime path inside $HOME/OmniAgentOS
    if [ -d "$HOME/OmniAgentOS" ] && _path_inside "$runtime_path" "$HOME/OmniAgentOS"; then
      echo "FATAL: runtime state path is inside the sibling product ~/OmniAgentOS: $runtime_path" >&2
      exit 1
    fi
  done
fi

_prepare_runtime() {
  mkdir -p "$RUNTIME_ROOT" "$OMNIAGENTOS_LEDGER_DIR" "$OMNIAGENTOS_VAULT_DIR"
}

# --- the trusted-hop secret (LS-003 / D-1) ----------------------------------
# ONE file is the single source of truth for this value. Caddy injects it and
# the dashboard compares against it; if the two ever read different values,
# every /api/** request 403s again. Deriving both from the same file is what
# makes that drift structurally impossible for a supervised start — the risk
# the repair plan named survives only for a hand-rolled Caddy or a stale
# exported variable, and `dashboard/src/middleware.ts` now logs a named reason
# code for exactly that case.
HOP_SECRET_FILE="$ROOT/var/secrets/trusted-hop-secret"

# Trim leading/trailing whitespace. The dashboard applies `.trim()` to the
# EXPECTED value while comparing it against the header caddy sends VERBATIM, so
# without this a stray newline in the file would make caddy inject " abc" where
# the dashboard expects "abc" and every request would 403 with no side visibly
# at fault.
#
# This does NOT reproduce JavaScript's `.trim()`, and it must not be described
# as if it did: bash's `[:space:]` is locale-dependent and under LC_ALL=C it
# leaves U+00A0 in place, which JS strips. The ASCII gate in `_hop_secret` is
# what closes that gap — anything this trim fails to remove is refused there
# rather than silently exported as a secret the dashboard cannot match.
_hop_trim() {
  _hop_t="$1"
  _hop_t="${_hop_t#"${_hop_t%%[![:space:]]*}"}"
  _hop_t="${_hop_t%"${_hop_t##*[![:space:]]}"}"
  printf '%s' "$_hop_t"
  unset _hop_t
}

_hop_secret() {
  if [ ! -s "$HOP_SECRET_FILE" ]; then
    mkdir -p "$(dirname "$HOP_SECRET_FILE")"
    # ``$$`` is shared by bash background subshells. Using it here made all
    # first-start racers write the same temporary inode; after one linked that
    # inode into place, another racer could truncate it and hand children two
    # different values. BASHPID is unique per subshell.
    _hop_tmp="$HOP_SECRET_FILE.tmp.${BASHPID:-$$}"
    # 0600 from creation, never via a later chmod: a secret must not exist
    # world-readable for even the instant between write and chmod.
    if ! (umask 077; openssl rand -hex 32 > "$_hop_tmp") 2>/dev/null; then
      (umask 077; "$PYBIN" -c 'import secrets;print(secrets.token_hex(32))' > "$_hop_tmp")
    fi
    # `ln` is an atomic create-if-absent: the dashboard and caddy children can
    # race here, and the loser MUST discard its candidate and read the winner's
    # rather than clobber it. A plain `mv` would let the second writer replace a
    # secret the first child had already exported -- self-inflicted drift.
    ln "$_hop_tmp" "$HOP_SECRET_FILE" 2>/dev/null || true
    rm -f "$_hop_tmp"
    unset _hop_tmp
  fi
  _hop_value="$(_hop_trim "$(cat "$HOP_SECRET_FILE")")"
  # THE SECRET CONTRACT: printable ASCII, no whitespace, nothing else.
  #
  # Three implementations read this value — bash here, Python in the
  # fingerprint, and TypeScript in both dashboard guards — and they do NOT
  # agree about Unicode. Bash's `[:space:]` under LC_ALL=C does not strip
  # U+00A0; JavaScript's `.trim()` does. A file holding only U+00A0 therefore
  # exported a 2-byte "secret" and reported SUCCESS, while the dashboard saw an
  # empty expected value and 403'd every request — the exact LS-003 outage,
  # recreated silently by the repair for it.
  #
  # Rather than make three languages agree on Unicode whitespace (a losing
  # game: locale-dependent in bash, White_Space property in JS, str.strip in
  # Python), the contract narrows to a set where all three are IDENTICAL by
  # construction. `openssl rand -hex 32` is 64 lowercase hex characters; no
  # legitimate secret needs anything outside printable ASCII.
  #
  # LC_ALL=C on the grep is load-bearing. The obvious `case $v in *[!!-~]*)`
  # spelling REJECTS a valid hex secret under a UTF-8 locale — measured — so
  # the check must be forced into byte semantics rather than inherit the
  # operator's collation.
  if printf '%s' "$_hop_value" | LC_ALL=C grep -q '[^!-~]'; then
    echo "FATAL(trusted-hop): $HOP_SECRET_FILE contains characters outside printable ASCII (or embedded whitespace); the dashboard would read a different value than caddy sends. Delete it to have one regenerated" >&2
    return 1
  fi
  if [ -z "$_hop_value" ]; then
    # A file that exists but holds nothing usable. Refuse rather than silently
    # regenerate: a blank secret is INDISTINGUISHABLE from a healthy one at the
    # `-s` test above, and rewriting it here could hand a sibling child that
    # already read the old contents a different value -- drift, invented by the
    # repair for drift.
    echo "FATAL(trusted-hop): $HOP_SECRET_FILE exists but contains no secret; delete it to have one regenerated" >&2
    return 1
  fi
  printf '%s' "$_hop_value"
  unset _hop_value
}

# The operator's half of drift diagnosis. `middleware.ts` logs
# `expected_fp=`/`supplied_fp=` on every refusal; this prints the same tag for
# the file, so "the two sides hold different secrets" and "somebody forged a
# header" stop being the same observation. FNV-1a/32 — a deliberately lossy tag
# (see hopFingerprint in dashboard/src/lib/serverProxy.ts), never the secret.
_hop_fingerprint() {
  if [ ! -s "$HOP_SECRET_FILE" ]; then
    echo "unset"
    return
  fi
  "$PYBIN" - "$HOP_SECRET_FILE" <<'PY'
import pathlib
import sys

value = pathlib.Path(sys.argv[1]).read_text().strip().encode()
if not value:
    # A blank file must not fingerprint as a real-looking value; an operator
    # comparing tags would read "matches" from two absent secrets.
    print("unset")
    raise SystemExit(0)
h = 0x811C9DC5
for byte in value:
    h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
print(f"{h:08x}")
PY
}

# Every process that must agree about the hop boundary reads it from here.
_export_hop_env() {
  OMNIAGENTOS_TRUSTED_HOP_SECRET="$(_hop_secret)"
  if [ -z "$OMNIAGENTOS_TRUSTED_HOP_SECRET" ]; then
    echo "FATAL(trusted-hop): $HOP_SECRET_FILE is empty or unreadable; refusing to start a boundary that cannot be enforced" >&2
    exit 1
  fi
  export OMNIAGENTOS_TRUSTED_HOP_SECRET
}

_api() {
  _prepare_runtime
  exec "$PYBIN" -m uvicorn omniagentos.api:app \
    --host 127.0.0.1 --port "$OMNIAGENTOS_API_PORT"
}

_runner() {
  _prepare_runtime
  exec "$PYBIN" -m omniagentos.runner
}

_sessions() {
  _prepare_runtime
  exec "$PYBIN" -m omniagentos.sessions.supervisor
}

_dashboard() {
  _prepare_runtime
  # The comparison half of the trusted-hop boundary. Without this the guards
  # have nothing to compare against and refuse every /api/** request -- the
  # LS-003 outage. `npm run start` inherits it via this exported variable.
  _export_hop_env
  # The operator's browser now loads the dashboard from the CADDY origin, not
  # from :3003, so the Caddy origin has to be in the S-B1 same-origin allowlist
  # for the `Origin`-fallback path (browsers send Sec-Fetch-Site and take the
  # primary path; this covers the clients that do not). Appended, never
  # clobbering an operator-configured value.
  _caddy_origins="http://127.0.0.1:$OMNIAGENTOS_CADDY_PORT,http://localhost:$OMNIAGENTOS_CADDY_PORT"
  if [ -n "${OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS:-}" ]; then
    export OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS="${OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS},$_caddy_origins"
  else
    export OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS="$_caddy_origins"
  fi
  unset _caddy_origins
  cd dashboard
  NEXT_PUBLIC_BUILD_SHA="$(git rev-parse --verify HEAD)"
  export NEXT_PUBLIC_BUILD_SHA
  npm run build
  exec env PORT="$OMNIAGENTOS_DASH_PORT" npm run start
}

_caddy() {
  _prepare_runtime
  if ! command -v caddy >/dev/null 2>&1; then
    echo "FATAL(trusted-hop): no 'caddy' on PATH; the dashboard front door cannot start (brew install caddy)" >&2
    exit 1
  fi
  # The injection half. Same file as _dashboard reads => the two cannot drift.
  _export_hop_env
  # The identity Caddy vouches for. `api/auth/login` mints a signed credential
  # from this header, so the value must come from the PROXY, never from the
  # client -- the Caddyfile Sets it unconditionally. Loopback reachability is
  # the trust level being asserted, which is the same level the session-token
  # proxy already grants anything that can reach the dashboard.
  export OMNIAGENTOS_DASHBOARD_PRINCIPAL="${OMNIAGENTOS_DASHBOARD_PRINCIPAL:-${USER:-operator}@localhost}"
  exec caddy run --config "$ROOT/configs/dashboard-caddy/Caddyfile" --adapter caddyfile
}

_comms_poller() {
  source="$1"
  _prepare_runtime
  # exec, NOT a `while true` retry wrapper.  A wrapper that never exits makes
  # the poller permanently "alive" from the supervisor's point of view, so a
  # child that is failing every single attempt is indistinguishable from a
  # healthy one and nothing ever escalates -- supervision in name only.
  # Restarting is the supervisor's job and it does it under a bounded budget
  # (process_supervisor.POLLER_RESTART_BUDGET); crash-looping past that
  # budget is a real failure and tears the fleet down like any other.
  exec "$PYBIN" -m omniagentos.comms.poll --source "$source"
}

_supervisor_args=(
  --root "$ROOT"
  --pid-file "$PID_FILE"
  --api-port "$OMNIAGENTOS_API_PORT"
  --dashboard-port "$OMNIAGENTOS_DASH_PORT"
)

case "${1:-all}" in
  api) _api ;;
  runner) _runner ;;
  sessions) _sessions ;;
  dashboard) _dashboard ;;
  caddy) _caddy ;;
  comms-poll-telegram) _comms_poller telegram ;;
  comms-poll-slack) _comms_poller slack ;;
  preflight)
    echo "OMNIAGENTOS_SIM_MODE=${OMNIAGENTOS_SIM_MODE:-}"
    echo "OMNIAGENTOS_SIM_CAMPAIGN=${OMNIAGENTOS_SIM_CAMPAIGN:-}"
    echo "OMNIAGENTOS_VAR_DIR=${OMNIAGENTOS_VAR_DIR:-}"
    echo "OMNIAGENTOS_VAR=${OMNIAGENTOS_VAR:-}"
    echo "OMNIAGENTOS_DB=${OMNIAGENTOS_DB:-}"
    echo "OMNIAGENTOS_LEDGER_DIR=${OMNIAGENTOS_LEDGER_DIR:-}"
    echo "OMNIAGENTOS_VAULT_DIR=${OMNIAGENTOS_VAULT_DIR:-}"
    echo "OMNIAGENTOS_SIM_ROOT=${OMNIAGENTOS_SIM_ROOT:-}"
    echo "OMNIAGENTOS_SIM_CAMPAIGN_ROOT=${OMNIAGENTOS_SIM_CAMPAIGN_ROOT:-}"
    echo "OMNIAGENTOS_API_PORT=${OMNIAGENTOS_API_PORT:-}"
    echo "OMNIAGENTOS_DASH_PORT=${OMNIAGENTOS_DASH_PORT:-}"
    echo "OMNIAGENTOS_CADDY_PORT=${OMNIAGENTOS_CADDY_PORT:-}"
    # The value is never printed; the fingerprint is what an operator compares.
    echo "TRUSTED_HOP_FINGERPRINT=$(_hop_fingerprint)"
    echo "OMNIAGENTOS_PROJECT_BASES=${OMNIAGENTOS_PROJECT_BASES:-}"
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
      echo "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}"
    fi
    exit 0
    ;;
  status)
    set +e
    "$PYBIN" scripts/process_supervisor.py status "${_supervisor_args[@]}"
    status=$?
    set -e
    echo "API: http://127.0.0.1:$OMNIAGENTOS_API_PORT"
    # The Caddy port is the operator entry point. :3003 is named as the origin
    # server, not offered as a URL: it 403s every /api/** request by design.
    echo "UI:  http://127.0.0.1:$OMNIAGENTOS_CADDY_PORT  (trusted hop; origin :$OMNIAGENTOS_DASH_PORT)"
    echo "HOP: fingerprint $(_hop_fingerprint)  (compare with expected_fp= / supplied_fp= in the dashboard log)"
    exit "$status"
    ;;
  all|start)
    _prepare_runtime
    exec "$PYBIN" scripts/process_supervisor.py run \
      "${_supervisor_args[@]}" \
      --launcher "$ROOT/scripts/launch-supervised.sh" \
      --health-timeout "$HEALTH_TIMEOUT" \
      --stop-timeout "$STOP_TIMEOUT"
    ;;
  -h|--help|help)
    echo "Usage: $0 [{all|start|api|runner|sessions|dashboard|caddy|status|preflight}]"
    echo "Coordinated start is supervised and fails closed on health or child exit."
    echo "Browse the dashboard at the caddy port (OMNIAGENTOS_CADDY_PORT, default 3013);"
    echo "a request that skips caddy carries no trusted-hop header and is refused."
    ;;
  *)
    echo "Usage: $0 [{all|start|api|runner|sessions|dashboard|caddy|status|preflight}]" >&2
    exit 2
    ;;
esac
