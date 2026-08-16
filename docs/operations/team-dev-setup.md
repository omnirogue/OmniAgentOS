# Team development setup

This is the setup and isolation contract for the operator, Alice, and Bob. Follow it on
every laptop and every server. The Team Work OS is the canonical queue.

## One-time setup: each person, each machine

### Git identity

Set identity in each clone before making any commit:

```sh
git config user.name "<your full name>"
git config user.email "<your mapped email>"
git config user.email
```

Worktrees of the same clone share `.git/config` — running `git config
user.email` inside one worktree silently changes the identity of every other
worktree of that clone. If worktrees of one clone must be shared between
people, use per-worktree config instead:

```sh
git config extensions.worktreeConfig true
git config --worktree user.email "<your mapped email>"
```

Expected output is your mapped email. The email is how GitHub activity inference
knows work is YOURS. A server configured with someone else’s email attributes
your commits to that person. Use the exact email values in
`configs/team_github_map.yaml` on the `p2-inference` branch:

- Bob: `<bob-email>` (the map currently contains `bob-dev@example.com`)
- Alice: `<alice-email>` (the map currently contains `alice@example.com`)

Do not use `git config --global` for shared-server work. A per-clone setting
(or, for shared worktrees, a `--worktree` setting as above) is the safe
default. Verify again immediately before the first commit.

### Slack

The bot sends a morning queue DM at about 06:55, assignment DMs when a card is
assigned, an hourly team pulse from 08:00 through 18:00, and overnight-work
suggestions at 16:00. Reply in the configured team report channel with the
queue commands defined in [the queue protocol](team-queue-protocol.md):

```text
claim <REF>
my queue
done <REF> [note]
progress <REF> <note>
blocked <REF> <reason>
```

`claim` is a compare-and-swap lock. `progress` requires a note; `blocked`
requires a reason. Slack updates are not a substitute for verification.

### Remote board

Use the public board URL `https://team-board.<your-zone>/team`. the operator provisions
and protects it as described in [Remote team board access](remote-board-access.md)
(lands in the same release); do not reproduce or improvise those tunnel and
Access steps here.

## Daily loop

1. Read the Slack DM and your board queue.
2. Claim the card with `claim <REF>` in Slack or through the board before work.
3. Work in your own branch. Put the bare `<REF>` as a delimited UPPERCASE
   segment in the branch name (see the `<type>/<REF>-<slug>` shape below);
   put `refs <REF>` in every relevant commit message and the PR body.
4. Open the PR with `refs <REF>` in its body. A merge auto-attaches code evidence
   and advances the card.
5. Mark the card done only when its acceptance criteria are met. Verification
   is separate: mechanical evidence may be verified by anyone; without it,
   someone other than the owner verifies (except the operator, who may verify their own).

GitHub inference is flag-gated behind `OMNIAGENTOS_TEAM_INFERENCE` and ships
OFF, with a dry-run soak first; the operator enables it after that soak. Until then,
`refs` and author-email hygiene still matter, because history is read once
inference is turned on — correct values let the board maintain itself once
live; incorrect values leave work unattributed and it does not count.

## Shared-server rules: anti-crossing

**Never share a checkout.** Each person works in their own clone or in a git
worktree under their own directory, for example
`$HOME/work-NAME/OmniAgentOS`. One working tree has exactly one writer:
one human or one agent.

On a shared server, set identity per directory:

```sh
git -C "$HOME/work-NAME/OmniAgentOS" config user.email "<your mapped email>"
git -C "$HOME/work-NAME/OmniAgentOS" config user.email
```

Separate OS users are better when practical. Per-clone identity still prevents
two people’s clones from attributing commits to the same person under one OS
user.

Agents inherit the identity of the clone they run in. If the operator launches an AI
agent in Bob’s clone, that agent commits AS Bob. Check the directory and
`git config user.email` before launching an agent.

Claim before you—or your agent—touch anything. The board’s claim CAS is the
lock. If two agents race for one card, exactly one wins; the loser must pick
another card and must not work on the card anyway.

Use `<type>/<REF>-<slug>` for branches, such as `docs/U3-team-setup`. Never
work directly on `main` and never reuse another person’s branch.

