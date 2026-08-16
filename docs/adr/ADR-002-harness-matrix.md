# ADR-002: Harness matrix and the baseline ladder

**Status:** accepted · 2026-07-11 · Blueprint §11.8, Epic G

## Decision
Every run records its harness identity (HarnessProfile: harness, version, env_hash,
params). Four harness families in H1:

| Harness | Role |
|---|---|
| cli-claude / cli-codex / cli-grok | B0 arms + lightweight workers (subscription CLIs, no API keys) |
| mini-swe (2.4.5, pinned) | B1 minimal agent loop for code — with a **CLI-shim Model** (duck-typed Protocol) that shells to `claude -p`, so B1 needs NO API key |
| openhands (SDK 1.35.0, pinned) | controlled experiment harness: programmatic agents, isolated replicated runs. litellm-backed → live runs are **key-gated**; `health()` reports `live_runs` honestly |
| fusion / improve | imported historical runs (wrapper), harness recorded as such |

Baseline ladder: disciplines keep B0 (single model, one shot), B1 (minimal loop),
champion arms; bench manifests make "does orchestration earn its complexity?"
queryable (G1 criterion B6).

## Empirical basis
docs/research/harnesses.md + docs/research/cli-adapters.md (installed, inspected,
probed on this machine 2026-07-11). Key facts: no Docker required for local
execution of either harness; mini-swe `Model` is a Protocol; litellm needs provider
API keys the subscription CLIs cannot provide.

## Fallback (recorded up front)
If the CLI-shim cannot conform to mini-swe 2.4.5's expected response format
(FormatError ceiling), B1 falls back to a minimal in-repo tool-loop harness
(single model, single bash tool, no reviewers) driving the CLI adapters — still
honestly "the simplest agent loop", and the ADR gets amended, not silently ignored.
