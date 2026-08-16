# Team shared system — onboarding checklists

Companion to `HANDOFF/team-shared-system-2026-08-14.md`. Three checklists:
one per dev (Bob, Alice — ~15 min each, on EVERY machine you run AI/coding
sessions on), then the operator's hub-side rollout. Prerequisites you already have:
tailnet membership, a OmniAgentOS clone, GitHub access to the
`Globex` org, python3.

Everything here is additive to the 2026-08-11 setup (session collector +
compute pool) — keep that running; don't redo it.

## Bob (`emp_bob`) — and Alice (`emp_alice`), same steps with your id

### 1. Transcript uploader (~5 min) — logs every AI session centrally

```sh
# one-time
gh repo clone Globex/ai-transcripts ~/ai-transcripts
cp <your OmniAgentOS clone>/omniagentos/team/transcript_uploader.py ~/bin/

# test (prints what it would upload; sends nothing)
python3 ~/bin/transcript_uploader.py --employee emp_bob --print

# daily cron (crontab -e) — 18:10 local, after your workday
10 18 * * * python3 $HOME/bin/transcript_uploader.py --employee emp_bob >> $HOME/.transcript-uploader.log 2>&1
```

Notes: it harvests Claude Code / Codex / Kimi / Gemini / Grok session files
automatically; anything else you want captured, drop into
`~/.ai-transcripts-spool/`. Known credential SHAPES are best-effort redacted in the uploaded copy
(not a security guarantee: encoded or split secrets can slip through, so do
not paste live secrets into sessions you upload). Failures/parked loops are wanted — **especially** those.

### 2. Skills sync (~3 min) — new/updated team skills land on your machine

```sh
cp <your OmniAgentOS clone>/scripts/team/skills_sync.py ~/bin/

# test
python3 ~/bin/skills_sync.py --repo ~/OmniAgentOS --print

# cron — every 30 min
*/30 * * * * python3 $HOME/bin/skills_sync.py --repo $HOME/OmniAgentOS >> $HOME/.skills-sync.log 2>&1
```

It installs team skills into `~/.claude/skills/` and never touches skills it
didn't install. Non-Claude tooling: the same `SKILL.md` files are readable
docs — point your tool of choice at `~/OmniAgentOS/skills-lib/`.

### 3. Shared memory (knowledge recall) (~2 min)

Add to your shell profile (and to any env your agents launch with):

```sh
export OMNIAGENTOS_KNOWLEDGE=1
export OMNIAGENTOS_KNOWLEDGE_PG_DSN='postgresql://knowledge_agent:<ask the operator for the password>@203.0.113.20:5433/omniagentos_knowledge'
```

Recall then injects shared team knowledge into runner briefs on your machine,
and your machine's consolidated lessons flow back. Optional (for the vector
leg): install ollama + `ollama pull bge-m3`; without it, keyword recall still
works and ingest queues embeddings for later.

### 4. Secrets vault — NOT YET. Wait for the operator's go-ahead

When the operator says go (and only then):

```sh
brew install age sops   # linux: apt/pacman equivalents
age-keygen -o ~/.config/omni/age.key && chmod 600 ~/.config/omni/age.key
age-keygen -y ~/.config/omni/age.key   # send THIS (public key) to the operator; private key never moves
gh repo clone Globex/estate-vault ~/estate-vault   # after the operator enrolls you
~/estate-vault/bin/vault-env print | head              # verify decrypt works
```

## the operator — hub-side rollout (after the PR lands)

1. `gh repo clone Globex/ai-transcripts ~/ai-transcripts` — the fleetcap
   `dev-uploads` device (already in `configs/fleetcap/devices.yaml`) reads this
   clone; add a pull to the existing fleetcap cadence or a `*/30` cron:
   `git -C ~/ai-transcripts pull --rebase --quiet`.
2. Schedule the skills publisher (hub, e.g. hourly): serving-checkout
   `.venv/bin/python scripts/team/skills_publish.py` then `git push` when it
   committed. (launchd plist per house conventions; absolute paths.)
3. Own uploader + sync: install steps 1–2 above with `--employee emp_owner` on
   this Mac (and optionally the estate boxes fleetcap doesn't already cover).
4. **Curator flip (recommended):** add `OMNIAGENTOS_CURATOR_ENABLED=1` to the
   launch env → the scheduler's `selfimprove-curator` job starts distilling
   completed runs/sessions into the skill library.
5. Knowledge DSN cutover for hub daemons at a quiet moment:
   `OMNIAGENTOS_KNOWLEDGE_PG_DSN=$OMNIAGENTOS_KNOWLEDGE_PG_DSN_CENTRAL`
   (value already in `~/.config/omni/connections.env`); local PG stays as
   rollback. Dev machines start on central directly.
6. **Vault distribution (the hard stop):** do the rotation pass (vault README),
   then on your explicit go: collect each dev's age PUBLIC key,
   `~/.omniagentos/ops/estate-vault/bin/vault-issue bob age1…` (and alice), commit,
   push, add both as collaborators on `Globex/estate-vault`.
7. Back up `~/.config/omni/age.key` into your password manager now — losing it
   loses the vault.

## Verify it's working (any of us)

- Tracker: your sessions in the hourly #dev-agentic-alerts post (collector).
- Transcripts: `ai-transcripts` repo shows your daily commit; within a day,
  `fleet.sqlite` rows with your `device_owner`.
- Skills: `ls ~/.claude/skills/` gains team skills within 30 min of a publish.
- Memory: `psql "$OMNIAGENTOS_KNOWLEDGE_PG_DSN" -c 'select count(*) from facts'`
  grows over time; recall blocks appear in runner briefs.
