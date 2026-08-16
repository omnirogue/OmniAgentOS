#!/usr/bin/env bash
# THE CANONICAL VERDICT-LINE GRAMMAR — one definition, every consumer.
#
# WHY THIS IS A FILE AND NOT A LINE IN FOUR SCRIPTS
# -------------------------------------------------
# The tolerant pattern `^\s*\**\s*#*\s*VERDICT` matched a markdown HEADING, so a
# verdict file's TITLE was extracted as its verdict. That is not hypothetical:
# `vault/swarm/transcript-compilation-2026-07-29.md:232` records "verdict-line
# extraction grabbed decorative headers for 2 lanes". The pattern was tightened in
# `merge-if-only-known-blocker.sh` and the fix never reached its three siblings —
# which is the same incomplete-propagation defect this program keeps paying for.
# Copying a corrected regex into four files just re-arms it for the next edit, so
# the grammar lives here and the carriers source it.
#
# CONSUMERS (keep this list current — a new one belongs here, not in a fifth copy):
#   scripts/dispatch-verifier.sh          validity + display
#   scripts/review-pump.sh                validity + display
#   scripts/integrate.sh                  partition (decides APPROVE/REJECT/FAILED)
#   (scripts/merge-if-only-known-blocker.sh gate decision + display -- DELETED 2026-08-13 by
#   operator ruling: receipts exist, the signed-receipt producer landed 2026-07-30, and the
#   bypass had never once executed.)
#   scripts/review-integration.sh         aggregate validity + display
#   scripts/land-approved-lanes.py        landing decision (shells out to verdict_decision)
#
# WHAT THIS CHANGED ON REAL DATA — recorded because a flip must never be silent.
# Replacing the four inline patterns with this grammar reclassifies 5 of the 63 artifacts
# in var/swarm/verdicts (measured by executing both, old and new, over the live corpus):
#
#   integration_batch2.md          REJECT -> APPROVE   ** a refusal becoming an approval **
#       Legitimate, and checked line by line: it is a RE-REVIEW whose title says
#       "supersedes the 2026-07-29 REJECT", and its decision line reads
#       `VERDICT: **APPROVE** — the single blocking defect behind my prior REJECT is
#       closed`. The old code took the FIRST matching line and searched it for the
#       substring REJECT, so the reviewer's phrase "my prior REJECT" refused their own
#       approval. Read as a whole file with whole-token values, nothing in it refuses.
#   fix_sweep-audit.md             FAILED -> APPROVE   } all three carry a decorative
#   fix_sweep-banking.md           FAILED -> APPROVE   } title (`# Verdict — branch ...`)
#   fix_sweep-selfimprove.md       FAILED -> APPROVE   } that the old first-match read as
#       the verdict; matching no value, it fell through to FAILED. Their real lines are
#       `VERDICT: APPROVE-WITH-NOTES`.
#   fix_r2-review-queue.md, integration_verified-review.md, main.md,
#   mission_p1_fe-activity.md      FAILED -> REJECT
#       Refusals that the same title-read had been masking as failures.
#
# Everything else that moves goes FAILED -> NONE (files with no verdict line at all), and
# integrate.sh maps NONE to the same FAILED bucket, so those are no change in behaviour.
# Outside the gate's own corpus one more artifact moves — var/jira-goals/verdicts/
# jg-hyg2-sol-r3.md, FAILED -> APPROVE, same decorative-title cause — but no carrier in
# scripts/ reads that directory.
#
# THE GRAMMAR
# -----------
# A verdict line is `VERDICT` at the START of a line, optionally wrapped in markdown
# emphasis or preceded by heading markers, followed by a separator (`:` `=` `-`) and
# a RECOGNISED VALUE. Both halves matter:
#
#   accepted   VERDICT: APPROVE        VERDICT = APPROVE      VERDICT - REJECT
#              ## VERDICT: REJECT      **VERDICT: APPROVE     **VERDICT**: APPROVE
#              Verdict: approve-with-notes                    VERDICT: FAILED (...)
#
#   refused    # Verdict — lane/foo    a TITLE: no separator, no recognised value
#              # Verdict: Approve-flow lane
#                                      a TITLE that HAS a separator and starts with an
#                                      approval word. Prefix matching read this as an
#                                      approval and merged a lane whose body said
#                                      `VERDICT: REJECT` (MGI-001, reproduced against the
#                                      real gate). The value must be a whole TOKEN.
#              ## Verdict rationale    a section heading
#              VERDICT: MAYBE          unrecognised value — unparseable is not a pass
#              VERDICT:                empty value
#                VERDICT: APPROVE      INDENTED: a quoted or fenced line is not this
#                                      file's verdict
#              > VERDICT: APPROVE      a blockquote is something being quoted
#
# Allowing `#` is deliberate and evidence-based — 12 of the 95 verdict files in the
# live corpus carry their real decision as `## VERDICT: <value>`, and refusing those
# would turn genuine reviews into silent refusals. The heading marker was never the
# discriminator. Neither, on its own, is the separator: the discriminator is a
# RECOGNISED VALUE MATCHED AS A WHOLE TOKEN, which is what `Approve-flow` fails.
#
# Values are WHOLE TOKENS, not prefixes. `APPROVE-WITH-NOTES` and `APPROVED` are listed
# explicitly rather than being reached by prefix, because prefix matching is precisely
# how `Approve-flow lane` became an approval. This now agrees with
# `omniagentos/integration/verdicts.py`, which matches exact tokens for the same reason.
#
# Prefer the FUNCTIONS to the raw patterns — they encode the refusal precedence that a
# lone `grep -q` cannot:
#   verdict_decision <file>  APPROVE|REJECT|FAILED|NONE, reading the WHOLE file
#   verdict_line <file>      the line that decision came from, for operator output
# Use the raw patterns only for narrower questions, with `grep -E -i`.
#
# shellcheck shell=bash

