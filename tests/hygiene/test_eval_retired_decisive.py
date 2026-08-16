"""Decisive test for HYG-2 D2: omniagentos.eval is retired.

Property (unit = true absence of package + absence of DoD pin):
  1) find_spec("omniagentos.eval") is None (no import side effects; distinguishes
     absent-package from half-present/broken package that fails on a dependency)
  2) import_module raises ModuleNotFoundError with e.name == "omniagentos.eval" exactly
     (not any nested missing dependency)
  3) DoD matrix module has no live test_floor_validation_api and no omniagentos.eval import

Case label for red-first: Case B inversion against pre-deletion tree
(base 9705d103 still has eval; the decisive assertion is ABSENCE after retirement).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from pathlib import Path

import pytest


def test_omniagentos_eval_import_raises_module_not_found() -> None:
    # True absence: find_spec does not execute package __init__ (no side effects)
    # and returns None only when the package is not discoverable — a half-present
    # package (stub __init__ that fails on a missing dep) would still find a spec.
    assert importlib.util.find_spec("omniagentos.eval") is None

    with pytest.raises(ModuleNotFoundError) as exc_info:
        importlib.import_module("omniagentos.eval")
    # Bound the failure to THIS package name, not a nested missing dependency.
    assert exc_info.value.name == "omniagentos.eval"


def test_dod_matrix_has_no_eval_pin() -> None:
    # Load the DoD module source from the tree under test (not a cached install).
    import tests.certification.test_definition_of_done as dod

    src = Path(inspect.getsourcefile(dod) or "").read_text(encoding="utf-8")
    # Live pin must be gone (retirement comment may still mention the name).
    assert "def test_floor_validation_api" not in src
    assert "from omniagentos.eval" not in src
    assert "import omniagentos.eval" not in src
    # Collect live tests: pin name must not be a collected test function.
    assert not hasattr(dod, "test_floor_validation_api")
