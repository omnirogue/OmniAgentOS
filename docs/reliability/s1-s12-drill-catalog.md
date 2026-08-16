# Reliability Drill Catalog (S1–S12)

This catalog is the human-readable companion to `scripts/drills/scenarios.yaml`.
Both describe the **state of the code**, never the state of a run.

> **Sandbox verification.** On 2026-08-03, the required authorized package
> check `uv run python scripts/drills/grok_sandbox_drills.py --self-test`
> completed S1–S3 in fresh throwaway temporary directories; the targeted shell
> syntax and Ruff checks also passed. This is evidence for the sandboxed code
> paths only, not a live backup, restore, service, provider, network, PID or
> disk drill. S4–S12 remain unimplemented and NOT RUN.

S1–S3 are implemented and run entirely inside throwaway temporary directories.
S4–S12 need PID, network, provider or disk actions and remain operator-only:
the operator's explicit approval is required before any of them is attempted.

---

## Implemented drills

### S1: Backup/restore integrity

* **Driver:** `run_drill_s1` and `run_drill_s1_git` in
  `scripts/drills/grok_sandbox_drills.py`.
* **Subjects:** `scripts/backup/db-backup.sh`,
  `scripts/backup/grok-db-restore.sh`, `scripts/backup/git-backup.sh`.
* **Core flow:**
  1. Builds a throwaway SQLite source (with and without `schema_migrations`) and
     a throwaway Git repository.
  2. Runs each production script and checks the JSON receipt: the exact key set,
     the published object's device/inode/mode/link-count/size, the checksum
     against the published bytes, the complete `sqlite_master` schema, every
     table's row count, the migration head and the `user_version`, or — for Git
     — the object format, the exact ref map and count, and the HEAD state.
  3. Checks the refusal surface: no-clobber, hardlink/inode match,
     source-equals-destination, the configured default/live database, and a
     bundle destination inside the worktree, the Git directory or the common
     directory.
  4. Restores to a clean destination and compares schema, row inventory and
     checksum against the backup.
  5. Re-reads the source database bytes and inode and the source Git refs and
     HEAD and requires them to be unchanged.
* **Adversarial oracles:** A–J, described below. Each one arms a deterministic
  test-only hook in the production script. That hook can only pause or raise: it
  can never skip, weaken or substitute a control, it is inert unless the caller
  supplies both an existing hook directory and an explicit phase spec, and every
  oracle asserts on the marker file the hook leaves behind, so an unrelated
  earlier failure cannot masquerade as a passing race oracle.

| Oracle | What it injects | What the code is expected to do |
|---|---|---|
| A | A WAL source with an open writer, `wal_autocheckpoint=0` and a committed uncheckpointed row | Copy that row into the backup and the restore, and leave the source main file uncheckpointed |
| B | Source-ancestor, destination-ancestor, restore-source leaf, Git-destination leaf and default-path-alias symlinks | Refuse each one before any write |
| C | Both source and destination parents exchanged at pre-open, pre-link and post-unlink for backup, restore and Git | The source driver waits for each rendezvous, checks refusal/no output, requires the exact preserved temp contents for destination pre-link/post-unlink cases, and additionally bounds post-unlink quarantine residue to one single-link regular entry; this is unexecuted source evidence only |
| D | A `BaseException` immediately after the publishing link for each publication path | Roll back backup, restore and bundle publication with no unsafe residue |
| E | The destination replaced with a foreign regular file, symlink or directory during late failure for each path | Preserve exact foreign bytes/tree; remove only the expected published inode |
| F | A same-size byte mutation after temporary unlink for each publication path | Fail terminal checksum/evidence validation and roll back |
| G | The private temporary directory replaced before cleanup, plus a deterministic failure immediately after the exact new directory is pinned, for each publication path | Preserve and report the replacement tree; for the pinned creation failure, remove only the identity-authoritative empty directory, emit byte-exact refusal and leave no residue |
| H | Poisoned ambient `GIT_*` selectors | Ignore them entirely |
| I | Object-format, repository-path, and the full `show-ref`/`bundle list-heads` byte grammar matrix | The source captures bytes, proves fake-command firing and compares exact stderr with byte-empty stdout; no observed result is claimed before an authorized gate |
| J | Nested-source, linked-worktree, attached-HEAD and detached-HEAD runs | Independently verify/list-heads the published bundle and compare receipt/source evidence, including `.git` marker mode/link-count, regular-marker size/content, and null size for non-regular markers; no result is claimed before an authorized run |

* **Status:** IMPLEMENTED IN SOURCE. **Verification: PASSED in the 2026-08-03
  isolated self-test; no live operation was performed.**

