# shellcheck shell=bash
# Canonical launch-environment include for OmniAgentOS.
#
# Source-safe (no `exit`, no side effects when sourced twice). Honour any
# value already set in the environment — never clobber a preset OMNIAGENTOS_DB.
#
# Usage:
#   # from a script under scripts/ or the repo root:
#   # shellcheck source=scripts/launch-env.sh
#   . "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/scripts/launch-env.sh"
#   # or when ROOT is already known:
#   . "$ROOT/scripts/launch-env.sh"
#
# Idempotent guard: re-sourcing is a no-op after the first successful load.

# Helper for simulation failure (must not exit when sourced).
_omni_sim_fail() {
  echo "$1" >&2
  return 1 2>/dev/null || exit 1
}

# Resolve repo root relative to this file when possible (happens BEFORE any guards/blocks).
_omni_launch_env_self="${BASH_SOURCE[0]:-$0}"
if [ -n "${_omni_launch_env_self}" ] && [ -f "${_omni_launch_env_self}" ]; then
  _omni_launch_env_dir="$(CDPATH= cd -- "$(dirname -- "${_omni_launch_env_self}")" && pwd)"
  _omni_repo_root="$(CDPATH= cd -- "${_omni_launch_env_dir}/.." && pwd)"
else
  _omni_repo_root="${OMNIAGENTOS_ROOT:-${ROOT:-$(pwd)}}"
fi

# Source the preflight library for environment validation.
if [ -f "${_omni_launch_env_dir}/lib/preflight.sh" ]; then
  # shellcheck source=scripts/lib/preflight.sh
  . "${_omni_launch_env_dir}/lib/preflight.sh"
fi

