# backlog-executor — nightly unattended backlog work (00:30)

The highest-care nightly agent: it does REAL repo work unattended, on
main, behind hard rails. Kimi selects from the already-reviewed actionable
plan; execution runs through the existing swarm (`POST /api/swarm`).

## Night shape

1. **Collect** — open `⬜` rows from `devtasks/SWARM-EXECUTION-TODO.md`,
   "Deferred"/"Proposed …" bullets from the latest 3 curator reports
   (`~/.claude/curator-reports/`), and the swarm playbook's
   "Improvement opportunities" tail (`vault/swarm/playbook.md`).
2. **Select** — Kimi picks ≤3 items that are ALL of: small (≤2h
   single-agent), risk_class none, self-verifiable. Malformed → one Kimi
   retry → skip the night.
   Prompt + runtime policy: `prompt.md` (editable; policy yaml parsed at
   runtime, malformed → code defaults).
3. **Deny-list in code** — post-selection regex over pick briefs
   (policy.yaml|approvals|migration|settings.json|secrets|payment|delete)
   drops the pick regardless of what the prompt said.
4. **Execute** — sequentially, each pick in a FRESH local clone under
   `var/backlog/<date>-<id>/repo` (the live checkout is never a swarm
   working dir); poll the run to terminal.
5. **Merge gate** — full pytest suite in the clone (main repo's venv,
   `PYTHONPATH=clone`, known env flakes deselected). Green work is fetched
   into the live repo as `backlog/<date>-<id>`.
   - **Clean tier** (risk_class none + single attempt + no test files +
     ≤6 files): merged `--no-ff` immediately; the live suite re-runs; red →
     `git revert -m 1` + alert + stop the night.
   - **Held tier** (green but bigger): `HOLD:` note commit, then the FINAL
     MERGE PASS re-verifies the candidate merge (live main merged into the
     clone, full suite there) and merges with a `[held-tier]` message tag —
     all before 05:00 (pass skipped past 04:45; `merge_deadline_hour` in
     prompt.md policy). Only RED branches remain unmerged.
6. **Bookkeeping** — improvement-log line per item, TODO row `✅` flips for
   merged items, digest `var/backlog/digest-<date>.md`
   (merged-clean / merged-held-tier / failed-unmerged, ≤30 lines) + one
   "info" bell. Failures alert (`kind="alert"`). Rollback path is the
   hourly GitHub backup — this job NEVER pushes.

## Install / arm

```sh
sh scripts/backlog-executor/install.sh            # loads at 00:30, DRY-RUN
OMNIAGENTOS_BACKLOG_DRY_RUN=0 sh scripts/backlog-executor/install.sh   # arm
```

Dry-run does selection + digest + bell, dispatches NOTHING. Logs:
`var/log/backlog-executor.log`.

## Env knobs

- `OMNIAGENTOS_BACKLOG_DRY_RUN` (plist ships =1)
- `OMNIAGENTOS_BACKLOG_API` (default `http://127.0.0.1:8485/api`)
- `OMNIAGENTOS_BACKLOG_POLL_SECONDS` / `ITEM_TIMEOUT_MIN` / `SUITE_TIMEOUT_MIN`
- `OMNIAGENTOS_BACKLOG_DESELECT` — extra pytest node ids to deselect in the
  gate suite (comma-separated), on top of the known env flake list in
  `executor.py`.

## Rails inventory (in code, not prompt-editable)

≤3 items/night hard cap; sequential execution; deny-list post-selection;
fresh-clone working dirs only; suite-green merge gate; two-tier merge with
held-tier re-verify on the exact candidate merge; auto-revert on post-merge
red + stop night; 04:45/05:00 merge-pass deadline; never `git push`; live
checkout preflight (clean + on main) before every merge; alerts on every
failure path; single-instance flock.
