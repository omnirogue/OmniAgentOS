"""The memlife decision routes had NO tests at all, which is why a NameError shipped.

Commit `a2aa324f` renamed the request model to `MemlifeDecisionRequest` (breaking an
OpenAPI component-name collision with `reflection.py`) but a too-clever regex with a
negative lookahead `(?!\\()` excluded CONSTRUCTOR CALLS — so the three annotations were
renamed and the three `or DecisionRequest()` fallbacks were not. A pydantic instance is
always truthy, so the fallback fires exactly when `req is None`: every body-less POST hit
an undefined name.

A body-less POST is an explicitly supported call — the signature is
`req: MemlifeDecisionRequest | None = None`. These tests exercise that path, which is the
one that was broken and the one nothing covered.
"""

from __future__ import annotations

import inspect

from omniagentos.api.routes import memlife


class TestNoUndefinedNames:
    """Static guards. These would have caught the regression before it shipped."""

    def test_module_has_no_undefined_decision_request_reference(self) -> None:
        src = inspect.getsource(memlife)
        assert "or DecisionRequest()" not in src, (
            "undefined name: the module defines MemlifeDecisionRequest, not DecisionRequest"
        )

    def test_every_name_used_in_the_module_resolves(self) -> None:
        """Compile-and-resolve check for the specific class of bug: a rename that
        updates annotations but misses call sites.

        `compile()` alone would not catch it — an undefined NAME is a runtime error,
        not a syntax error, which is exactly why this reached production.
        """
        import ast

        src = inspect.getsource(memlife)
        tree = ast.parse(src)
        defined = set(dir(memlife)) | set(dir(__builtins__)) | {"__name__", "__doc__"}
        for node in ast.walk(tree):
            # Only check names being CALLED — that is where the miss happened.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                if name.endswith("Request") or name.endswith("Response"):
                    assert name in defined, f"call to undefined name {name!r} in memlife routes"


class TestDecisionRequestModel:
    def test_default_construction_works(self) -> None:
        """The body-less path constructs this with no arguments — it must be legal."""
        body = memlife.MemlifeDecisionRequest()
        assert body.decided_by == "human"
        assert body.reason == ""

    def test_named_distinctly_from_reflection(self) -> None:
        """FastAPI keys OpenAPI component schemas by CLASS NAME.

        Two route modules exporting `DecisionRequest` collide and one silently
        overwrites the other, so clients receive the wrong shape for whichever
        endpoint loses. These two are NOT interchangeable — this one carries
        `reason`, reflection's does not.
        """
        from omniagentos.api.routes import reflection

        assert memlife.MemlifeDecisionRequest.__name__ != reflection.DecisionRequest.__name__
        assert "reason" in memlife.MemlifeDecisionRequest.model_fields
        assert "reason" not in reflection.DecisionRequest.model_fields
