#!/usr/bin/env python3
"""Record a manifest-selected North Star JUnit run in existing evidence stores."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.contracts import digest, utc_now_iso
from omniagentos.lab.contracts import EvalCase, EvalResult, EvalSplit, EvalSuite, MetricSpec
from omniagentos.lab.db import LabStore
from omniagentos.pulse.store import PulseStore
from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceExists,
    GateEvidenceStore,
    binding_digest,
    normalize_gate_command,
    workspace_digest_for,
)

DISCIPLINE = "northstar_cert"
ROUTINE_ID = "northstar-cert"
DEFAULT_MANIFEST = Path("configs/northstar-cert/manifest.yaml")
DEFAULT_WRITER_REGISTRY = Path("configs/northstar-cert/writers.yaml")
DEFAULT_RESULTS_DB = Path("var/northstar-cert/results.sqlite3")
DEFAULT_EVIDENCE_ROOT = Path("var/gate-evidence")
LIVE_DB = Path("var/runtime/state.sqlite3")
_REASON_PREFIX = "__nsc_reason__:"
_REQUIRED_FIELDS = frozenset(
    {"id", "capability", "binding", "tier", "gate", "requires", "scope", "provenance"}
)

#: A pytest carrier may attest at most two checks, and only when BOTH bindings
#: declare ``shared: true``.  Anything wider means one test result is standing in
#: for claims it never examined — the vacuous-binding class.  It is an instrument
#: fault, so the affected checks VOID; they are never scored either way.
MAX_BINDERS_PER_TARGET = 2
VACUOUS_BINDING_REASON = "instrument_error:vacuous_binding"
#: The mask was satisfied, so the carrier was expected to run, and it did not
#: appear in the JUnit at all.  Absence is never favorable.
NOT_EXECUTED_REASON = "no_writer_evidence:not_executed"
#: pytest reported ``<error>`` for the carrier: a fixture/import/collection fault.
#: The check was NOT measured, so it VOIDs rather than scoring either way.
PYTEST_ERROR_REASON = "instrument_error:pytest_error"
NO_WRITER_EVIDENCE_REASON = "no_writer_evidence"
MASK_REGISTRY_UNAVAILABLE_REASON = "mask_gating:registry_unavailable"
# Compatibility name for callers/tests written against registry v1. The
# registry now governs every colon-shaped mask token, not only ``writer:``.
WRITER_REGISTRY_UNAVAILABLE_REASON = MASK_REGISTRY_UNAVAILABLE_REASON

#: ``launchd:<label>`` requirements are probed automatically (a loaded launchd
#: job is a fact this process can OBSERVE). Every colon-shaped requirement is
#: also retained in the sticky reverse index immediately before PASS; deleting
#: a manifest mask therefore cannot delete the obligation. ``writer:`` tokens
#: additionally require their witness/proof evidence.
LAUNCHCTL = "/bin/launchctl"
LAUNCHD_REQUIREMENT_PREFIX = "launchd:"
WRITER_REQUIREMENT_PREFIX = "writer:"
SHALLOW_HISTORY_ACK_ENV = "NSCERT_ACK_SHALLOW_HISTORY"
#: The human-approved launchd label record. It is DATA (it actuates nothing);
#: read here only to translate a manifest's short token into the real label.
LAUNCHD_APPROVED_FILE = Path("configs/launchd-approved.yaml")
_LAUNCHD_PROBE_CACHE: dict[str, bool] = {}
#: Approved labels, keyed by the file they were read from. A file that could not
#: be read caches as ``()`` — fail-soft to exact-token probing, never to a guess.
_LAUNCHD_APPROVED_CACHE: dict[str, tuple[str, ...]] = {}


class CertificationError(RuntimeError):
    """The adapter could not produce trustworthy certification evidence."""


class UnchangedInputRefusal(CertificationError):
    """This exact run identity already has an append-only receipt."""


class CheckVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    #: Instrument fault on this check: it could not be measured at all, so it is
    #: excluded from gate math and never rendered as a candidate defect.
    VOID = "VOID"


class RunVerdict(StrEnum):
    CERTIFIED = "CERTIFIED"
    MEASURED = "MEASURED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    VOID = "VOID"


@dataclass(frozen=True)
class ManifestCheck:
    id: str
    capability: str
    binding_type: str
    target: str
    tier: str
    gate: bool
    requires: tuple[str, ...]
    scope: str
    provenance: tuple[str, ...]
    #: ``binding.shared`` — this carrier is deliberately co-bound with exactly one
    #: other check.  Declared sharing is the ONLY way two checks may name one target.
    shared: bool = False

    def case_input(self) -> dict[str, Any]:
        binding: dict[str, Any] = {"type": self.binding_type, "target": self.target}
        if self.shared:
            binding["shared"] = True
        return {
            "check_id": self.id,
            "capability": self.capability,
            "binding": binding,
            "tier": self.tier,
            "gate": self.gate,
            "requires": list(self.requires),
            "scope": self.scope,
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True)
class PairEvidence:
    """Landed evidence for ONE (mask token, check) pair.

    ``witness``/``proof`` both ``None`` means NOT LANDED, which is exactly the
    unsatisfied semantics a bare gates list has always carried — so converting a
    list to the map form changes no verdict until a pair actually gains
    evidence.
    """

    check_id: str
    witness: str | None = None
    proof: str | None = None
    #: Why this pair's evidence is not usable, when it declared some. A defect
    #: is per PAIR: it masks its own check with a named reason and never
    #: invalidates the file, so one bad line cannot stop 303 other pairs from
    #: grading. Structural corruption of the document itself still refuses.
    defect: str | None = None

    @property
    def landed(self) -> bool:
        """Declared evidence that is also USABLE. A defective pair is not landed."""
        return self.witness is not None and self.proof is not None and self.defect is None

    @property
    def declared(self) -> bool:
        """Evidence was written down, whether or not it holds up."""
        return self.witness is not None or self.proof is not None


@dataclass(frozen=True)
class MaskEvidence:
    """Sticky obligation for every check named in one mask token's gates.

    ``witness``/``proof`` are TOKEN-wide evidence (``writer:`` tokens, whose
    capability lands once for every check they gate). ``pairs`` is the v3
    per-(token, check) form used by non-writer tokens: one glue/binding token
    masks a hundred unrelated checks, and one landed leg discharges exactly the
    check it was built for — never the other ninety-nine.
    """

    gates: tuple[str, ...]
    witness: str | None = None
    proof: str | None = None
    pairs: tuple[PairEvidence, ...] = ()

    def pair_for(self, check_id: str) -> PairEvidence | None:
        return next((pair for pair in self.pairs if pair.check_id == check_id), None)


# Public compatibility alias: round-2 tests and callers imported this name.
WriterEvidence = MaskEvidence


@dataclass(frozen=True)
class JUnitOutcome:
    nodeid: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class CheckResult:
    check: ManifestCheck
    verdict: CheckVerdict
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check.id,
            "capability": self.check.capability,
            "gate": self.check.gate,
            "scope": self.check.scope,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecordSummary:
    run_id: str
    tier: str
    verdict: RunVerdict
    results: tuple[CheckResult, ...]
    receipt_path: str
    pulse: dict[str, float]
    #: Whether this run loaded and history-validated the non-empty mask registry
    #: for every governed requirement prefix observed in manifest history.
    #: This is emitted in the result JSON so a path override can never make a
    #: silently ungated PASS look like a genuinely mask-gated PASS. The field
    #: name is retained for result-schema compatibility with registry v1.
    writer_gating_active: bool = False
    #: HARD GATES the run never executed (see :func:`deselected_hard_gates`).
    #: Observability only — the verdict algebra already renders these
    #: INCONCLUSIVE — but it is what makes a SILENT deselection nameable in the
    #: cadence log instead of arriving as an anonymous NOT_EVALUABLE row.
    deselected_gates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verdict"] = self.verdict.value
        payload["results"] = [result.to_dict() for result in self.results]
        payload["deselected_gates"] = list(self.deselected_gates)
        return payload


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def vacuous_bindings(checks: Iterable[ManifestCheck]) -> dict[str, str]:
    """Return ``target -> reason detail`` for every vacuously bound pytest carrier.

    A binding is vacuous when one pytest target is made to stand for more claims
    than it can examine: more than ``MAX_BINDERS_PER_TARGET`` checks on one target,
    or exactly two checks that did not both declare ``shared: true``.  This is the
    defect that let ~212 manifest entries inherit one carrier and render a wall of
    PASS from a single testcase, so it is keyed on the SHAPE (how many checks bind
    the target, and whether the sharing was declared) — never on an anchor name,
    a target spelling, or where in the file the binding came from.
    """

    binders: dict[str, list[ManifestCheck]] = {}
    for check in checks:
        if check.binding_type == "pytest":
            binders.setdefault(check.target, []).append(check)
    vacuous: dict[str, str] = {}
    for target, group in binders.items():
        if len(group) > MAX_BINDERS_PER_TARGET:
            vacuous[target] = f"target_bound_by_{len(group)}_checks"
        elif len(group) > 1 and not all(check.shared for check in group):
            vacuous[target] = "shared_not_declared"
    return vacuous


def _load_manifest(path: Path, tier: str) -> tuple[list[ManifestCheck], int, str, dict[str, str]]:
    try:
        raw = path.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise CertificationError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("checks"), list):
        raise CertificationError("manifest must contain a checks list")
    if "version" not in document:
        # A missing version is an UNKNOWN suite identity, not a healthy v1. The
        # defaulted read is exactly the favourable absence that let a manifest
        # content change ship without a bump and VOIDed the estate: every later
        # run silently claimed to be the same suite as every earlier one.
        raise CertificationError(
            "manifest must declare an explicit integer version (e.g. `version: 1`); "
            "a missing version is an unknown suite identity, not v1"
        )
    version = document["version"]
    if type(version) is not int or version < 1:
        raise CertificationError("manifest version must be a positive integer")

    every: list[ManifestCheck] = []
    seen: set[str] = set()
    for index, item in enumerate(document["checks"]):
        if not isinstance(item, dict) or not _REQUIRED_FIELDS <= item.keys():
            raise CertificationError(f"manifest checks[{index}] lacks the fixed entry schema")
        binding = item["binding"]
        requires = item["requires"]
        provenance = item["provenance"]
        if (
            not isinstance(binding, dict)
            or not isinstance(binding.get("type"), str)
            or not isinstance(binding.get("target"), str)
            or not isinstance(requires, list)
            or not all(isinstance(value, str) for value in requires)
            or not isinstance(provenance, list)
            or not all(isinstance(value, str) for value in provenance)
            or type(binding.get("shared", False)) is not bool
        ):
            raise CertificationError(f"manifest checks[{index}] has invalid binding metadata")
        check_id = str(item["id"])
        if not check_id or check_id in seen:
            raise CertificationError(f"manifest check id is blank or duplicated: {check_id!r}")
        seen.add(check_id)
        every.append(
            ManifestCheck(
                id=check_id,
                capability=str(item["capability"]),
                binding_type=binding["type"],
                target=binding["target"],
                tier=str(item["tier"]),
                gate=bool(item["gate"]),
                requires=tuple(requires),
                scope=str(item["scope"]),
                provenance=tuple(provenance),
                shared=binding.get("shared", False),
            )
        )
    # The census spans the WHOLE manifest, not the selected tier: a carrier shared
    # across tiers is just as vacuous, and a tier-local count would let the defect
    # hide by splitting its binders across t1/t2/t3.
    vacuous = vacuous_bindings(every)
    selected = [check for check in every if check.tier == tier]
    if not selected:
        raise CertificationError(f"manifest selected zero checks for tier {tier!r}")
    return selected, version, hashlib.sha256(raw).hexdigest(), vacuous


def _python_tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None


def _repo_python_path(raw_path: str, repo_root: Path) -> Path | None:
    """Resolve a registry path inside *repo_root*, never outside it."""

    root = repo_root.resolve()
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix != ".py" or not candidate.is_file():
        return None
    return candidate


def _named_child(node: ast.AST, name: str) -> ast.AST | None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if child.name == name:
                return child
        elif isinstance(child, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in child.targets):
                return child
        elif isinstance(child, ast.AnnAssign):
            if isinstance(child.target, ast.Name) and child.target.id == name:
                return child
        elif isinstance(child, ast.Import | ast.ImportFrom):
            if any(
                (alias.asname or alias.name.rsplit(".", 1)[-1]) == name for alias in child.names
            ):
                return child
    return None


def _witness_resolves(witness: str | None, repo_root: Path) -> bool:
    """Probe a Python module or direct symbol named by ``path.py[::Symbol]``.

    A null, malformed, escaping, unreadable, or syntactically invalid witness
    fails closed. Nested symbols may be written as ``Outer.Inner`` or as
    ``Outer::Inner``; each component must be a direct AST child of the previous
    one, so a textual mention or comment cannot satisfy the probe.
    """

    if not witness or not isinstance(witness, str):
        return False
    raw_path, separator, raw_symbol = witness.partition("::")
    path = _repo_python_path(raw_path, repo_root)
    if path is None:
        return False
    tree = _python_tree(path)
    if tree is None:
        return False
    if not separator:
        return True
    parts = [part for chunk in raw_symbol.split("::") for part in chunk.split(".") if part]
    if not parts:
        return False
    node: ast.AST = tree
    for part in parts:
        found = _named_child(node, part)
        if found is None:
            return False
        node = found
    return True


#: Directories that hold shipped, non-test code. A landed witness is only REAL
#: when something HERE consumes it; a proof (a test) is never a consumer. This is
#: the realness guard that used to live ONLY in the meta-test — porting it in
#: band is what stops a pure-stub witness (no production caller) from passing the
#: grader on its own, the stub/test-only/self-recursive-witness class.
PRODUCTION_ROOTS = ("omniagentos", "scripts", "pipeline")
#: AST of the production tree, cached per resolved repo root. One certification
#: run probes realness once per landed witness; re-parsing the whole tree per
#: pair would be the only slow thing this module does. Files do not change within
#: a run, so the cache is never stale for the process that populated it.
_PRODUCTION_TREE_CACHE: dict[str, list[tuple[Path, ast.Module]]] = {}


def _production_trees(repo_root: Path) -> list[tuple[Path, ast.Module]]:
    """Every shipped, non-test module under *repo_root*, as ``(relative path, AST)``."""

    root = repo_root.resolve()
    key = str(root)
    cached = _PRODUCTION_TREE_CACHE.get(key)
    if cached is not None:
        return cached
    trees: list[tuple[Path, ast.Module]] = []
    for name in PRODUCTION_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            relative = path.relative_to(root)
            if "tests" in relative.parts or path.name.startswith("test_"):
                continue
            try:
                trees.append((relative, ast.parse(path.read_text(encoding="utf-8"))))
            except (OSError, SyntaxError, UnicodeError):
                continue
    _PRODUCTION_TREE_CACHE[key] = trees
    return trees


def _production_module_name(path: Path) -> str:
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _defines_symbol(tree: ast.AST, symbol: str) -> bool:
    return any(
        isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == symbol
        for node in ast.walk(tree)
    )


def _imports_the_symbol(tree: ast.AST, symbol: str, defining_modules: set[str]) -> bool:
    """Whether this module actually imported the witness (or its defining module).

    Without it, any same-named local helper anywhere in the tree would launder a
    witness into looking used.
    """

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in defining_modules and any(
                alias.name == symbol for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name in defining_modules for alias in node.names):
                return True
    return False


def _production_references(witness: str, repo_root: Path) -> list[str]:
    """Production sites that USE the witness — never its own definition, never itself.

    ``witness`` is ``path.py::Symbol`` (a registry witness) or a bare symbol
    name. Three laundering routes are closed here, because each one lets a
    witness with no consumer look consumed:

    * its own definition (a def is not a use);
    * anything INSIDE any definition of that same name — self-recursion is the
      cheapest fake caller there is;
    * a same-named symbol in a module that never imported the real one.
    """

    witness_path, _, raw_symbol = witness.partition("::")
    symbol = (raw_symbol or witness_path).split(".")[-1]
    trees = _production_trees(repo_root)
    if raw_symbol:
        defining_paths = {Path(witness_path)}
    else:  # bare-name mode: whatever defines it, minus same-name impostors below
        defining_paths = {path for path, tree in trees if _defines_symbol(tree, symbol)}
    defining_modules = {_production_module_name(path) for path in defining_paths}

    hits: list[str] = []
    for path, tree in trees:
        # Only a witness given by PATH has an unambiguous defining module whose
        # in-module uses can be trusted. With a bare name, several modules may
        # define it and each one's references resolve to its OWN definition, so
        # nothing local counts and only imports do.
        own = bool(raw_symbol) and path in defining_paths
        if not own:
            # Another module counts only if it IMPORTED this witness, and only
            # if it does not shadow the name with a definition of its own.
            if _defines_symbol(tree, symbol) or not _imports_the_symbol(
                tree, symbol, defining_modules
            ):
                continue
        shadowed: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == symbol
            ):
                shadowed.update(id(child) for child in ast.walk(node))
        for node in ast.walk(tree):
            if id(node) in shadowed:
                continue
            if (isinstance(node, ast.Name) and node.id == symbol) or (
                isinstance(node, ast.Attribute) and node.attr == symbol
            ):
                hits.append(f"{path}:{node.lineno}")
    return hits


def _witness_has_production_reference(witness: str | None, repo_root: Path) -> bool:
    """A landed witness must be consumed by real production code, not only a proof.

    Fails closed on a null/malformed witness: realness must never be the check
    that lets one of those slip through, even though ``_witness_resolves`` would
    already have refused it.
    """

    if not witness or not isinstance(witness, str):
        return False
    return bool(_production_references(witness, repo_root))


def _proof_marker_token(decorator: ast.expr) -> str | None:
    """Return the literal token from ``@pytest.mark.nsc_writer_proof(...)``."""

    if not isinstance(decorator, ast.Call) or len(decorator.args) != 1 or decorator.keywords:
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute) or func.attr != "nsc_writer_proof":
        return None
    mark = func.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return None
    if not isinstance(mark.value, ast.Name) or mark.value.id != "pytest":
        return None
    value = decorator.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _proof_declares_token(proof: str, token: str, repo_root: Path) -> bool:
    """Verify that a proof node exists and self-declares its exact writer token."""

    raw_path, separator, raw_parts = proof.partition("::")
    if not separator:
        return False
    path = _repo_python_path(raw_path, repo_root)
    if path is None:
        return False
    tree = _python_tree(path)
    if tree is None:
        return False
    parts = [part.partition("[")[0] for part in raw_parts.split("::") if part]
    if not parts:
        return False
    node: ast.AST = tree
    for part in parts:
        found = _named_child(node, part)
        if found is None:
            return False
        node = found
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    return any(_proof_marker_token(decorator) == token for decorator in node.decorator_list)


def _proof_declares_pair(proof: str, token: str, check_id: str, repo_root: Path) -> bool:
    """Verify a per-pair proof self-declares its exact ``token@check`` pair.

    Deliberately NOT the bare token: one glue token masks a hundred unrelated
    checks, so a proof that declared only ``glue:fold-legs-pending`` would read
    as evidence for every one of them. The pair spelling makes a fold leg
    discharge exactly the check it was built for.
    """

    return _proof_declares_token(proof, f"{token}@{check_id}", repo_root)


def _proof_exercises_witness(proof: str, witness: str | None, repo_root: Path) -> bool:
    """Statically bind a proof test to the writer symbol it corroborates.

    A marker on ``def test(): pass`` is only a self-assertion.  A plausible
    proof must import the witness's own module/symbol and use that imported
    symbol inside a non-bare assertion or call. This is a STRUCTURAL LOWER
    BOUND, not a full semantic guarantee: it cannot prove that the assertion is
    adequate for the capability or that the witness was newly landed by the
    reviewed change. Cross-lineage review of manifest.yaml/writers.yaml diffs
    backstops that residual; this is a lower-bound + review model.
    """

    if witness is None:
        return False
    witness_path_raw, witness_separator, witness_symbol_raw = witness.partition("::")
    if not witness_separator:
        return False
    witness_parts = [
        part for chunk in witness_symbol_raw.split("::") for part in chunk.split(".") if part
    ]
    if not witness_parts:
        return False
    witness_symbol = witness_parts[0]
    witness_path = _repo_python_path(witness_path_raw, repo_root)
    if witness_path is None:
        return False

    proof_path_raw, separator, raw_parts = proof.partition("::")
    if not separator:
        return False
    proof_path = _repo_python_path(proof_path_raw, repo_root)
    if proof_path is None:
        return False
    tree = _python_tree(proof_path)
    if tree is None:
        return False
    parts = [part.partition("[")[0] for part in raw_parts.split("::") if part]
    node: ast.AST = tree
    for part in parts:
        found = _named_child(node, part)
        if found is None:
            return False
        node = found
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False

    root = repo_root.resolve()
    module_parts = witness_path.relative_to(root).with_suffix("").parts
    if module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]
    module = ".".join(module_parts)
    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == module:
            for alias in statement.names:
                if alias.name == witness_symbol:
                    imported_names.add(alias.asname or alias.name)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == module:
                    imported_modules.add(alias.asname or alias.name.split(".", 1)[0])

    # A fixture parameter named like the witness SHADOWS the import inside the
    # body: a reference to that name is the parameter, not the production symbol,
    # so it must not be credited as exercising the witness. Only the simple name
    # can be shadowed this way — ``module.Witness`` still names the real symbol.
    shadow = _shadowing_parameter_names(node)
    imported_names = imported_names - shadow

    for operation in _live_nodes(node):
        if not isinstance(operation, ast.Assert | ast.Call):
            continue
        # ``assert ImportedWitness`` only proves that Python bound a truthy
        # object. It exercises no behavior and is the exact vacuous F2 shape.
        if isinstance(operation, ast.Assert) and isinstance(operation.test, ast.Name):
            continue
        if _substantive_witness_use(
            operation,
            imported_names=imported_names,
            imported_modules=imported_modules,
            witness_symbol=witness_symbol,
        ):
            return True
    return False


def _is_bare_witness_ref(
    node: ast.AST,
    *,
    imported_names: set[str],
    imported_modules: set[str],
    witness_symbol: str,
) -> bool:
    """The witness NAMED and nothing done to it: ``Witness`` or ``module.Witness``."""

    if isinstance(node, ast.Name):
        return node.id in imported_names
    if isinstance(node, ast.Attribute):
        return (
            node.attr == witness_symbol
            and isinstance(node.value, ast.Name)
            and node.value.id in imported_modules
        )
    return False


def _witness_expr_key(
    node: ast.AST,
    *,
    imported_names: set[str],
    imported_modules: set[str],
    witness_symbol: str,
) -> tuple[str, ...] | None:
    """A canonical key for an expression rooted SOLELY at the witness, else ``None``.

    ``Witness`` and ``module.Witness`` both key to ``("W",)``; ``Witness.attr``
    and ``module.Witness.attr`` both to ``("W", ".attr")``. Two operands that
    share a key are the SAME witness-derived expression, so comparing them is a
    tautology that executes nothing — the R6 attribute shape ``Witness.a ==
    Witness.a`` that a bare-reference check alone missed (name-import plus an
    attribute read on both sides). Subscripts are deliberately NOT keyed: their
    index would have to match too, so they fall through to the ordinary
    substantive-read path and never to a false tautology.
    """

    if isinstance(node, ast.Name):
        return ("W",) if node.id in imported_names else None
    if isinstance(node, ast.Attribute):
        if (
            node.attr == witness_symbol
            and isinstance(node.value, ast.Name)
            and node.value.id in imported_modules
        ):
            return ("W",)
        base = _witness_expr_key(
            node.value,
            imported_names=imported_names,
            imported_modules=imported_modules,
            witness_symbol=witness_symbol,
        )
        if base is not None:
            return (*base, f".{node.attr}")
    return None


def _shadowing_parameter_names(func: ast.AST) -> set[str]:
    """Every parameter name of *func* (a fixture named like the witness shadows it)."""

    args = getattr(func, "args", None)
    if not isinstance(args, ast.arguments):
        return set()
    slots = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        slots.append(args.vararg)
    if args.kwarg is not None:
        slots.append(args.kwarg)
    return {slot.arg for slot in slots}


#: The expression is NOT something this module can fold to a pure-constant value.
_NOT_CONSTANT: Any = object()


def _fold_compare(op: ast.cmpop, left: Any, right: Any) -> Any:
    """One constant comparison, or ``_NOT_CONSTANT``. ``is``/``is not`` are refused
    because identity of equal literals is interpreter-defined, not a static fact."""

    try:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
    except TypeError:
        return _NOT_CONSTANT
    return _NOT_CONSTANT


def _fold_binop(op: ast.operator, left: Any, right: Any) -> Any:
    """Constant arithmetic that cannot make a small literal expensive.

    ``**`` and ``<<`` are refused (a 7-character ``2 ** 9**9`` is a memory bomb),
    and BOTH operands must be numbers so ``[0] * 10**9`` / ``"a" * 10**9`` can
    never be reached either. Every allowed op on two source-bounded numbers has a
    source-bounded result; a defensive bit-length cap catches anything else.
    """

    numbers = (int, float, complex)
    if not isinstance(left, numbers) or not isinstance(right, numbers):
        return _NOT_CONSTANT
    try:
        if isinstance(op, ast.Add):
            result: Any = left + right
        elif isinstance(op, ast.Sub):
            result = left - right
        elif isinstance(op, ast.Mult):
            result = left * right
        elif isinstance(op, ast.Div):
            result = left / right
        elif isinstance(op, ast.FloorDiv):
            result = left // right
        elif isinstance(op, ast.Mod):
            result = left % right
        elif isinstance(op, ast.BitAnd):
            result = left & right
        elif isinstance(op, ast.BitOr):
            result = left | right
        elif isinstance(op, ast.BitXor):
            result = left ^ right
        elif isinstance(op, ast.RShift):
            result = left >> right
        else:  # Pow, LShift, MatMult: blow-up prone or not defined on numbers
            return _NOT_CONSTANT
    except (TypeError, ValueError, ZeroDivisionError):
        return _NOT_CONSTANT
    if isinstance(result, int) and result.bit_length() > 65536:
        return _NOT_CONSTANT  # source-bounded ops never reach this; a bomb would
    return result


def _fold_constant(node: ast.AST) -> Any:
    """The VALUE of a pure-constant expression, or ``_NOT_CONSTANT`` the instant a
    runtime reference — a ``Name``/``Call``/``Attribute``/``Subscript``,
    comprehension, or any node outside this closed literal grammar — appears.

    An expression built only of literals, literal containers and
    unary/boolean/comparison/arithmetic operators over pure constants folds to its
    value; anything that could read runtime state is undecidable, and its branch
    then stays LIVE. This is the ONE construction that closes the constant-folding
    class: there is no operator-by-operator special-casing of reachability, only
    "all-literal → decide, any runtime reference → live".
    """

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        operand = _fold_constant(node.operand)
        if operand is _NOT_CONSTANT:
            return _NOT_CONSTANT
        try:
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.Invert):
                return ~operand
        except TypeError:
            return _NOT_CONSTANT
        return _NOT_CONSTANT
    if isinstance(node, ast.BoolOp):
        result = _NOT_CONSTANT
        for value in node.values:
            result = _fold_constant(value)
            if result is _NOT_CONSTANT:
                return _NOT_CONSTANT
            if (not result) if isinstance(node.op, ast.And) else bool(result):
                return result  # ``and`` stops at the first falsy, ``or`` at truthy
        return result
    if isinstance(node, ast.Compare):
        # Fold LAZILY left-to-right, exactly as :func:`_live_compare_operands`
        # walks reachability. A chained comparison is ``(a op b) and (b op c) …``,
        # so the FIRST constant-false pair decides the whole chain False without
        # inspecting the tail: ``0 > 1 > dummy()`` is False (``0 > 1`` settles it),
        # so ``if 0 > 1 > dummy(): witness()`` is dead and the witness is not
        # credited — eager-folding the tail would wrongly read that as undecidable
        # and credit the dead body. A runtime operand REACHED before the chain
        # settles is undecidable → live (the tail's own reachability is handled by
        # :func:`_live_compare_operands`).
        left = _fold_constant(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _fold_constant(comparator)
            if left is _NOT_CONSTANT or right is _NOT_CONSTANT:
                return _NOT_CONSTANT
            outcome = _fold_compare(op, left, right)
            if outcome is _NOT_CONSTANT:
                return _NOT_CONSTANT
            if not outcome:
                return False  # first false pair decides the chain; the tail is not read
            left = right
        return True
    if isinstance(node, ast.BinOp):
        left = _fold_constant(node.left)
        right = _fold_constant(node.right)
        if left is _NOT_CONSTANT or right is _NOT_CONSTANT:
            return _NOT_CONSTANT
        return _fold_binop(node.op, left, right)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        elements = _fold_elements(node.elts)
        if elements is _NOT_CONSTANT:
            return _NOT_CONSTANT
        if isinstance(node, ast.List):
            return elements
        if isinstance(node, ast.Tuple):
            return tuple(elements)
        try:
            return set(elements)
        except TypeError:
            return _NOT_CONSTANT
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):  # ``{**a}`` unpacking
            return _NOT_CONSTANT
        mapping: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            key = _fold_constant(key_node)
            value = _fold_constant(value_node)
            if key is _NOT_CONSTANT or value is _NOT_CONSTANT:
                return _NOT_CONSTANT
            try:
                mapping[key] = value
            except TypeError:  # an unhashable literal key
                return _NOT_CONSTANT
        return mapping
    if isinstance(node, ast.IfExp):
        test = _fold_constant(node.test)
        if test is _NOT_CONSTANT:
            return _NOT_CONSTANT
        return _fold_constant(node.body if test else node.orelse)
    return _NOT_CONSTANT  # Name, Call, Attribute, Subscript, comprehension, ...


def _fold_elements(elements: list[ast.expr]) -> Any:
    folded: list[Any] = []
    for element in elements:
        if isinstance(element, ast.Starred):
            return _NOT_CONSTANT  # ``[*a]``: leave the whole display undecidable
        value = _fold_constant(element)
        if value is _NOT_CONSTANT:
            return _NOT_CONSTANT
        folded.append(value)
    return folded


def _constant_condition(test: ast.AST) -> bool | None:
    """Static truthiness of *test*, or ``None`` when it is not statically decidable.

    ONE invariant, no operator enumeration: a test decides only when it is a pure
    constant. Truthiness is propagated through the short-circuit operators (an
    ``and`` with any provably-falsy operand is falsy however the rest is spelled —
    even ``x and False`` — and an ``or`` with a provably-truthy operand is truthy),
    a container display decides by whether it is non-empty (its element VALUES do
    not matter to its truthiness, so ``if [witness()]`` stays truthy and the
    ``witness()`` in the test is still walked as live), and everything else is
    handed to the pure-constant value folder. The instant a runtime reference
    appears anywhere the fold is abandoned and the branch is LIVE — the residual
    the writers.yaml header names for Class-A review.
    """

    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _constant_condition(test.operand)
        return None if inner is None else (not inner)
    if isinstance(test, ast.BoolOp):
        truths = [_constant_condition(value) for value in test.values]
        if isinstance(test.op, ast.And):
            if any(truth is False for truth in truths):
                return False  # any falsy operand makes the whole ``and`` falsy
            return True if all(truth is True for truth in truths) else None
        if any(truth is True for truth in truths):
            return True  # any truthy operand makes the whole ``or`` truthy
        return False if all(truth is False for truth in truths) else None
    if isinstance(test, ast.List | ast.Tuple | ast.Set):
        if any(isinstance(element, ast.Starred) for element in test.elts):
            return None  # ``[*a]`` could be empty at runtime
        return bool(test.elts)
    if isinstance(test, ast.Dict):
        if any(key is None for key in test.keys):  # ``{**a}`` unpacking
            return None
        return bool(test.keys)
    try:
        value = _fold_constant(test)
    except Exception:  # noqa: BLE001 - a pathological literal is simply undecidable
        return None
    if value is _NOT_CONSTANT:
        return None
    return bool(value)


def _live_boolop_operands(node: ast.BoolOp) -> list[ast.AST] | None:
    """Operands of a short-circuiting ``and``/``or`` that actually run, or ``None``
    when no CONSTANT forces a short circuit.

    ``and`` stops at the first falsy operand, ``or`` at the first truthy one;
    every operand up to and INCLUDING the short-circuiting one is evaluated, and
    everything after it is dead — so ``False and witness()`` never runs
    ``witness()`` and ``True or witness()`` never runs it either.
    """

    short_circuit = False if isinstance(node.op, ast.And) else True
    live: list[ast.AST] = []
    for operand in node.values:
        live.append(operand)  # this operand runs before any short circuit
        if _constant_condition(operand) is short_circuit:
            return live  # a constant short-circuits here; the rest is dead
    return None  # no constant short circuit: every operand may run, walk them all


def _live_compare_operands(node: ast.Compare) -> list[ast.AST] | None:
    """Operands of a chained comparison that actually run, or ``None`` when no
    CONSTANT settles a pair.

    ``a < b < c`` is ``(a < b) and (b < c)``: the chain stops at the first pair a
    constant proves false, so ``0 > 1 > witness()`` evaluates ``0`` and ``1``,
    finds ``0 > 1`` false, and never runs ``witness()``. The instant a pair is
    undecidable (a runtime operand), the rest may run and everything stays live —
    ``1 < 2 < witness()`` reaches and runs ``witness()``.
    """

    live: list[ast.AST] = [node.left]
    left = _fold_constant(node.left)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        live.append(comparator)  # both sides of a pair are evaluated before the test
        right = _fold_constant(comparator)
        if left is _NOT_CONSTANT or right is _NOT_CONSTANT:
            return None  # a runtime operand: this pair and the rest may run
        outcome = _fold_compare(op, left, right)
        if outcome is _NOT_CONSTANT:
            return None
        if not outcome:
            return live  # this pair is false: the chain short-circuits, the tail is dead
        left = right
    return None  # every pair decidably true: no short circuit, walk them all


def _live_children(node: ast.AST) -> list[ast.AST] | None:
    """The children of *node* that are actually EVALUATED, or ``None`` when
    nothing is statically decidable and every child must be walked.

    ONE place for the whole statically-dead-code class, so a new control-flow
    shape is enumerated here once rather than patched into both walkers. The
    deciding sub-expression is ALWAYS live — an ``if``/``while`` test, a
    ``for``/comprehension iterable and the operands of ``and``/``or`` are
    evaluated even when they decide a branch away, and a container display like
    ``[witness()]`` still runs its elements — so only the branch a CONSTANT
    proves unreachable is dropped. A shape this cannot decide credits its whole
    subtree: it NEVER refuses a real, data-dependent live path (``if runtime:``,
    ``for x in runtime:`` are always walked), which is why the prune is framed as
    "drop the provably-dead" and not "keep only the provably-reachable" — the
    latter would over-refuse legitimate witnesses. The undecidable residual is
    the writers.yaml header's named Class-A backstop.
    """

    if isinstance(node, ast.If):
        constant = _constant_condition(node.test)
        if constant is True:
            return [node.test, *node.body]
        if constant is False:
            return [node.test, *node.orelse]
        return None
    if isinstance(node, ast.IfExp):
        constant = _constant_condition(node.test)
        if constant is True:
            return [node.test, node.body]
        if constant is False:
            return [node.test, node.orelse]
        return None
    if isinstance(node, ast.While):
        if _constant_condition(node.test) is False:
            # Body never runs; ``while False`` completes normally, so ``else`` does.
            return [node.test, *node.orelse]
        return None
    if isinstance(node, ast.For | ast.AsyncFor):
        if _constant_condition(node.iter) is False:
            # A statically empty iterable: the body never runs; the ``else`` does.
            return [node.iter, *node.orelse]
        return None
    if isinstance(node, ast.BoolOp):
        return _live_boolop_operands(node)
    if isinstance(node, ast.Compare):
        return _live_compare_operands(node)
    if isinstance(node, ast.Assert):
        # ``assert test, msg`` evaluates ``msg`` ONLY when ``test`` is falsy (to
        # build the AssertionError), so a statically-true test makes the message
        # dead: ``assert True, witness()`` never runs ``witness()``.
        if node.msg is not None and _constant_condition(node.test) is True:
            return [node.test]
        return None
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp):
        live: list[ast.AST] = []
        for generator in node.generators:
            live.append(generator.iter)  # the source iterable is evaluated
            if _constant_condition(generator.iter) is False:
                return live  # empty source: the element and later clauses are dead
            live.extend(generator.ifs)  # the filters are evaluated per element
            if any(_constant_condition(condition) is False for condition in generator.ifs):
                return live  # a constant-false filter yields nothing: the element is dead
        return None
    return None


def _live_nodes(root: ast.AST) -> list[ast.AST]:
    """``ast.walk`` minus every subtree a constant condition proves unreachable.

    Uniform over the whole statically-dead-code class (see :func:`_live_children`):
    ``if False`` / ``if []`` bodies, ``witness() if 0 else X`` dead arms,
    ``while False`` and ``for _ in []`` bodies, ``False and witness()`` /
    ``True or witness()`` short-circuited operands, and comprehensions over an
    empty source all drop out here, so a dead witness call never reaches the
    live-node list and cannot be graded as executed.
    """

    live: list[ast.AST] = []
    pending: list[ast.AST] = [root]
    while pending:
        node = pending.pop()
        live.append(node)
        children = _live_children(node)
        pending.extend(children if children is not None else ast.iter_child_nodes(node))
    return live


def _substantive_witness_use(
    operation: ast.AST,
    *,
    imported_names: set[str],
    imported_modules: set[str],
    witness_symbol: str,
) -> bool:
    """Whether this assert/call does something WITH the witness, not merely to it.

    Substantive means the witness is CALLED, or its attributes/subscripts are
    read into the value being asserted. Merely MENTIONING the symbol is not
    evidence: ``assert witness == witness`` mentions it twice, executes none of
    it, and is true for any object Python can bind — the vacuous shape a proof
    can always reach for when the capability does not actually exist.

    Statically dead code is pruned by the SAME reachability rule the live-node
    walk uses (:func:`_live_children`), so a witness call under ``if False`` /
    ``while False`` / ``for _ in []`` / ``False and ...`` / an empty comprehension
    is not execution; and a comparison whose operands are the same
    witness-derived expression is a tautology that reads nothing.
    """

    bare = {
        "imported_names": imported_names,
        "imported_modules": imported_modules,
        "witness_symbol": witness_symbol,
    }
    pending: list[ast.AST] = [operation]
    while pending:
        node = pending.pop()
        children = _live_children(node)
        if children is not None:
            pending.extend(children)  # only the reachable sub-expressions run
            continue
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            keys = [_witness_expr_key(operand, **bare) for operand in operands]
            if all(key is not None for key in keys) and len(set(keys)) == 1:
                # X == X, X is X, X.a == X.a ...: a tautology about identity that
                # reads no state the comparison did not already put on both sides.
                # Keyed on the witness-derived EXPRESSION, so the attribute shape
                # ``Witness.attr == Witness.attr`` (name-import plus attribute) is
                # pruned too, not only the bare ``Witness == Witness``.
                continue
        if isinstance(node, ast.Call) and _is_bare_witness_ref(node.func, **bare):
            return True  # the witness was EXECUTED
        if isinstance(node, ast.Attribute | ast.Subscript) and _is_bare_witness_ref(
            node.value, **bare
        ):
            return True  # its state was READ into the asserted value
        pending.extend(ast.iter_child_nodes(node))
    return False


def _canonical_nodeid(nodeid: str) -> str:
    """Normalize pytest parameter suffixes for proof/carrier alias checks."""

    return "::".join(part.partition("[")[0] for part in nodeid.replace("\\", "/").split("::"))


def _manifest_targets(path: Path) -> dict[str, str]:
    """Return every manifest check id's carrier for registry distinctness."""

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CertificationError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("checks"), list):
        raise CertificationError("manifest must contain a checks list")
    targets: dict[str, str] = {}
    for index, item in enumerate(document["checks"]):
        binding = item.get("binding") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(binding, dict)
            or not isinstance(binding.get("target"), str)
        ):
            raise CertificationError(f"manifest checks[{index}] has invalid binding metadata")
        targets[item["id"]] = binding["target"]
    return targets


