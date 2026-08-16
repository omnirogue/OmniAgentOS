"""Structural invariants for the North Star certification manifest.

These tests forbid a CLASS of defect, not one incident of it.  The measured
incident was a YAML merge anchor whose default `binding` carried a target, so
~212 entries inherited ONE pytest carrier: the JUnit held a single testcase and
~150 checks rendered PASS.  Nothing below keys on that anchor's name, on that
target's spelling, or on the ids that happened to inherit it — they key on the
SHAPE that made the wall of PASSes possible:

* a binding that is defined once and reused (by anchor, by alias, or by merge);
* one pytest carrier standing in for more claims than it can examine;
* a target that names no test function that could ever have run;

so the same manifest written with different names, in a different order, with a
different anchor, still fails.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import yaml

from pipeline.bridge import review_policy
from pipeline.bridge.review_policy import risky_review_paths

MANIFEST = Path("configs/northstar-cert/manifest.yaml")

#: One pytest carrier may attest at most this many checks, and then only when
#: EVERY binder declares `shared: true`.  Kept in step with
#: ``scripts.northstar_cert.record_results.MAX_BINDERS_PER_TARGET``, which VOIDs
#: any run whose manifest exceeds it.
MAX_BINDERS_PER_TARGET = 2

#: `tests/northstar/` is not a carrier root: the certification suite may not bind
#: its checks to tests it ships for itself.  Evidence has to come from the system
#: under test, not from the instrument.
FORBIDDEN_TARGET_ROOTS = ("tests/northstar/",)

_SCHEMA_FIELDS = frozenset(
    {"id", "capability", "binding", "tier", "gate", "requires", "scope", "provenance"}
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _manifest_text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _checks() -> list[dict[str, Any]]:
    data = yaml.safe_load(_manifest_text())
    assert isinstance(data, dict) and isinstance(data["checks"], list)
    return data["checks"]


def _pytest_checks() -> list[dict[str, Any]]:
    return [item for item in _checks() if item["binding"]["type"] == "pytest"]


def _binders_by_target() -> dict[str, list[dict[str, Any]]]:
    """Census over the WHOLE manifest, every tier.

    Tier-local counting would let the defect hide by splitting one carrier's
    binders across t1/t2/t3, and a carrier shared across tiers is exactly as
    vacuous as one shared inside a tier.
    """

    binders: dict[str, list[dict[str, Any]]] = {}
    for item in _pytest_checks():
        binders.setdefault(item["binding"]["target"], []).append(item)
    return binders


class _Frame:
    """One open YAML collection while walking the raw event stream."""

    __slots__ = ("anchor", "current_key", "expect_key", "is_map", "keys", "parent_key")

    def __init__(self, *, is_map: bool, anchor: str | None, parent_key: str | None) -> None:
        self.is_map = is_map
        self.anchor = anchor
        self.parent_key = parent_key
        self.expect_key = True
        self.current_key: str | None = None
        self.keys: list[str] = []


def _slot(stack: list[_Frame], scalar: str | None = None) -> str | None:
    """Register one node with its parent; return the mapping key it fills.

    ``None`` means the node is not a mapping value (it is a key, a sequence
    item, or the document root).
    """

    if not stack or not stack[-1].is_map:
        return None
    frame = stack[-1]
    if frame.expect_key:
        frame.expect_key = False
        frame.current_key = scalar
        if scalar is not None:
            frame.keys.append(scalar)
        return None
    frame.expect_key = True
    return frame.current_key


def shared_binding_offenders(text: str) -> list[str]:
    """Every place the raw YAML lets one binding definition serve many checks.

    Works on the event stream, before merge resolution, because ``safe_load``
    erases exactly the evidence we need: after loading, an inherited binding is
    indistinguishable from a hand-written one.
    """

    offenders: list[str] = []
    stack: list[_Frame] = []
    for event in yaml.parse(text):
        if isinstance(event, yaml.MappingStartEvent | yaml.SequenceStartEvent):
            parent_key = _slot(stack)
            stack.append(
                _Frame(
                    is_map=isinstance(event, yaml.MappingStartEvent),
                    anchor=event.anchor,
                    parent_key=parent_key,
                )
            )
        elif isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
            frame = stack.pop()
            if not frame.is_map or frame.anchor is None:
                continue
            if "binding" in frame.keys:
                offenders.append(
                    f"anchor &{frame.anchor} defines a reusable block carrying a binding "
                    f"(keys: {sorted(frame.keys)})"
                )
            if frame.parent_key == "binding":
                offenders.append(f"anchor &{frame.anchor} makes one binding reusable")
        elif isinstance(event, yaml.ScalarEvent):
            key = _slot(stack, event.value)
            if key == "binding" and event.anchor is not None:
                offenders.append(f"anchor &{event.anchor} makes one binding reusable")
        elif isinstance(event, yaml.AliasEvent):
            if _slot(stack) == "binding":
                offenders.append(f"a binding is supplied by alias *{event.anchor}")
    return offenders


def _target_parts(target: str) -> tuple[Path, list[str]]:
    path, _, rest = target.partition("::")
    return Path(path), [part for part in rest.split("::") if part]


_TREES: dict[Path, ast.Module | None] = {}


def _tree(path: Path) -> ast.Module | None:
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            _TREES[path] = None
    return _TREES[path]


def _defines_test(target: str) -> bool:
    """True when ``path::[Class::]function`` names a real, collectable function.

    Resolution mirrors pytest's own: a bare name must be defined at module level,
    a dotted name must be a direct member of the named class.  ``Path.is_file()``
    is not enough — a target may point at a real file and a function that does
    not exist, which is precisely a check that can never run.
    """

    path, parts = _target_parts(target)
    if not parts or not path.is_file():
        return False
    tree = _tree(path)
    if tree is None:
        return False
    node: ast.AST = tree
    for class_name in parts[:-1]:
        found = next(
            (
                child
                for child in ast.iter_child_nodes(node)
                if isinstance(child, ast.ClassDef) and child.name == class_name
            ),
            None,
        )
        if found is None:
            return False
        node = found
    wanted = parts[-1].partition("[")[0]
    return any(
        isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child.name == wanted
        for child in ast.iter_child_nodes(node)
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_northstar_manifest_parses_and_ids_are_unique() -> None:
    checks = _checks()
    ids = [item["id"] for item in checks]
    assert {"NSC-C10-05", "NSC-C11-05", "NSC-C43-09"} <= set(ids)
    assert len(ids) == len(set(ids))
    for item in checks:
        assert _SCHEMA_FIELDS <= item.keys(), item["id"]
        binding = item["binding"]
        assert isinstance(binding["type"], str) and isinstance(binding["target"], str)
        if "shared" in binding:
            assert type(binding["shared"]) is bool, item["id"]
        if binding["type"] == "pytest":
            path, separator, test_name = binding["target"].partition("::")
            assert separator and test_name, item["id"]
            assert path.endswith(".py"), item["id"]


def test_northstar_manifest_and_writer_registry_are_risky_merge_gate_paths() -> None:
    paths = {
        "configs/northstar-cert/manifest.yaml",
        "configs/northstar-cert/writers.yaml",
        "docs/routine.md",
    }
    assert risky_review_paths(paths) == [
        "configs/northstar-cert/manifest.yaml",
        "configs/northstar-cert/writers.yaml",
    ]


def test_the_instrument_that_grades_itself_is_risky_too() -> None:
    """The recorder decides what a verdict IS, emit_gaps decides which reds
    become work, and the migration can rewrite every sticky obligation at once.
    Protecting only the config files they read leaves the interpreter of those
    files unreviewed — a change to any of these three can quietly retune the
    instrument that grades the estate."""
    instrument = [
        "scripts/northstar_cert/emit_gaps.py",
        "scripts/northstar_cert/migrate_writers_v3.py",
        "scripts/northstar_cert/record_results.py",
    ]
    # `risk_tier` lives under pipeline/ and is imported the way its own suite
    # does it (pipeline on sys.path), not as a pipeline.bridge submodule.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))
    from bridge import risk_tier

    assert risky_review_paths({*instrument, "docs/routine.md"}) == instrument
    for path in instrument:
        assert path in review_policy._RISKY_EXACT, path
        # ...and the risk-tier classifier agrees: hard-HIGH, never downgradable.
        assert risk_tier.is_hard_high(path), path


# ---------------------------------------------------------------------------
# the vacuous-binding class
# ---------------------------------------------------------------------------


def test_no_binding_definition_is_shared_between_checks() -> None:
    """(a) A binding may never be inherited, aliased, or anchored for reuse.

    Every check has to say, in its own entry, what evidence it stands on.  This
    is the defect's actual mechanism: a merge anchor supplied the target, so a
    reviewer reading the checks could not see what any of them were bound to.
    """

    offenders = shared_binding_offenders(_manifest_text())
    assert not offenders, "reusable binding definitions in the manifest:\n" + "\n".join(
        sorted(set(offenders))
    )


def _sample(ids: list[str]) -> str:
    return f"{ids[:6]}{'...' if len(ids) > 6 else ''}"


def test_no_pytest_target_is_bound_by_more_than_two_checks() -> None:
    """(b) One testcase cannot attest an unbounded number of distinct claims."""

    oversubscribed = {
        target: sorted(item["id"] for item in group)
        for target, group in _binders_by_target().items()
        if len(group) > MAX_BINDERS_PER_TARGET
    }
    assert not oversubscribed, "\n".join(
        f"{target} is bound by {len(ids)} checks: {_sample(ids)}"
        for target, ids in sorted(oversubscribed.items())
    )


def test_co_bound_targets_declare_sharing_on_every_binder() -> None:
    """(c) Two checks may share a carrier only when both say so, in writing."""

    undeclared = {
        target: sorted(item["id"] for item in group if item["binding"].get("shared") is not True)
        for target, group in _binders_by_target().items()
        if len(group) > 1 and not all(item["binding"].get("shared") is True for item in group)
    }
    assert not undeclared, "\n".join(
        f"{target} is co-bound by {len(ids)} undeclared checks: {_sample(ids)}"
        for target, ids in sorted(undeclared.items())
    )


def test_every_unblocked_pytest_target_names_a_real_test_function() -> None:
    """(d) A target that cannot run can never produce evidence.

    Checks with a non-empty ``requires[]`` mask are exempt: they name carriers a
    writer has not landed yet.  The exemption is safe because absence is never
    favorable downstream — an unmet mask renders NOT_EVALUABLE(precondition_missing)
    and a satisfied mask over a missing carrier renders NOT_EVALUABLE(not_executed).
    Neither can ever become a PASS.
    """

    unresolved = sorted(
        f"{item['id']} -> {item['binding']['target']}"
        for item in _pytest_checks()
        if not item["requires"] and not _defines_test(item["binding"]["target"])
    )
    assert not unresolved, "pytest targets that name no such test function:\n" + "\n".join(
        unresolved
    )


def test_no_check_binds_to_the_certification_suites_own_tests() -> None:
    """(e) The instrument may not certify itself."""

    self_bound = sorted(
        f"{item['id']} -> {item['binding']['target']}"
        for item in _pytest_checks()
        if item["binding"]["target"].startswith(FORBIDDEN_TARGET_ROOTS)
    )
    assert not self_bound, "checks bound to instrument-owned tests:\n" + "\n".join(self_bound)


# ---------------------------------------------------------------------------
# the detectors themselves
# ---------------------------------------------------------------------------


def test_shared_binding_detector_catches_the_class_not_a_spelling() -> None:
    """The (a) detector must fire on any name, any mechanism, any nesting."""

    merge_anchor = (
        "anything: &whatever_you_call_it\n"
        "  binding: {type: pytest, target: tests/x.py::test_y}\n"
        "checks:\n"
        "  - {<<: *whatever_you_call_it, id: A}\n"
        "  - {<<: *whatever_you_call_it, id: B}\n"
    )
    aliased_binding = (
        "pool:\n"
        "  reused: &carrier {type: pytest, target: tests/x.py::test_y}\n"
        "checks:\n"
        "  - {id: A, binding: *carrier}\n"
        "  - {id: B, binding: *carrier}\n"
    )
    anchored_in_place = (
        "checks:\n"
        "  - {id: A, binding: &carrier {type: pytest, target: tests/x.py::test_y}}\n"
        "  - {id: B, binding: *carrier}\n"
    )
    clean = (
        "checks:\n"
        "  - {id: A, binding: {type: pytest, target: tests/x.py::test_y}}\n"
        "  - {id: B, binding: {type: pytest, target: tests/x.py::test_z}}\n"
    )
    for text in (merge_anchor, aliased_binding, anchored_in_place):
        assert shared_binding_offenders(text), text
    assert shared_binding_offenders(clean) == []


def test_target_resolution_rejects_a_file_that_lacks_the_function() -> None:
    """`Path.is_file()` passing is not evidence that the test exists."""

    real = "tests/scripts/test_northstar_cert_manifest.py"
    assert _defines_test(f"{real}::test_target_resolution_rejects_a_file_that_lacks_the_function")
    assert not _defines_test(f"{real}::test_this_function_does_not_exist")
    assert not _defines_test("tests/scripts/no_such_file_at_all.py::test_x")
    assert not _defines_test(real)
