"""Blind judging primitives (blueprint Section 11.7; design review HD-015).

A judge is shown ONLY a ``BlindPair``: ``{case_id, blind_token, output}`` —
never an ``arm`` label, a candidate/agent/lab identity, or prior scores.
``blind_token`` is CRYPTO-RANDOM per case x arm and unlinkable to the arm by
construction: it is drawn from ``secrets`` (the OS CSPRNG), never derived
from ``case_id``/``arm`` (a hash of known, enumerable ids would be a
known-plaintext dictionary attack away from being reversible — NOT
unlinkable, even though it "looks" random). Presentation order is shuffled
independently with a *recorded* seed (`random.Random(seed)`, deliberately a
SEPARATE RNG from the token draws) so a judge cannot infer the arm from
position either, and the seed can be replayed for audit without ever being
able to predict a `blind_token` from it.
"""

from __future__ import annotations

import random
import secrets
from typing import Any, TypedDict


class BlindPair(TypedDict):
    """The ONLY shape a judge is shown for one case: no ``arm``, no candidate
    identity, no prior score. ``case_id`` lets a judge harness look up
    candidate-safe ``input``/``rubric`` (e.g. via ``LabStore.candidate_cases``
    — never ``expected``); ``output`` is that arm's produced answer."""

    case_id: str
    blind_token: str
    output: dict[str, Any]


def unlinkable_token() -> str:
    """A crypto-random token carrying zero information about which case/arm
    it belongs to (HD-015): an independent ``secrets.token_urlsafe`` draw,
    never a hash or other derivation of ``case_id``/``arm``."""
    return secrets.token_urlsafe(24)


def build_blind_pairs(
    case_outputs: dict[str, dict[str, dict[str, Any]]],
    *,
    seed: int | None = None,
) -> tuple[list[BlindPair], dict[str, tuple[str, str]], int]:
    """Turn ``{case_id: {arm: output}}`` into a randomized, blind-tokenized
    presentation list.

    Returns ``(pairs, token_map, seed)``:
      - ``pairs``: the shuffled ``BlindPair`` list (no ``arm`` field) — what a
        judge is shown.
      - ``token_map``: ``blind_token -> (case_id, arm)``. Held by the caller
        (``ProtectedEvaluator``) ONLY, consulted AFTER judging to attribute a
        score back to an arm — never handed to a judge.
      - ``seed``: the presentation-order seed actually used (recorded for
        reproducibility of ORDER only — ``blind_token`` values are always a
        fresh ``secrets`` draw regardless of ``seed``, so recording it can
        never be used to predict or reproduce a token).
    """
    resolved_seed = secrets.randbits(32) if seed is None else seed
    pairs: list[BlindPair] = []
    token_map: dict[str, tuple[str, str]] = {}
    for case_id in sorted(case_outputs):
        for arm in sorted(case_outputs[case_id]):
            token = unlinkable_token()
            while token in token_map:  # astronomically unlikely; belt & suspenders
                token = unlinkable_token()
            token_map[token] = (case_id, arm)
            pairs.append(
                BlindPair(case_id=case_id, blind_token=token, output=case_outputs[case_id][arm])
            )
    random.Random(resolved_seed).shuffle(pairs)
    return pairs, token_map, resolved_seed
