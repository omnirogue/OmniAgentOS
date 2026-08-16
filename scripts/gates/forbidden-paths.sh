#!/usr/bin/env bash
# Refuse a branch that carries a generated or secret-bearing file.
#
# Promoted from repeated incidents, not from principle:
#   - ARCHI.md / ARCHI.json arrived via a lane merge three separate times. They are
#     archdocs-generated, and two automated regenerations collided on them in one day.
#   - PLAN.md / WORKBOOK.md are written by live automation and have NO owner. A lane that
#     carries them clobbers whatever the automation wrote; one such clobber cost 103 lines.
#   - configs/accounts.yaml and vault/sources/*.enc reached main inside swarm snapshot
#     commits. Checked afterwards: no live credentials in either. Near-zero exposure by luck
#     of content, not by control.
#
# Exit 2 means "could not run" and is a refusal, per the registry contract.
set -uo pipefail
# A path is a bag of BYTES, not text. Under a UTF-8 locale BSD sed rejects a
# path carrying an invalid byte with "illegal byte sequence" and exits 1 — and
# because this script has no `set -e`, that turned $SWEPT empty and the gate
# answered "no files changed", exit 0, for a branch carrying `.env-\377`. A tool
# error rendered as a clean pass. LC_ALL=C makes every filter below byte-wise,
# so such a path is classified rather than crashing the pipeline, and it makes
# `sort` deterministic regardless of the operator's locale.
export LC_ALL=C

