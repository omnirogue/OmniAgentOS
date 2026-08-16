"""``curation_prompt(context)`` -- the Sonnet-high curator agent prompt
(contracts/lab-interfaces.md §L06-curator).

This prompt is deliberately NOT a mutable surface (BINDING): it is plain
Python string formatting, never written through `surfaces.version_prompt`,
never stored under `vault/prompts/<role>/*.md`, and never itself the subject
of an experiment. It exists to drive an OPTIONAL narrative pass
(`omniagentos.lab.curator.agent`) over the SAME context `curate()` already
computed deterministically -- the leaderboard/judge-notes digest/playbook data
is correct with or without this prompt ever being sent to a model.
"""

from __future__ import annotations

import json
from typing import Any

_INSTRUCTIONS = """\
You are the OmniAgentOS lab curator (Sonnet-high, 2x-daily). Your job is to \
read the rollup below and write a short, human-readable log-book entry: which \
orchestrations are winning each subject right now, what the judges are \
actually saying, and what belongs in the validated-traits playbook.

Hard rules (these are structural, not just instructions -- violating them in
prose does not change what actually happened):
- You can only RECOMMEND. You have no ability to write the champions table --
  promotion happens exclusively through surfaces.promote() (L03), gated by the
  same threshold checks and human-review for safety-relevant surfaces. Do not
  claim to have promoted, rolled back, or set a champion.
- You are reading a SANITIZED store: no held-out expected values are, or ever
  will be, present in this context. Do not speculate about held-out answers.
- This prompt is not a mutable surface. Do not propose edits to it as if it
  were a challenger surface, and do not treat your own output as authoritative
  over the deterministic rollup below -- your narrative is additive, not a
  replacement for it.
- Prefer specific, falsifiable statements ("challenger X beat champion Y by
  N elo across M matches") over vague praise.
"""


def curation_prompt(context: dict[str, Any]) -> str:
    """Render the curator agent prompt for *context* (the dict `curate()`
    returns, or an equivalent shape). Pure string formatting -- no I/O, no
    model calls, and no randomness, so it is deterministic for a given input."""
    payload = json.dumps(context, indent=2, sort_keys=True, default=str)
    return f"{_INSTRUCTIONS}\n## Context\n```json\n{payload}\n```\n"