### S2: Interrupted-work recovery

* **Driver:** `run_drill_s2`.
* **Flow:** writes one valid and one corrupt spooled observation into a
  throwaway ledger directory, instantiates the real
  `omniagentos.toolplane.observe.ObservationSink`, and asserts that
  `recover_pending()` finds the valid entry, `retry_pending()` drains it into the
  observations directory, and the corrupt entry lands in the dead-letter
  directory.
* **Status:** IMPLEMENTED IN SOURCE. **Verification: PASSED in the 2026-08-03
  isolated self-test; no live operation was performed.**

### S3: Idempotent replay

* **Driver:** `run_drill_s3`.
* **Flow:** pre-records an observation, leaves an identical spool entry as a
  crash would, and asserts that recovery neither duplicates nor overwrites the
  recorded observation.
* **Status:** IMPLEMENTED IN SOURCE. **Verification: PASSED in the 2026-08-03
  isolated self-test; no live operation was performed.**

---

## Operator-only drills (unimplemented)

These are contracts, not code. the operator's explicit approval is required before any
PID, network, provider or disk action. Each entry has the same fields in
`scripts/drills/scenarios.yaml`, which is the machine-readable source of truth.

### S4: Network partition
* **Inputs:** `interface_name`, `partition_seconds`
* **Fault:** drop packets on the named interface.
* **Oracle:** timeouts and degraded states are logged rather than silently retried.
* **Containment:** a temporary network namespace or an equivalent local scope only.
* **Recovery:** re-enable the interface.
* **Completion criteria:** normal operation resumes after the interface returns.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S5: Process crash
* **Inputs:** `pid_pattern`
* **Fault:** send SIGKILL to the matched process.
* **Oracle:** the supervisor notices and restarts within the stated threshold.
* **Containment:** a sandboxed process tree only.
* **Recovery:** the supervisor's own restart loop.
* **Completion criteria:** the target process is running and accepting requests.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S6: Corrupted ledger
* **Inputs:** `ledger_path`
* **Fault:** inject garbage bytes into a copy of the ledger.
* **Oracle:** the parser raises an explicit verification error and halts processing.
* **Containment:** a sandboxed copy of the ledger, never the live one.
* **Recovery:** fall back to the last known-good snapshot.
* **Completion criteria:** state matches the snapshot.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S7: Config drift
* **Inputs:** `config_key`, `bad_value`
* **Fault:** mutate the in-memory configuration away from the on-disk source of truth.
* **Oracle:** the drift is detected and the run warns or aborts.
* **Containment:** an isolated configuration registry.
* **Recovery:** reload the configuration from disk.
* **Completion criteria:** configuration matches its disk representation.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S8: Model degradation
* **Inputs:** `provider`, `latency_ms`
* **Fault:** a local proxy delays or fails provider requests.
* **Oracle:** the agent falls back or queues safely instead of hanging.
* **Containment:** a local proxy only; no live provider traffic.
* **Recovery:** remove the delay/error injection.
* **Completion criteria:** original routing is restored.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S9: Disk full
* **Inputs:** `mount_point`
* **Fault:** exhaust free space on a dedicated loopback mount or quota.
* **Oracle:** ENOSPC is handled and no partially written artifact is published.
* **Containment:** a loopback mount or quota, never a real volume.
* **Recovery:** remove the ballast file.
* **Completion criteria:** free space is restored and operations resume.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S10: Clock skew
* **Inputs:** `skew_seconds`
* **Fault:** skew the process clock with a process-local shim.
* **Oracle:** cryptographic and lease validations fail closed rather than silently pass.
* **Containment:** process-level only; the system clock is never changed.
* **Recovery:** remove the shim.
* **Completion criteria:** time-dependent validations pass again.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S11: Rate-limit exhaustion
* **Inputs:** `api_endpoint`
* **Fault:** a mock server returns 429 responses.
* **Oracle:** the client backs off exponentially instead of spinning.
* **Containment:** a mock server only; no live endpoint is called.
* **Recovery:** resume 200 responses.
* **Completion criteria:** the request succeeds after backoff.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED

### S12: Orphaned locks
* **Inputs:** `lock_file`
* **Fault:** create a lock file naming a dead PID.
* **Oracle:** the dead PID is detected and the lock is reaped.
* **Containment:** an isolated temporary directory.
* **Recovery:** not applicable; the reap is the recovery.
* **Completion criteria:** the lock is acquired and execution proceeds.
* **Status:** OPERATOR-ONLY / UNIMPLEMENTED
