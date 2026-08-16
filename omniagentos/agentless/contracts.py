"""Frozen data contracts for the Agentless pipeline (arXiv 2407.01489).

The thesis this package implements: spend compute only where a verifier converts
it into accuracy. An agent that wanders a repo turn by turn burns tokens on
navigation; Agentless instead does one localization pass (see
:mod:`omniagentos.agentless.localize`, built on :mod:`omniagentos.repomap`), samples
N candidate patches against a SINGLE byte-stable prompt, and lets the project's own
test suite (the verifier) pick the winner (:mod:`omniagentos.agentless.select`).
Compute-optimal sampling (Snell et al. 2408.03314) says sequential, adaptive
sampling with early stopping beats a fixed best-of-N — see
:mod:`omniagentos.agentless.pipeline`'s batch/early-stop loop.

These dataclasses are the seam between localization, generation, verification, and
selection; every stage below reads/writes only these shapes, never ad-hoc dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SymbolRef:
    """One ranked symbol the localizer surfaced (from repomap's Definition)."""

    rel_path: str
    name: str
    line: int
    signature: str


@dataclass(frozen=True)
class LocalizationResult:
    """Where to look, and the rendered map used to justify it."""

    repo_dir: str
    focus_files: list[str]
    top_symbols: list[SymbolRef]
    repo_map: str


@dataclass(frozen=True)
class SampleSpec:
    """One candidate-generation request: which adapter/model/effort to use."""

    adapter: str
    model: str | None = None
    effort: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class CandidatePatch:
    """One raw sample from an adapter, before apply/verify."""

    index: int
    diff: str | None
    adapter: str
    model: str | None
    raw_output: str
    gen_seconds: float
    usage: dict[str, Any]


@dataclass(frozen=True)
class VerifiedCandidate:
    """A candidate after the apply-and-test-in-a-scratch-checkout cycle.

    ``tests_passed`` is None when verification never ran (e.g. the diff never
    applied), so callers must not conflate "did not run" with "failed"."""

    patch: CandidatePatch
    applied: bool
    tests_passed: bool | None
    test_output_tail: str
    returncode: int | None
    verify_seconds: float


@dataclass(frozen=True)
class AgentlessResult:
    """The full run: what was asked, where we looked, every candidate tried, and
    which one (if any) was selected — with a human-readable reason either way."""

    task: str
    localization: LocalizationResult
    candidates: list[VerifiedCandidate] = field(default_factory=list)
    selected: VerifiedCandidate | None = None
    selection_reason: str = ""
    wall_seconds: float = 0.0