# Recognised decision values, as WHOLE TOKENS. Refusals are listed so that a REFUSAL
# parses as a verdict — an unparseable refusal would be recorded as "no verdict", a
# weaker and less honest state than the refusal it really is.
#
# The token lists are drawn from the live corpus: REJECT, APPROVE-WITH-NOTES, APPROVE,
# FAILED, APPROVED are the only values 94 real verdict files use.
#
# THE OTHER SOURCE OF TRUTH IS THE REVIEWER'S OWN CONTRACT. `~/.claude-account-4/agents/`
# `opus-critic.md` and `codex-critic.md` both instruct the reviewer to answer with one of
#   approve | approve-with-changes | rework | unreproducible-findings | inconclusive
# and three of those five parsed as NO VERDICT AT ALL. A reviewer who followed the
# documented format produced an artifact this grammar reported as "no VERDICT line found"
# and that NO pump owned — refused work rendered as un-reviewed work
# (`scripts/fleet-ledger.py:231`). Fail-closed, so never a merge risk; but the refusal was
# silently dropped, which is the same favourable-absence shape by a different route.
# Observed live: the closing review of this very lane used that vocabulary.
#
# The mapping is by CONSEQUENCE, not by wording:
#   approve-with-changes     an approval carrying non-blocking follow-ups -> APPROVE,
#                            exactly as APPROVE-WITH-NOTES already is.
#   rework                   a refusal that names the next action -> REJECT, so the
#                            rework pump owns it (scripts/rework-pump.sh).
#   unreproducible-findings  the review established nothing -> FAILED. Not an approval:
#                            "I could not reproduce" is not "I verified".
#   inconclusive             already FAILED below.
VERDICT_APPROVE_VALUES='APPROVE-WITH-CHANGES|APPROVE-WITH-NOTES|APPROVED|APPROVE|PASS|CONFIRM'
VERDICT_REJECT_VALUES='REJECTED|REJECT|REWORK|BLOCKED|BLOCK'
VERDICT_FAILED_VALUES='UNREPRODUCIBLE-FINDINGS|FAILED|FAIL|INCONCLUSIVE|UNKNOWN'
VERDICT_VALUES="${VERDICT_APPROVE_VALUES}|${VERDICT_REJECT_VALUES}|${VERDICT_FAILED_VALUES}"

