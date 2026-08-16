# Release gate (H-30)

Immutable, pinned-SHA release / convergence gate for OmniAgentOS.

## Why

Curated certification (`scripts/certify-omniagentos.sh`) and the comprehensive subset
are component probes. They can pass while the real full suite, dashboard unit
tests, E2E, lint, format, mypy, migrations, API contracts, dependency scans,
and load exits are red. H-30 replaces that gap with **one orchestration** that:

1. pins `HEAD` once at start;
2. refuses a dirty worktree;
3. refuses if `HEAD` moves during the run;
4. runs **named phases** in a fixed order;
5. writes per-phase **evidence JSON** under `$OMNIAGENTOS_VAR_DIR/release-gate/`.

## Entry points

```bash
# List named phases (no execution)
./scripts/release-gate.sh --list
make release-gate-list

# Pin + clean-tree check + plan only (safe while other lanes are active)
./scripts/release-gate.sh --dry-run
make release-gate-dry

# Focused static phases (still refuse dirty/moving HEAD)
./scripts/release-gate.sh --phases ruff,format,mypy

# Full gate — only after integration; resource-heavy
./scripts/release-gate.sh
make release-gate
```

`make validate` is an alias for the full release gate.

Every phase subprocess is launched with `OMNIAGENTOS_REQUIRE_PG=1`, so a
certification run cannot go green because PostgreSQL-backed tests skipped
themselves. `make validate` used to export this flag directly; routing
`validate` through the gate would otherwise have dropped it silently. An
operator-exported `OMNIAGENTOS_REQUIRE_PG=0` is overridden, not honoured.

## Named phases

| Phase | What it proves |
|---|---|
| `backend` | Full backend pytest (not curated subset) |
| `dashboard_unit` | Vitest unit suite |
| `dashboard_lint` | Next/ESLint |
| `dashboard_build` | Production build into isolated `distDir` (H-32) |
| `e2e` | Official cross-process smoke E2E |
| `ruff` | Ruff lint |
| `format` | Ruff format check (no rewrite) |
| `mypy` | Package type check |
| `migrations` | Migration apply/verify |
| `api_contracts` | Whole-API OpenAPI artifact present/diffed (S19A payload) |
| `dependency_scan` | High-severity production npm audit (S19A payload) |
| `live_restart` | Selected smoke/restart paths (conditional) |
| `load_contention` | Perf/scale/backpressure exits (conditional; S19B payload) |

Conditional phases default **on** for full certification. Set
`RELEASE_GATE_LIVE=0` / `RELEASE_GATE_LOAD=0` to skip with explicit evidence;
a full-gate run with skips is **not** a certified green.

## Provenance rules

- Dirty tree → gate status `refused` (exit 3).
- HEAD changes after pin → gate status `refused` (exit 3).
- Phase non-zero exit → gate status `failed` (exit 1); stops by default.
- `RELEASE_GATE_ALLOW_DIRTY=1` is debug-only and recorded in evidence; never use for certification.

## Evidence

Each run writes JSON:

```text
$OMNIAGENTOS_VAR_DIR/release-gate/<UTC-µs>-<sha12>-p<pid>-s<seq>-<rand8>.json
```

Fields include `pinned_sha`, overall `status`, the resolved `python`, the
`require_pg` flag, and per-phase `status` / `exit_code` / `command` /
`log_excerpt` / head SHAs.

The filename carries four independent sources of uniqueness — microsecond
stamp, in-process sequence, pid, and a random suffix. Re-running the gate at the
same commit within the same second must produce a **second** audit record; a
second-resolution `<UTC>-<sha12>.json` name silently overwrote the earlier run
and destroyed the evidence the gate exists to produce.

Uniqueness is *enforced* by the write, not by the name: evidence is opened
`O_CREAT | O_EXCL`, so a colliding auto-generated name is regenerated and a
colliding operator-supplied `--evidence` path fails rather than clobbering. An
existence check before the write would be a TOCTOU gap that two concurrent runs
could both pass. If the record cannot be written at all, the gate exits 3 with
a legible reason — an unrecordable verdict is a refusal, not a crash.

## Interpreter verification

The release gate **refuses certification under a foreign Python**. It must use:

1. `OMNIAGENTOS_PYTHON` if set and executable; or
2. The project's own `.venv/bin/python`; or
3. `sys.executable` if it lies under the repo root.

This prevents certifying with packages/versions that differ from the audited
environment.

**The final path component is never canonicalised.** `.venv/bin/python` is a
symlink to the base interpreter under uv, pyenv and `python -m venv --symlinks`.
Calling `Path.resolve()` (or `realpath` / `readlink -f` in the shell wrapper)
returns the *bare base interpreter*, whose `sys.prefix` is the standalone
install — so pytest, ruff, mypy and psycopg are not importable and the gate
would certify under a Python that cannot run its own phases. The parent
directory *is* normalised (so `..` segments cannot smuggle a path past the
repo-root check); only the final component is kept verbatim, so CPython's
`site` initialisation still finds `pyvenv.cfg` next to the executable.

Path shape alone is not trusted. `verify_certification_interpreter()` **runs**
the selected interpreter and requires it to report a `sys.prefix` that differs
from `sys.base_prefix` (i.e. a real virtualenv) and to import every module in
`REQUIRED_INTERPRETER_MODULES` (pytest, ruff, mypy, psycopg). Auto-selected
interpreters must additionally report a `sys.prefix` inside the repository.

