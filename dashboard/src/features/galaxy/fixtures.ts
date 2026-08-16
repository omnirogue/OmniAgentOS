/**
 * Typed vault fixtures — DEV/SHOWCASE ONLY. Gated behind USE_FIXTURES (see
 * useVaultData.ts); never used as a fallback for a real, empty, or failed backend
 * response. Shaped to match the same 8-field frontmatter and the tree/note/search
 * response types real routes will return, so the reviewer sees the real component tree
 * rendering real-shaped (if invented) data. No held-out/eval values of any kind here —
 * this is entirely synthetic "day in the life of the lab" content.
 */

import type { AnyNoteType, Confidence, VaultNoteDetail, VaultSearchHit, VaultTreeNote } from "./types";

interface FixtureSpec {
  path: string;
  id: string;
  type: AnyNoteType;
  title: string;
  discipline: string | null;
  created: string;
  source_run: string | null;
  confidence: Confidence | null;
  status: string;
  supersedes: string | null;
  links: string[];
  body: string;
}

const SPECS: FixtureSpec[] = [
  {
    path: "Home.md",
    id: "home",
    type: "source",
    title: "OmniAgentOS — Home",
    discipline: null,
    created: "2026-07-11T15:40:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["research-briefs", "code-changes"],
    body: `# OmniAgentOS — Home

Operator dashboard note. The system refreshes the **Status** section; everything else is yours.

## Status

_47 runs recorded across 2 disciplines this week._

## Map

- Disciplines: [[research-briefs]] · [[code-changes]]
- Recent runs: \`runs/\` (one note per run)
- Benchmarks: \`benchmarks/\` (B0/B1 baseline reports land here)
- Decisions: \`decisions/\` (gate evidence, ADR mirrors)
- Learnings: \`learnings/\` · Failures: \`failures/\` · Playbook: \`playbook/\`

## Notes (human)

Nothing yet — this section is yours.`,
  },
  {
    path: "disciplines/research-briefs.md",
    id: "research-briefs",
    type: "discipline",
    title: "Discipline: research briefs",
    discipline: "research-briefs",
    created: "2026-07-05T09:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["Home", "exp-brief-summarizer-v2", "playbook-research-briefs", "leaderboard-research-briefs", "tournament-brief-arena"],
    body: `# Discipline: research briefs

Summarizing long-form research sources into a one-page brief for the operator.

## Champion surface

\`prompt/research-briefs/summarizer\` — see [[prompt-summarizer-v3]].

## Active work

- [[exp-brief-summarizer-v2]] — temperature + structure sweep
- [[tournament-brief-arena]] — champion vs. challenger arena
- [[playbook-research-briefs]] — validated traits so far

## Related

[[Home]] · [[leaderboard-research-briefs]]`,
  },
  {
    path: "disciplines/code-changes.md",
    id: "code-changes",
    type: "discipline",
    title: "Discipline: code changes",
    discipline: "code-changes",
    created: "2026-07-06T09:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["Home", "exp-diff-review-prompt", "failure-diff-review-timeout"],
    body: `# Discipline: code changes

Reviewing incoming diffs for risk before they reach a human approver.

## Active work

- [[exp-diff-review-prompt]] — challenger prompt for risk triage
- [[failure-diff-review-timeout]] — known failure mode, tracked

## Related

[[Home]]`,
  },
  {
    path: "experiments/exp-brief-summarizer-v2.md",
    id: "exp-brief-summarizer-v2",
    type: "experiment",
    title: "Experiment: summarizer v2 temperature sweep",
    discipline: "research-briefs",
    created: "2026-07-09T12:00:00Z",
    source_run: null,
    confidence: "high",
    status: "active",
    supersedes: null,
    links: ["research-briefs", "run-2026-07-10-champion", "run-2026-07-10-challenger", "decision-promote-summarizer-v2", "learning-shorter-system-prompts"],
    body: `# Experiment: summarizer v2 temperature sweep

Hypothesis: a shorter system prompt with an explicit output schema improves brief
quality without hurting latency, for [[research-briefs]].

## Design

| Arm | Surface | Temp |
|---|---|---|
| Champion | \`prompt/research-briefs/summarizer@4\` | 0.4 |
| Challenger | \`prompt/research-briefs/summarizer@5\` | 0.3 |

Budget: 40 eval-suite tasks, 2 judged replicas each.

## Result

Challenger won **62%** of judged pairs (95% CI 54–70%). See [[run-2026-07-10-champion]]
and [[run-2026-07-10-challenger]] for the underlying runs, and
[[decision-promote-summarizer-v2]] for the disposition.

> Shorter prompts kept judges from penalizing verbose preambles — see
> [[learning-shorter-system-prompts]].`,
  },
  {
    path: "experiments/exp-diff-review-prompt.md",
    id: "exp-diff-review-prompt",
    type: "experiment",
    title: "Experiment: diff-review risk triage prompt",
    discipline: "code-changes",
    created: "2026-07-08T10:00:00Z",
    source_run: null,
    confidence: "medium",
    status: "draft",
    supersedes: null,
    links: ["code-changes", "failure-diff-review-timeout"],
    body: `# Experiment: diff-review risk triage prompt

Challenger asks the model to output a risk tier (\`low\`/\`medium\`/\`high\`) before free text,
for [[code-changes]].

## Status

Still gathering eval-suite runs; one known failure mode already logged:
[[failure-diff-review-timeout]].`,
  },
  {
    path: "runs/run-2026-07-10-champion.md",
    id: "run-2026-07-10-champion",
    type: "run",
    title: "Run: summarizer v2 — champion arm",
    discipline: "research-briefs",
    created: "2026-07-10T08:12:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["research-briefs", "exp-brief-summarizer-v2"],
    body: `# Run: summarizer v2 — champion arm

Discipline: [[research-briefs]] · Experiment: [[exp-brief-summarizer-v2]]

## Summary

- **State:** completed
- **Harness:** cli-claude (arm: champion)
- **Queued:** 2026-07-10T08:12:00Z
- **Finished:** 2026-07-10T08:14:41Z

## Usage

- input_tokens: 3,412
- output_tokens: 640
- cost_usd: 0.09 (exact)`,
  },
  {
    path: "runs/run-2026-07-10-challenger.md",
    id: "run-2026-07-10-challenger",
    type: "run",
    title: "Run: summarizer v2 — challenger arm",
    discipline: "research-briefs",
    created: "2026-07-10T08:15:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["research-briefs", "exp-brief-summarizer-v2"],
    body: `# Run: summarizer v2 — challenger arm

Discipline: [[research-briefs]] · Experiment: [[exp-brief-summarizer-v2]]

## Summary

- **State:** completed
- **Harness:** cli-claude (arm: challenger)
- **Queued:** 2026-07-10T08:15:00Z
- **Finished:** 2026-07-10T08:17:02Z

## Usage

- input_tokens: 2,988
- output_tokens: 598
- cost_usd: 0.08 (exact)`,
  },
  {
    path: "decisions/decision-promote-summarizer-v2.md",
    id: "decision-promote-summarizer-v2",
    type: "decision",
    title: "Decision: promote summarizer v2 challenger",
    discipline: "research-briefs",
    created: "2026-07-10T18:30:00Z",
    source_run: null,
    confidence: "high",
    status: "active",
    supersedes: null,
    links: ["Home", "exp-brief-summarizer-v2"],
    body: `# Decision: promote summarizer v2 challenger

Promoted the challenger surface from [[exp-brief-summarizer-v2]] to champion for
[[research-briefs]].

## Evidence

| Check | Result |
|---|---|
| Win rate | 62% (95% CI 54–70%) |
| Safety checks | pass |
| Rollback plan | previous champion pinned as \`v1\` |

**Decided by:** operator · **Note:** shorter prompt, same schema, clear win.`,
  },
  {
    path: "learnings/learning-shorter-system-prompts.md",
    id: "learning-shorter-system-prompts",
    type: "learning",
    title: "Learning: shorter system prompts score higher with LLM judges",
    discipline: "research-briefs",
    created: "2026-07-10T19:00:00Z",
    source_run: "run-2026-07-10-challenger",
    confidence: "medium",
    status: "active",
    supersedes: null,
    links: ["research-briefs", "exp-brief-summarizer-v2"],
    body: `# Learning: shorter system prompts score higher with LLM judges

Observed across [[exp-brief-summarizer-v2]] for [[research-briefs]]: trimming
preamble/boilerplate from the system prompt correlated with higher judge scores, even
holding the output schema constant.

## Why it might be true

- Judges appear to lightly penalize verbosity even when told not to.
- A shorter prompt leaves more context budget for the source material itself.

**Confidence:** medium — one experiment, one discipline. Worth replicating before
promoting to the general playbook.`,
  },
  {
    path: "failures/failure-diff-review-timeout.md",
    id: "failure-diff-review-timeout",
    type: "failure",
    title: "Failure: diff-review challenger times out on large diffs",
    discipline: "code-changes",
    created: "2026-07-09T14:20:00Z",
    source_run: null,
    confidence: "high",
    status: "active",
    supersedes: null,
    links: ["code-changes", "exp-diff-review-prompt"],
    body: `# Failure: diff-review challenger times out on large diffs

[[exp-diff-review-prompt]] (challenger arm, [[code-changes]]) exceeded the step budget
on 3 of 40 eval-suite tasks — all involving diffs over ~1,500 lines.

## Root cause

The risk-tier-first prompt asks the model to restate the whole diff before triaging it,
which blows the token/time budget on large diffs.

## Mitigation

Cap the restated-diff excerpt at ~200 lines; track as a follow-up before re-running the
sweep.`,
  },
  {
    path: "benchmarks/benchmark-2026-07-10.md",
    id: "benchmark-2026-07-10",
    type: "benchmark",
    title: "Benchmark: B0/B1 baseline — 2026-07-10",
    discipline: null,
    created: "2026-07-10T22:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["Home"],
    body: `# Benchmark: B0/B1 baseline — 2026-07-10

10 dev tasks × {b0=cli-claude, b1=mini-swe} = 20 runs, same task set, same-budget
metadata. Rolled up from the ledger — see [[Home]].

## Result

| Arm | Runs | Completed | Failed |
|---|---|---|---|
| b0 | 10 | 9 | 1 |
| b1 | 10 | 10 | 0 |`,
  },
  {
    path: "playbook/playbook-research-briefs.md",
    id: "playbook-research-briefs",
    type: "playbook",
    title: "Playbook: research briefs — validated traits",
    discipline: "research-briefs",
    created: "2026-07-10T20:00:00Z",
    source_run: null,
    confidence: "medium",
    status: "active",
    supersedes: null,
    links: ["research-briefs", "learning-shorter-system-prompts"],
    body: `# Playbook: research briefs — validated traits

Traits validated across two or more independent experiments for [[research-briefs]].

- **Explicit output schema** — fewer malformed outputs, validated 3x.
- **Shorter system prompt** — see [[learning-shorter-system-prompts]] (one replication
  so far; tracked here pending a second).`,
  },
  {
    path: "tournaments/tournament-brief-arena.md",
    id: "tournament-brief-arena",
    type: "tournament",
    title: "Tournament: brief-arena — champion vs. challenger",
    discipline: "research-briefs",
    created: "2026-07-10T09:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["research-briefs", "run-2026-07-10-champion", "run-2026-07-10-challenger"],
    body: `# Tournament: brief-arena — champion vs. challenger

Head-to-head arena for [[research-briefs]], judged pairwise on the same source
documents. Matches drew from [[run-2026-07-10-champion]] and
[[run-2026-07-10-challenger]].

## ELO after this round

Challenger: 1190 · Champion: 1148`,
  },
  {
    path: "leaderboard/leaderboard-research-briefs.md",
    id: "leaderboard-research-briefs",
    type: "leaderboard",
    title: "Leaderboard: research briefs",
    discipline: "research-briefs",
    created: "2026-07-10T23:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["research-briefs"],
    body: `# Leaderboard: research briefs

The curated log-book of top orchestrations for [[research-briefs]].

| Rank | Surface | ELO |
|---|---|---|
| 1 | summarizer@5 (challenger) | 1190 |
| 2 | summarizer@4 (prior champion) | 1148 |`,
  },
  {
    path: "prompts/prompt-summarizer-v3.md",
    id: "prompt-summarizer-v3",
    type: "prompt",
    title: "Prompt surface: summarizer@5",
    discipline: "research-briefs",
    created: "2026-07-10T18:35:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: "prompt-summarizer-v2",
    links: ["research-briefs"],
    body: `# Prompt surface: summarizer@5

Current champion surface for [[research-briefs]] (promoted from
\`decision-promote-summarizer-v2\`).

\`\`\`text
You are a research-brief summarizer. Read the source and produce a single page:
title, three key findings, one open question. No preamble.
\`\`\``,
  },
  {
    path: "sources/source-anthropic-context-eval.md",
    id: "source-anthropic-context-eval",
    type: "source",
    title: "Source: eval-design notes (external reading)",
    discipline: null,
    created: "2026-07-04T11:00:00Z",
    source_run: null,
    confidence: null,
    status: "active",
    supersedes: null,
    links: ["Home"],
    body: `# Source: eval-design notes (external reading)

Background reading captured while designing the judge rubric. Filed under [[Home]].

- LLM judges are sensitive to length and formatting artifacts, not just content.
- Pairwise comparison is more stable than absolute scoring for close arms.`,
  },
];

