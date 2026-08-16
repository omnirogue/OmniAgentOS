"""promptshape: deterministic prompt assembly with lost-in-the-middle discipline.

Long-context evaluations (Liu et al. 2307.03172, "Lost in the Middle") show models
attend best to the HEAD and TAIL of a prompt and worst to its middle. This package
gives every caller in the repo a single, boring way to build prompts that respect
that finding: cacheable/stable content goes first (and is provider-prompt-cache
friendly because it renders byte-identically call to call), volatile "bulk" context
goes in the middle where attention is weakest anyway, and the task/instruction that
most needs the model's attention goes last, right before generation starts.

``segments`` provides the ordering primitive; ``compress`` provides an opt-in,
env-gated way to shrink noisy 'bulk' content (logs, tracebacks) without ever
touching a diff/patch segment, whose bytes must survive to be applied verbatim.
"""

from __future__ import annotations

from omniagentos.promptshape.assembly import assemble
from omniagentos.promptshape.compress import compress
from omniagentos.promptshape.rolepack import JOB_ROLES, role_pack
from omniagentos.promptshape.segments import Segment, render, stable_prefix

__all__ = ["Segment", "render", "stable_prefix", "compress", "assemble", "role_pack", "JOB_ROLES"]