Checking `sys.prefix` rather than the executable's path is what makes this
unforgeable: `sys.prefix` is computed by CPython from the `pyvenv.cfg` beside
the executable, so symlinking a foreign interpreter to `<repo>/.venv/bin/python`
does not make it report a repo-local prefix. An explicit `OMNIAGENTOS_PYTHON`
is exempt from the locality rule only — documented operator trust — but must
still be a genuine virtualenv carrying the full toolchain.

Failure raises `InterpreterError` and the gate exits 3 (`refused`).

## Satellite payload handoff

The gate names each phase and requires it to be present, ordered and
fail-closed. Satellites own what the payload *does*; the gate must not reach
into their modules.

| Owner | Phase | Coupling surface |
|---|---|---|
| S19A | `api_contracts` | Artifact `contracts/openapi.json`, generator `scripts/generate_openapi.py`, enforced by `tests/api/test_openapi_contract.py` |
| S19A | `dependency_scan` | `npm audit --omit=dev --audit-level=high` |
| S19B | `live_restart` | pytest markers `smoke and s19b_live_restart` |
| S19B | `load_contention` | pytest markers `perf and s19b_load_contention` |

`api_contracts` runs S19A's own contract test rather than a bespoke existence
check, so both fail-closed behaviours — **absent** artifact and **drifted**
artifact — are inherited from the satellite instead of reimplemented here. If
the satellite is not merged, the test path does not exist, pytest exits
non-zero, and the phase fails; absence never reads as success.

### Why the S19B phases need a payload marker

`smoke` and `perf` are repository-wide markers that predate these phases and are
carried by tests belonging to nobody in particular. Selecting on them alone
certifies "some smoke test ran", not "S19B's restart payload ran" — S19B's
payload could be deleted outright and `live_restart` would still record
`passed`, resting on someone else's test.

Each phase therefore selects the **conjunction** of the infrastructure marker
and a payload marker that only S19B applies (`s19b_live_restart`,
`s19b_load_contention`, both registered in `pyproject.toml`). The coupling
surface stays exactly what this handoff says it is — a pytest marker — and the
gate still does not reach into S19B's modules or dictate what the tests assert.

**This is fail-closed, and that has a consequence: until S19B applies the
payload markers, both phases fail.** An unmatched marker expression deselects
everything, and the no-silent-skip guard refuses an empty selection. That is the
intended posture — a gate that cannot see the payload must refuse rather than
certify — but it is a contract change S19B has to act on, not a silent default.

### Skipped payloads are not passes

`pytest` exits 0 when every selected test skips, and when a marker expression
deselects everything. For a phase whose entire selection *is* one required
payload that is a false green: the phase records `passed` having certified
nothing. `OMNIAGENTOS_REQUIRE_PG=1` closes only one source of those skips (an
unreachable database) and says nothing about a missing marker, an unmerged
satellite payload, or a module that skips itself.

The three single-payload phases therefore run under
`-p omniagentos.harnesses.no_silent_skip`, which inspects pytest's own report
objects and converts "everything skipped" or "nothing selected" into a non-zero
exit. The whole-suite `backend` phase deliberately does **not** load it: it has
legitimate optional skips (live providers, optional extras).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `passed` or `planned` (`--dry-run`) |
| 1 | `failed` — a phase exited non-zero, or a full-gate run had skips |
| 3 | `refused` — dirty tree, moved HEAD, unknown phase, or unverifiable interpreter |

These are asserted by invoking `main()` for real against a throwaway git
repository in `tests/harnesses/test_release_gate_cli.py`; a test that
re-implements the mapping in its own body proves nothing about `main()`.

## Test ownership

| Test file | Finding | Notes |
|---|---|---|
| `tests/harnesses/test_release_gate.py` | H-30 | Gate orchestration and evidence, against an injected runner |
| `tests/harnesses/test_release_gate_cli.py` | H-30 | Real `main()` exit codes, interpreter probe, evidence collisions, `REQUIRE_PG` parity, satellite alignment |
| `omniagentos/harnesses/no_silent_skip.py` | H-30 | Not a test — the pytest plugin the required-payload phases load |
| `tests/harnesses/test_no_silent_skip.py` | H-30 | Real pytest subprocesses: all-skip, module-skip, empty selection, genuine pass/fail passthrough |
| `tests/knowledge/db_ownership.py` | H-31 | Not a test — the single force-drop guard both `conftest.py` and the perf fixture import |
| `tests/knowledge/test_db_ownership.py` | H-31 | Force-drop guards, DSN parsing, ownership tokens, repo-wide scan for unguarded destructive SQL |
| `dashboard/next.config.test.ts` | H-32 | Next root/distDir isolation |
| `tests/adapters/conftest.py` | (infra) | argv-construction fixtures; not release-gate owned |

Adapter argv-construction tests (`tests/adapters/`) are infrastructure tests
that validate command-line argument building. They use `FakePopen` fixtures
and are **not owned by the release-gate finding** (H-30). Coverage for adapter
reliability belongs to the adapter test suite itself.

## Related findings

| ID | Owned here? | Notes |
|---|---|---|
| H-30 | **yes** | This harness |
| H-31 | yes (sibling) | Unique PG/SQLite ownership for concurrent tests |
| H-32 | yes (sibling) | Next root pin + isolated `.next-build` |
| H-34, M-21 | S19A satellite | Dep advisories + OpenAPI artifact content |
| M-10, M-20–23, M-41, L-02, L-11, L-15 | S19B satellite | Coverage, scale, skips, hygiene |