Keep job and agent output attributable. If a server process writes evidence,
transcripts, or commits, run it from the person’s clone and carry that person’s
identity: the mapped git email, and the employee ID wherever a tool asks for
one. Never use a shared or service identity for human work.

## Troubleshooting

**My work is not showing.** Run `git config user.email` and compare it with
`configs/team_github_map.yaml` on `p2-inference`. Then check the branch name,
commit message, and PR body for the exact `refs <REF>` token. Unmapped emails or
missing refs are intentionally left unattributed.

**Someone else’s card moved.** This is a claim race. CAS makes exactly one
claim win; choose another card if you lost.

**Slack says “not owned.”** The card is assigned to someone else. Pick an open
card from `my queue` or the board pool; do not work around ownership.

For the complete claim, evidence, blocked, done, and verify rules, use
[the Team queue protocol](team-queue-protocol.md).

## Hourly session reporting (required — this is how the team sees your work)

Every hour, each machine you run AI sessions on reports what those sessions
are working on. The tracker posts a combined team report to Slack hourly; a
machine that never reports shows up as "no session report received" next to
your name.

**One-time setup, per machine (laptop AND any server you run agents on):**

Before anything else, read the privacy contract:
[team-telemetry-privacy.md](team-telemetry-privacy.md). Short version: only
terminal/AI session activity is collected (never screen, keystrokes, browser,
or personal files), everything is read-only on your machine, and you carry a
kill switch (`telemetry_ctl.py off`) you can flip at any time without asking.

1. Get the collector and your kill switch — self-contained Python files, no
   dependencies:

   ```sh
   mkdir -p ~/bin
   # from a clone of this repo:
   cp omniagentos/team/session_collector.py ~/bin/session_collector.py
   cp omniagentos/team/telemetry_ctl.py ~/bin/telemetry_ctl.py
   ```

2. Test it (prints JSON, sends nothing):

   ```sh
   python3 ~/bin/session_collector.py --employee emp_alice --print   # or emp_bob
   ```

   You should see your recent Claude Code / Codex sessions with a one-line
   description each (the session's first user message). Nothing sensitive
   leaves the machine beyond those lines — check the output.

3. Schedule it hourly with the transport the operator has given you:

   - **Tunnel API (preferred, once the remote board is live):**
     `--post https://team-board.<zone>/api/team/sessions/report`
   - **Interim Slack webhook:** `--slack-webhook <url the operator DMs you>`

   Linux server (cron):

   ```sh
   crontab -e
   # add ONE line (pick your transport):
   5 * * * * python3 $HOME/bin/session_collector.py --employee emp_bob --slack-webhook <URL> >> $HOME/.session-collector.log 2>&1
   ```

   macOS (launchd) — save as
   `~/Library/LaunchAgents/com.team.session-collector.plist` with
   `StartCalendarInterval` Minute 5 and the same command, then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.team.session-collector.plist`.

4. Shared server rule: run the collector under YOUR account with YOUR
   `--employee` id. Two people on one server = two crontabs, two collectors.
   Never report another person's sessions.

That is all. Descriptions come from your sessions' first user message, so
starting a session with a clear one-line goal ("fix webhook retries in X")
makes the team report read well — and matches the refs discipline above.

## Claude balance reporting (added 2026-08-12 — re-copy the collector)

The collector now also reports your machine's **Claude account balances**
(`claude_usage` in the drop-file): every `~/.claude` / `~/.claude-account-*`
profile's cached weekly usage, deduplicated by Anthropic account, with the
best remaining percentage. This is what lets dispatch route work to machines
with balance and what fires the fleet alert when a machine drops **below 10%
remaining with no fallback account** (`omniagentos/team/balance_alerts.py`).

To pick it up, re-copy the single file exactly as in step 1 above
(`cp omniagentos/team/session_collector.py ~/bin/session_collector.py` from a
fresh pull, or ask the operator to DM the file). Nothing else changes — same schedule,
same transport, still stdlib-only. It reads only the local usage cache the
`claude` CLI already writes; it never sends credentials.

A machine still running the old collector shows as "balance unknown" in the
hourly tracker and is exempt from balance alerts until updated — unknown is
rendered as unknown, never as healthy.
