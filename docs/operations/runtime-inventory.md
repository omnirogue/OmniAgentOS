# Runtime Inventory

Canonical network ports and database/ledger identities for OmniAgentOS, read
from source. This document covers **ports and storage identities only**; launchd
labels and service registration are owned by UP-05 and are deliberately not
described here.

> **Everything below is static.** It was derived by reading source, not by
> observing a running system. No service was started, no port was probed, no
> database file was opened and no launchd state was queried while writing it.
> The live status of this machine is **UNOBSERVED**.

Symbols are cited by name rather than by line number, because line numbers go
stale silently and a stale citation is worse than none. Re-read the named symbol
in `omniagentos/contracts.py` before relying on any value here.

---

## 1. Network ports

| Purpose | Value | Source symbol |
|---|---|---|
| API bind host | `127.0.0.1` | `omniagentos/contracts.py::API_HOST` |
| API gateway port | `8485` | `omniagentos/contracts.py::API_PORT` |
| Dashboard origin | `http://127.0.0.1:3003` | `omniagentos/contracts.py::DASHBOARD_ORIGIN` |

* **8485** is the loopback-only control-plane API. The comment beside `API_PORT`
  records why it is 8485 and not 8484: the sibling Omni product owns 8484, and
  the two must never collide (see `SEPARATE-PRODUCT.md`).
* **3003** is the Next.js dashboard. `dashboard/package.json`'s `dev` script
  binds `127.0.0.1` and honours `PORT`, defaulting to 3003.
* Both are loopback bindings in source. Whether anything is currently listening
  on either port is unobserved.

---

## 2. Database and ledger identities

These are resolved at runtime by functions, not by constants, so "the database
path" is only meaningful relative to a process's environment.

### Control-plane SQLite database — `default_db_path()`

Resolution order:

1. `OMNIAGENTOS_DB` if set — it wins outright. The launchers export it, and every
   consumer must open the same file.
2. Otherwise, under a simulation/campaign context (`resolve_sim_context_or_none()`
   returns a context), the campaign-anchored `state.sqlite3` under the resolved
   var root.
3. Otherwise, the repo-relative `var/omniagentos.db`.

The launcher path is intentionally more specific than that final fallback:
`scripts/launch-env.sh` and `scripts/launch-omniagentos.sh` set
`OMNIAGENTOS_VAR_DIR` to `var/runtime` unless an override is supplied and
export `OMNIAGENTOS_DB=$OMNIAGENTOS_VAR_DIR/state.sqlite3`. Once a launcher has
set that environment, rule 1 wins for every consumer. A backup receipt reports
the applied `schema_migrations` head in its source copy; repository migration
files currently run through `109`, `110`, `111`, `112` and `113`, but that file
head must not be mistaken for proof that any particular runtime database has
applied all five.

### Durable ledger directory — `default_ledger_dir()`

Resolution order:

1. `OMNIAGENTOS_LEDGER_DIR` if set.
2. Otherwise, under a simulation/campaign context, the campaign-anchored `ledger`
   directory under the resolved var root.
3. Otherwise, the repo-relative `ledger` directory.

### Consequences for backup and restore

* A backup or restore path is only comparable to "the live database" **within one
  environment**. `scripts/backup/db-backup.sh` and
  `scripts/backup/grok-db-restore.sh` therefore resolve `default_db_path()` in
  their own process and compare the destination against it by **parent directory
  identity plus basename**, not by text — so a second spelling of the same file
  cannot slip past the guard.
* If that default path is reached through a symlink at any component, both
  scripts refuse the run rather than resolving it.
* If `default_db_path()` cannot be resolved at all, both scripts refuse. The
  guard fails closed; it never degrades to "no live database to protect".

---

## 3. Real-time status

* **Live status: UNOBSERVED.**
* This package was qualified in the sandboxed checkout for c404. The required
  S1–S3 self-test created only temporary databases, ledgers and Git repositories;
  it did not inspect this machine's runtime database. No daemon was started, no
  port was probed, no provider was called and no launchd label was inspected.
  There is no live-runtime measurement or readiness claim here.