# `VERDICT` + optional emphasis + separator + optional emphasis. Leading whitespace is
# NOT allowed: an indented line is quoted material, not this file's decision.
VERDICT_KEY_RE='^([*_#]+[[:space:]]*)?VERDICT[*_]*[[:space:]]*[-:=][[:space:]]*[*_]*[[:space:]]*'

# A REFUSAL MAY WEAR MORE DECORATION THAN AN APPROVAL MAY. This is the one deliberate
# asymmetry in the grammar; do not "tidy" it into symmetry.
#
# `- VERDICT: REJECT` and `1. VERDICT: REJECT` are ordinary things to write inside a
# findings list, and the strict opener above did not recognise either. Alone that is
# merely fail-closed (NONE), but a file carrying one ABOVE a plain `VERDICT: APPROVE`
# decided APPROVE — the refusal did not lose the ranking, it was never seen. Reproduced
# against this grammar before the fix.
#
# Widening the opener for approvals too would be the fail-OPEN version of the same
# tidiness: `- VERDICT: APPROVE` in a list of options the reviewer was weighing would
# become a merge. Refusals cost nothing when over-recognised (a wrongly-refused lane is
# re-reviewed; a wrongly-merged one is in main), so the wide opener is refusals-only.
#
# `*` is not added here because it is already in the strict opener as emphasis — a `* `
# bullet has always been accepted for approvals, and 61 live files depend on `**VERDICT`.
# Blockquote `>` and leading whitespace stay excluded on BOTH paths: those mean the text
# is being quoted, not asserted.
VERDICT_REFUSAL_KEY_RE='^([*_#+-]+[[:space:]]*|[0-9]+[.)][[:space:]]+)?VERDICT[*_]*[[:space:]]*[-:=][[:space:]]*[*_]*[[:space:]]*'

# THE VALUE MUST END. A separator alone is not a discriminator: an ordinary human title
#     # Verdict: Approve-flow lane
# is a colon followed by something that STARTS with "approve", and prefix matching read
# it as an approval. Executed against the real gate it merged a lane whose body said
# `VERDICT: REJECT`. So the value is matched as a whole token, terminated by end-of-line
# or by a character that cannot continue a token — which excludes `-flow` while leaving
# `APPROVE-WITH-NOTES` (61 live files) and `APPROVE**` intact.
#
# `_` IS A TERMINATOR, and leaving it out was a live fail-OPEN. The opener above accepts
# `_` as emphasis, so `_VERDICT: REJECT_` opened correctly and then failed to close: the
# trailing `_` continued the token, `REJECT_` was not the value `REJECT`, and the whole
# REFUSAL vanished. On a file that also carried `## VERDICT: APPROVE` the deciding
# function returned APPROVE — reproduced against this grammar, on the path
# `integrate.sh` and `merge-if-only-known-blocker.sh` decide with. An emphasis character
# that can OPEN must also be able to CLOSE; `*` always could, `_` could not. Dropping it
# here makes the pair symmetric and reclassifies 0 of the 147 live verdict artifacts.
VERDICT_TERM_RE='([^A-Za-z0-9-]|$)'

VERDICT_APPROVE_RE="${VERDICT_KEY_RE}(${VERDICT_APPROVE_VALUES})${VERDICT_TERM_RE}"
VERDICT_REJECT_RE="${VERDICT_REFUSAL_KEY_RE}(${VERDICT_REJECT_VALUES})${VERDICT_TERM_RE}"
VERDICT_FAILED_RE="${VERDICT_REFUSAL_KEY_RE}(${VERDICT_FAILED_VALUES})${VERDICT_TERM_RE}"

