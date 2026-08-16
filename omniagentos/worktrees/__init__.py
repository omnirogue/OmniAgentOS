"""Lane-agnostic git worktree isolation.

``SubprocessWorktrees`` is the shared mechanism (branch namespace + var root
are constructor parameters); each lane binds it once — swarm's binding is
``omniagentos.swarm.worktrees.SubprocessSwarmWorktrees``.
"""

from .git import (
    MergeOutcome,
    MergeStatus,
    RemoveOutcome,
    RemoveStatus,
    SubprocessWorktrees,
    WorktreeInfo,
    WorktreesProto,
)

__all__ = [
    "MergeOutcome",
    "MergeStatus",
    "RemoveOutcome",
    "RemoveStatus",
    "SubprocessWorktrees",
    "WorktreeInfo",
    "WorktreesProto",
]
