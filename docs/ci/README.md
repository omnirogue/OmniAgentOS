# CI Part B: PR-safe checks

`.github/workflows/ci.yml` runs these checks on fresh GitHub-hosted Ubuntu runners:

- `lint` — Python Ruff checks.
- `type` — Python MyPy checks.
- `test` — the repository's PR-safe Python test lane (`make test-pr`). **Blocking at the
  run level as of 2026-08-14**; add it to branch-protection required checks to also block
  merge — see "`test` job status" below.
- `dashboard` — dashboard install, lint, type-check (`npm run typecheck`), production
  build, and Vitest suite.

The workflow uses no repository secrets, provider credentials, production hosts, SSH,
Tailscale, or live infrastructure. It deliberately contains no `gate/darwin-surface`
equivalent. A workflow triggered by an external pull request must never run PR-authored
code on the live production Mac or any other production infrastructure.

## `test` job status: blocking (2026-08-14)

The prior review found `make test-pr` failing on a clean checkout of `main` — about 30
pre-existing failures unrelated to any one PR — so the `test` job ran with
`continue-on-error: true`: visible on every PR/push, but not blocking, so those failures
did not block PRs nobody caused.

That precondition is now met. The ~30 reds were driven to zero on 2026-08-14: host-shape
and CLI-availability premises (`tests/intake/**`, fastlane, proxy/filesearch), the
migration-pointer and installer-guard docs (#445), module-identity collection collisions
and swarm/workqueue premise pins (#454), the four security/hygiene ratchet clusters
(#457), the archdocs trial-merge stamp gate (#464), the transcript-uploader containment
migration and dev-upload path exclusions (#479), and the verdict-dispatch pair — which
turned out to be a real production liveness bug (BSD `nohup` never detaches in a tty-less
caller, so the gate-loop daemon silently dropped verifiers), fixed in #484, not a flake.

`continue-on-error: true` is therefore removed from the `test` job: a red PR test lane
now fails the run instead of accumulating silently — closing the exact class that let
those ~30 reds hide. The job keeps its computed in-step budget and 600s pre-step reserve
(`tests/scripts/test_ci_workflow_budget.py`): an in-step budget overrun now exits nonzero
and FAILS the run (accurate signal — a lane that cannot finish in budget is a real
problem), while the job-level `timeout-minutes: 60` backstop yields `cancelled` only for
a pre-step setup stall, before that timer starts.

**Residual, now higher-stakes:** `make test-pr`'s impacted-analysis can still escalate a
dashboard/docs/workflow-only PR to the full tree. While the job was informational that
was only wasted minutes; now such an escalation that goes red would fail the run. The
tree is green as of this change (this very PR is workflow+docs-only and its escalated,
unshielded `test` job passed), so the risk is a future regression, not a present block —
but scoping the escalation so non-Python PRs never run the full tree is a tracked
follow-up, not resolved here.

**Remaining step (repo admin):** add `test` to the branch-protection required checks so
the red also blocks MERGE, not only the run — see the list below.

## Enable branch protection after the Team-plan unblock

This repository currently belongs to the `Globex` organization on GitHub's free
plan. For private repositories, protected branches with required status checks need
GitHub Team. Per the handoff, the operator must first move the organization to Team (currently
about $4/user/month for three seats). This is an administrative GitHub plan decision,
not a repository change.

After that unblock, the operator or another repository administrator should:

1. Open `Globex/OmniAgentOS` on GitHub.
2. Go to **Settings** -> **Branches** -> **Branch protection rules**.
3. Create a rule for `main`, or edit the existing rule for `main`.
4. Enable **Require status checks to pass before merging**.
5. Search for and select these CI job names as required checks **now** (all verified
   green as of this change): `lint`, `type`, `dashboard`, and `test`. GitHub may display
   them with the workflow prefix, such as `CI / lint`; select the check whose job-name
   suffix matches each name.
6. `test` is now blocking at the run level (`continue-on-error: true` was removed
   2026-08-14 — see "`test` job status" above); adding it here is what also makes a red
   `test` block MERGE.
7. Save the branch-protection rule. Optionally enable **Require branches to be up to
   date before merging** if the team wants the checks re-run after the PR is rebased or
   merged with `main`.

The branch-protection portion is documentation only: workflow YAML defines the CI that
runs, while GitHub's plan-gated branch-protection UI/API is separate and cannot be
configured by a change in this worktree. No GitHub API call is part of this change.