#: Registry schema version that introduced per-(token, check) evidence for
#: NON-writer tokens. Older documents keep the bare gates list, which loads
#: unchanged and means exactly what it always meant: nothing is landed.
PER_CHECK_REGISTRY_VERSION = 3


def _load_pair_entry(
    token: str,
    raw_entry: Mapping[str, Any],
    *,
    version: int,
    repo_root: Path,
    manifest_targets: Mapping[str, str] | None,
) -> MaskEvidence:
    """Load and validate one v3 map-form (non-writer) registry entry.

    Every rule the token-wide writer form enforces is enforced here PER PAIR:
    witness and proof land together or not at all, the witness must resolve,
    the proof must self-declare its own ``token@check`` pair, must exercise the
    witness, and must never be the gated carrier itself. A pair that fails any
    of them is a registry error, not a quietly unsatisfied pair — the sticky
    obligation stays either way, but a half-landed pair must be loud.
    """

    if version < PER_CHECK_REGISTRY_VERSION:
        raise CertificationError(
            f"mask registry entry {token!r} uses per-check gates, which require "
            f"version {PER_CHECK_REGISTRY_VERSION} (document declares {version})"
        )
    if token.startswith(WRITER_REQUIREMENT_PREFIX):
        raise CertificationError(
            f"mask registry entry {token!r} is a writer token: writer evidence is "
            "token-wide, so its gates stay a list"
        )
    if set(raw_entry) - {"gates"}:
        raise CertificationError(
            f"mask registry entry {token!r} carries per-check gates, so token-wide "
            "witness/proof fields are not allowed"
        )
    raw_gates = raw_entry["gates"]
    if not raw_gates:
        raise CertificationError(f"writer registry entry {token!r} has invalid evidence")
    pairs: list[PairEvidence] = []
    for check_id, raw_pair in raw_gates.items():
        if not isinstance(check_id, str) or not check_id:
            raise CertificationError(f"writer registry entry {token!r} has invalid evidence")
        if raw_pair is None:
            raw_pair = {}
        if not isinstance(raw_pair, dict) or set(raw_pair) - {"witness", "proof"}:
            raise CertificationError(f"mask registry pair {token!r}@{check_id} has invalid fields")
        witness = raw_pair.get("witness")
        proof = raw_pair.get("proof")
        if (witness is not None and (not isinstance(witness, str) or not witness)) or (
            proof is not None and (not isinstance(proof, str) or not proof)
        ):
            raise CertificationError(
                f"mask registry pair {token!r}@{check_id} has invalid evidence"
            )
        # From here the failures are EVIDENCE defects, and evidence is per pair.
        # Raising would let one operator's bad line take the whole file down —
        # and with it every OTHER pair's ability to grade, which is exactly the
        # blast radius this per-pair design exists to remove. A defective pair
        # is recorded as INVALID: it renders as unsatisfied for its own check,
        # with the reason named, and nothing else changes.
        defect: str | None = None
        if (witness is None) != (proof is None):
            defect = "witness and proof must be declared together"
        elif witness is not None:
            if not _witness_resolves(witness, repo_root):
                defect = f"witness {witness!r} does not resolve"
            elif not _proof_declares_pair(proof, token, check_id, repo_root):
                defect = f"proof {proof!r} does not self-declare {token}@{check_id}"
            elif not _proof_exercises_witness(proof, witness, repo_root):
                defect = f"proof {proof!r} does not exercise witness {witness!r}"
            elif (
                manifest_targets is not None
                and check_id in manifest_targets
                and _canonical_nodeid(proof) == _canonical_nodeid(manifest_targets[check_id])
            ):
                defect = f"proof {proof!r} aliases the gated carrier"
            elif not _witness_has_production_reference(witness, repo_root):
                # The witness resolves and the proof exercises it, but nothing in
                # SHIPPED code consumes it: a metric-shaped stub whose only caller
                # is its own proof. Refuse it per pair, never as a PASS.
                defect = f"witness {witness!r} has no production reference"
        pairs.append(PairEvidence(check_id, witness=witness, proof=proof, defect=defect))
    gates = tuple(pair.check_id for pair in pairs)
    if len(gates) != len(set(gates)):
        raise CertificationError(f"writer registry entry {token!r} has invalid evidence")
    if manifest_targets is not None:
        unknown_gates = sorted(set(gates) - set(manifest_targets))
        if unknown_gates:
            raise CertificationError(
                f"writer registry entry {token!r} names unknown gates {unknown_gates}"
            )
    return MaskEvidence(gates, pairs=tuple(pairs))


