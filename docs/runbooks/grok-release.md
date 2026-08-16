# Grok Release Runbook

Operator procedure for running the release convergence gate on OmniAgentOS.
The gate itself is `./scripts/release-gate.sh` (also `make release-gate`, and
`make validate` as an alias); its contract lives in `docs/RELEASE-GATE.md`, which
is the authority whenever this runbook and it disagree.

> **Status.** This runbook's release gate has not been exercised for this
> package. Targeted UP-08 sandbox checks are recorded in the backup/recovery
> runbook, but no release-gate phase has run. Nothing below is a claim that this
> tree is release-qualified, and no live service, provider, database or launchd
> state has been observed.

---

## 1. What the gate guarantees about provenance

* **HEAD is pinned once at startup.** If HEAD moves during the run, the gate
  stops with status `refused` (exit 3).
* **A dirty worktree is refused** with the same status. `RELEASE_GATE_ALLOW_DIRTY=1`
  exists for debugging only, is recorded in the evidence, and must never be used
  for certification.
* **The interpreter is verified.** The gate refuses to certify under a foreign
  Python: it uses `OMNIAGENTOS_PYTHON`, else the project's own `.venv/bin/python`,
  else `sys.executable` when it lies under the repo root.
* **`OMNIAGENTOS_REQUIRE_PG=1` is forced** into every phase subprocess, so
  PostgreSQL-backed tests cannot go green by skipping themselves. An
  operator-exported `0` is overridden, not honoured.

---

## 2. Dry run

Plan-only. It pins HEAD, checks the tree is clean and lists what would run. It
is safe to use while other lanes are active.

```bash
./scripts/release-gate.sh --list      # named phases, no execution
./scripts/release-gate.sh --dry-run   # pin + clean-tree check + plan
make release-gate-dry
```

A successful dry run exits 0 with status `planned`. That is a plan, not a pass.

---

## 3. Focused phases

Subsets still refuse a dirty or moving HEAD, so they carry the same provenance
guarantee as the full run — they simply prove less.

```bash
./scripts/release-gate.sh --phases ruff,format,mypy
```

| Phase | What it proves |
|---|---|
| `backend` | The full backend pytest suite |
| `dashboard_unit` | The Vitest unit suite |
| `dashboard_lint` | Next/ESLint |
| `dashboard_build` | A production Next.js build into an isolated `distDir` |
| `e2e` | The cross-process smoke end-to-end suite |
| `ruff` | Ruff lint |
| `format` | Ruff format verification, with no rewrites |
| `mypy` | Package-level type checking |
| `migrations` | Migration apply/verify |
| `api_contracts` | The whole-API OpenAPI artifact, present and diffed |
| `dependency_scan` | High-severity production npm audit |
| `live_restart` | Selected smoke/restart paths (conditional) |
| `load_contention` | Perf/scale/backpressure exits (conditional) |

---

## 4. Operator-supervised live step

The full gate is the live step. It is resource-heavy, it runs only after
integration, and `e2e` and `live_restart` **spawn real processes** — this is not a
purely static run, and it should be watched.

```bash
./scripts/release-gate.sh
make release-gate     # or: make validate
```

The two conditional phases default **on**. `RELEASE_GATE_LIVE=0` and
`RELEASE_GATE_LOAD=0` skip them and the skip is recorded, but a full-gate run
with skips is **not** a certified green — do not report it as one.

---

## 5. Rollback trigger

The gate fails fast. Any phase exiting non-zero stops the run with status
`failed` (exit 3 is `refused`, exit 1 is `failed`).

| Exit | Status | Meaning |
|---|---|---|
| 0 | `passed` / `planned` | The run finished, or a dry run planned successfully |
| 1 | `failed` | A phase exited non-zero |
| 3 | `refused` | Dirty tree, moved HEAD, unknown phase, unverifiable interpreter, or an unwritable evidence record |

The gate publishes and promotes nothing, so "rollback" is bounded: no artifact
has been shipped and no deployment has happened. What it *does* leave behind is
whatever its own phases created — a dashboard build directory, any processes the
`e2e` and `live_restart` phases spawned, and the evidence record. Confirm those
processes are gone before re-running, fix the failing phase on the branch, and
run the gate again from a clean tree.

---

## 6. Receipt

Every run writes one JSON evidence record:

```text
$OMNIAGENTOS_VAR_DIR/release-gate/<UTC-µs>-<sha12>-p<pid>-s<seq>-<rand8>.json
```

It carries `pinned_sha`, the overall `status`, the resolved `python`, the
`require_pg` flag, and per-phase `status`, `exit_code`, `command` and log
excerpt. The record is written with `O_CREAT | O_EXCL`, so two runs at the same
commit in the same second produce two records rather than one overwriting the
other; if the record cannot be written at all, the gate refuses rather than
reporting a verdict it could not record.

That file is the convergence audit record. A release claim without one is not
evidence.
