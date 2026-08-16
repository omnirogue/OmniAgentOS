# Daily team alerts — what posts when

The Team Work OS posts three kinds of scheduled Slack messages plus event
nudges. All channel alerts go to the team channel (`C0000EXAMPLE`, overridable
via `OMNI_TEAM_PULSE_CHANNEL`); DMs go to the mapped person from
`configs/team_slack_map.yaml`. Every string crosses the `_safe_title` egress
guard (links, tokens, and `<!channel>`-style mentions never survive).

Renderers live in `omniagentos/team/notify.py` (gather + text) and
`omniagentos/team/slack_blocks.py` (Block Kit); spec:
`devtasks/multi-company-workos-0813/PLAN-v3-task-commands.md` ("Alerts").

## Cadence

| What | When | Where | Entry point |
|---|---|---|---|
| **Morning brief** | First `--pulse` run of the local day — 08:00 under the current launchd schedule (state: `var/…/team-daybrief-state.json`) — or on demand | team channel | `python -m omniagentos.team.notify --daybrief` |
| **Hourly pulse** | Every other `--pulse` run: launchd `com.omniagentos.team-pulse` fires 08:00–18:00 local; the 16:00 slot is the `--overnight` edition (`…team-pulse-overnight`) | team channel | `… --pulse` (`--overnight` appends suggestions) |
| **Morning queue DMs** | Daily (launchd `com.omniagentos.team-notify`) | DM per mapped person | `… --morning` (unchanged) |
| **07:00 production report** | Daily | team channel | `omniagentos/team/report.py` (unchanged) |
| **Event nudges** (assignments, inference cards) | Every 5 min | DM / channel | `… --watch-once` (unchanged) |

Flags: `--dry-run` prints the JSON payload instead of posting. `--test`
(valid with `--daybrief`/`--pulse`) prefixes the header with `🧪 TEST —` so a
live demo post is unambiguous — and a `--test` or `--dry-run` brief never
consumes the day's slot, so the real first-pulse brief still goes out.

## Morning brief

All five companies in fixed order — Globex, AcmeUni, Hooli, Initech,
OmniAgentOS — each with its shared-queue count and top cards (priority
order, max 5, `+N more`), then per-person load (max 5 Work cards each). Empty
companies stay visible as `— empty` so absence is stated, never implied.
Priorities render 🔥 urgent / ⬆ high / • normal / ⬇ low; deadlines render
⏰, overdue ones 🔴⏰. The side-bar goes amber when anything is overdue.

Each person's section splits the two streams, **Tasks on top** (v4, the operator's
ruling 2026-08-13): `📌 Tasks (N)` first — ad-hoc zero-point items, each with
its deadline front-and-center, omitted entirely at zero — then `🔧 Work x/5`
(ongoing Work = owned open + claimed + in_progress + blocked cards; `⚠ below
floor` appears while x < 5 — supply visibility, never a block), then the Work
cards themselves.

Rendered example (real renderer output):

```
☀️ Work queue — 2026-08-13
*Globex* — 2 queued
🔥 CF7 Fix Stripe rebill retries — urgent 🔴⏰2026-08-12
• CF9 Comment moderation queue UI — normal ⏰2026-08-15
*AcmeUni* — 1 queued
⬆ AP3 Webinar replay page copy — high
*Hooli* — empty
*Initech* — empty
*OmniAgentOS* — 1 queued
• GR12 Gate healthcheck dashboards — normal ⏰2026-08-14
👤 the operator — in progress 0 · queued 0
🔧 Work 0/5 ⚠ below floor
👤 Alice — in progress 1 · queued 1
🔧 Work 2/5 ⚠ below floor
▶️ GR8 Load-balancer alert wiring
▫️ GR9 Review PR 240
👤 Bob — in progress 1 · queued 1
📌 Tasks (1)
▫️ T4 Renew the SSL cert 🔴⏰2026-08-12
🔧 Work 2/5 ⚠ below floor
▶️ GR5 Twin-pool saturation fix ⏰2026-08-13
▫️ GR6 Harvest emptiness predicate
📌 claim: /task claim <REF> · assign: /task assign @name <REF> · help: /task help
```

"Queued" for a company means the shared pool (unowned, pool-eligible cards);
"in progress" for a person is their claimed/in-progress Work, "queued" their
owned open Work — ad-hoc Tasks never move those counts.

## Hourly pulse

One compact line per person — same Tasks-then-Work order, compressed:
`👤 Bob — 📌 2 tasks · 🔧 Work 3/5 ⚠` (the 📌 segment is omitted at zero
tasks), keeping blocked cards and 🔥 urgent markers on the Work stream. When
any task is due today or overdue, the task refs/deadlines ride a second line.
Then the Friday-pace ⚠/✓ lines (unchanged — they read scoring, which ignores
Tasks), one per-company depth line, and the pool depth with its low warning.

Rendered example (real renderer output):

```
*Team pulse*
👤 the operator — 🔧 Work 0/5 ⚠
👤 Alice — 🔧 Work 2/5 ⚠
👤 Bob — 📌 1 task · 🔧 Work 2/5 ⚠
📌 T4 🔴⏰2026-08-12
• ⚠ emp_bob 4/15 pts, Friday pace short
• ✓ emp_alice 16/15 pts, on pace
🏢 Globex 2 · AcmeUni 1 · Hooli 0 · Initech 0 · OmniAgentOS 1
Pool: 4 ⚠ low (<10)
📌 claim: /task claim <REF> · assign: /task assign @name <REF> · help: /task help
```

## Work vs Tasks (v4, 2026-08-13)

**Work** is the shared queue: delegated or claimed, points-bearing
(S=1 · M=3 · L=8, verified only), five ongoing expected per person. **Tasks**
are minor ad-hoc items (`/task assign @name <free title>`, `@name task <…>`),
stamped `source='task-adhoc'` at creation: **zero points**, excluded from
scoring and pace at the card-gathering stage, rendered above Work everywhere a
person's load appears, deadline-first. The company queue sections are all
Work by construction (Tasks are owned from birth and never pool).

## The footer

Every channel alert ends with the same three CTAs, because the alert's whole
purpose is that someone acts on it in the next ten seconds:

- `claim: /task claim <REF>` — take an unowned queue card for yourself
  (anyone on the roster; the REF is the code shown on every card line).
- `assign: /task assign @name <REF>` — delegate a queue card (the operator/Alice) or
  hand someone a new ad-hoc task (anyone, with a free-text title).
- `help: /task help` — the one-screen command card.

Both messages ride Block Kit inside one colored attachment (green = calm,
amber = something needs attention: an overdue card, a low pool, a pace
shortfall). The plain-text fallback carries the same content, so
notifications and no-blocks surfaces never say less than the styled view.
