# Backup and Restore Runbook

Operator procedure for taking a SQLite snapshot, restoring one to a fresh
destination, and producing a local Git bundle.

> **Status of this runbook.** The isolated S1–S3 self-test, shell syntax checks
> and the targeted Ruff check passed in this package's sandbox on 2026-08-03.
> Those checks created only throwaway databases, ledgers, repositories and
> bundles. No live database, service, provider, launchd state or operator backup
> was touched. There is still no qualified production backup, verified production
> restore or recovery-point claim; an operator-supervised real run and its receipt
> are the only evidence for those claims.

---

## 1. What the scripts enforce

Each script is a small Python program driven from a shell wrapper. Read the
source before trusting this summary; it is the authority, this table is not.

* **Descriptor-pinned paths.** Directory components are walked one at a time with
  `O_DIRECTORY | O_NOFOLLOW` from the filesystem root. A symlink at any ancestor
  or at the final component is refused, including a dangling one and including
  the configured default-database path. `realpath` is never used as the authority
  boundary. Filesystem publication and cleanup operations are basename-relative
  through held parent descriptors. The sole pathname SQLite opens are the
  WAL-aware source and the private pinned temporary copy; their held identities
  are revalidated immediately around each connection and backup operation.
* **Held identities, revalidated.** The source file, the destination parent, the
  private temporary directory and the published object are pinned by
  `(st_dev, st_ino, st_mode)` and revalidated — including re-walking the textual
  path and comparing it against the held descriptor — before the copy, before the
  publishing link, after the temporary unlink and after private cleanup. SQLite
  evidence after DELETE-mode normalization is collected
  through the already-held file descriptor, not the final pathname.
  A parent directory exchanged underneath the run is a refusal, not a retry.
* **No overwrite, no aliasing.** The destination must not exist. A destination
  that is a symlink, or that is the same inode as the source, or that is the
  configured default/live database (compared by parent identity and basename, not
  by text) is refused. A default-database path that is itself reached through a
  symlink is refused outright rather than resolved.
* **WAL-aware copies.** The SQLite online backup API runs against the validated
  source pathname while the source identity is held, so the companion `-wal` and
  `-shm` files are still in play and committed-but-uncheckpointed frames are
  copied. A `file:/dev/fd/N` handle is deliberately **not** used, because it
  loses those companion names. The copy is then normalised to `journal_mode=DELETE`
  so the published artifact is a self-contained, cleanly closed database.
* **Private staging.** Work happens in a `0700` directory created inside the
  pinned destination parent, in a `0600` file created with
  `O_CREAT | O_EXCL | O_NOFOLLOW`. Git precreates its bundle through that held
  descriptor and directs `git bundle create -` to it; bundle verify/list-heads
  also use an inherited descriptor-backed name rather than a published pathname.
* **One publication transaction.** The transaction opens before the publishing
  `link` and covers the link, the destination open, validation, the temporary
  unlink, the full post-unlink revalidation, the `fsync` of both the file and the
  pinned parent directory, private-temp cleanup, terminal evidence regeneration,
  receipt construction and the terminal stdout flush. The rollback flag is set
  *before* the link, so an asynchronous exception delivered between the syscall
  and the assignment cannot leave an unrolled-back publication.
* **Identity-checked rollback.** On any failure the final basename is renamed
  into a unique private quarantine entry in the same pinned parent; the
  quarantined regular object is then opened with `O_NOFOLLOW`, pinned and
  revalidated immediately before removal, and it is deleted **only** if it
  is exactly the inode this invocation published. A foreign object that replaced
  the destination is never unlinked and never recursively removed: it is
  preserved under its exact quarantine name and reported. Portable Python/POSIX
  does not expose an identity-bound no-replace restore primitive, so all foreign
  object types remain quarantined; overwriting rename and raceable pathname link
  are never used to emulate restoration.
* **Checked cleanup.** The private temporary directory is compared against the
  identity it was created with before anything is removed, and it is never
  removed recursively. A replaced temporary directory is left alone and the
  cleanup failure is reported; it is not swallowed.