export const FIXTURE_TREE: VaultTreeNote[] = SPECS.map((s) => ({
  id: s.id,
  path: s.path,
  type: s.type,
  title: s.title,
  links: s.links,
  discipline: s.discipline,
}));

export const FIXTURE_DETAILS: Record<string, VaultNoteDetail> = Object.fromEntries(
  SPECS.map((s) => [
    s.path,
    {
      frontmatter: {
        id: s.id,
        type: s.type,
        discipline: s.discipline,
        created: s.created,
        source_run: s.source_run,
        confidence: s.confidence,
        status: s.status,
        supersedes: s.supersedes,
      },
      body: s.body,
      links: s.links,
    } satisfies VaultNoteDetail,
  ]),
);

function plainText(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[#>*_`|-]/g, " ")
    .replace(/\[\[([^\]|]+)(\|[^\]]+)?\]\]/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/** In-memory substring search over the fixture set — dev-only stand-in for vault/search. */
export function fixtureSearch(query: string): VaultSearchHit[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const hits: VaultSearchHit[] = [];
  for (const s of SPECS) {
    const titleHit = s.title.toLowerCase().includes(q);
    const plain = plainText(s.body);
    const bodyIdx = plain.toLowerCase().indexOf(q);
    if (!titleHit && bodyIdx === -1) continue;
    let snippet: string;
    if (bodyIdx >= 0) {
      const start = Math.max(0, bodyIdx - 40);
      const end = Math.min(plain.length, bodyIdx + q.length + 60);
      snippet = `${start > 0 ? "… " : ""}${plain.slice(start, end)}${end < plain.length ? " …" : ""}`;
    } else {
      snippet = plain.slice(0, 100);
    }
    hits.push({ path: s.path, title: s.title, type: s.type, snippet });
  }
  return hits;
}