def _load_writer_registry(
    path: Path,
    *,
    repo_root: Path,
    manifest_targets: Mapping[str, str] | None = None,
) -> dict[str, MaskEvidence]:
    """Load the sticky mask reverse-index and validate writer proof provenance."""

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CertificationError(f"cannot read writer registry {path}: {exc}") from exc
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] < 1
    ):
        raise CertificationError("writer registry must contain a positive integer version")
    raw_tokens = document.get("tokens")
    if not isinstance(raw_tokens, dict):
        raise CertificationError("writer registry must contain a tokens mapping")

    version = int(document["version"])
    registry: dict[str, MaskEvidence] = {}
    for token, raw_entry in raw_tokens.items():
        if not _is_mask_token(token):
            raise CertificationError(f"mask registry has invalid token {token!r}")
        if not isinstance(raw_entry, dict) or set(raw_entry) - {"gates", "witness", "proof"}:
            raise CertificationError(f"writer registry entry {token!r} has invalid fields")
        if isinstance(raw_entry.get("gates"), dict):
            registry[token] = _load_pair_entry(
                token,
                raw_entry,
                version=version,
                repo_root=repo_root,
                manifest_targets=manifest_targets,
            )
            continue
        gates = raw_entry.get("gates")
        witness = raw_entry.get("witness")
        proof = raw_entry.get("proof")
        if (
            not isinstance(gates, list)
            or not gates
            or not all(isinstance(gate, str) and gate for gate in gates)
            or len(gates) != len(set(gates))
            or (witness is not None and (not isinstance(witness, str) or not witness))
            or (proof is not None and (not isinstance(proof, str) or not proof))
        ):
            raise CertificationError(f"writer registry entry {token!r} has invalid evidence")
        if (witness is None) != (proof is None):
            raise CertificationError(
                f"writer registry entry {token!r} must declare witness and proof together"
            )
        if not token.startswith(WRITER_REQUIREMENT_PREFIX) and witness is not None:
            raise CertificationError(
                f"mask registry entry {token!r} must use its designed requirement satisfier, "
                "not writer witness/proof evidence"
            )
        if witness is not None and not _witness_resolves(witness, repo_root):
            raise CertificationError(
                f"writer registry witness {witness!r} for {token!r} does not resolve"
            )
        if proof is not None and not _proof_declares_token(proof, token, repo_root):
            raise CertificationError(
                f"writer registry proof {proof!r} does not self-declare {token!r}"
            )
        if proof is not None and not _proof_exercises_witness(proof, witness, repo_root):
            raise CertificationError(
                f"writer registry proof {proof!r} does not exercise witness {witness!r}"
            )
        if manifest_targets is not None:
            unknown_gates = sorted(set(gates) - set(manifest_targets))
            if unknown_gates:
                raise CertificationError(
                    f"writer registry entry {token!r} names unknown gates {unknown_gates}"
                )
        if proof is not None and manifest_targets is not None:
            proof_node = _canonical_nodeid(proof)
            aliases = sorted(
                gate
                for gate in gates
                if gate in manifest_targets
                and _canonical_nodeid(manifest_targets[gate]) == proof_node
            )
            if aliases:
                raise CertificationError(
                    f"writer registry proof {proof!r} aliases gated carrier(s) {aliases}"
                )
        if witness is not None and not _witness_has_production_reference(witness, repo_root):
            # Token-wide evidence is held to the same realness bar as a pair: a
            # witness nothing in shipped code consumes is a stub, whatever its
            # proof does with it.
            raise CertificationError(
                f"writer registry witness {witness!r} for {token!r} has no production reference"
            )
        entry = MaskEvidence(tuple(gates), witness=witness, proof=proof)
        registry[token] = entry
    return registry


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CertificationError(f"writer registry git history unavailable: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise CertificationError(f"writer registry git history unavailable: {detail}")
    return completed.stdout


def _is_mask_token(value: object) -> bool:
    """Whether *value* has the future-proof ``prefix:rest`` mask shape."""

    if not isinstance(value, str):
        return False
    prefix, separator, rest = value.partition(":")
    return bool(prefix and separator and rest)


def _mask_obligations_in_document(text: str, *, source: str) -> dict[str, set[str]]:
    """Return the token -> check reverse index recorded by one YAML document."""

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CertificationError(
            f"cannot inspect historical mask tokens in {source}: {exc}"
        ) from exc
    obligations: dict[str, set[str]] = {}
    if not isinstance(document, dict):
        return obligations
    raw_tokens = document.get("tokens")
    if isinstance(raw_tokens, dict):
        for token, raw_entry in raw_tokens.items():
            if not _is_mask_token(token) or not isinstance(raw_entry, dict):
                continue
            gates = raw_entry.get("gates")
            # Both shapes are historical fact: v1/v2 wrote a list, v3 writes a
            # per-check map. A reader that understood only one of them would
            # read every pair in the other as DELETED — which is precisely the
            # accusation the sticky history check exists to make.
            if isinstance(gates, list):
                obligations.setdefault(token, set()).update(
                    gate for gate in gates if isinstance(gate, str) and gate
                )
            elif isinstance(gates, dict):
                obligations.setdefault(token, set()).update(
                    gate for gate in gates if isinstance(gate, str) and gate
                )
    checks = document.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("requires"), list)
            ):
                continue
            for requirement in item["requires"]:
                if _is_mask_token(requirement):
                    obligations.setdefault(requirement, set()).add(item["id"])
    return obligations


