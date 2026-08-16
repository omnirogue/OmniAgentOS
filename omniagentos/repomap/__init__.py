"""Repo-map: a compact, task-aware, PageRank-ranked map of a codebase's symbols.

Gives a coding agent high-signal structural context in a few hundred tokens instead of
dumping files or paying for a full-repo scan. Implements Aider's repo-map algorithm
(tags → dependency graph → personalized PageRank → budget-fit render), dependency-free.

    from omniagentos.repomap import build_repo_map, RepoMap
    print(build_repo_map(".", focus_terms=["dispatch_spec"], max_tokens=800))
"""

from omniagentos.repomap.service import RepoMap, build_repo_map
from omniagentos.repomap.tags import Definition

__all__ = ["Definition", "RepoMap", "build_repo_map"]