* **Strict Git evidence.** Every `git` invocation runs with all ambient `GIT_*`
  selectors stripped. `--show-object-format` must be exactly one record of `sha1`
  or `sha256` with one terminal newline — there is no 40-character fallback. The
  repository path query must be exactly three records. Object IDs must be full
  lower-hex of the repository's own length. Duplicate, malformed, whitespace-bearing,
  missing, extra, invalid and pseudoref records are all refusals. The bundle's ref
  map must equal the source's exactly, in content and count, and the HEAD object
  id plus its symbolic-ref name (or the literal `DETACHED`) must be identical
  before and after. Git stdout is captured as bytes and strict UTF-8/terminal-LF
  grammar rejects CRLF, bare CR and noncanonical records. Every pathname Git
  invocation after bootstrap discovery is bracketed by worktree, Git-dir,
  common-dir and top-level `.git` marker revalidation. Bootstrap discovery is
  source-parent/source-directory bracketed, then repeated after those authorities
  are pinned; linked-worktree marker content and `gitdir:` target are included.

---

## 2. Database backup

```bash
./scripts/backup/db-backup.sh <source_db_path> <destination_db_path>
```

Both arguments are required. The destination must not exist. On success a JSON
receipt is written to stdout and nothing else is. Before terminal receipt
emission, failures write no stdout; a low-level partial stdout write or flush can
emit incomplete bytes but is treated as failure and rolls publication back. A
complete receipt is never paired with a failed publication.

The receipt's evidence fields are measured **after** the temporary link is
unlinked, which is what `evidence_stage` records:

```json
{
  "source": "/absolute/path/to/source.db",
  "destination": "/absolute/path/to/backup.db",
  "integrity": "ok",
  "user_version": 0,
  "migration_head": 113,
  "row_counts": {"schema_migrations": 1, "test_data": 3},
  "schema": [["table", "test_data", "test_data", "CREATE TABLE ..."]],
  "sha256": "<64 hex characters>",
  "identity": {"dev": 0, "ino": 0, "mode": 33152, "nlink": 1, "size": 0,
               "path": "/absolute/path/to/backup.db"},
  "source_identity": {"dev": 0, "ino": 0, "mode": 33188, "nlink": 1, "size": 0,
                      "path": "/absolute/path/to/source.db"},
  "evidence_stage": "post_unlink"
}
```

`migration_head` is `null` when the database has no `schema_migrations` table.
At c404, the repository migration files extend through `109`–`113`, so `113` is
the current file head; an individual receipt reports the database it copied and
may legitimately be lower. The numbers above show the shape, not a recorded
production run.

---

## 3. Database restore

```bash
./scripts/backup/grok-db-restore.sh <backup_file_path> <new_destination_db_path>
```

The restore writes only the destination the caller names, it never overwrites an
existing path, and it refuses the configured default/live database. It applies
the same pinning, publication, rollback and cleanup rules as the backup and emits
the same receipt shape.

Restoring **over** a live database is not something these scripts do. The
operator procedure is: restore to a fresh path, verify the receipt against the
backup receipt, stop the consumers, and move the file into place as a separate,
supervised step.

---

## 4. Git repository backup

```bash
./scripts/backup/git-backup.sh <source_git_repo_path> <destination_bundle_path>
```

Creates a local bundle from an explicit source and destination. It performs no
push, no fetch and no network action of any kind, and it has no hard-coded
sibling path. The destination may not sit inside the repository's worktree, Git
directory or common directory. Linked worktrees are supported: the worktree, Git
directory, common directory and `.git` marker identities are pinned and
revalidated around every pathname invocation, and they are reported in the
receipt under `repository`. `head_ref` is the symbolic ref name for an attached
HEAD and the literal `DETACHED` otherwise. An unborn HEAD is refused.

---

## 5. Dry run, live step, rollback trigger, receipt

* **Dry run.** Run both scripts against throwaway copies in a scratch directory
  and read the receipts. Nothing here needs the live database or the live
  repository.
* **Operator-supervised live step.** Taking a backup of the real control-plane
  database is the first step that touches real state. It is read-only on the
  source, but it must still be run with the operator watching, and the receipt
  must be kept.
* **Rollback trigger.** Any non-zero exit. The scripts roll their own publication
  back; the operator's job is to read the `Rollback:` lines on stderr, because
  they are the only place a preserved foreign object or a refused cleanup is
  reported. If a `Rollback:` line names a preserved quarantine entry, do not
  delete it — inspect it.
* **Receipt.** The JSON on stdout is the artifact. Store it beside the backup. A
  backup with no receipt has not been verified by anything.

---

## 6. Scheduling (launchd)