# "Does this file carry a verdict line at all?" — the UNION of the three, not a fourth
# spelling. Built from the same three patterns so it can never answer "no verdict line"
# about a line verdict_decision just decided with: a list-marked refusal is recognised by
# REJECT_RE, and a VERDICT_LINE_RE built from the strict opener would have called that
# same file unparseable. One question, one source.
VERDICT_LINE_RE="(${VERDICT_APPROVE_RE}|${VERDICT_REJECT_RE}|${VERDICT_FAILED_RE})"

# THE DECISION, WITH REFUSAL PRECEDENCE — never "the first line that matched".
#
# Reading one line and trusting it is what let a title outrank the body below it. The
# whole FILE is the evidence, so a refusal ANYWHERE outranks an approval anywhere: to
# merge, a verdict file must contain an approval and NOTHING that refuses. That ordering
# is free on real data — 0 of the 94 live verdict files carry both.
#
# WHAT THIS ORDERING DOES NOT BUY, stated plainly because the earlier version of this
# comment claimed it did: precedence only ranks refusals it can SEE. A decoration this
# grammar does not recognise does not lose the ranking — it removes the refusal from the
# file entirely, and the approval below it then wins unopposed. That is how
# `_VERDICT: REJECT_` merged (see VERDICT_TERM_RE). So the load-bearing guarantee lives
# in the PATTERNS, not here: every decoration that can open a verdict line must also be
# able to close one, and anything else fails to parse as a verdict at all. When you widen
# the opener, widen the terminator in the same edit.
#
#   APPROVE  an approval token, and no refusal token anywhere in the file
#   REJECT   a rejection token anywhere
#   FAILED   a failure token anywhere (and no rejection)
#   NONE     absent, empty, or no recognised verdict line at all
verdict_decision() {  # file -> APPROVE|REJECT|FAILED|NONE
  [ -s "$1" ] || { printf 'NONE\n'; return 0; }
  if grep -qiE "$VERDICT_REJECT_RE" "$1"; then printf 'REJECT\n'
  elif grep -qiE "$VERDICT_FAILED_RE" "$1"; then printf 'FAILED\n'
  elif grep -qiE "$VERDICT_APPROVE_RE" "$1"; then printf 'APPROVE\n'
  else printf 'NONE\n'
  fi
}

# The line the decision came from, for operator output. Prints nothing when the decision
# is NONE — callers must render that case explicitly rather than as an empty success.
verdict_line() {  # file -> the deciding verdict line
  local re
  case "$(verdict_decision "$1")" in
    REJECT)  re="$VERDICT_REJECT_RE" ;;
    FAILED)  re="$VERDICT_FAILED_RE" ;;
    APPROVE) re="$VERDICT_APPROVE_RE" ;;
    *) return 0 ;;
  esac
  grep -m1 -iE "$re" "$1" | tr -d '*#'
}

# review-pump.sh dispatches its reviewer through `bash -c "$(declare -f ...)"`, which
# inherits the ENVIRONMENT and not the shell's variables. Exporting keeps the grammar
# identical on both sides of that boundary instead of silently empty on one.
export VERDICT_APPROVE_VALUES VERDICT_REJECT_VALUES VERDICT_FAILED_VALUES VERDICT_VALUES
export VERDICT_KEY_RE VERDICT_REFUSAL_KEY_RE VERDICT_TERM_RE
export VERDICT_LINE_RE VERDICT_APPROVE_RE VERDICT_REJECT_RE VERDICT_FAILED_RE
# `export -f` is a bashism; sourcing this from another shell must stay silent rather
# than dumping function bodies into that script's stdout.
export -f verdict_decision verdict_line >/dev/null 2>&1 || true
