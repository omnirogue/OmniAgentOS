# Repo governance — merge protection with Alice review

Standing ruling (the operator, 2026-08-13): merge protection applies to the **Globex
org only**, with all PRs reviewed by Alice (GitHub login `alice-dev`). An initial
estate-wide apply ran the same day and was immediately narrowed: all 16
`example-org` personal repos were fully rolled back (rulesets deleted, CODEOWNERS
removed, pending invites cancelled — receipt:
`~/.omniagentos/ops/repo-protection-rollback-2026-08-13.json`). The rollout tool now
scopes to the Globex org by default (`PROTECTION_SCOPE_OWNERS`). This doc
records the scheme, how to re-run it, and the known caveats.

## The scheme (per repo, idempotent)

Mirrors the ruleset the operator ratified on `Globex/initech`:

1. **Access** — `alice-dev` is a collaborator with at least `push`; if not, a
   collaborator invite (permission `push`) is sent.
2. **CODEOWNERS** — `.github/CODEOWNERS` on the default branch contains the
   default rule `* @alice-dev`. Created if absent (commit message
   `chore(governance): route reviews to Alice`); if a CODEOWNERS exists without
   a `*` rule naming Alice, the rule is appended. Existing lines are never
   removed.
3. **Ruleset** — `Require Alice review on <default-branch>`, target
   `~DEFAULT_BRANCH`, enforcement `active`, rules:
   - `pull_request`: 1 required approving review, code-owner review required,
     dismiss stale reviews on push, thread resolution not required, last-push
     approval not required;
   - `non_fast_forward`;
   - `deletion`;
   - bypass: repository **admin role** (`actor_id 5, RepositoryRole, always`)
     so the operator / the landing fleet are not broken.

Net effect: direct pushes to the default branch are blocked for non-admins;
changes flow through PRs that CODEOWNERS routes to Alice.

**Authorization boundary**: the rollout only sends alice-dev invites, commits
CODEOWNERS files, and creates/updates its own named ruleset. It never creates/
deletes/transfers/archives repos, never removes protections, collaborators, or
foreign rulesets.

## Registry and tooling

- Registry: `configs/company_repos.yaml` (company slug → repos + local paths;
  entries with `confirmed: false` are naming-based company guesses, not
  the operator-confirmed attribution).
- Tool: `scripts/ops/repo_protection_rollout.py` (gh CLI subprocess, PyYAML
  only). Runs under the repo venv:

```sh
# read-only plan (default)
.venv/bin/python scripts/ops/repo_protection_rollout.py

# mutate (idempotent — re-running on a compliant estate is all no-ops)
.venv/bin/python scripts/ops/repo_protection_rollout.py --apply

# single repo
.venv/bin/python scripts/ops/repo_protection_rollout.py --only example-org/omniagentos --apply
```

- Receipts (before-state, actions, after-state per repo):
  `/Users/youruser/Work/Ops/repo-protection-receipts-<YYYY-MM-DD>.json`
  (dry runs write `...-dryrun.json` and never clobber apply receipts).

## Hard exclusions (in the script, not the registry)

- `Globex/OmniAgentOS` — already compliant; it is the landing-train
  serving repo, the rollout never touches it (not even reads).
- Scoped repos (`ACTION_OVERRIDES`): `OmniAgentOS-sandbox` invite-only,
  `Globex/initech` ruleset-only, `example-org/ThreeLoops`
  CODEOWNERS+ruleset — per the 2026-08-13 recon of what each already had.

## Final state 2026-08-13 (after scope narrowing)

Protected under the scheme (Globex org):

| repo | protection | Alice access |
|---|---|---|
| `Globex/OmniAgentOS` | pre-existing classic (1 review + CODEOWNERS + 4 checks) — untouched | admin |
| `Globex/OmniAgentOS-sandbox` | pre-existing classic + CODEOWNERS `* @alice-dev` | **invite pending** |
| `Globex/initech` | pre-existing `Protect critical paths` + `Require Alice approval on main` (the rollout's duplicate ruleset was deleted same-day) | admin |
| `Globex/ai-transcripts` | new ruleset 00000000 + CODEOWNERS | **invite pending** |
| `Globex/content-studio-poster` | new ruleset 00000000 + CODEOWNERS | **invite pending** |
| `Globex/initech-cs-sim-handoff` | new ruleset 00000000 + CODEOWNERS | **invite pending** |
| `Globex/OmniAgentOS-Improvement-Plans` | new ruleset 00000000 + CODEOWNERS | **invite pending** |
| `Globex/initech-cs-sim-2026-08-01` | unprotectable (empty repo) | — |

All 16 `example-org` personal repos from the initial apply were rolled back the
same day (rulesets deleted, CODEOWNERS commits reverted, invites cancelled) —
receipt `~/.omniagentos/ops/repo-protection-rollback-2026-08-13.json`. Hooli has zero
GitHub repos; nothing to protect until one exists.

## Known caveats

- **Pending invites**: until Alice accepts, CODEOWNERS `@alice-dev` does not
  bind — PRs already require **1 approving review** (protection is live), but
  review is not yet routed specifically to Alice on those repos. No further
  mutation needed after acceptance.
- **Empty repos are unprotectable** (no default branch). If
  `initech-cs-sim-2026-08-01` gets its first push, re-run the rollout.
- **Admin-role bypass is deliberate**: repo admins (the operator, the fleet where admin)
  can still push/land directly. The protection binds non-admin collaborators
  and makes review the default path, per the ratified initech scheme.
