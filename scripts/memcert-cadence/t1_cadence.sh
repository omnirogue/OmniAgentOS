#!/bin/bash
# One daily memcert tier-1 certification cycle, run by launchd at 06:40
# (com.omniagentos.memcert-t1, StartCalendarInterval).
#
# Mirrors scripts/northstar-cert-cadence/t1_cadence.sh conventions: bash (not
# sh) because launch-env.sh resolves its location via ${BASH_SOURCE[0]}; a
# pinned venv interpreter because launchd's PATH has neither uv nor make.
#
# Stages (each stage's rc semantics follow the nscert doctrine — instrument
# failures abort before they can masquerade as product verdicts):
#   1. seed_holdout ensure        — this ISO week's cert-split seed exists
#                                   (out-of-checkout; logs the sha only)
#   2. hermetic suite             — pytest tests/memcert (junit is evidence;
#                                   rc>1 = instrument fault, abort)
#   3. live system-arm benchmark  — run_bench vs configs/memcert/bars.yaml
#                                   (rc 0 pass / 1 bar regression = evidence /
#                                   2 unchanged-input DO NOT RETRY / 70 VOID);
#                                   skipped cleanly when no OpenRouter key
#   4. hypothesizer tick          — proposals ONLY on measured improvement;
#                                   outbox by default, loopqueue only under
#                                   two keys (--live AND MEMCERT_LIVE=1)
#
# Arming: MEMCERT_LIVE=1 lets the hypothesizer file into var/loopqueue. The
# rendered plist bakes =0. Nothing here can arm itself.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

RESULTS_DIR=${MEMCERT_RESULTS_DIR:-$ROOT_DIR/var/memcert}
RUN_LOG=${MEMCERT_RUN_LOG:-$ROOT_DIR/var/log/memcert-t1.log}
LIVE=${MEMCERT_LIVE:-0}
MODELS=${MEMCERT_LIVE_MODEL:-qwen/qwen3-coder-flash}
TRIALS=${MEMCERT_TRIALS:-3}
SEEDS=${MEMCERT_SEEDS:-42,43}
BUDGET=${MEMCERT_BUDGET_TOKENS:-12000}
mkdir -p "$(dirname -- "$RUN_LOG")" "$RESULTS_DIR/runs"

log() {
    printf '%s memcert-t1 %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

# shellcheck source=scripts/launch-env.sh
if ! . "$ROOT_DIR/scripts/launch-env.sh"; then
    log "WARN: launch-env.sh returned nonzero; continuing with ambient env"
fi

VENV_PY=${MEMCERT_VENV_PY:-${OMNIAGENTOS_PYTHON:-$ROOT_DIR/.venv/bin/python}}
if [ ! -x "$VENV_PY" ]; then
    log "ABORT: venv python not found at $VENV_PY (run 'uv sync' first)"
    echo "memcert-cadence: venv python not found at $VENV_PY" >&2
    exit 1
fi

cd "$ROOT_DIR"
RUN_ID="memcert-t1-$(date -u +%Y%m%dT%H%M%SZ)"
log "run start run_id=$RUN_ID live=$LIVE py=$VENV_PY models=$MODELS"

# 1. Cert-split rotation seed for the current ISO week (hash-only output).
"$VENV_PY" "$ROOT_DIR/scripts/memcert/seed_holdout.py" ensure >> "$RUN_LOG" 2>&1 \
    || log "WARN: seed_holdout ensure rc=$? (cert rotation unavailable this run)"

# 2. Hermetic decisive suite. rc 0/1 are certification evidence; >1 means the
# instrument did not run — abort rather than record absence as verdicts.
SUITE_RC=0
"$VENV_PY" -m pytest -q tests/memcert --junitxml="$RESULTS_DIR/t1-junit.xml" \
    >> "$RUN_LOG" 2>&1 || SUITE_RC=$?
if [ "$SUITE_RC" -gt 1 ]; then
    log "ABORT: pytest could not run (exit $SUITE_RC); no results recorded"
    echo "memcert-cadence: pytest could not run (exit $SUITE_RC)" >&2
    exit "$SUITE_RC"
fi
log "hermetic suite rc=$SUITE_RC junit=$RESULTS_DIR/t1-junit.xml"

# 3. Live system-arm benchmark vs bars. A missing credential is an INSTRUMENT
# fault, not a green day (Grok review SHOULD-FIX-6 / favourable-absence
# doctrine): the run exits 70 (VOID) unless the operator has explicitly
# declared this box live-less with MEMCERT_ALLOW_NO_LIVE=1.
BENCH_RC=0
BENCH_OUT="$RESULTS_DIR/runs/$RUN_ID"
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    if [ "${MEMCERT_ALLOW_NO_LIVE:-0}" = "1" ]; then
        log "SKIP: OPENROUTER_API_KEY absent; operator-declared live-less box (MEMCERT_ALLOW_NO_LIVE=1)"
    else
        log "VOID: OPENROUTER_API_KEY absent; live benchmark NOT run — absence is never favorable (rc=70)"
        BENCH_RC=70
    fi
else
    "$VENV_PY" "$ROOT_DIR/scripts/memcert/run_bench.py" \
        --models "$MODELS" --arms system --trials "$TRIALS" --seeds "$SEEDS" \
        --scale S --split dev --adapter openrouter --budget-tokens "$BUDGET" \
        --bars "$ROOT_DIR/configs/memcert/bars.yaml" \
        --out "$BENCH_OUT" >> "$RUN_LOG" 2>&1 || BENCH_RC=$?
    log "benchmark rc=$BENCH_RC out=$BENCH_OUT"
    if [ "$BENCH_RC" -eq 2 ]; then
        log "ABORT: unchanged-input refusal (rc=2); do not retry this input"
        exit 2
    fi
    if [ "$BENCH_RC" -ge 70 ]; then
        log "ABORT: benchmark instrument failure (rc=$BENCH_RC); no hypothesizing on a broken run"
        exit "$BENCH_RC"
    fi
fi

# 4. Hypothesizer tick over the fresh run (outbox by default; two-key live).
# --exec makes the SAME tick run the registered A/B end-to-end (Sol review
# MC-006: a loop that registers but never tests silently does nothing).
HYP_RC=0
if [ -d "$BENCH_OUT" ]; then
    HYP_ARGS=(--latest-run "$BENCH_OUT" --exec)
    if [ "$LIVE" = "1" ]; then
        HYP_ARGS+=(--live)
    fi
    "$VENV_PY" "$ROOT_DIR/scripts/memcert/hypothesizer.py" "${HYP_ARGS[@]}" \
        >> "$RUN_LOG" 2>&1 || HYP_RC=$?
    log "hypothesizer rc=$HYP_RC (0=ok, 2=unchanged input, 70=void evidence)"
fi

# The benchmark verdict wins; the suite rc speaks only when the benchmark was
# clean; a VOID hypothesizer (rc>=70) surfaces when everything else was green
# — a self-improvement loop that silently never runs is its own failure mode
# (Sol review MC-006), while rc=2 (nothing new to test) is a normal day.
EXIT_RC=$BENCH_RC
if [ "$EXIT_RC" -eq 0 ]; then
    EXIT_RC=$SUITE_RC
fi
if [ "$EXIT_RC" -eq 0 ] && [ "$HYP_RC" -ge 70 ]; then
    EXIT_RC=$HYP_RC
fi
log "run end run_id=$RUN_ID rc=$EXIT_RC"
exit "$EXIT_RC"