def _manifest_mask_obligations(path: Path) -> dict[str, set[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CertificationError(f"cannot read manifest {path}: {exc}") from exc
    return _mask_obligations_in_document(text, source=str(path))


def _assert_registry_covers_obligations(
    registry: Mapping[str, MaskEvidence], obligations: Mapping[str, set[str]]
) -> None:
    missing = sorted(
        f"{token}@{gate}"
        for token, gates in obligations.items()
        for gate in gates
        if token not in registry or gate not in registry[token].gates
    )
    if missing:
        raise CertificationError(
            "mask registry omits live token/check obligations: " + ",".join(missing)
        )


def _assert_registry_history_complete(
    registry: Mapping[str, MaskEvidence],
    *,
    registry_path: Path,
    manifest_path: Path,
    repo_root: Path,
) -> None:
    """Require every historical mask token/check pair to retain a live entry."""

    root = repo_root.resolve()
    relative_paths: list[str] = []
    for path in (registry_path, manifest_path):
        try:
            relative_paths.append(path.resolve().relative_to(root).as_posix())
        except ValueError as exc:
            raise CertificationError(
                f"writer registry git history unavailable: {path} is outside {root}"
            ) from exc
    if _git_output(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise CertificationError("writer registry git history unavailable: not a work tree")
    shallow = _git_output(root, "rev-parse", "--is-shallow-repository").strip() != "false"
    if shallow:
        try:
            unshallow = subprocess.run(
                ["git", "-C", str(root), "fetch", "--unshallow"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            unshallow_detail = str(exc)
        else:
            unshallow_detail = unshallow.stderr.strip() or unshallow.stdout.strip()
        shallow = _git_output(root, "rev-parse", "--is-shallow-repository").strip() != "false"
        if shallow and os.environ.get(SHALLOW_HISTORY_ACK_ENV) != "1":
            detail = unshallow_detail or "git fetch --unshallow did not complete history"
            raise CertificationError(
                "mask registry history incomplete: shallow repository; "
                f"fetch --unshallow failed ({detail}); set {SHALLOW_HISTORY_ACK_ENV}=1 "
                "to acknowledge validation against only the available history"
            )
        if shallow:
            print(
                "instrument_warning:mask_registry_history_incomplete:"
                f"acknowledged_by={SHALLOW_HISTORY_ACK_ENV}",
                file=sys.stderr,
            )

    # A non-zero return code of 1 is the normal detached-HEAD signal, not a git
    # failure. Include HEAD explicitly below so an unreferenced detached tip is
    # still part of the history boundary being validated.
    try:
        symbolic_ref = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CertificationError(f"mask registry git history unavailable: {exc}") from exc
    if symbolic_ref.returncode not in (0, 1):
        detail = symbolic_ref.stderr.strip() or symbolic_ref.stdout.strip() or "git command failed"
        raise CertificationError(f"mask registry git history unavailable: {detail}")

    revisions = list(
        dict.fromkeys(
            _git_output(root, "log", "HEAD", "--all", "--format=%H", "--", *relative_paths).split()
        )
    )
    if not revisions:
        raise CertificationError("writer registry git history unavailable: no tracked revisions")

    historical: dict[str, set[str]] = {}
    readable_documents = 0
    for revision in revisions:
        for relative in relative_paths:
            completed = subprocess.run(
                ["git", "-C", str(root), "show", f"{revision}:{relative}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                continue
            readable_documents += 1
            for token, gates in _mask_obligations_in_document(
                completed.stdout, source=f"{revision}:{relative}"
            ).items():
                historical.setdefault(token, set()).update(gates)
    if readable_documents == 0:
        raise CertificationError("writer registry git history unavailable: no readable documents")
    missing = sorted(
        f"{token}@{gate}"
        for token, gates in historical.items()
        for gate in gates
        if token not in registry or gate not in registry[token].gates
    )
    if missing:
        raise CertificationError(
            "mask registry deleted historical token/check entries: " + ",".join(missing)
        )


def _writer_registry_for_manifest(
    manifest_path: Path, *, repo_root: Path
) -> dict[str, MaskEvidence] | None:
    """Load and history-validate the manifest's sibling mask registry."""

    registry_path = manifest_path.with_name(DEFAULT_WRITER_REGISTRY.name)
    if registry_path.is_file():
        registry = _load_writer_registry(
            registry_path,
            repo_root=repo_root,
            manifest_targets=_manifest_targets(manifest_path),
        )
        _assert_registry_covers_obligations(registry, _manifest_mask_obligations(manifest_path))
        _assert_registry_history_complete(
            registry,
            registry_path=registry_path,
            manifest_path=manifest_path,
            repo_root=repo_root,
        )
        return registry
    canonical_manifest = (repo_root / DEFAULT_MANIFEST).resolve()
    if manifest_path.resolve() == canonical_manifest:
        raise CertificationError(f"canonical manifest requires writer registry {registry_path}")
    return None


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _nodeid(testcase: ET.Element, repo_root: Path) -> str:
    name = testcase.get("name") or ""
    classname = testcase.get("classname") or ""
    file_attr = (testcase.get("file") or "").replace("\\", "/")
    if file_attr:
        module = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
        classes = (
            classname[len(module) + 1 :].split(".")
            if module and classname.startswith(module + ".")
            else []
        )
        return "::".join([file_attr, *classes, name])
    parts = classname.split(".") if classname else []
    for index in range(len(parts), 0, -1):
        candidate = "/".join(parts[:index]) + ".py"
        if (repo_root / candidate).is_file():
            return "::".join([candidate, *parts[index:], name])
    if parts and parts[-1][:1].isupper():
        return "/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + name
    return ("/".join(parts) + ".py::" + name) if parts else name


def parse_junit(path: Path, *, repo_root: Path) -> list[JUnitOutcome]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CertificationError(f"cannot parse JUnit XML {path}: {exc}") from exc
    outcomes: list[JUnitOutcome] = []
    for testcase in (node for node in root.iter() if _tag(node) == "testcase"):
        status = "passed"
        message = ""
        for child in testcase:
            child_tag = _tag(child)
            if child_tag in {"failure", "error"}:
                status = child_tag
                message = (child.get("message") or (child.text or "").strip())[:500]
                break
            if child_tag == "skipped":
                status = "skipped"
                message = (child.get("message") or (child.text or "").strip())[:500]
        outcomes.append(JUnitOutcome(_nodeid(testcase, repo_root), status, message))
    return outcomes


def _matches(target: str, nodeid: str) -> bool:
    expected = target.replace("\\", "/")
    observed = nodeid.replace("\\", "/")
    return observed == expected or observed.startswith(expected + "[")


def _approved_label_paths() -> tuple[Path, ...]:
    """Where the approved-label record may be found, most specific first.

    The script's own checkout is tried before the process cwd so the answer does
    not change with the directory a runner happened to start in.
    """
    return (Path(__file__).resolve().parents[2] / LAUNCHD_APPROVED_FILE, LAUNCHD_APPROVED_FILE)


def _approved_launchd_labels() -> tuple[str, ...]:
    """Every ``label:`` in the approved-label record, or ``()`` if unreadable.

    Fail-soft is deliberate and NARROW: with no record, token resolution falls
    back to the exact token, which is the pre-existing behaviour. It never
    invents a label, and it never makes a check pass — an unresolved token still
    has to survive the launchctl probe below.
    """
    for path in _approved_label_paths():
        key = str(path)
        cached = _LAUNCHD_APPROVED_CACHE.get(key)
        if cached is None:
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                cached = ()
            else:
                cached = tuple(_walk_labels(document))
            _LAUNCHD_APPROVED_CACHE[key] = cached
        if cached:
            return cached
    return ()


def _walk_labels(node: Any) -> Iterable[str]:
    """Collect every ``label`` string in the document, whatever nests it.

    Keyed on the field rather than on one path into the file: the record has
    grown sections before, and a resolver that silently found nothing would
    reintroduce exactly the defect this function exists to close.
    """
    if isinstance(node, dict):
        label = node.get("label")
        if isinstance(label, str) and label:
            yield label
        for value in node.values():
            yield from _walk_labels(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_labels(value)


def _launchd_label_candidates(token: str) -> tuple[str, ...]:
    """The real labels a manifest ``launchd:<token>`` could mean, exact first.

    Manifest tokens are written short (``health-sentinel``) while the installed
    job carries the reverse-DNS label (``com.omniagentos.health-sentinel``),
    so probing the token literally could never observe the running job — and the
    resulting False is indistinguishable from a genuinely absent one (R1-010).
    Resolution is deterministic: the exact token, then every approved label
    ending in ``.<token>``, in the order the record lists them.
    """
    candidates = [token]
    suffix = f".{token}"
    candidates.extend(
        label
        for label in _approved_launchd_labels()
        if label != token and label.endswith(suffix) and _is_probeable_label(label)
    )
    return tuple(dict.fromkeys(candidates))


def _is_probeable_label(label: str) -> bool:
    return bool(label) and "/" not in label and not any(c.isspace() for c in label)


def _probe_launchd_label(label: str) -> bool:
    for domain in (f"gui/{os.getuid()}", "system"):
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, validated label
                [LAUNCHCTL, "print", f"{domain}/{label}"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False


def _launchd_label_loaded(token: str) -> bool:
    """Is the job a manifest ``launchd:<token>`` names loaded in this user's
    launchd domain (or the system domain)?

    PROBED, never assumed.  Every failure mode — no launchctl, a timeout, a
    malformed label — answers False, which leaves the check MASKED
    (NOT_EVALUABLE) instead of granting it a PASS it did not earn.  An
    unobservable prerequisite is not a satisfied prerequisite.

    The token is resolved to candidate labels first (see
    :func:`_launchd_label_candidates`); ANY candidate loaded satisfies it.
    """
    if not _is_probeable_label(token):
        return False
    cached = _LAUNCHD_PROBE_CACHE.get(token)
    if cached is not None:
        return cached
    loaded = any(_probe_launchd_label(label) for label in _launchd_label_candidates(token))
    _LAUNCHD_PROBE_CACHE[token] = loaded
    return loaded


def _missing_requirements(
    check: ManifestCheck, available: frozenset[str], repo_root: Path
) -> tuple[str, ...]:
    return tuple(
        requirement
        for requirement in check.requires
        if not _requirement_satisfied(requirement, available, repo_root)
    )


def _requirement_satisfied(requirement: str, available: frozenset[str], repo_root: Path) -> bool:
    """Use each token type's existing, genuine satisfaction mechanism."""

    if requirement in available:
        return True
    root = repo_root.resolve()
    if requirement.startswith(LAUNCHD_REQUIREMENT_PREFIX) and _launchd_label_loaded(
        requirement[len(LAUNCHD_REQUIREMENT_PREFIX) :]
    ):
        # A landed launchd mechanism unmasks itself on the next scheduled run:
        # without this, a repaired prerequisite could only be observed via a
        # manually supplied availability flag.
        return True
    if requirement.startswith("fs:"):
        candidate = (root / requirement[3:]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return candidate.exists()
    return False


def _pair_static_refusal(
    token: str,
    pair: PairEvidence,
    check: ManifestCheck,
    repo_root: Path,
) -> str | None:
    """The pair questions answerable WITHOUT a run: does this evidence exist and bind?

    Split out because target selection happens BEFORE any test executes: a
    statically-valid landed pair is what makes its check runnable and its proof
    worth collecting, and the run itself then decides whether the proof passed.
    """

    label = f"{token}@{check.id}"
    if not _witness_resolves(pair.witness, repo_root):
        return f"{NO_WRITER_EVIDENCE_REASON}:witness_absent:{label}"
    if not _witness_has_production_reference(pair.witness, repo_root):
        # Defense in depth for a registry handed in already-constructed (not via
        # the loader, which records this as a pair defect): a witness nothing in
        # shipped code consumes never makes its check runnable or PASS.
        return f"{NO_WRITER_EVIDENCE_REASON}:witness_not_production:{label}"
    if pair.proof is None:
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_absent:{label}"
    if not _proof_declares_pair(pair.proof, token, check.id, repo_root):
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_invalid:{label}"
    if not _proof_exercises_witness(pair.proof, pair.witness, repo_root):
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_unrelated:{label}"
    if _canonical_nodeid(pair.proof) == _canonical_nodeid(check.target):
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_aliases_carrier:{label}"
    return None


def _pair_evidence_refusal(
    token: str,
    pair: PairEvidence,
    check: ManifestCheck,
    outcomes: list[JUnitOutcome],
    repo_root: Path,
) -> str | None:
    """Why this landed pair does not discharge its check in THIS run, if it does not.

    Same five mechanical questions the writer path asks, asked per pair, and
    named per pair: the witness still resolves, the proof still self-declares
    this pair, still exercises the witness, is not the gated carrier wearing a
    second hat, and actually EXECUTED AND PASSED in this run's report. The last
    one is what makes evidence revert to unsatisfied when a leg is deleted or
    starts failing — there is no ratchet to PASS.
    """

    static = _pair_static_refusal(token, pair, check, repo_root)
    if static is not None:
        return static
    label = f"{token}@{check.id}"
    assert pair.proof is not None  # _pair_static_refusal rejects a null proof
    proof_outcomes = [outcome for outcome in outcomes if _matches(pair.proof, outcome.nodeid)]
    if not proof_outcomes:
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_not_executed:{label}"
    if any(outcome.status != "passed" for outcome in proof_outcomes):
        return f"{NO_WRITER_EVIDENCE_REASON}:proof_not_passing:{label}"
    return None


def _pair_evidence_status(
    check: ManifestCheck,
    outcomes: list[JUnitOutcome],
    registry: Mapping[str, MaskEvidence] | None,
    repo_root: Path,
) -> tuple[frozenset[str], dict[str, str]]:
    """``(tokens this check's own pair discharges, token -> named refusal)``.

    The satisfied set feeds the requirement pre-gate, so a check whose leg
    landed stops being masked without arming the token for the other
    ninety-nine checks it gates. The refusals feed the REASON: a pair that
    landed and then broke deserves to say so ("proof_not_passing"), not to be
    reported with the generic "nothing was ever built here" text.
    """

    if not registry:
        return frozenset(), {}
    satisfied: set[str] = set()
    refusals: dict[str, str] = {}
    for token, entry in registry.items():
        if token.startswith(WRITER_REQUIREMENT_PREFIX):
            continue
        pair = entry.pair_for(check.id)
        if pair is None:
            continue
        if pair.defect is not None:
            # Declared, unusable: name it against THIS pair and move on. The
            # other pairs of this token are untouched.
            refusals[token] = (
                f"{NO_WRITER_EVIDENCE_REASON}:pair_invalid:{token}@{check.id}:{pair.defect}"
            )
            continue
        if not pair.landed:
            continue
        reason = _pair_evidence_refusal(token, pair, check, outcomes, repo_root)
        if reason is None:
            satisfied.add(token)
        else:
            refusals[token] = reason
    return frozenset(satisfied), refusals


def _mask_evidence_refusal(
    check: ManifestCheck,
    outcomes: list[JUnitOutcome],
    registry: Mapping[str, MaskEvidence] | None,
    repo_root: Path,
    available_requirements: frozenset[str],
) -> str | None:
    """Explain the first unmet sticky mask obligation for *check*, if any.

    This reverse-index deliberately consults ``entry.gates`` and never
    ``check.requires``. A manifest-only token deletion therefore changes
    neither the obligation nor the verdict.
    """

    required_tokens = tuple(filter(_is_mask_token, check.requires))
    if not registry:
        # Preserve direct-evaluation compatibility for designed non-writer
        # satisfiers (notably launchd probes). A canonical capability run with
        # no registry is independently forced to overall VOID by record_results.
        return (
            MASK_REGISTRY_UNAVAILABLE_REASON
            if any(token.startswith(WRITER_REQUIREMENT_PREFIX) for token in required_tokens)
            else None
        )
    missing_entries = sorted(set(required_tokens) - set(registry))
    if missing_entries:
        return f"{NO_WRITER_EVIDENCE_REASON}:registry_entry_absent:{missing_entries[0]}"

    for token, entry in registry.items():
        if check.id not in entry.gates:
            continue
        if not token.startswith(WRITER_REQUIREMENT_PREFIX):
            pair = entry.pair_for(check.id)
            if pair is not None and pair.defect is not None:
                # Declared but unusable evidence, isolated to this pair.
                if _requirement_satisfied(token, available_requirements, repo_root):
                    continue
                return f"{NO_WRITER_EVIDENCE_REASON}:pair_invalid:{token}@{check.id}:{pair.defect}"
            if pair is not None and pair.landed:
                # This pair has landed evidence: hold it to the SAME mechanical
                # discipline a writer token gets. Operator arming stays an OR
                # (additive, never subtractive), so a broken proof can only make
                # a run more masked than the operator asked for, never less.
                reason = _pair_evidence_refusal(token, pair, check, outcomes, repo_root)
                if reason is None:
                    continue
                if _requirement_satisfied(token, available_requirements, repo_root):
                    continue
                return reason
            if not _requirement_satisfied(token, available_requirements, repo_root):
                return f"precondition_missing:{token}"
            continue
        if not _witness_resolves(entry.witness, repo_root):
            return f"{NO_WRITER_EVIDENCE_REASON}:witness_absent:{token}"
        if entry.proof is None:
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_absent:{token}"
        if not _proof_declares_token(entry.proof, token, repo_root):
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_invalid:{token}"
        if not _proof_exercises_witness(entry.proof, entry.witness, repo_root):
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_unrelated:{token}"
        if _canonical_nodeid(entry.proof) == _canonical_nodeid(check.target):
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_aliases_carrier:{token}"
        proof_outcomes = [outcome for outcome in outcomes if _matches(entry.proof, outcome.nodeid)]
        if not proof_outcomes:
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_not_executed:{token}"
        if any(outcome.status != "passed" for outcome in proof_outcomes):
            return f"{NO_WRITER_EVIDENCE_REASON}:proof_not_passing:{token}"
    return None


def evaluate_checks(
    checks: list[ManifestCheck],
    outcomes: list[JUnitOutcome],
    *,
    available_requirements: frozenset[str] = frozenset(),
    repo_root: Path,
    vacuous_targets: Mapping[str, str] | None = None,
    writer_registry: Mapping[str, WriterEvidence] | None = None,
) -> list[CheckResult]:
    """Adjudicate each manifest check against the JUnit outcomes.

    ``vacuous_targets`` carries the manifest-wide census (see
    :func:`vacuous_bindings`); the census of the checks actually handed in is
    always recomputed and unioned with it, so a caller that forgets to pass it
    still cannot buy a PASS out of a vacuous binding.
    """

    census = dict(vacuous_bindings(checks))
    census.update(vacuous_targets or {})
    results: list[CheckResult] = []
    for check in checks:
        # Instrument first: a check whose binding cannot attest it is VOID whatever
        # else is true of it.  Grading it would report the manifest defect as a
        # product verdict, which is the thing an instrument error must never do.
        detail = census.get(check.target) if check.binding_type == "pytest" else None
        if detail is not None:
            results.append(
                CheckResult(
                    check,
                    CheckVerdict.VOID,
                    f"{VACUOUS_BINDING_REASON}:{detail}:{check.target}",
                )
            )
            continue
        # A landed (token, check) pair satisfies that token FOR THIS CHECK only.
        # Union, never replacement: operator arming stays exactly as powerful as
        # it was, and a pair can only ever add satisfaction it proved.
        pair_satisfied, pair_refusals = _pair_evidence_status(
            check, outcomes, writer_registry, repo_root
        )
        effective_requirements = available_requirements | pair_satisfied
        missing = _missing_requirements(check, effective_requirements, repo_root)
        if missing:
            # A pair that LANDED and then broke names its own failure; only a
            # token nobody ever built evidence for gets the generic reason.
            named = next(
                (pair_refusals[token] for token in missing if token in pair_refusals), None
            )
            results.append(
                CheckResult(
                    check,
                    CheckVerdict.NOT_EVALUABLE,
                    named or "precondition_missing:" + ",".join(missing),
                )
            )
            continue
        if check.binding_type != "pytest":
            results.append(
                CheckResult(
                    check,
                    CheckVerdict.NOT_EVALUABLE,
                    f"no_writer_evidence:binding_not_executed:{check.binding_type}",
                )
            )
            continue
        matched = [outcome for outcome in outcomes if _matches(check.target, outcome.nodeid)]
        if not matched:
            # Mask satisfied, carrier expected to run, nothing in the JUnit for it:
            # the run simply never executed it.  Never a PASS, never silence.
            results.append(
                CheckResult(
                    check,
                    CheckVerdict.NOT_EVALUABLE,
                    f"{NOT_EXECUTED_REASON}:{check.target}",
                )
            )
            continue
        error = next((outcome for outcome in matched if outcome.status == "error"), None)
        skipped = next((outcome for outcome in matched if outcome.status == "skipped"), None)
        failed = next((outcome for outcome in matched if outcome.status == "failure"), None)
        if error is not None:
            # pytest could not MEASURE this check (fixture, import or collection
            # fault).  That is an instrument failure, so it VOIDs the check —
            # rendering it NOT_EVALUABLE would let a broken instrument pass for a
            # merely-unmet precondition and leave the run reporting MEASURED.
            reason = PYTEST_ERROR_REASON
            if error.message:
                reason += f":{error.message}"
            results.append(CheckResult(check, CheckVerdict.VOID, reason))
        elif skipped is not None:
            reason = "precondition_missing:pytest_skipped"
            if skipped.message:
                reason += f":{skipped.message}"
            results.append(CheckResult(check, CheckVerdict.NOT_EVALUABLE, reason))
        elif failed is not None:
            reason = "pytest_failure" + (f":{failed.message}" if failed.message else "")
            results.append(CheckResult(check, CheckVerdict.FAIL, reason))
        else:
            # This is intentionally the final gate on the would-otherwise-PASS
            # arm. Every earlier verdict keeps its established precedence; mask
            # evidence can only refuse an unearned PASS, never hide a carrier
            # failure or turn an instrument fault into a product verdict.
            mask_refusal = _mask_evidence_refusal(
                check,
                outcomes,
                writer_registry,
                repo_root,
                available_requirements,
            )
            if mask_refusal is not None:
                results.append(CheckResult(check, CheckVerdict.NOT_EVALUABLE, mask_refusal))
            else:
                results.append(CheckResult(check, CheckVerdict.PASS))
    return results


def mission_closure_admissible(
    *,
    mission_unmapped: int | None,
    injection_registry_closed: bool,
    live_portfolio_corroborated: bool,
) -> bool:
    return mission_unmapped == 0 and injection_registry_closed and live_portfolio_corroborated


def deselected_hard_gates(results: Iterable[CheckResult]) -> tuple[str, ...]:
    """Hard gates whose carrier the run never executed, in manifest order.

    A pytest-bound gate whose ``requires`` were SATISFIED (an unmet mask renders
    ``precondition_missing``, never ``not_executed``) and whose target produced
    no JUnit row at all did not fail and was not refused — it did not RUN. The
    honest per-check verdict for that is already NOT_EVALUABLE and the run is
    already INCONCLUSIVE; none of that changes here.

    What is missing is the NAME of the condition. A default marker exclusion
    silently removing an explicitly selected node id (pyproject's addopts vs the
    counterfeit-bound gates, R3-002) is indistinguishable, in a receipt, from a
    renamed test or a genuinely absent writer — all three arrive as one more
    NOT_EVALUABLE row. Listing the ids makes the instrument fault visible in the
    summary the cadence logs, so the next reader debugs the RUNNER instead of
    the product.
    """
    return tuple(
        result.check.id
        for result in results
        if result.check.gate
        and result.check.binding_type == "pytest"
        and result.verdict is CheckVerdict.NOT_EVALUABLE
        and result.reason.startswith(NOT_EXECUTED_REASON)
    )


def compute_run_verdict(
    results: list[CheckResult],
    *,
    mission_closure: bool,
    instrument_error: bool = False,
) -> RunVerdict:
    if instrument_error or not results:
        return RunVerdict.VOID
    # A VOID check means the instrument itself is broken for that check.  The run
    # cannot report a product verdict over a partly-unmeasurable manifest, and it
    # must not report the instrument fault as a candidate defect either.
    if any(result.verdict is CheckVerdict.VOID for result in results):
        return RunVerdict.VOID
    if any(result.check.gate and result.verdict is CheckVerdict.FAIL for result in results):
        return RunVerdict.FAILED
    if any(
        result.check.gate and result.verdict is CheckVerdict.NOT_EVALUABLE for result in results
    ):
        return RunVerdict.INCONCLUSIVE
    return RunVerdict.CERTIFIED if mission_closure else RunVerdict.MEASURED


def _manifest_uses_canonical_capability_set(manifest_path: Path, repo_root: Path) -> bool:
    """Whether an override claims the canonical check/capability set."""

    canonical = (repo_root / DEFAULT_MANIFEST).resolve()
    if not canonical.is_file():
        return False

    def capabilities(path: Path) -> set[tuple[str, str]] | None:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        checks = document.get("checks") if isinstance(document, dict) else None
        if not isinstance(checks, list):
            return None
        values = {
            (item["id"], item["capability"])
            for item in checks
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("capability"), str)
        }
        return values if values else None

    candidate_set = capabilities(manifest_path)
    return candidate_set is not None and candidate_set == capabilities(canonical)


def verdict_exit_code(verdict: RunVerdict) -> int:
    return {
        RunVerdict.CERTIFIED: 0,
        RunVerdict.MEASURED: 0,
        RunVerdict.FAILED: 1,
        RunVerdict.INCONCLUSIVE: 2,
        RunVerdict.VOID: 70,
    }[verdict]


def _encoded_reason(reason: str) -> str:
    encoded = base64.urlsafe_b64encode(reason.encode("utf-8")).decode("ascii")
    return _REASON_PREFIX + encoded


def _ensure_suite(store: LabStore, *, version: int, manifest_digest: str) -> str:
    suite_id = f"evs_northstar_cert_v{version}"
    existing = store.get_eval_suite(suite_id)
    if existing is None:
        store.create_eval_suite(
            EvalSuite(
                id=suite_id,
                discipline=DISCIPLINE,
                version=version,
                metrics=[
                    MetricSpec(name="pass", role="primary"),
                    MetricSpec(name="fail", role="guardrail", direction="minimize"),
                    MetricSpec(name="not_evaluable", role="guardrail", direction="minimize"),
                ],
                protected=False,
                dataset_hash=manifest_digest,
            )
        )
    elif (
        existing["discipline"] != DISCIPLINE
        or int(existing["version"]) != version
        or existing["dataset_hash"] != manifest_digest
    ):
        raise CertificationError(
            "manifest content changed without a suite version bump; refusing mixed evidence"
        )
    return suite_id


def _ensure_case(store: LabStore, suite_id: str, check: ManifestCheck) -> None:
    """Give THIS suite version its own row for ``check``.

    Eval-case identity is suite-SCOPED. A suite version is immutable evidence,
    so two versions must be able to hold different definitions of the same
    check id at the same time. Keying the case on ``check.id`` alone made the
    first version that ran own every case row forever: the next version found
    the existing row, accepted it, and created NOTHING — so a bumped suite
    owned zero cases and `candidate_cases` returned an empty set for it. Worse,
    a version whose check metadata legitimately CHANGED hit "stored eval case
    disagrees with manifest" and could never record at all.

    Rows written before scoping existed are adopted, not duplicated: a legacy
    unscoped row already associated with this suite is this suite's case.
    """
    case_id = _stable_id("evc_nsc", suite_id, check.id)
    legacy_id = _stable_id("evc_nsc", check.id)
    expected_input = check.case_input()

    existing = store._connection.execute(
        "SELECT input_json FROM eval_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if existing is None:
        legacy = store._connection.execute(
            "SELECT input_json, suite_id FROM eval_cases WHERE id = ?", (legacy_id,)
        ).fetchone()
        # Only if it belongs to THIS suite. A legacy row owned by another suite
        # version is that version's evidence and says nothing about this one.
        if legacy is not None and str(legacy["suite_id"]) == suite_id:
            existing = legacy

    if existing is None:
        store.add_eval_case(
            EvalCase(
                id=case_id,
                suite=suite_id,
                split=EvalSplit.DEV,
                input=expected_input,
                rubric="North Star check verdict: PASS / FAIL / NOT_EVALUABLE",
            )
        )
    elif json.loads(existing["input_json"]) != expected_input:
        raise CertificationError(
            f"stored eval case disagrees with manifest for {check.id} in suite {suite_id}"
        )


def _record_eval_result(
    store: LabStore, *, run_id: str, suite_id: str, version: int, result: CheckResult
) -> None:
    result_id = _stable_id("evr_nsc", run_id, result.check.id)
    existing = store._connection.execute(
        "SELECT experiment_id FROM eval_results WHERE id = ?", (result_id,)
    ).fetchone()
    if existing is not None:
        if existing["experiment_id"] != run_id:
            raise CertificationError(f"eval result identity collision for {result.check.id}")
        return
    void = result.verdict is CheckVerdict.VOID
    metrics = {
        "pass": 1.0 if result.verdict is CheckVerdict.PASS else 0.0,
        "fail": 1.0 if result.verdict is CheckVerdict.FAIL else 0.0,
        # A VOID also carries not_evaluable=1 so every reader that only knows the
        # three-metric vocabulary still sees "not a measurement" rather than
        # silence; the `void` metric and the reason string carry the finer class.
        "not_evaluable": 1.0 if result.verdict is CheckVerdict.NOT_EVALUABLE or void else 0.0,
        "void": 1.0 if void else 0.0,
    }
    per_case: dict[str, dict[str, float]] = {result.check.id: metrics}
    if result.reason:
        per_case[_encoded_reason(result.reason)] = {}
    store.record_eval_result(
        EvalResult(
            id=result_id,
            experiment_id=run_id,
            arm="champion",
            suite_id=suite_id,
            suite_version=version,
            split=EvalSplit.DEV,
            metrics=metrics,
            per_case=per_case,
            deterministic_passed=result.verdict is CheckVerdict.PASS,
        )
    )


def _pulse_values(results: list[CheckResult]) -> dict[str, float]:
    # VOID checks were never measured, so they belong in no rate's numerator or
    # denominator.  An empty denominator publishes NOTHING (per pulse storage
    # doctrine, absence — not a coerced 0.0 — is how "no observation" is recorded).
    scored = [result for result in results if result.verdict is not CheckVerdict.VOID]
    values: dict[str, float] = {}
    if results:
        values["nsc.void_ratio"] = (len(results) - len(scored)) / len(results)
    if not scored:
        return values
    total = len(scored)
    passed = sum(result.verdict is CheckVerdict.PASS for result in scored)
    not_evaluable = sum(result.verdict is CheckVerdict.NOT_EVALUABLE for result in scored)
    values["nsc.distance"] = 100.0 * passed / total
    values["nsc.mechanics_refusal_ratio"] = not_evaluable / total
    gates = [result for result in scored if result.check.gate]
    if gates:
        values["nsc.gate_pass_rate"] = sum(
            result.verdict is CheckVerdict.PASS for result in gates
        ) / len(gates)
    detectors = [result for result in scored if result.check.id.startswith("NSC-C43-")]
    if detectors:
        values["nsc.detection_rate"] = sum(
            result.verdict is CheckVerdict.PASS for result in detectors
        ) / len(detectors)
    return values


def _receipt(
    *,
    run_id: str,
    results: list[CheckResult],
    outcomes: list[JUnitOutcome],
    verdict: RunVerdict,
    command: str,
    repo_root: Path,
    sut_sha: str,
    workspace_clean: bool,
    started_at: str,
) -> GateEvidence:
    targets = tuple(
        sorted({result.check.target for result in results if result.check.binding_type == "pytest"})
    )
    normalized = normalize_gate_command(command)
    workspace_digest = workspace_digest_for(repo_root)
    return GateEvidence(
        schema=SCHEMA,
        routine_id=ROUTINE_ID,
        run_id=run_id,
        iteration=0,
        gate_type="northstar_cert",
        command=normalized,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id=ROUTINE_ID,
            run_id=run_id,
            iteration=0,
            gate_type="northstar_cert",
            command=normalized,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=sut_sha,
        ),
        tool="pytest",
        tool_version=pytest.__version__,
        exit_code=verdict_exit_code(verdict),
        # VOID checks are collected but land in none of passed/failed/skipped, so
        # `collected - (passed + failed + skipped)` is the void count on the face of
        # the receipt.  Do NOT fold them into `skipped`: that would let an
        # unmeasurable check be spent against a skip budget as if it were a real one.
        checks_collected=len(results),
        checks_passed=sum(result.verdict is CheckVerdict.PASS for result in results),
        checks_skipped=sum(result.verdict is CheckVerdict.NOT_EVALUABLE for result in results),
        checks_failed=sum(result.verdict is CheckVerdict.FAIL for result in results),
        started_at=started_at,
        finished_at=utc_now_iso(),
        nonce=secrets.token_hex(16),
        workspace_sha=sut_sha,
        workspace_tree_clean=workspace_clean,
        interpreter=str(Path(sys.executable).absolute()),
        interpreter_version=sys.version.split()[0],
        node_inventory_digest=digest(json.dumps(sorted(outcome.nodeid for outcome in outcomes))),
        candidate_sha=sut_sha,
    )


def record_results(
    *,
    manifest_path: Path,
    junit_path: Path,
    tier: str,
    run_id: str,
    db_path: Path,
    evidence_root: Path,
    repo_root: Path,
    available_requirements: frozenset[str] = frozenset(),
    mission_unmapped: int | None = None,
    injection_registry_closed: bool = False,
    live_portfolio_corroborated: bool = False,
    sut_sha: str = "",
    workspace_clean: bool = False,
    command: str = "",
    writer_registry: Mapping[str, WriterEvidence] | None = None,
) -> RecordSummary:
    if db_path.resolve() == (repo_root / LIVE_DB).resolve():
        raise CertificationError("the live runtime DB is read-only for North Star runs")
    if mission_unmapped is not None and mission_unmapped < 0:
        raise CertificationError("mission_unmapped cannot be negative")

    started_at = utc_now_iso()
    checks, version, manifest_digest, vacuous = _load_manifest(manifest_path, tier)
    effective_writer_registry = (
        dict(writer_registry)
        if writer_registry is not None
        else _writer_registry_for_manifest(manifest_path, repo_root=repo_root)
    )
    outcomes = parse_junit(junit_path, repo_root=repo_root)
    results = evaluate_checks(
        checks,
        outcomes,
        available_requirements=available_requirements,
        repo_root=repo_root,
        vacuous_targets=vacuous,
        writer_registry=effective_writer_registry,
    )
    verdict = compute_run_verdict(
        results,
        mission_closure=mission_closure_admissible(
            mission_unmapped=mission_unmapped,
            injection_registry_closed=injection_registry_closed,
            live_portfolio_corroborated=live_portfolio_corroborated,
        ),
    )
    # An override that claims the canonical capability set may not silently
    # disable the history-backed mask registry. Per-check fallback alone cannot
    # see tokens deleted from the override, so the instrument itself is VOID.
    if not effective_writer_registry and _manifest_uses_canonical_capability_set(
        manifest_path, repo_root
    ):
        verdict = RunVerdict.VOID

    evidence_store = GateEvidenceStore(evidence_root)
    if evidence_store.load(ROUTINE_ID, run_id) is not None:
        raise UnchangedInputRefusal(f"signed receipt already exists for run {run_id}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    lab = LabStore(str(db_path))
    try:
        suite_id = _ensure_suite(lab, version=version, manifest_digest=manifest_digest)
        for result in results:
            _ensure_case(lab, suite_id, result.check)
            _record_eval_result(
                lab,
                run_id=run_id,
                suite_id=suite_id,
                version=version,
                result=result,
            )
        pulse = _pulse_values(results)
        day = datetime.now(UTC).date().isoformat()
        PulseStore(lab._store).upsert_many(
            [(metric, day, value) for metric, value in pulse.items()]
        )
    finally:
        lab._store.close()

    actual_command = command or f"pytest --junitxml {junit_path}"
    try:
        evidence_store.record(
            _receipt(
                run_id=run_id,
                results=results,
                outcomes=outcomes,
                verdict=verdict,
                command=actual_command,
                repo_root=repo_root,
                sut_sha=sut_sha,
                workspace_clean=workspace_clean,
                started_at=started_at,
            )
        )
    except GateEvidenceExists as exc:
        raise UnchangedInputRefusal(str(exc)) from exc

    receipt_path = evidence_root / "records" / ROUTINE_ID / f"{run_id}.json"
    return RecordSummary(
        run_id=run_id,
        tier=tier,
        verdict=verdict,
        results=tuple(results),
        receipt_path=str(receipt_path),
        pulse=pulse,
        writer_gating_active=bool(effective_writer_registry),
        deselected_gates=deselected_hard_gates(results),
    )


def _new_run_id(tier: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{tier}-{stamp}-{secrets.token_hex(4)}"


def runnable_targets(
    manifest_path: Path,
    tier: str,
    *,
    repo_root: Path,
    available_requirements: frozenset[str] = frozenset(),
    writer_registry: Mapping[str, WriterEvidence] | None = None,
) -> list[str]:
    """Pytest node ids the run can actually execute for a tier.

    Masked checks (unresolved ``requires[]``) never reach pytest: the recorder
    renders them NOT_EVALUABLE(precondition_missing) without a junit row, and a
    single unresolvable node id aborts pytest collection for the WHOLE run —
    which then renders every check not_executed (measured live 2026-08-09).
    """

    checks, _, _, _ = _load_manifest(manifest_path, tier)
    effective_writer_registry = (
        dict(writer_registry)
        if writer_registry is not None
        else _writer_registry_for_manifest(manifest_path, repo_root=repo_root)
    )
    targets: list[str] = []
    seen: set[str] = set()
    runnable_check_ids: set[str] = set()
    for check in checks:
        if check.binding_type != "pytest":
            continue
        # A statically-valid landed pair makes its OWN check runnable. Only
        # static questions can be asked here (nothing has executed yet); the run
        # still decides whether the proof passed, so this can add a target but
        # never a verdict.
        pair_satisfied = frozenset(
            token
            for token, entry in (effective_writer_registry or {}).items()
            if not token.startswith(WRITER_REQUIREMENT_PREFIX)
            and (pair := entry.pair_for(check.id)) is not None
            and pair.landed
            and _pair_static_refusal(token, pair, check, repo_root) is None
        )
        effective_requirements = available_requirements | pair_satisfied
        if _missing_requirements(check, effective_requirements, repo_root):
            continue
        if any(
            check.id in entry.gates
            and not token.startswith(WRITER_REQUIREMENT_PREFIX)
            and not _requirement_satisfied(token, effective_requirements, repo_root)
            for token, entry in (effective_writer_registry or {}).items()
        ):
            continue
        runnable_check_ids.add(check.id)
        if check.target not in seen:
            seen.add(check.target)
            targets.append(check.target)
    # A declared proof must be present in the SAME JUnit as its carrier. Include
    # it in the recorder-generated target set once a gated check is runnable and
    # its witness exists; otherwise an honest proof-backed landing would be
    # permanently refused as proof_not_executed by construction.
    for entry in (effective_writer_registry or {}).values():
        if (
            entry.proof is not None
            and any(gate in runnable_check_ids for gate in entry.gates)
            and _witness_resolves(entry.witness, repo_root)
            and entry.proof not in seen
        ):
            seen.add(entry.proof)
            targets.append(entry.proof)
        # Same rule for per-pair proofs: a landed pair whose check is runnable
        # must have its proof in the SAME JUnit, or the run would refuse it as
        # proof_not_executed by construction.
        for pair in entry.pairs:
            if (
                pair.landed
                and pair.check_id in runnable_check_ids
                and pair.proof not in seen
                and _witness_resolves(pair.witness, repo_root)
            ):
                seen.add(pair.proof)
                targets.append(pair.proof)
    return targets


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="print runnable pytest targets for the tier (requires-mask aware) and exit",
    )
    parser.add_argument("--tier", choices=("t1", "t2", "t3"), required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--db", type=Path, default=DEFAULT_RESULTS_DB)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--available-requirement", action="append", default=[])
    parser.add_argument("--mission-unmapped", type=int)
    parser.add_argument("--injection-registry-closed", action="store_true")
    parser.add_argument("--live-portfolio-corroborated", action="store_true")
    parser.add_argument("--sut-sha", default="")
    parser.add_argument("--workspace-clean", action="store_true")
    parser.add_argument("--command", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.list_targets:
        try:
            for target in runnable_targets(
                args.manifest,
                args.tier,
                repo_root=Path.cwd(),
                available_requirements=frozenset(args.available_requirement),
            ):
                print(target)
        except CertificationError as exc:
            print(f"instrument_error:{exc}", file=sys.stderr)
            return 70
        return 0
    if args.junitxml is None:
        parser.error("--junitxml is required unless --list-targets is given")
    run_id = args.run_id or _new_run_id(args.tier)
    try:
        summary = record_results(
            manifest_path=args.manifest,
            junit_path=args.junitxml,
            tier=args.tier,
            run_id=run_id,
            db_path=args.db,
            evidence_root=args.evidence_root,
            repo_root=Path.cwd(),
            available_requirements=frozenset(args.available_requirement),
            mission_unmapped=args.mission_unmapped,
            injection_registry_closed=args.injection_registry_closed,
            live_portfolio_corroborated=args.live_portfolio_corroborated,
            sut_sha=args.sut_sha,
            workspace_clean=args.workspace_clean,
            command=args.command,
        )
    except UnchangedInputRefusal as exc:
        print(json.dumps({"run_id": run_id, "verdict": "INCONCLUSIVE", "reason": str(exc)}))
        return 2
    except CertificationError as exc:
        print(json.dumps({"run_id": run_id, "verdict": "VOID", "reason": str(exc)}))
        return 70
    except Exception as exc:  # noqa: BLE001 - CLI boundary maps instrument faults to VOID
        reason = f"instrument_error:{type(exc).__name__}:{exc}"
        print(json.dumps({"run_id": run_id, "verdict": "VOID", "reason": reason}))
        return 70
    if summary.deselected_gates:
        # stderr, not the JSON stream: the machine-readable statement is
        # `deselected_gates` in the summary, this is the line a human greps out
        # of the cadence log. It changes no verdict and no exit code.
        print(
            "instrument_warning:hard_gates_not_executed:"
            + ",".join(summary.deselected_gates)
            + " (a satisfied-requires pytest gate produced no JUnit row: check the"
            " runner's marker filter and the target's spelling before reading these"
            " as product defects)",
            file=sys.stderr,
        )
    if not summary.writer_gating_active:
        print(
            "instrument_warning:mask_registry_unavailable:canonical capability runs VOID",
            file=sys.stderr,
        )
    print(json.dumps(summary.to_dict(), sort_keys=True))
    return verdict_exit_code(summary.verdict)


if __name__ == "__main__":
    raise SystemExit(main())