`db-backup.sh` gained two additive, bash-only flags. Neither changes the
descriptor-pinned Python backup logic in section 1 above; `--nightly` re-execs
the script in its ordinary two-argument form, so a scheduled run goes through
exactly the same security path as a manual one.

* **`--nightly <source_db> <backup_dir>`** — computes a timestamped, never-
  existing destination (`<backup_dir>/<basename>.<UTC timestamp>.bak`) and
  invokes the normal backup. Safe to call repeatedly; every run gets a new
  filename so "destination must not exist" never fires against a prior run.
* **`--emit-launchd-plist [label]`** — prints a RENDER-ONLY plist to stdout.
  Nothing is installed, loaded or written outside stdout by this flag.

Per the estate's launchd convention (see
`scripts/gate-watch/com.omniagentos.gate-watch.plist.template`), a plist
in the repo is never loaded from the repo. Installing is a separate,
explicit operator action:

```bash
./scripts/backup/db-backup.sh --emit-launchd-plist com.omniagentos.db-backup \
  > ~/Library/LaunchAgents/com.omniagentos.db-backup.plist
```

Then, **before bootstrapping**, edit the plist and replace the two
placeholder `ProgramArguments` entries
(`REPLACE-WITH-SOURCE-DB-...` and `REPLACE-WITH-BACKUP-DIR-...`) with the
real absolute source-database path and the real absolute backup directory —
launchd strips `PATH` and does not source a shell profile, so every path in
the plist must already be absolute. Validate before loading:

```bash
plutil -lint ~/Library/LaunchAgents/com.omniagentos.db-backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.omniagentos.db-backup.plist
launchctl kickstart -p gui/$(id -u)/com.omniagentos.db-backup
```

The emitted job runs daily at 03:00 local time (`StartCalendarInterval`,
`Hour: 3, Minute: 0`), with `RunAtLoad` set to `false` so bootstrapping it
does not immediately touch a live database — the first run happens at the
next 03:00. `StandardOutPath`/`StandardErrorPath` point at
`var/log/<label>.log` under the repo root; that log is a plain file, not a
push alert. To also get a phone alert on failure, wrap the
`ProgramArguments` command in a small shell script that tees stderr and
calls the estate's existing ntfy transport (the `OMNI_NTFY_URL` convention
used by `scripts/gate-watch/gate_watch.py`) only on a non-zero exit; the
emitted plist intentionally ships without that wrapper so a first install
cannot silently start pushing to a channel nobody has configured yet.

**Verifying the schedule is alive:** `ls -t var/log/<label>.log
<backup_dir>` after the first scheduled run; the falsifier for this section
is the same one the underlying proposal names — if 30 days after install
`ls` of the backup directory shows no artifact newer than 24h, the job is
not running and must be re-bootstrapped and investigated, not assumed
healthy.

---

## 7. Restore drill

A restore drill proves the backup is actually restorable, not just present.
Run it against a **scratch destination only** — never over a live database
(section 3 already refuses to overwrite the configured default/live path,
but the operator procedure below adds a second, human-level guard).

1. Pick the newest artifact under the scheduled backup directory
   (`ls -t <backup_dir> | head -1`).
2. Restore it to a fresh scratch path:
   ```bash
   ./scripts/backup/grok-db-restore.sh <backup_dir>/<artifact> /tmp/restore-drill/<name>.db
   ```
3. Compare the restore receipt's `sha256`, `schema`, `row_counts` and
   `migration_head` fields against the original backup receipt for the same
   artifact (kept per section 5's "a backup with no receipt has not been
   verified by anything"). They must match.
4. Record the drill below. This runbook does not claim a drill has been run
   until an operator fills this in with a real date and real receipts —
   see the status note at the top of this file.

| Field | Value |
|---|---|
| Drill date (UTC) | _unset — fill in after the first operator-supervised drill_ |
| Source DB | _unset_ |
| Backup artifact restored | _unset_ |
| Restore receipt matched backup receipt | _unset_ |
| Recovery Point Objective (RPO) | _unset — set once the launchd job in section 6 has run at least once; RPO is bounded by the `StartCalendarInterval` above (nominally 24h) plus however long a missed run went uninvestigated_ |
| Operator | _unset_ |

Until every row above is filled in with real values, this runbook's schedule
and drill sections describe capability, not a completed, verified backup —
the same distinction the status note at the top of this file already makes
for the underlying scripts.
