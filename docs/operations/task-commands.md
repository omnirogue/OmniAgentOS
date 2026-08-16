# /task — the shared work queue (cheat sheet)

Type these as ordinary messages in **#dev-agentic-alerts**. The bot answers in a thread.
One queue for all five companies; the operator fills it, you drain it.

## The commands

| You type | What happens | Who can |
|---|---|---|
| `/task add Fix checkout #initech !high tomorrow` | Puts a card in the shared queue | **the operator only** |
| `/task assign @bob Q1` | Hands queue card Q1 to Bob (he gets a DM) | **the operator + Alice** |
| `/task assign @alice review the pricing page today` | New ad-hoc task, straight to Alice (DM) | anyone → a teammate, never yourself |
| `/task claim Q1` | You take Q1 from the queue | anyone |
| `/task done Q1 shipped it` | Marks it done; whoever assigned it gets a DM | the card's owner only |
| `/task note Q1 waiting on staging` | Comment on the card; the assigner is DMed | anyone |
| `/task reassign Q1 @alice` | Moves the card; new owner gets a DM | the operator/Alice, or the current owner |
| `/task queue` (or `/task queue #acmeuni`) | Shows the shared queue | anyone |
| `/task mine` | Your own cards | anyone |
| `/task help` | This card, condensed | anyone |

## Deadlines — just say when (always the LAST words of the message)

`immediately` · `in 30 minutes` · `in 2 hours` · `in 3 days` ·
`today` (= 18:00) · `tomorrow` (= 10:00) · `by friday` (= Friday 10:00)

The deadline shows up with ⏰ in the DM and in the queue alerts; overdue turns 🔴.

## Flags (anywhere in the title)

- Priority: `!top` 🔥 urgent · `!high` ⬆ · `!low` ⬇ (nothing = normal •)
- Company: `#globex` `#acmeuni` `#hooli` `#initech` `#grok`
- Acceptance criteria on `/task add`: append `| ac: what "done" means`
  (without it, the title itself is the acceptance bar)

## Work vs Tasks (the operator's ruling, 2026-08-13)

Two separate streams, wherever your load renders:

- **Work** = the shared queue. Delegated or claimed, earns points
  (S=1 · M=3 · L=8, verified only), and everyone keeps **5 ongoing at all
  times** — every load view shows `🔧 Work x/5`, with `⚠ below floor` when
  you have room (visibility, never a block).
- **Tasks** = minor ad-hoc items someone hands you on top of Work —
  `/task assign @name <free title>` or `@name task <title>`. They earn
  **zero points**, render FIRST (`📌 Tasks (N)`), and their deadlines stay
  front-and-center (⏰, 🔴 once overdue).

A queue card handed over by REF (`/task assign @name Q1`) stays Work; only
free-title assigns and the bare `task` verb create Tasks.

## Good to know

- Old short commands still work: `done U3`, `claim U3`, `blocked U3 reason`,
  `progress U3 note`, `!top U3`, `my queue`, `report`.
- Nobody is auto-assigned anything, ever. Work reaches you only when you
  `claim` it or the operator/Alice hand it to you — each hand-off is exactly one DM.
- A typo'd `/task ...` gets a reply pointing here; ordinary chat is ignored.
- Bare `done U3` keeps the operator's old operator override; `/task done` is
  owner-only for everyone, the operator included — use the bare form for overrides.
- If your `| ac: ...` text ENDS in a deadline word (`... ship it today`), the
  last words become the deadline — put the deadline before the `| ac:` part
  when in doubt.
