# OmniAgentOS — operator entry points.
# `make validate` / `make release-gate` = H-30 immutable pinned-SHA full gate.

.PHONY: api api-contracts bench bench-lane build-dash classify-debt counterfeit-gate coverage-policy dash e2e lint migrate nscert-t1 nscert-t2 openapi path-security-gate reflect reflect-apply release-gate release-gate-dry release-gate-list runner scale-gate secrets-catalog secrets-doctor secrets-inventory secrets-rotate simharness smoke sync test test-comprehensive test-coverage-scale test-doctrine test-entrypoints test-live test-perf type validate

sync:
	uv sync --all-extras
	$(MAKE) migrate

# Source launch-env first so migrate targets the same OMNIAGENTOS_DB that
# api/runner use (otherwise migrate would hit the process default while
# launchd/recipes operate on the product runtime DB).
migrate:
	if [ "$${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED; fi; \
	. ./scripts/launch-env.sh && uv run python -m omniagentos.db.migrate

# api/runner depend on migrate (idempotent — tracks applied versions in
# schema_migrations) so a first-press launch against an un-migrated DB applies
# schema first instead of 500ing on missing tables/columns.
# Both recipes source scripts/launch-env.sh so OMNIAGENTOS_DB is canonical.
api: migrate
	if [ "$${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED; fi; \
	. ./scripts/launch-env.sh && uv run uvicorn omniagentos.api:app --host 127.0.0.1 --port "$${OMNIAGENTOS_API_PORT:-8485}"

runner: migrate
	if [ "$${OMNIAGENTOS_SIM_MODE:-}" = "1" ]; then unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED; fi; \
	. ./scripts/launch-env.sh && uv run python -m omniagentos.runner

dash:
	cd dashboard && npm run dev

test:
	uv run pytest -q

# Fast parallel lane (pytest-xdist, worksteal scheduling): exploratory speed, NOT
# certification. Excludes process-spawning smoke tests (serial lanes own those) and
# the quarantined parallel-unsafe certification suites; they stay serial under
# `make test`/the release gate, which remain the trusted signal. A parallel-only
# failure is an isolation bug in the test, fix it there (see TESTING.md).
#
# MARKER COMPOSITION (do not "simplify" back to -m "not smoke"): pytest's -m is
# store-not-append, so a bare -m on the command line REPLACES the expression in
# pyproject.toml:98 addopts rather than adding to it. The old `-m "not smoke"` silently
# readmitted 9 live/perf tests across 8 files — 202.0s of the 3,033.9s cumulative in the
# 2026-07-31 06:51 run (6.66%), including test_recall_10k_p95 at 158.3s, which was that
# run's LONGEST SINGLE TEST and therefore the lane's critical-path floor (wall clock is
# COLLECT + max(cum/eff_par, longest_test), so this one test set the floor by itself).
# It also fired live Jira/OpenHands/Anthropic/Ollama/Claude-bridge calls on every dev run.
# The leaked set, for regression-checking:
#   test_jira_live::test_live_myself_read_only
#   openhands/test_adapter::test_run_live_with_real_key_and_sdk
#   test_embeddings::test_ollama_embedding_real
#   test_ingest::test_live_claude_extraction
#   test_recall_perf::test_recall_10k_p95                          <- 158.3s
#   test_sandbox_cli_state::test_fable_planner_not_degraded
#   test_live_all_providers::{test_live_provider_exec_all_non_claude, ..._bridge_account_3}
#   test_scale_gates::test_bounded_scale_gate_certifies_100_sessions_and_10_projects
# The expression below must stay a superset of pyproject's. (Kept in the single-negation
# form landed by repair/qa-20260731; `not (a or b)` == `not a and not b`.)
#
# WORKER COUNT: capped at the P-core count. This box is 16 performance + 8 efficiency
# cores; workers 17-24 land on E-cores at ~1/3 speed and measurably RAISE cumulative
# test time (-n 16 cum 2430.8s vs -n 24 cum 3149.8s on the same tree, 2026-07-31).
# -n auto (24) is separately documented to deadlock the suite. Do not raise past 16.
FAST_LANE_MARKERS := not (smoke or live_cli or perf or live_ollama or live or counterfeit_gate or feature_health or e2e or livesim)
# PER-HOST OPTIMUM (measured 2026-07-31, override via env; ?= respects it):
#   macOS M2 Ultra (16 P + 8 E cores): 16. Workers 17-24 land on E-cores at ~1/3 speed
#     and RAISE cumulative time (-n 16 cum 2430.8s vs -n 24 cum 3149.8s). -n auto deadlocks.
#   initech-crmnew (24 physical / 48 SMT): 32 — wall 106.3s@16 -> 87.9s@24 -> 81.4s@32.
#     Set via FAST_LANE_WORKERS=32 in that box's ~/.bashrc.
# CAVEAT for anyone A/B-ing an optimisation: CUMULATIVE seconds RISE with worker count
#   (1414.9s@16 -> 1914.2s@32 on crmnew) because per-test contention grows. Wall clock and
#   cumulative move in OPPOSITE directions here. Pin a FIXED N across both sides of any
#   before/after comparison, or you will measure the scheduler instead of your change.
FAST_LANE_WORKERS ?= 16

test-fast:
	@mkdir -p var/test-reports
	uv run pytest -q -n $(FAST_LANE_WORKERS) --dist worksteal -m "$(FAST_LANE_MARKERS)" \
		--ignore=tests/simharness --ignore=tests/counterfeits --ignore=tests/longhaul \
		--durations=25 --junitxml=var/test-reports/fast-lane-latest.xml

# Phase 0 measurement floor. A single wall-clock number is not evidence: the same lane
# on this box on 2026-07-31 produced wall 271.7-814.5s and 22-66 failures purely as a
# function of load. `make bench` repeats the lane, refuses to start on a contended box,
# and reports cumulative seconds + eff_par + failure stability alongside wall clock.
#   make bench-lane                 # 5 runs of test-fast, refuses above load 4.0
#   make bench-lane RUNS=3 MAX_LOAD=8 TAG=phase1
# (distinct from `bench`, which captures arm baselines via scripts.benchmarks)
BENCH_LANE ?= test-fast
RUNS ?= 5
MAX_LOAD ?= 4.0
TAG ?= latest

bench-lane:
	uv run python scripts/bench_lane.py --lane $(BENCH_LANE) --runs $(RUNS) \
		--max-load $(MAX_LOAD) --tag $(TAG)

# ---------------------------------------------------------------------------
# Phase 4 — lane architecture. Four honestly-labelled lanes, in order of scope
# (see TESTING.md "Lane Architecture" for what each one is for, its exact
# composition, and its measured p50 — NONE of these are certification except
# test-full):
#
#   test-dev      impacted tests + acceptance_smoke        target p50 <=5s
#   test-pr       impacted tests + acceptance_smoke/daily   target p50 <=60s
#   test-full     complete suite, SERIAL, authoritative — alias for `make test`
#   test-nightly  csi + longhaul + simharness + live (no mutation harness exists
#                 in this repo yet — not faked here; see TESTING.md)
#
# A lane's cost is a function of WHAT CHANGED, not a constant: test-dev/test-pr
# resolve the impacted test set with scripts/testlanes/impacted.py (a transitive
# reverse-import + data-reference analysis, not a directory-name heuristic) and
# pass explicit PATHS to pytest, so collection stays scoped instead of paying the
# ~4s full-tree floor. TESTING.md publishes per-scenario p50s, not one number.
#
# Three properties these targets must never lose (all three were review blockers):
#   * every step composes FAST_LANE_MARKERS — a bare `-m acceptance_smoke`
#     REPLACES pyproject.toml's addopts and re-admits live/perf/counterfeit_gate;
#   * tests/doctrine runs SERIALLY (its revert harness mutates shared fixtures in
#     place and races itself under -n, leaving the checkout dirty);
#   * a SIGKILLed step is a failure, not exit 0.
#
# LANE_BASE / LANE_CHANGED are for reproducing TESTING.md's benchmarks:
#   make test-dev LANE_CHANGED="--changed omniagentos/db/store.py"
# ---------------------------------------------------------------------------
.PHONY: test-dev test-pr test-full test-nightly

LANE_BASE ?=
LANE_CHANGED ?=

test-dev:
	@mkdir -p var/test-reports
	uv run python -m scripts.testlanes.run_lane --lane dev \
		$(if $(LANE_BASE),--base $(LANE_BASE),) $(LANE_CHANGED) \
		--junit var/test-reports/test-dev-latest.xml

test-pr:
	@mkdir -p var/test-reports
	uv run python -m scripts.testlanes.run_lane --lane pr \
		$(if $(LANE_BASE),--base $(LANE_BASE),) $(LANE_CHANGED) \
		--junit var/test-reports/test-pr-latest.xml

# Same command as `make test` (left untouched, per Phase 4 scope) — this is just
# the lane-architecture name for it, so TESTING.md's four-lane story has one
# command per lane and `test`/`test-full` can never silently drift apart.
test-full: test

# Frozen/quarantined engines (csi, longhaul, simharness — see their own module
# docstrings) plus real external-service calls. Not run on every commit or PR;
# this is the sweep that catches what the fast lanes structurally cannot.
test-nightly:
	@mkdir -p var/test-reports
	uv run pytest -q tests/csi tests/longhaul --junitxml=var/test-reports/test-nightly-latest.xml
	$(MAKE) simharness
	$(MAKE) test-live

# ---------------------------------------------------------------------------
# A4 — TestFarm hermetic lane (STRICTLY OPT-IN; see TESTING.md "Hermetic Lane").
# Same DEFAULT suite selection as `make test` (serial, pyproject addopts markers
# apply) with the TestFarm socket guard ACTIVE. Exact intercepted surface:
# socket.socket.connect / connect_ex, the address-carrying datagram sends
# sendto / sendmsg, and socket.getaddrinfo (hostname resolution for non-local
# names). NOT intercepted: gethostbyname / gethostbyname_ex / gethostbyaddr,
# raw _socket.socket use that bypasses the socket-module subclass, native
# extensions connecting in C, and child processes. Loopback + unix allowed.
#
# ISOLATION CONTRACT (why this lane has its OWN, NON-REDIRECTABLE venv): the
# testfarm plugin auto-activates via its pytest11 entry point the moment the
# package is installed — there is no dormant-while-installed mode. If testfarm
# ever landed in .venv, EVERY pytest lane would silently run guarded. So:
#   - HERMETIC_VENV is `override`-pinned to .venv-hermetic; a command-line or
#     environment override attempt is a hard parse-time error (guard below).
#   - scripts/hermetic-venv-guard.sh additionally refuses at run time if
#     .venv-hermetic is a symlink (e.g. redirected to .venv) or not a real
#     repo-local directory. testfarm is NEVER installed into .venv;
#     `make test` / `make test-fast` are byte-identical whether or not this
#     target has ever run.
#
# IDEMPOTENCY (scope stated precisely): `uv sync --locked` refuses to rewrite
# uv.lock (fails loudly if the lock is stale) and prunes anything not in the
# lockfile — including a previously installed testfarm — then the editable
# install restores testfarm. Rerunning therefore converges to lockfile state
# PLUS whatever the TESTFARM_SRC checkout currently contains: testfarm and its
# own deps (pytest-asyncio, pytest-recording, vcrpy) are resolved at install
# time, NOT pinned by this repo's lock. The target prints the testfarm commit
# so every run records which harness it enforced with. Venv preparation is
# serialized via a .venv-hermetic.preparing lockdir so two concurrent lane
# invocations cannot interleave sync/install.
#
# TESTFARM_HERMETIC=1 is a fail-loud handshake, not the activation switch:
# tests/conftest.py refuses to start when the flag is set but the plugin is
# missing/disabled, refuses --testfarm-allow-network without an explicit
# TESTFARM_HERMETIC_ALLOW_NETWORK_ACK=1 acknowledgement, and prints a mode
# banner to stderr that -q cannot suppress (also recorded as a JUnit
# testsuite property). Without the flag, conftest never imports testfarm.
#
# CONTRACT LIMIT: Python-socket-level tripwire only — see the exact surface
# above; libpq via psycopg[binary] and spawned child processes need an OS
# boundary (docker --network=none / netns / firewall). Escape hatches:
# @pytest.mark.live per test; --testfarm-allow-network per run (hermetic lane
# additionally requires TESTFARM_HERMETIC_ALLOW_NETWORK_ACK=1).
#
#   make test-hermetic                                   # full default suite
#   make test-hermetic HERMETIC_PATHS="tests/scheduler"  # scoped
# ---------------------------------------------------------------------------
.PHONY: test-hermetic

TESTFARM_SRC ?= /Users/youruser/testfarm
# Refuse any attempt to point the hermetic venv elsewhere (the counterfeit
# shape `make test-hermetic HERMETIC_VENV=.venv` would install the always-on
# testfarm plugin into the production venv). Parse-time, so it can never be
# reached-around by recipe edits.
ifdef HERMETIC_VENV
ifneq ($(HERMETIC_VENV),.venv-hermetic)
$(error HERMETIC_VENV is not overridable: the hermetic lane always uses .venv-hermetic (got '$(HERMETIC_VENV)'))
endif
endif
override HERMETIC_VENV := .venv-hermetic
HERMETIC_PATHS ?=

test-hermetic:
	@test -d "$(TESTFARM_SRC)/src/testfarm/harness" || { echo "error: TESTFARM_SRC=$(TESTFARM_SRC) does not look like a testfarm checkout (set TESTFARM_SRC=/path/to/testfarm)" >&2; exit 2; }
	@sh scripts/hermetic-venv-guard.sh "$(HERMETIC_VENV)"
	@mkdir -p var/test-reports
	@if ! mkdir "$(HERMETIC_VENV).preparing" 2>/dev/null; then \
		echo "error: another test-hermetic venv preparation appears active ($(HERMETIC_VENV).preparing exists; rmdir it if stale)" >&2; exit 2; \
	fi; \
	trap 'rmdir "$(HERMETIC_VENV).preparing" 2>/dev/null' EXIT; \
	UV_PROJECT_ENVIRONMENT=$(HERMETIC_VENV) uv sync --locked --all-extras && \
	uv pip install --quiet --editable "$(TESTFARM_SRC)" --python $(HERMETIC_VENV)/bin/python && \
	sh scripts/hermetic-venv-guard.sh "$(HERMETIC_VENV)"
	@echo "testfarm checkout: $$(git -C "$(TESTFARM_SRC)" rev-parse HEAD 2>/dev/null || echo 'not a git checkout')"
	TESTFARM_HERMETIC=1 $(HERMETIC_VENV)/bin/pytest -q $(HERMETIC_PATHS) \
		--junitxml=var/test-reports/test-hermetic-latest.xml

# Test doctrine helpers + self-proofs (revert, counterfeit, trap guards).
# See tests/doctrine/TEST-DOCTRINE.md. Required evidence shape for later lanes.
# Also collected by `make test`; this target is the explicit, fast entry point.
test-doctrine:
	uv run pytest -q tests/doctrine

# Deterministic API -> coordinator -> provider-exec simulations (no live CLIs).
simharness:
	@SIMHARNESS_EVIDENCE_DIR="$${SIMHARNESS_EVIDENCE_DIR:-$${TMPDIR:-/tmp}/omniagentos-simharness-evidence}" uv run pytest -q -s tests/simharness
# Production entry-point suite: enters via real lifespan/HTTP, never imports the
# mechanism under test (see tests/entrypoints/ and O-6/O-16).
test-entrypoints:
	uv run pytest -q tests/entrypoints

# Max parallelism + feature matrix + product packages (Grok product).
test-comprehensive:
	bash scripts/test-comprehensive.sh

lint:
	uv run ruff check .

type:
	uv run mypy omniagentos

build-dash:
	cd dashboard && npm install && npm run lint && npm run build

smoke:
	uv run pytest -q -m smoke

e2e:
	bash scripts/smoke/e2e.sh

bench:
	@test -n "$(BENCH_ARM)" || { echo "error: BENCH_ARM is required (for example, BENCH_ARM=oracle)" >&2; exit 2; }
	uv run python -m scripts.benchmarks.capture_baseline --arm "$(BENCH_ARM)" $(BENCH_ARGS)

test-perf:
	uv run pytest -q -m perf

test-live:
	uv run pytest -q -m 'live or live_ollama'

# S19A / M-21 — whole-API OpenAPI artifact (production docs stay disabled).
openapi:
	uv run python scripts/generate_openapi.py

# S19A / M-21 — fail-closed contract presence + anti-drift diff.
api-contracts:
	uv run pytest -q tests/api/test_openapi_contract.py

# L19/S19B focused harness (no full-repo gate, no live services).
test-coverage-scale:
	uv run pytest -q tests/testpolicy tests/reliability/test_memory.py

# M-10/M-22: structured demo-subset self-check (exit 0 only when policy rejects
# the audited subset for the expected reasons — not a shell exit-code trick).
coverage-policy:
	uv run python scripts/coverage/check_coverage_policy.py --demo-subset

# M-23: in-process scale/backpressure certification.
scale-gate:
	uv run pytest -q tests/testpolicy/test_scale_gates.py

# M-20/L-02: classify broad handlers + dead code (no rewrites).
classify-debt:
	uv run python scripts/coverage/classify_debt.py --actionable-only | head -c 4000

# H-30 — immutable pinned-SHA release / convergence gate.
# Refuses dirty or moving HEAD; writes phase evidence under $OMNIAGENTOS_VAR_DIR.
# Do not treat certify-omniagentos.sh / test-comprehensive.sh as substitutes.
#
# OMNIAGENTOS_REQUIRE_PG=1 is NOT exported here on purpose. `validate` used to be
# `OMNIAGENTOS_REQUIRE_PG=1 uv run pytest -q`; that guarantee now lives in
# release_gate.run_phase/certification_env, which applies it to every phase
# subprocess. Re-adding it to this recipe would imply the gate depends on Make.
# See docs/RELEASE-GATE.md.
release-gate:
	bash scripts/release-gate.sh

release-gate-dry:
	bash scripts/release-gate.sh --dry-run

release-gate-list:
	bash scripts/release-gate.sh --list

# Full acceptance gate = H-30 release gate (not the old lint+pytest+build chain).
validate: release-gate

# North Star certification is manifest-selected and receipts are recorded by
# the adapter owned by the certification package.  The protected pool is
# created at runtime only; var/ remains intentionally untracked.
NSCERT_MANIFEST := configs/northstar-cert/manifest.yaml
NSCERT_RESULTS_DIR := var/northstar-cert
# Live gap filing is OFF unless NSCERT_GAPS_LIVE=1 is in the environment.  With
# it, gaps are emitted to $(NSCERT_GAPS_DIR) (labelled live, not dry-run),
# checks that now pass get their gap artifact stamped resolved, and the open
# gaps are filed into the loop queue as findings.  Without it, both targets do
# exactly what they did before: one dry-run emit into gaps-dryrun/, no queue
# writes.  Both adapters ALSO require the same env key, so the make branch alone
# cannot arm them.
NSCERT_GAPS_DIR := $(NSCERT_RESULTS_DIR)/gaps

# ONE procedure, parameterised by tier: t1 and t2 must not be able to drift
# apart, because a fix landed in one of two identical carriers is how this
# recipe's exit contract went wrong the first time.  The third carrier is
# scripts/northstar-cert-cadence/t1_cadence.sh (launchd has neither make nor
# uv on its PATH); it implements the SAME contract and must change with this.
#
# Exit contract:
#   * target selection uses the recorder's own requires-aware --list-targets.
#     A masked or pending node id aborts pytest COLLECTION for the whole run,
#     so the selector and the grader must be one implementation, never two.
#   * pytest runs with a TAUTOLOGICAL `-m` expression, which overrides the
#     default marker exclusion in pyproject's addopts (counterfeit_gate, e2e,
#     livesim, perf, live*) while preserving every other addopt.  Those
#     exclusions are for the DEFAULT whole-suite selection; a manifest target is
#     an EXPLICIT node id, and pytest applies -m filters to explicit node ids
#     too -- so the counterfeit-bound hard gates were silently deselected, pytest
#     exited 0, and the recorder rendered NOT_EVALUABLE(not_executed) for a check
#     that was never allowed to run.  What the manifest selects, the run executes.
#   * pytest 0/1  -> both are certification evidence; continue.
#   * pytest >=2  -> the instrument did not run (interrupted / internal error /
#     usage error / nothing collected).  ABORT before recording: recording that
#     junit would mark every check not_executed and, when armed, file a queue
#     full of findings about a pytest invocation.
#   * recorder 70 (VOID) -> the run was not measurable.  NO gap emission: an
#     instrument fault must never be reported as a candidate product defect.
#   * recorder 0/1/2 -> a real verdict.  Emit (and file, when armed) FIRST, then
#     exit with the recorder's rc, so an honest FAILED/INCONCLUSIVE still feeds
#     the loop instead of dropping its gaps on the way out.
#   * the emit/file stage can only speak when the recorder said 0 -- it can
#     never turn a nonzero verdict green, and `|| true` appears nowhere.
# $$NSCERT_AVAILABLE_REQUIREMENTS (space separated) forwards operator-declared
# satisfied requirements to BOTH selection and grading; launchd: tokens are
# probed automatically by the recorder.
define nscert_run
	@AVAIL=""; \
	for requirement in $${NSCERT_AVAILABLE_REQUIREMENTS:-}; do \
		AVAIL="$$AVAIL --available-requirement $$requirement"; \
	done; \
	TARGETS="$$(uv run python scripts/northstar_cert/record_results.py --manifest $(NSCERT_MANIFEST) --tier $(1) --list-targets $$AVAIL)" || exit $$?; \
	if [ -z "$$TARGETS" ]; then \
		echo "nscert-$(1): manifest selected zero runnable pytest targets" >&2; \
		exit 70; \
	fi; \
	RUN_ID="nscert-$(1)-$$(date -u +%Y%m%dT%H%M%SZ)"; \
	JUNIT="$(NSCERT_RESULTS_DIR)/$(1)-junit.xml"; \
	rm -f "$$JUNIT"; \
	PYTEST_RC=0; \
	uv run pytest -q -m "counterfeit_gate or not counterfeit_gate" --junitxml="$$JUNIT" $$TARGETS || PYTEST_RC=$$?; \
	if [ "$$PYTEST_RC" -gt 1 ]; then \
		echo "nscert-$(1): pytest could not run (exit $$PYTEST_RC); nothing recorded" >&2; \
		exit $$PYTEST_RC; \
	fi; \
	RECORD_RC=0; \
	uv run python scripts/northstar_cert/record_results.py --manifest $(NSCERT_MANIFEST) --tier $(1) --junitxml "$$JUNIT" --run-id $$RUN_ID $$AVAIL || RECORD_RC=$$?; \
	if [ "$$RECORD_RC" -eq 70 ]; then \
		echo "nscert-$(1): run is VOID (recorder exit 70); no gaps emitted" >&2; \
		exit 70; \
	fi; \
	STAGE_RC=0; \
	if [ "$${NSCERT_GAPS_LIVE:-0}" = "1" ]; then \
		uv run python scripts/northstar_cert/emit_gaps.py --run-id $$RUN_ID --output-dir $(NSCERT_GAPS_DIR) --live --resolve || STAGE_RC=$$?; \
		if [ "$$STAGE_RC" -eq 0 ]; then \
			uv run python scripts/northstar_cert/file_gap_findings.py --gaps-dir $(NSCERT_GAPS_DIR) --live || STAGE_RC=$$?; \
		fi; \
	else \
		uv run python scripts/northstar_cert/emit_gaps.py --run-id $$RUN_ID || STAGE_RC=$$?; \
	fi; \
	if [ "$$RECORD_RC" -ne 0 ]; then exit $$RECORD_RC; fi; \
	exit $$STAGE_RC
endef

nscert-t1:
	@mkdir -p $(NSCERT_RESULTS_DIR)
	@uv run python scripts/northstar_cert/seed_holdout.py
	$(call nscert_run,t1)

nscert-t2:
	@mkdir -p $(NSCERT_RESULTS_DIR)
	$(call nscert_run,t2)

secrets-doctor:
	uv run python -m omniagentos.connectors.doctor

secrets-inventory:
	uv run python -m omniagentos.connectors.inventory

# U-S2 name-only catalog. `report` is read-only. To reconcile it, run
# `make secrets-inventory` first and hand this its report:
#   python -m omniagentos.connectors.secret_catalog sync \
#       --inventory-report var/secrets-inventory-<timestamp>.json
# Neither command ever reads or prints a credential value.
secrets-catalog:
	uv run python -m omniagentos.connectors.secret_catalog report

# U-S2 rotation ceremony. The operator performs each out-of-band step and this
# sequences, verifies, and receipts it; no credential value enters the process.
# The provider-revoke step is [OPERATOR]-gated (D-05) and stays OFF unless BOTH
# --arm-provider-revoke and OMNIAGENTOS_ROTATION_PROVIDER_REVOKE=ARMED are set.
secrets-rotate:
	uv run python -m omniagentos.security.secret_rotation status

reflect: migrate
	uv run python -m omniagentos.reflection.propose
	uv run python -m omniagentos.reflection.report

reflect-apply: migrate
	uv run python -m omniagentos.reflection.propose
	uv run python -m omniagentos.reflection.apply
	uv run python -m omniagentos.reflection.report

# ---------------------------------------------------------------------------
# G7 / AT-18 — Wiring reachability registry (AST call/ref paths, not grep).
# Curated (mechanism, entry_point, status) table verified over omniagentos/
# with tests/ excluded. Known-dead wiring expected to flip soon is
# xfail(strict=True, reason=GAP:...); XPASS means flip status to REACHABLE.
# TEST_ONLY_CALLER is not xfail — a new production path hard-fails until
# reclassified. Also collected by `make test` / release-gate backend phase.
# Another lane may append nearby — keep-both merge is fine.
# ---------------------------------------------------------------------------
.PHONY: wiring-gate
wiring-gate:
	uv run pytest -q tests/acceptance/test_18_wiring_reachability.py

# Codebase-wide AST registry: every filesystem containment/identity decision
# must use the shared inode primitive or carry a source-bound written reason
# explaining why it is not a security decision. Also collected by `make test`.
path-security-gate:
	./.venv/bin/pytest -q tests/acceptance/test_19_path_security_registry.py

# ---------------------------------------------------------------------------
# W4-10 — Counterfeit corpus gate.
# Standing corpus of fakes the suite must catch. Each entry: apply mutation →
# run named must_fail → assert RED for the recorded reason → restore.
# A surviving counterfeit is a finding (coverage is decoration) and fails the
# gate. Not part of `make test`; release-adjacent like wiring-gate.
# ---------------------------------------------------------------------------
.PHONY: counterfeit-gate
counterfeit-gate:
	@if [ -n "$${OMNIAGENTOS_PYTHON:-}" ] && [ -x "$${OMNIAGENTOS_PYTHON}" ]; then \
		PY="$${OMNIAGENTOS_PYTHON}"; \
	elif [ -x .venv/bin/python ]; then \
		PY=".venv/bin/python"; \
	else \
		PY="python3"; \
	fi; \
	PYTHONPATH=. "$$PY" -m pytest -q -m counterfeit_gate tests/counterfeits/test_selftest.py && \
	PYTHONPATH=. "$$PY" -m tests.counterfeits.harness

# --- memcert ---
# Memory & self-learning certification suite (devtasks/memcert/DESIGN.md).
# These targets wire scripts/memcert/* as production entry points for the
# reachability gate; result bindings live in configs/memcert/manifest.yaml.
#   memcert-gen         deterministic dev fixture world (seed 42)
#   memcert-run         offline mock-adapter smoke (no network, no spend)
#   memcert-live        REAL SPEND: openrouter system-arm dev-split run
#   memcert-hypothesize daily hypothesizer over the latest run (DESIGN §8;
#                       dry-run by default, two-key live arming)
MEMCERT_FIXTURES_DIR := var/memcert/fixtures-dev
MEMCERT_BARS := configs/memcert/bars.yaml
MEMCERT_LIVE_MODEL ?= qwen/qwen3-coder-flash

.PHONY: memcert-gen memcert-run memcert-live memcert-hypothesize \
	memcert-sufficiency memcert-capacity memcert-retention
memcert-gen:
	uv run python scripts/memcert/gen.py --seed 42 --out $(MEMCERT_FIXTURES_DIR)

# v2 deterministic instruments (DESIGN-v2.md): no LLM, no network, no spend.
#   memcert-sufficiency  context-evidence certification vs sufficiency bars
#   memcert-capacity     MEM-I sufficiency curve across S/M/L scales
#   memcert-retention    MEM-J paired run-over-run regression check
MEMCERT_SUFFICIENCY_BARS := configs/memcert/sufficiency-bars.yaml
memcert-sufficiency:
	uv run python scripts/memcert/sufficiency.py --seeds 42,43 \
		--arms system_legacy,system,rag --bars $(MEMCERT_SUFFICIENCY_BARS)

memcert-capacity:
	uv run python scripts/memcert/capacity.py --seeds 42 --arms system,system_legacy

memcert-retention:
	@test -n "$(PREV)" -a -n "$(CURR)" || { \
		echo "usage: make memcert-retention PREV=var/memcert/runs/<a> CURR=var/memcert/runs/<b>"; \
		exit 2; }
	uv run python scripts/memcert/retention.py --prev $(PREV) --curr $(CURR)

memcert-run:
	uv run python scripts/memcert/run_bench.py --adapter mock --models mock-smoke \
		--arms none,rag --seeds 42 --trials 1 --limit-items 2

memcert-live:
	uv run python scripts/memcert/run_bench.py --adapter openrouter \
		--models $(MEMCERT_LIVE_MODEL) --arms system --split dev --seeds 42 \
		--trials 1 --bars $(MEMCERT_BARS)

memcert-hypothesize:
	uv run python scripts/memcert/hypothesizer.py
