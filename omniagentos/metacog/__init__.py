"""Artifacts, typed memory, and metacognitive control (OmniAgentOS).

Closed loop:
  Task → Plan → Execute → Artifacts → Verify → Reflect → Promote Memory → Skills

Default mode is ``enforce`` (configs/metacog.yaml): artifacts, memory promotion,
strategy switches, and skill canaries are LIVE. Set modes to ``shadow`` or
``off`` via config/env to record-only or disable.
"""

from omniagentos.metacog.config import (
    memory_promotion_mode,
    metacog_mode,
    skill_canary_mode,
    strategy_switch_mode,
)
from omniagentos.metacog.service import MetacogService

__all__ = [
    "MetacogService",
    "metacog_mode",
    "memory_promotion_mode",
    "skill_canary_mode",
    "strategy_switch_mode",
]
