"""FastAPI dependencies for the self-improvement lab."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from omniagentos.lab.db import LabStore
from omniagentos.lab.eval.evaluator import ProtectedEvaluator
from omniagentos.lab.eval.grader import ProtectedGrader
from omniagentos.lab.seed import ensure_demo_seeded


@lru_cache(maxsize=1)
def get_lab_store() -> LabStore:
    """Construct the lab store lazily, sharing H1's configured database path."""
    from omniagentos.contracts import default_db_path

    store = LabStore(default_db_path())
    ensure_demo_seeded(store)
    return store


@lru_cache(maxsize=1)
def get_protected_grader() -> ProtectedGrader:
    """Construct the protected grader and ingest idempotent held-out seeds."""
    store = get_lab_store()
    grader = ProtectedGrader(shared_db_path=store.db_path)
    ensure_demo_seeded(store, grader)
    return grader


@lru_cache(maxsize=1)
def get_protected_evaluator() -> ProtectedEvaluator:
    """Compose the shared lab store with its process-local protected grader."""
    return ProtectedEvaluator(get_lab_store(), get_protected_grader())


LabStoreDep = Annotated[LabStore, Depends(get_lab_store)]
ProtectedGraderDep = Annotated[ProtectedGrader, Depends(get_protected_grader)]
ProtectedEvaluatorDep = Annotated[ProtectedEvaluator, Depends(get_protected_evaluator)]