# The repository registry is authoritative. Refresh the runtime copy on every
# launch-environment load so stale gitignored state cannot silently remove or
# weaken capabilities. Stage beside the destination, then rename, so concurrent
# launchers can never expose a partial file.
#
# BEST-EFFORT, and never aborts the load. This file is SOURCED — by launchd
# plists, by Make recipes, and by test fixtures that stand up a minimal repo
# root with no configs/ directory at all — so a `return 1` here would not merely
# skip the sync, it would silently truncate the rest of the environment (ports,
# DB path, gate workspace probe, the secrets load) for every caller. Refusing to
# run on a missing or drifted registry is the API's job:
# omniagentos/simgate.py:assert_registry_truth() compares the runtime copy
# against configs/connectors.yaml at import time, before any request is served.
#
# Called from BOTH the simulation path (after the campaign root is derived) and
# the production path, because a simulation launch needs its runtime copy for
# exactly the same reason a production launch does.
_omni_sync_registry() {
  _omni_registry_source="${_omni_repo_root}/configs/connectors.yaml"
  if [ ! -f "$_omni_registry_source" ] && [ -n "${OMNIAGENTOS_ROOT:-${ROOT:-}}" ]; then
    _omni_registry_source="${OMNIAGENTOS_ROOT:-${ROOT}}/configs/connectors.yaml"
  fi
  _omni_registry_target="${OMNIAGENTOS_VAR_DIR}/connectors.yaml"

  # Never leave an untracked file in the checkout. A var dir that resolves
  # inside the repository — including a RELATIVE one, which resolves against
  # whatever cwd the caller happened to have — would drop an untracked
  # connectors.yaml into the working tree on every source, dirtying it (and,
  # through the gate-workspace probe, silently disqualifying that checkout as a
  # gate pin source). Writing there is allowed only when the path is gitignored.
  case "${OMNIAGENTOS_VAR_DIR}" in
    /*) _omni_registry_abs="${OMNIAGENTOS_VAR_DIR}" ;;
    *) _omni_registry_abs="$(pwd)/${OMNIAGENTOS_VAR_DIR}" ;;
  esac
  case "${_omni_registry_abs}/" in
    "${_omni_repo_root}/"*)
      if ! git -C "${_omni_repo_root}" check-ignore -q -- "${_omni_registry_abs}/connectors.yaml" 2>/dev/null; then
        echo "WARN(registry-sync): refusing to write ${_omni_registry_abs}/connectors.yaml into the checkout (not gitignored)" >&2
        unset _omni_registry_source _omni_registry_target _omni_registry_abs
        return 0
      fi
      ;;
  esac
  unset _omni_registry_abs

  # The staging name is built from shell builtins ($$ and $RANDOM), NOT from
  # `$(mktemp ...)`. Everything above the export block runs while this file is
  # being SOURCED, and a command substitution forks a subshell that inherits the
  # script's own input descriptor — when that descriptor is a pipe (the
  # `. /dev/stdin` spelling that tests/scripts/test_launch_env.py and several
  # launchers use) the subshell swallows the unread remainder and the rest of
  # the environment silently never loads. $$ plus $RANDOM is unique enough to
  # keep concurrent launchers off each other's stage file, which is all the
  # staging name has to do; the rename below is what makes the swap atomic.
  _omni_registry_tmp=""
  if [ -f "$_omni_registry_source" ] \
    && mkdir -p -- "${OMNIAGENTOS_VAR_DIR}" 2>/dev/null \
    && _omni_registry_tmp="${_omni_registry_target}.tmp.$$.${RANDOM:-0}" \
    && cp -- "$_omni_registry_source" "$_omni_registry_tmp" 2>/dev/null \
    && mv -f -- "$_omni_registry_tmp" "$_omni_registry_target" 2>/dev/null; then
    : # refreshed atomically
  else
    # Leave no partial stage behind, and stay visible in the launchd log.
    if [ -n "${_omni_registry_tmp:-}" ]; then
      rm -f -- "$_omni_registry_tmp" 2>/dev/null || true
    fi
    echo "WARN(registry-sync): unable to refresh $_omni_registry_target from $_omni_registry_source; the API boot assertion refuses on drift" >&2
  fi
  unset _omni_registry_source _omni_registry_target _omni_registry_tmp
  return 0
}

# --- SIMULATION BLOCK ---
if [ -n "${OMNIAGENTOS_SIM_MODE:-}" ] && [ "${OMNIAGENTOS_SIM_MODE}" != "1" ]; then
  _omni_sim_fail "FATAL(sim-isolation): OMNIAGENTOS_SIM_MODE='${OMNIAGENTOS_SIM_MODE}' is invalid; set it exactly to '1' for simulation or unset it for production." || return 1
fi

if [ "${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then
  # SIM_ENV_LOADED is only a same-shell deduplication marker. It is valid
  # only when the launcher that set it also owns the matching nonce.
  if [ "${OMNIAGENTOS_SIM_ENV_LOADED:-}" = "1" ]; then
    if [ -z "${_omni_sim_launcher_nonce:-}" ] || [ "${OMNIAGENTOS_SIM_ENV_NONCE:-}" != "${_omni_sim_launcher_nonce}" ]; then
      _omni_sim_stale_var=""
      for _omni_sim_var in OMNIAGENTOS_DB OMNIAGENTOS_VAR OMNIAGENTOS_VAR_DIR OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR OMNIAGENTOS_TRANSCRIPTS_ROOT; do
        eval _omni_sim_val="\${$_omni_sim_var:-}"
        if [ -n "$_omni_sim_val" ]; then
          _omni_sim_stale_var="${_omni_sim_var}"
          break
        fi
      done
      _omni_sim_stale_detail=""
      if [ -n "$_omni_sim_stale_var" ]; then
        _omni_sim_stale_detail=" Also unset inherited ${_omni_sim_stale_var} before retrying."
      fi
      _omni_sim_fail "FATAL(sim-isolation): inherited OMNIAGENTOS_SIM_ENV_LOADED=1 without the matching launcher nonce; start via scripts/launch-omniagentos.sh so the simulation environment is minted for this invocation.${_omni_sim_stale_detail}" || return 1
    fi
    # A valid same-invocation re-source is a no-op. The first source already
    # validated and derived the state paths in this shell.
    if [ "${OMNIAGENTOS_LAUNCH_ENV_LOADED:-}" = "1" ]; then
      unset _omni_launch_env_self _omni_launch_env_dir _omni_repo_root _omni_sim_fail _omni_sync_registry _omni_sim_campaign_root _omni_sim_root _omni_sim_var _omni_sim_val
      return 0 2>/dev/null || true
    fi
  else
    # Direct callers may source this file without the launcher. A nonce
    # inherited from another process is never trusted; mint one locally.
    if [ -z "${_omni_sim_launcher_nonce:-}" ]; then
      _omni_sim_launcher_nonce="$(uuidgen 2>/dev/null || printf '%s-%s-%s' "$(date +%s)" "$$" "${RANDOM:-0}")"
      export OMNIAGENTOS_SIM_ENV_NONCE="$_omni_sim_launcher_nonce"
    fi
  fi

  # 1. Campaign required
  if [ -z "${OMNIAGENTOS_SIM_CAMPAIGN:-}" ]; then
    _omni_sim_fail "FATAL(sim-isolation): simulation mode requires a campaign id; set OMNIAGENTOS_SIM_CAMPAIGN=<id> or pass --campaign <id>. Refusing to run a simulation without a campaign-scoped runtime root." || return 1
  fi

  # 2. Campaign id must be one safe path segment
  case "$OMNIAGENTOS_SIM_CAMPAIGN" in
    "" | "." | ".." | -*)
      _omni_sim_fail "FATAL(sim-isolation): invalid campaign id '${OMNIAGENTOS_SIM_CAMPAIGN}': expected a single path segment matching [A-Za-z0-9._-]+" || return 1
      ;;
    *[!A-Za-z0-9._-]*)
      _omni_sim_fail "FATAL(sim-isolation): invalid campaign id '${OMNIAGENTOS_SIM_CAMPAIGN}': expected a single path segment matching [A-Za-z0-9._-]+" || return 1
      ;;
  esac

  # Derive campaign root to use in refusal messages and later variables
  _omni_sim_root="${_omni_sim_root_override:-${OMNIAGENTOS_SIM_ROOT:-$HOME/OmniAgentOS-sims}}"
  _omni_sim_campaign_root="${_omni_sim_root}/${OMNIAGENTOS_SIM_CAMPAIGN}"

  # 3. Refuse inheritance. Launcher-side doctrine: explicit state-path
  # overrides are refused under simulation, including OMNIAGENTOS_DB,
  # OMNIAGENTOS_VAR, OMNIAGENTOS_VAR_DIR, OMNIAGENTOS_LEDGER_DIR,
  # OMNIAGENTOS_VAULT_DIR,
  # OMNIAGENTOS_SIM_ROOT, OMNIAGENTOS_SIM_CAMPAIGN_ROOT,
  # OMNIAGENTOS_SWARM_DIR, OMNIAGENTOS_MEMORIES_DIR, and
  # OMNIAGENTOS_TRANSCRIPTS_ROOT. reflection/adapters.py has not been changed
  # and may argue the opposite; flag it for a D-consistency follow-up.
  for _omni_sim_var in \
    OMNIAGENTOS_DB OMNIAGENTOS_VAR OMNIAGENTOS_VAR_DIR \
    OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR \
    OMNIAGENTOS_SIM_ROOT OMNIAGENTOS_SIM_CAMPAIGN_ROOT \
    OMNIAGENTOS_SWARM_DIR OMNIAGENTOS_MEMORIES_DIR \
    OMNIAGENTOS_TRANSCRIPTS_ROOT
  do
    eval _omni_sim_val="\${$_omni_sim_var:-}"
    if [ -n "$_omni_sim_val" ]; then
      _omni_sim_fail "FATAL(sim-isolation): inherited ${_omni_sim_var}=${_omni_sim_val} from the ambient environment; simulation mode requires ${_omni_sim_var} to resolve under the campaign root ${_omni_sim_campaign_root}. unset ${_omni_sim_var} (e.g. 'env -u ${_omni_sim_var} ...') and re-run." || return 1
    fi
  done

  # 4. Derive all four under the campaign root
  OMNIAGENTOS_VAR_DIR="$_omni_sim_campaign_root"
  OMNIAGENTOS_VAR="$_omni_sim_campaign_root"
  OMNIAGENTOS_DB="$_omni_sim_campaign_root/state.sqlite3"
  OMNIAGENTOS_LEDGER_DIR="$_omni_sim_campaign_root/ledger"
  OMNIAGENTOS_VAULT_DIR="$_omni_sim_campaign_root/vault"

  # Keep the operator's explicit port choices. Otherwise derive a
  # collision-free pair via kernel-assigned ephemeral bind + read-back rather
  # than a hashed/computed number: `cksum(campaign) % 1000` gave only 1000
  # slots, so two campaigns launched at once (parallel test workers, a swarm
  # fan-out) can legitimately hash to the same pair and race for the real
  # bind underneath them — measured as the gate's intermittent
  # port-allocation collision under full-suite xdist. A kernel-assigned port
  # can never collide with another live bind at the moment it is minted. The
  # pair is cached under the campaign root so a second `--campaign <same-id>`
  # invocation (repeat sourcing, or a re-entrant supervised child) gets back
  # the SAME pair instead of re-rolling one out from under a process that is
  # already listening on the first.
  if [ -z "${OMNIAGENTOS_API_PORT:-}" ] || [ -z "${OMNIAGENTOS_DASH_PORT:-}" ]; then
    # Never touch disk under a campaign root that resolves inside the repo
    # checkout: launch-omniagentos.sh refuses those as source-controlled
    # (or inside the sibling ~/OmniAgentOS product) right after this file
    # returns, but that refusal runs AFTER sourcing completes — a cache
    # mkdir/write here would land an untracked directory in the working tree
    # a beat before the refusal fires. The port pair below is still derived
    # (the doomed-to-be-refused caller gets a harmless value) — only the
    # cache read/write is skipped.
    case "${_omni_sim_campaign_root}/" in
      "${_omni_repo_root}/"*) _omni_sim_root_in_repo=1 ;;
      *) _omni_sim_root_in_repo="" ;;
    esac
    _omni_sim_ports_cache=""
    _omni_cached_api_port=""
    _omni_cached_dash_port=""
    if [ -z "$_omni_sim_root_in_repo" ]; then
      mkdir -p -- "$_omni_sim_campaign_root" 2>/dev/null || true
      _omni_sim_ports_cache="${_omni_sim_campaign_root}/.grok-sim-ports"
      if [ -f "$_omni_sim_ports_cache" ]; then
        # PARSE the cache, never `.`/source it. This file lives under the
        # campaign root; sourcing it would EXECUTE any shell an attacker (or a
        # buggy writer) placed there, in the launcher's own context — and
        # launch-env.sh is sourced by production launchers AND by the merge
        # gate. The cache is data: exactly two integer ports. Accept only the
        # two known keys with all-digit values; ignore everything else, so a
        # tampered cache degrades to a fresh derivation, never code execution.
        # `|| [ -n "$_omni_ck" ]`: a cache file whose last line has no trailing
        # newline still yields its final key (plain `while read` would drop it,
        # splitting the pair — one port cached, one re-minted).
        _omni_pc_api=""
        _omni_pc_dash=""
        while IFS='=' read -r _omni_ck _omni_cv || [ -n "$_omni_ck" ]; do
          case "$_omni_ck" in
            _omni_cached_api_port)  _omni_pc_api="$_omni_cv" ;;
            _omni_cached_dash_port) _omni_pc_dash="$_omni_cv" ;;
          esac
        done < "$_omni_sim_ports_cache"
        unset _omni_ck _omni_cv
        # Validate the PAIR as untrusted data. Both must be plain integers in
        # the ephemeral range; NEITHER may be a production/control port (8485
        # api, 3003 dash — the mint path refuses these, so the parse path must
        # too, or a tampered cache pins a sim campaign onto live ports); and
        # they must differ. ANY failure discards the WHOLE cache and forces a
        # fresh derivation — a partially-valid cache is never trusted.
        case "$_omni_pc_api" in ''|*[!0-9]*) _omni_pc_api="" ;; esac
        case "$_omni_pc_dash" in ''|*[!0-9]*) _omni_pc_dash="" ;; esac
        # CANONICALISE to base-10 before the denylist compare. A zero-padded
        # value ("08485") is all-digit and passes the string `!= 8485` test,
        # but every downstream consumer (Python int(), uvicorn, node Number())
        # coerces it straight back to the forbidden production port — F2r. `10#`
        # forces base-10 (also avoids the leading-zero octal-parse footgun) so
        # the denylist and the assigned value are the true integer.
        [ -n "$_omni_pc_api" ] && _omni_pc_api=$((10#$_omni_pc_api))
        [ -n "$_omni_pc_dash" ] && _omni_pc_dash=$((10#$_omni_pc_dash))
        if [ -n "$_omni_pc_api" ] && [ -n "$_omni_pc_dash" ] \
          && [ "$_omni_pc_api" -ge 1 ] && [ "$_omni_pc_api" -le 65535 ] \
          && [ "$_omni_pc_dash" -ge 1 ] && [ "$_omni_pc_dash" -le 65535 ] \
          && [ "$_omni_pc_api" != 8485 ] && [ "$_omni_pc_api" != 3003 ] \
          && [ "$_omni_pc_dash" != 8485 ] && [ "$_omni_pc_dash" != 3003 ] \
          && [ "$_omni_pc_api" != "$_omni_pc_dash" ]; then
          _omni_cached_api_port="$_omni_pc_api"
          _omni_cached_dash_port="$_omni_pc_dash"
        fi
        unset _omni_pc_api _omni_pc_dash
      fi
    fi
    [ -n "$_omni_cached_api_port" ] && : "${OMNIAGENTOS_API_PORT:=$_omni_cached_api_port}"
    [ -n "$_omni_cached_dash_port" ] && : "${OMNIAGENTOS_DASH_PORT:=$_omni_cached_dash_port}"

    if [ -z "${OMNIAGENTOS_API_PORT:-}" ] || [ -z "${OMNIAGENTOS_DASH_PORT:-}" ]; then
      _omni_sim_pybin="${OMNIAGENTOS_PYTHON:-}"
      if [ -z "$_omni_sim_pybin" ] || [ ! -x "$_omni_sim_pybin" ]; then
        if [ -x "${_omni_repo_root}/.venv/bin/python" ]; then
          _omni_sim_pybin="${_omni_repo_root}/.venv/bin/python"
        else
          _omni_sim_pybin="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
        fi
      fi
      if [ -z "$_omni_sim_pybin" ]; then
        _omni_sim_fail "FATAL(sim-isolation): no python available to derive ephemeral simulation ports for campaign '${OMNIAGENTOS_SIM_CAMPAIGN}' (set OMNIAGENTOS_PYTHON)" || return 1
      fi
      _omni_sim_derived_ports="$("$_omni_sim_pybin" - <<'PY'
import socket


def free_port(taken):
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        if port not in taken and port not in (8485, 3003):
            return s, port
        s.close()


sock_api, port_api = free_port(set())
sock_dash, port_dash = free_port({port_api})
print(port_api)
print(port_dash)
sock_api.close()
sock_dash.close()
PY
)" || _omni_sim_fail "FATAL(sim-isolation): unable to derive ephemeral simulation ports via ${_omni_sim_pybin}" || return 1
      _omni_new_api_port="$(printf '%s\n' "$_omni_sim_derived_ports" | sed -n '1p')"
      _omni_new_dash_port="$(printf '%s\n' "$_omni_sim_derived_ports" | sed -n '2p')"
      : "${OMNIAGENTOS_API_PORT:=$_omni_new_api_port}"
      : "${OMNIAGENTOS_DASH_PORT:=$_omni_new_dash_port}"

      if [ -z "$_omni_sim_root_in_repo" ]; then
        # Persist atomically so a concurrent re-source never observes a
        # half-written cache file.
        _omni_sim_ports_cache_tmp="${_omni_sim_ports_cache}.tmp.$$.${RANDOM:-0}"
        if {
          printf '_omni_cached_api_port=%s\n' "$OMNIAGENTOS_API_PORT"
          printf '_omni_cached_dash_port=%s\n' "$OMNIAGENTOS_DASH_PORT"
        } >"$_omni_sim_ports_cache_tmp" 2>/dev/null \
          && mv -f -- "$_omni_sim_ports_cache_tmp" "$_omni_sim_ports_cache" 2>/dev/null; then
          : # persisted atomically
        else
          rm -f -- "$_omni_sim_ports_cache_tmp" 2>/dev/null || true
        fi
        unset _omni_sim_ports_cache_tmp
      fi
      unset _omni_new_api_port _omni_new_dash_port _omni_sim_derived_ports
    fi
    unset _omni_cached_api_port _omni_cached_dash_port _omni_sim_pybin _omni_sim_ports_cache _omni_sim_root_in_repo
  fi
  export OMNIAGENTOS_API_PORT OMNIAGENTOS_DASH_PORT

  export OMNIAGENTOS_VAR_DIR
  export OMNIAGENTOS_VAR
  export OMNIAGENTOS_DB
  export OMNIAGENTOS_LEDGER_DIR
  export OMNIAGENTOS_VAULT_DIR
  export OMNIAGENTOS_SIM_ROOT="${_omni_sim_root}"
  export OMNIAGENTOS_SIM_CAMPAIGN_ROOT="$_omni_sim_campaign_root"

  # The campaign root is a brand-new runtime root, so it has no connector
  # registry at all until this runs. The simulation path returns through the
  # guard below and never reaches the production defaults, so the sync has to
  # happen HERE too — otherwise every simulated API boot refuses on a missing
  # runtime registry.
  _omni_sync_registry

  # 5. Set and export OMNIAGENTOS_SIM_ENV_LOADED=1 so a second source is a no-op
  OMNIAGENTOS_SIM_ENV_LOADED=1
  export OMNIAGENTOS_SIM_ENV_LOADED

  OMNIAGENTOS_LAUNCH_ENV_LOADED=1
  export OMNIAGENTOS_LAUNCH_ENV_LOADED
fi

# --- EXISTING GUARD ---
# shellcheck disable=SC2034
: "${OMNIAGENTOS_LAUNCH_ENV_LOADED:=}"

if [ "${OMNIAGENTOS_LAUNCH_ENV_LOADED:-}" = "1" ]; then
  unset _omni_launch_env_self _omni_launch_env_dir _omni_repo_root _omni_sim_fail _omni_sync_registry _omni_sim_campaign_root _omni_sim_root _omni_sim_var _omni_sim_val
  return 0 2>/dev/null || true
  # When executed (not sourced) the guard cannot `return`; fall through quietly.
fi

# --- EXISTING PRODUCTION DEFAULTS ---
# Product-scoped runtime root (Grok). Never defaults into ~/OmniAgentOS.
: "${OMNIAGENTOS_VAR_DIR:=${_omni_repo_root}/var/runtime}"

# Refresh the runtime connector registry from repository truth (see the
# _omni_sync_registry definition above for why this is best-effort).
_omni_sync_registry

: "${OMNIAGENTOS_VAR:=${OMNIAGENTOS_VAR_DIR}}"

# Canonical control-plane SQLite — honour a preset, else product runtime path.
if [ -z "${OMNIAGENTOS_DB:-}" ]; then
  OMNIAGENTOS_DB="${OMNIAGENTOS_VAR_DIR}/state.sqlite3"
fi

: "${OMNIAGENTOS_LEDGER_DIR:=${OMNIAGENTOS_VAR_DIR}/ledger}"
: "${OMNIAGENTOS_VAULT_DIR:=${OMNIAGENTOS_VAR_DIR}/vault}"

# Session hook control-plane API port for this product (OmniAgentOS):
# The supervisor (sessions/supervisor.py) stamps the hook's session environment
# with the control-plane URL derived from this port. If unset, the hook falls back
# to a compiled-in default (:8484, the SIBLING product's port), which causes
# approval POSTs to fail silently. Always set this
# in ANY runtime that sources launch-env.sh, not just launch-omniagentos.sh.
# Honour a preset (scripts/launch-omniagentos.sh exports its own without conflict).
: "${OMNIAGENTOS_API_PORT:=8485}"
: "${OMNIAGENTOS_DASH_PORT:=3003}"

# Synapse knowledge recall: ON by default for this product's launch path (the
# api/runner/migrate recipes source this file). The runner prepends recalled
# facts to every brief and credits helpful facts on completion; the agent-role
# DSN default (trust-auth local PG) lives in omniagentos.knowledge.contracts.
# Honour a preset — OMNIAGENTOS_KNOWLEDGE=0 keeps it dark. pytest never sources
# this file and simulation mode returns before this section, so neither inherits
# the flip.
: "${OMNIAGENTOS_KNOWLEDGE:=1}"

# Mid-run file search hint: the runner prepends a short <file-search> block to
# each brief telling agents how to query the federated file catalog
# (python -m omniagentos.filesearch). Same preset/test semantics as above.
: "${OMNIAGENTOS_FILESEARCH_HINT:=1}"

# Capability flags: ON for this product's launch path (operator decision 2026-07-28).
# Code defaults are "off"; these enable real behavior changes (routing cascade,
# reflection escalation, memory tracking, swarm execution, metacognition).
: "${OMNIAGENTOS_CASCADE:=1}"
: "${OMNIAGENTOS_REFLEXION:=1}"
: "${OMNIAGENTOS_METACOG_MEMORY_PROMOTION:=enforce}"
: "${OMNIAGENTOS_SWARM_EXECUTE:=1}"
: "${OMNIAGENTOS_MEMORY:=1}"

# Hybrid context assembly (memcert v2, 2026-08-13): retrieval leg over older
# turns + temporal stamps + abstention guard + budget-reserved packing. ON for
# the launch path; bare-env default is off so tests/tools are byte-identical
# unless they opt in. Certified by tests/memcert/test_sufficiency.py; the
# pre-registered hypotheses are H1/H2/H3 in devtasks/memcert/RESULTS-2026-08-12.md.
: "${OMNIAGENTOS_MEMORY_HYBRID:=1}"

# Context capsule observability: baseline shadow manifest before behavior change (U-C1).
: "${OMNIAGENTOS_CONTEXT_CAPSULE:=shadow}"

# Parallel-execution correctness (operator decision 2026-07-28).
# SCOPE_LOCKS promoted from shadow to enforce: with swarm worktrees on and parallel
# lanes writing, path locking is a correctness mechanism, not a rollout control.
# SWARM_WORKTREES enables per-task worktree isolation for swarm merge models.
: "${OMNIAGENTOS_SCOPE_LOCKS:=enforce}"
: "${OMNIAGENTOS_SWARM_WORKTREES:=1}"

# Measurement ramp: eight three-rung flags below stay at SHADOW for measurement
# while we accumulate data. Per FEATURE-FLAGS.md, shadow computes and records the
# decision but applies nothing; behavior stays byte-identical to off. The point is
# to start filling the empty decision tables (task_shape_decisions, routing_history,
# lease/provider/champion logs) so future enforce-mode decisions — and any learned
# router — have real data. Presets still win; move a flag to "enforce" only
# deliberately, per FEATURE-FLAGS.md's soak guidance.
: "${OMNIAGENTOS_TASK_SHAPE_ROUTER:=shadow}"
: "${OMNIAGENTOS_TOOL_CATALOG:=shadow}"
: "${OMNIAGENTOS_TOOL_SCHEDULER:=shadow}"
: "${OMNIAGENTOS_AUTONOMY_LEASE:=shadow}"
: "${OMNIAGENTOS_CHAMPION_ROUTING_MODE:=shadow}"
: "${OMNIAGENTOS_LAB_CURATION_MODE:=shadow}"
: "${OMNIAGENTOS_ALLOWED_PROVIDERS_MODE:=shadow}"
: "${OMNIAGENTOS_REFLECTION_REARM_MODE:=shadow}"

# Quick-intake project routing (A1, omniagentos/intake/service.py): tri-state
# off/shadow/enforce. Shadow LOGS the project-registry routing decision without
# applying it — dispatch behavior stays byte-identical to off while root_dirs
# coverage accumulates (operator seeded five projects' root_dirs 2026-08-05).
# Move to "enforce" only after shadow decisions prove the mapping, per
# FEATURE-FLAGS.md soak guidance.
: "${OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE:=shadow}"

# Budget enforcement mode (omniagentos/budget/policy.py): whether budget overages
# BLOCK work or are merely advisory. Set to "block" to restore hard limits; any other
# value (including unset) is advisory. Operator decision 2026-08-04.
: "${OMNIAGENTOS_BUDGET_ENFORCEMENT:=block}"

# Objective gate workspace for routine settlement (scheduler/gate_runner.py).
#
# `default_gate_workspace()` is opt-in: with OMNIAGENTOS_GATE_WORKSPACE unset,
# every routine settlement records `gate_evidence_unavailable` and lands in the
# NULL/NULL bucket a6c0cc7e created. 254/254 historical settlements did exactly
# that, so no routine has ever been judged against real evidence — the routines
# fire, produce runs, and settle unjudged.
#
# The default is PROBED, never assumed. PytestGateRunner refuses a workspace
# that is missing, non-git or dirty, and that refusal is classified as
# GateWorkspaceUnusable -> settled NULL/NULL and EXCLUDED from the acceptance
# floor. So an unusable default costs no false failures; what it costs is
# verdicts. Every tick against a bad workspace is a run nobody judged, which is
# the 254/254 state this variable exists to end — probing is how the value only
# gets set when it will actually buy a verdict.
#
# This directory is the SOURCE OF THE PIN, not the execution surface: the runner
# materialises a throwaway worktree at its SHA for every gate run, so no
# verifier can leave anything behind here. It should still be a DEDICATED,
# detached, main-pinned checkout that nothing else writes to
# (`scripts/gate-workspace.sh` creates and refreshes it) — a checkout somebody
# is merging in is dirty, and a dirty pin source produces no verdicts at all.
#
# An explicit operator preset always wins and is exported unprobed.
# Both git calls are checked for EXIT STATUS, not just for empty output. A
# broken checkout (corrupt index, unreadable object store) makes `git status`
# fail with an empty stdout, and reading that emptiness as "clean" would export
# precisely the workspace that cannot be executed against. This mirrors the
# runner's own test, `returncode != 0 or stdout.strip()`.
if [ -z "${OMNIAGENTOS_GATE_WORKSPACE:-}" ] && [ -d "${_omni_repo_root}-gate" ]; then
  _omni_gate_ws="${_omni_repo_root}-gate"
  if git -C "$_omni_gate_ws" rev-parse --verify HEAD >/dev/null 2>&1; then
    if _omni_gate_status=$(git -C "$_omni_gate_ws" status --porcelain=v1 --untracked-files=all 2>/dev/null); then
      if [ -z "$_omni_gate_status" ]; then
        OMNIAGENTOS_GATE_WORKSPACE="$_omni_gate_ws"
      fi
    fi
  fi
  unset _omni_gate_ws _omni_gate_status
fi

# Export for child processes (Make recipes, supervisor, Python entrypoints).
export OMNIAGENTOS_VAR_DIR
export OMNIAGENTOS_VAR
export OMNIAGENTOS_DB
export OMNIAGENTOS_LEDGER_DIR
export OMNIAGENTOS_VAULT_DIR
export OMNIAGENTOS_API_PORT
export OMNIAGENTOS_DASH_PORT
export OMNIAGENTOS_KNOWLEDGE
export OMNIAGENTOS_FILESEARCH_HINT
export OMNIAGENTOS_CASCADE
export OMNIAGENTOS_REFLEXION
export OMNIAGENTOS_METACOG_MEMORY_PROMOTION
export OMNIAGENTOS_SWARM_EXECUTE
export OMNIAGENTOS_MEMORY
export OMNIAGENTOS_MEMORY_HYBRID
export OMNIAGENTOS_CONTEXT_CAPSULE
export OMNIAGENTOS_SCOPE_LOCKS
export OMNIAGENTOS_SWARM_WORKTREES
export OMNIAGENTOS_TASK_SHAPE_ROUTER
export OMNIAGENTOS_TOOL_CATALOG
export OMNIAGENTOS_TOOL_SCHEDULER
export OMNIAGENTOS_AUTONOMY_LEASE
export OMNIAGENTOS_CHAMPION_ROUTING_MODE
export OMNIAGENTOS_LAB_CURATION_MODE
export OMNIAGENTOS_ALLOWED_PROVIDERS_MODE
export OMNIAGENTOS_REFLECTION_REARM_MODE
export OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE
export OMNIAGENTOS_BUDGET_ENFORCEMENT

# Optional CORS extras are operator-supplied only. The E2E stack opts into
# its port 3002 authority explicitly; production does not provide a fallback.
export OMNIAGENTOS_CORS_EXTRA_PORTS

# Exported ONLY when it resolved above: an exported empty value is not the same
# as unset to `default_gate_workspace()`'s callers, and a set-but-unusable path
# logs a warning on every five-minute tick.
if [ -n "${OMNIAGENTOS_GATE_WORKSPACE:-}" ]; then
  export OMNIAGENTOS_GATE_WORKSPACE
fi

OMNIAGENTOS_LAUNCH_ENV_LOADED=1
export OMNIAGENTOS_LAUNCH_ENV_LOADED

# JG-1: load var/secrets/*.env without letting an empty preset shadow the vault.
# A failure is non-fatal when this file is sourced, but must remain visible to launchd.
if _omni_secrets_stderr="$(mktemp "${TMPDIR:-/tmp}/omniagentos-secrets.XXXXXX")"; then
  if _omni_secrets_exports="$(OMNIAGENTOS_SECRETS_ROOT="${_omni_repo_root}" PYTHONPATH="${_omni_repo_root}${PYTHONPATH:+:$PYTHONPATH}" "${_omni_repo_root}/.venv/bin/python" -c 'import os; from omniagentos.connectors.secrets_env import emit_export_lines; print(emit_export_lines(root=os.environ["OMNIAGENTOS_SECRETS_ROOT"]))' 2>"${_omni_secrets_stderr}")"; then

# --- PREFLIGHT VALIDATION ---
# Verify that the connections.env file has been sourced and required vars are set.
# This ensures that any unit sourcing launch-env.sh has a properly configured
# environment, preventing silent half-configurations that exit with status 0.
if [ -n "${_omni_preflight_env_vars:-}" ] 2>/dev/null; then
  # Only run preflight on production path, not in simulation mode.
  if [ -z "${OMNIAGENTOS_SIM_MODE:-}" ] || [ "${OMNIAGENTOS_SIM_MODE}" != "1" ]; then
    # Check that connections.env exists and required API keys are set.
    # These vars would have been sourced by the plist before launch-env.sh.
    _omni_preflight_env_vars "$HOME/.config/omni/connections.env" || return 1
  fi
fi
    if ! eval "${_omni_secrets_exports}"; then
      printf '%s\n' 'launch-env.sh: failed to apply runtime secrets exports' >&2
    fi
  else
    printf '%s\n' 'launch-env.sh: failed to load runtime secrets' >&2
    sed -n '1,20p' "${_omni_secrets_stderr}" >&2
  fi
  rm -f "${_omni_secrets_stderr}"
else
  printf '%s\n' 'launch-env.sh: failed to create runtime secrets error log' >&2
fi
unset _omni_secrets_exports _omni_secrets_stderr

unset _omni_launch_env_self _omni_launch_env_dir _omni_repo_root _omni_sim_fail _omni_sync_registry _omni_sim_campaign_root _omni_sim_root _omni_sim_var _omni_sim_val
