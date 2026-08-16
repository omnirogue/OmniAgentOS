# Historical prompt location

The single-agent Fable curator prompt formerly stored here is retired.

The active, ordered prompts are implemented by
`omniagentos.improvement_chain` and their model policy lives in
`configs/loop_models.yaml`:

1. Kimi produces the evidence-backed draft.
2. Opus 5 at X High directly edits the staged actionable plan.
3. Fable performs the final read-only review.

This file remains only because older operational tooling and audit links refer
to the path.