# Every exit 2 says WHICH step could not run, on stderr.
#
# A bare `exit 2` is a refusal with no evidence, and it is not only a human who
# loses: scripts/gate-runner.py:194 records a broken gate as
# `(stdout + stderr)[-300:]`, so a silent refusal is filed with an EMPTY reason
# and the next operator has nothing to act on. Named after cross-lineage review
# pointed out the evidence gap.
#
# stderr, not stdout, deliberately: stdout is the classified-verdict channel
# that gate-runner.py and the tests parse, and a diagnostic must not be
# mistakable for a refusal line. This is a plain function call at top level, not
# inside a `$( )`, so its `exit` ends the SCRIPT — the subshell trap named at the
# classifiers below does not apply here.
#
# These messages deliberately do NOT spell a git subcommand ("the tree walk",
# not "the tree walk (git diff)"). The quotePath guard test scans this file's
# TEXT for path-producing git invocations, and it is deliberately conservative:
# it cannot tell a real walk from the same words inside a diagnostic string, so
# it flagged the message as an unpinned walk. Being refused by your own guard for
# a comment is the right trade against the guard missing a real one.
die2() {
  printf 'REFUSED: %s could not run (exit %s) — this gate fails closed\n' "$1" "$2" >&2
  exit 2
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
REPO="${REPO:-$ROOT_DIR}"
cd "$REPO" || die2 "cd to \$REPO ($REPO)" "$?"
BRANCH="${1:?usage: forbidden-paths.sh <branch> [base]}"
BASE="${2:-main}"

# TWO questions, and they are not the same question — the split follows
# scripts/merge-gate.sh:836-845 exactly.
#
# $FILES is the TREE question: what the tip differs by. Right for the generated
# and ownerless guards, because a lane that edited ARCHI.md and put it back
# collides with nobody (merge-gate.sh:829-831 makes the same call).
#
# $SWEPT is the HISTORY question: what a `git merge --no-ff` makes PERMANENT.
# A secret committed in one commit and deleted in the next has an EMPTY net
# diff and still lands its blob in main. merge-gate.sh:866-875 closed that hole
# on its own secret check and named it: "a branch that added a secret and
# deleted it before its tip was invisible to it". This probe was promoted from
# the accounts.yaml/vault incident and was still asking the tree question.
#
# The history question needs BOTH walks below, because a merge commit carries
# paths of its own. `--no-merges` alone was still a tree question wearing a
# history question's clothes: a secret introduced BY a merge (a conflict
# resolution, or an "evil merge" writing a file present on neither side) and
# removed by a later merge appears in NO non-merge commit and in NO net diff,
# so the gate reported "no forbidden paths" while the blob stayed permanently
# reachable from the merge's tree. Reproduced, not theorised.
#
# The split is what keeps this cheap and quiet:
#   $SWEPT_PLAIN — every path an ordinary commit touched. A file added on a
#     side branch and merged in normally is already here, via that branch's
#     own commit; it needs nothing from the merge pass.
#   $SWEPT_MERGE — `-c` is the COMBINED diff, which lists only paths differing
#     from EVERY parent, i.e. exactly what the merge itself introduced. A merge
#     that merely takes one side's file lists nothing, so this adds no false
#     refusals. Measured over 400 real merges here: 87 paths, none secret-shaped.
# Deliberately NOT `git diff` per commit against $BASE, which answers the same
# question but took 51s over a 1585-commit range where these two walks take 0.2s.
# Deliberately NOT `rev-list --objects`, which dedupes by CONTENT: an empty
# .env.local collides with every other empty blob and would go unlisted.
#
# ALL THREE walks pin core.quotePath=false, and all three need it. Git C-quotes
# a path in --name-only output for two unrelated reasons: because the name
# genuinely cannot be rendered raw — a control character, or the `"` and `\` that
# git's own quoting syntax must escape — and, under the DEFAULT
# core.quotePath=true, because the name contains any byte >= 0x80, which is just
# an accented, CJK or emoji filename. Only the first kind is unscannable, so
# leaving the default on would make the refusal below fire on ordinary names, and
# a gate that refuses ordinary filenames gets disabled.
# Pinning it with `-c` also makes the verdict a property of the REPOSITORY: it is
# client config, so without this the answer depends on the machine's git. The
# guard test in tests/gates_scripts/test_forbidden_paths.py fails the build if a
# FOURTH walk is ever added without the pin.
FILES=$(git -c core.quotePath=false diff --name-only "$BASE...$BRANCH" 2>/dev/null) || die2 "the tree walk" "$?"
SWEPT_PLAIN=$(git -c core.quotePath=false log --no-merges --format= --name-only "$BASE..$BRANCH" 2>/dev/null) || die2 "the history walk over ordinary commits" "$?"
SWEPT_MERGE=$(git -c core.quotePath=false log --merges -c --format= --name-only "$BASE..$BRANCH" 2>/dev/null) || die2 "the merge walk over combined diffs" "$?"
# Guarded because this pipeline CAN fail: pipefail propagates a filter's error,
# and exit 2 is "could not run", which the registry contract treats as a
# refusal. Without the guard a failed filter yields an empty set, which reads as
# the most favourable possible answer.
#
# Stated precisely, because a guard whose trigger is unreachable is worth less
# than it looks: the KNOWN trigger — BSD sed aborting on an invalid byte — is
# already prevented by the LC_ALL=C above, and was confirmed unreachable here by
# execution. This remains a catch-all for the failures LC_ALL=C does not cover
# (a filter missing from PATH, a resource limit, a killed process), so it is
# kept deliberately rather than because it fires today.
SWEPT=$(printf '%s\n%s\n%s\n' "$FILES" "$SWEPT_PLAIN" "$SWEPT_MERGE" | sed '/^$/d' | sort -u) || die2 "the path-set combine (sed/sort)" "$?"
# Checked against the SWEPT set, not $FILES: an empty net diff is not an empty
# merge, so this early-out can no longer skip a secret that only exists in the
# branch's history.
[ -z "$SWEPT" ] && { echo "no files changed"; exit 0; }

# What survives quotePath=false is a name git cannot render raw at all, and the
# anchored greps below cannot classify it — `"ordinary\nconfigs/accounts.yaml"`
# matches no rule and would read as clean. Refuse the unscannable name instead.
# This deliberately also refuses the rare-but-legal name containing `"` or `\`,
# which git quotes whatever quotePath says: those are unmatchable by the same
# anchored rules, so fail closed on them too.
#
# Asked of $SWEPT for the same reason the secret question is: a control-character
# path added mid-branch and removed by the tip has an empty net diff, and
# `git merge --no-ff` still makes its blob permanent.
#
# Reported alongside the other three rather than exiting here. An unscannable
# path does not stop ARCHI.md or accounts.yaml being classifiable, and a gate
# that reveals one reason per run costs a repair/re-run cycle per reason —
# the base reported all applicable categories at once, and that is worth keeping.
# `|| true` on every classifier below was the same favourable-absence shape as
# the sed abort above, and it survived the first pass of this fix. grep's exit
# status carries THREE meanings — 0 matched, 1 no match (normal), >=2 grep
# ITSELF FAILED — and `|| true` erased the difference between the last two, so a
# grep that could not run produced an empty set, which every check below reads
# as "nothing forbidden". Reproduced, not theorised: with a grep that exits 2 on
# PATH, the gate printed "no forbidden paths in  changed file(s)" and exit 0 for
# a branch carrying configs/accounts.yaml.
#
# The rc test has to sit OUTSIDE the command substitution. A helper function
# that called `exit 2` on failure would exit only the SUBSHELL that $( ) forks,
# assign empty, and fail open exactly as before — which is why this is written
# out per classifier instead of factored into one.
QUOTED=$(printf '%s\n' "$SWEPT" | grep '^"'); rc=$?
[ "$rc" -ge 2 ] && die2 "the unquotable-path classifier" "$rc"

# Generated artefacts: only the pipeline that owns them may write them.
GEN=$(printf '%s\n' "$FILES" | grep -E '^(ARCHI\.(md|json))$'); rc=$?
[ "$rc" -ge 2 ] && die2 "the generated-artefact classifier" "$rc"
# Ownerless automation files: a lane carrying these clobbers a live writer.
AUTO=$(printf '%s\n' "$FILES" | grep -E '^(PLAN\.md|WORKBOOK\.md)$'); rc=$?
[ "$rc" -ge 2 ] && die2 "the ownerless-automation classifier" "$rc"
# Secret-bearing: never in a merge, regardless of current content.
#
# The env clause was `^\.env.*$` — anchored at the repo ROOT, so it matched
# `.env` and `.envrc` and nothing below the top level. dashboard/.env.local is
# the standard Next.js secret file for the dashboard this repo runs, and there
# is no `.env` entry in .gitignore to stop it. `(^|/)\.env[^/]*(/|$)` is a
# strict SUPERSET of the old clause (`.envrc` still matches) and uses the same
# segment idiom as merge-gate.sh:850's tracked-env guard.
SEC=$(printf '%s\n' "$SWEPT" | grep -E '^configs/accounts\.yaml$|(^|/)\.env[^/]*(/|$)|^vault/sources/.*\.enc$'); rc=$?
[ "$rc" -ge 2 ] && die2 "the secret-bearing classifier" "$rc"

# Rendering paths for HUMAN output is a different problem from classifying them.
#
# `printf '%s\n'`, never `echo`: POSIX echo interprets backslash escapes, and
# these paths are exactly the ones containing backslashes — a C-quoted path is
# `"ordinary\nconfigs/accounts.yaml"`, backslash then n, which echo turns into a
# REAL newline, splitting the path the gate is refusing across two lines. The
# shebang is bash, whose echo does not do this, so it was latent; it becomes
# live the moment anything invokes this with `sh`.
#
# And the message must be ASCII-SAFE. scripts/gate-runner.py:184 reads this
# stdout with `text=True`, a STRICT utf-8 decode. Now that the walks pin
# quotePath=false, git hands back a path's raw bytes, so refusing `.env-\377`
# printed an invalid byte into stdout and the runner died with
# UnicodeDecodeError instead of recording the refusal — a clean REFUSE turned
# into a crash. Classification above stays byte-exact; only this rendering is
# sanitised, and `?` marks each byte that could not be shown.
render_paths() { printf '%s' "$1" | tr '\n' ' ' | tr -c '\40-\176' '?'; }

RC=0
[ -n "$QUOTED" ] && { printf 'REFUSED unquotable path: %s\n' "$(render_paths "$QUOTED")"; RC=1; }
[ -n "$GEN" ]  && { printf 'REFUSED generated (archdocs owns these): %s\n' "$(render_paths "$GEN")"; RC=1; }
[ -n "$AUTO" ] && { printf 'REFUSED ownerless automation file: %s\n' "$(render_paths "$AUTO")"; RC=1; }
[ -n "$SEC" ]  && { printf 'REFUSED secret-bearing path: %s\n' "$(render_paths "$SEC")"; RC=1; }
# The count is computed with its own rc check for the same reason as the
# classifiers: a failed `grep -c` printed an EMPTY count into the success line
# ("no forbidden paths in  changed file(s)") and still exited 0. The clean-pass
# message must never be printed on the strength of a filter that did not run.
if [ "$RC" = "0" ]; then
  COUNT=$(printf '%s\n' "$SWEPT" | grep -c .); rc=$?
  [ "$rc" -ge 2 ] && die2 "the changed-file count" "$rc"
  printf 'no forbidden paths in %s changed file(s)\n' "$COUNT"
fi
exit $RC
