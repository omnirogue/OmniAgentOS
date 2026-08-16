"""Tool-selection fixtures: can deferred search find the right tool in a big catalog?

The unit tests in ``test_search.py`` prove the scorer's mechanics on a handful of
hand-built entries. This module measures the thing those tests cannot: **recall at
realistic catalog size**. Three synthetic catalogs (100, 250 and 500 namespaced tools,
each a strict prefix of the next) and 50 selection cases give a single number —
top-5 hit rate — that a retrieval change can be judged against.

Three case kinds, and the difference between them matters:

``plain``
    A straightforward request. Measures baseline recall.

``adversarial``
    The catalog holds a genuine near-twin of the answer (``stripe.refund_charge`` vs
    ``stripe.refund_invoice``). Measures whether a lexical scorer can be talked into the
    wrong sibling.

``invisibility``
    The tool that *would* be the obvious top hit is in the caller's ``hidden`` set, and
    the expected answer is the best visible alternative. This is the one assertion in the
    file that never yields: a hidden id appearing in results at any rank is a hard failure,
    checked against the case's own query AND against the hidden tool's own id, label and
    description — the queries most likely to surface it if the construction-not-filtering
    property in ``search.py`` ever regressed into post-filtering.

Recall is a *measurement*, so this file reports it rather than legislating it. A case the
current scorer genuinely misses is listed in :data:`KNOWN_MISSES` with a reason and xfails,
which keeps the miss visible as tuning evidence instead of deleting the evidence by
softening the fixture. The aggregate assertion is on the TRUE rate over every case —
xfailed ones included — so the list cannot be used to inflate the number.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry, RiskLevel
from omniagentos.toolplane.exposure import ExposureDecision
from omniagentos.toolplane.search import search_tools, tokenize

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CATALOG_NAMES = ("catalog_100", "catalog_250", "catalog_500")
CATALOG_SIZES = {"catalog_100": 100, "catalog_250": 250, "catalog_500": 500}
TOP_K = 5

#: The floor the corpus must clear for the number to mean anything. Raise it when the
#: retriever improves; never lower it to make a red suite green.
MIN_HIT_RATE = 0.80

#: case id -> why the current BM25 retriever misses it. Every entry here is a real,
#: reproducible miss on a fixture that a human read and judged fair — NOT a broken case.
#: They xfail so the suite stays green and the list stays readable as a tuning backlog.
KNOWN_MISSES: dict[str, str] = {}

_ACTION_CLASS_BY_RISK: dict[str, ActionClass] = {
    "low": ActionClass.READ_ONLY,
    "medium": ActionClass.INTERNAL_REVERSIBLE,
    "high": ActionClass.EXTERNAL_REVERSIBLE,
}
_SIDE_EFFECT_BY_RISK: dict[str, SideEffectClass] = {
    "low": SideEffectClass.NONE,
    "medium": SideEffectClass.INTERNAL_WRITE,
    "high": SideEffectClass.EXTERNAL_WRITE,
}


@dataclass(frozen=True)
class SelectionCase:
    """One query and the tool it must retrieve, plus what the caller may not see."""

    id: str
    catalog: str
    query: str
    expected_tool_id: str
    hidden_ids: tuple[str, ...]
    kind: str
    note: str


def _to_entry(record: dict[str, Any]) -> CatalogEntry:
    """Map a fixture record onto the real :class:`CatalogEntry` the retriever indexes.

    Only the identity/metadata fields the fixture declares are meaningful here; the
    execution-planning fields (idempotency, cancellation group, ...) are derived from
    ``risk`` so every entry is internally consistent, and the scheduler never sees them.
    """
    risk: RiskLevel = record["risk"]
    return CatalogEntry(
        id=record["id"],
        namespace=record["namespace"],
        label=record["label"],
        compact_hint=record["compact_hint"],
        description=record["description"],
        source="connector",
        action_class=_ACTION_CLASS_BY_RISK[risk],
        read_only=bool(record["read_only"]),
        side_effect_class=_SIDE_EFFECT_BY_RISK[risk],
        resource_keys=(f"net:{record['namespace']}",),
        idempotent=bool(record["read_only"]),
        parallel_safe=bool(record["read_only"]),
        cancellation_group=f"toolplane-{record['namespace']}",
        credential_scope=record["namespace"],
        result_size_class=ResultSizeClass.SMALL,
        risk=risk,
        requires_scope=True,
        input_examples=tuple(record["input_examples"]),
        parameter_names=tuple(record["parameter_names"]),
        callable_now=True,
        classified=True,
    )


@functools.lru_cache(maxsize=len(CATALOG_NAMES))
def load_catalog(name: str) -> dict[str, CatalogEntry]:
    """Load one synthetic catalog, keyed by tool id (insertion order preserved)."""
    data = yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return {record["id"]: _to_entry(record) for record in data["tools"]}


@functools.lru_cache(maxsize=1)
def load_cases() -> tuple[SelectionCase, ...]:
    """Load the selection cases in file order."""
    data = yaml.safe_load((FIXTURES_DIR / "selection_cases.yaml").read_text(encoding="utf-8"))
    return tuple(
        SelectionCase(
            id=str(case["id"]),
            catalog=str(case["catalog"]),
            query=str(case["query"]),
            expected_tool_id=str(case["expected_tool_id"]),
            hidden_ids=tuple(str(h) for h in (case.get("hidden_ids") or ())),
            kind=str(case["kind"]),
            note=str(case.get("note") or ""),
        )
        for case in data["cases"]
    )


def _decision(catalog: dict[str, CatalogEntry], hidden: tuple[str, ...]) -> ExposureDecision:
    """Everything not explicitly hidden is deferred — i.e. discoverable by search."""
    hidden_set = set(hidden)
    return ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=tuple(i for i in catalog if i not in hidden_set),
        hidden=hidden,
        reason="selection-fixture",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )


def run_case(case: SelectionCase) -> tuple[str, ...]:
    """Return the ids the retriever surfaces for *case*, best first."""
    catalog = load_catalog(case.catalog)
    hits = search_tools(
        case.query, _decision(catalog, case.hidden_ids), catalog=catalog, limit=TOP_K
    )
    return tuple(hit.entry.id for hit in hits)


CASES = load_cases()
CASE_IDS = [case.id for case in CASES]


# --------------------------------------------------------------------------- catalogs


@pytest.mark.parametrize("name", CATALOG_NAMES)
def test_catalog_has_its_declared_size(name: str) -> None:
    assert len(load_catalog(name)) == CATALOG_SIZES[name]


def test_catalogs_are_strict_prefixes() -> None:
    """The same query must be scoreable against a growing catalog, so the small
    catalogs are prefixes of the big one rather than independent samples."""
    small = list(load_catalog("catalog_100"))
    medium = list(load_catalog("catalog_250"))
    large = list(load_catalog("catalog_500"))
    assert medium[: len(small)] == small
    assert large[: len(medium)] == medium


@pytest.mark.parametrize("name", CATALOG_NAMES)
def test_catalog_records_are_well_formed(name: str) -> None:
    catalog = load_catalog(name)
    for tool_id, entry in catalog.items():
        assert tool_id == entry.id
        assert entry.id.startswith(entry.namespace + "."), entry.id
        assert entry.label.strip() and entry.compact_hint.strip() and entry.description.strip()
        assert entry.parameter_names, entry.id
        assert entry.input_examples, entry.id
        assert entry.risk in ("low", "medium", "high")
        assert entry.read_only is (entry.risk == "low"), entry.id
        assert tokenize(entry.id), entry.id


def test_catalog_covers_many_namespaces() -> None:
    """A catalog whose tools all share a namespace would make retrieval trivially easy."""
    namespaces = {e.namespace for e in load_catalog("catalog_500").values()}
    assert len(namespaces) >= 40, sorted(namespaces)


# --------------------------------------------------------------------------- cases


def test_case_corpus_shape() -> None:
    assert len(CASES) == 50
    assert CASE_IDS == [f"sel_{i:03d}" for i in range(1, 51)]
    assert len(set(CASE_IDS)) == 50

    kinds = [c.kind for c in CASES]
    assert set(kinds) <= {"plain", "adversarial", "invisibility"}
    assert kinds.count("adversarial") >= 10, "the adversarial slice is what makes this hard"
    assert kinds.count("invisibility") >= 5, "invisibility is the security-relevant slice"

    per_catalog = {name: sum(1 for c in CASES if c.catalog == name) for name in CATALOG_NAMES}
    assert all(count >= 10 for count in per_catalog.values()), per_catalog

    per_namespace: dict[str, int] = {}
    for case in CASES:
        namespace = case.expected_tool_id.split(".", 1)[0]
        per_namespace[namespace] = per_namespace.get(namespace, 0) + 1
    assert max(per_namespace.values()) <= 4, per_namespace


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_case_references_real_tools(case: SelectionCase) -> None:
    catalog = load_catalog(case.catalog)
    assert case.catalog in CATALOG_NAMES
    assert case.expected_tool_id in catalog, f"{case.id}: unknown expected tool"
    for hidden in case.hidden_ids:
        assert hidden in catalog, f"{case.id}: unknown hidden tool {hidden}"
    assert case.expected_tool_id not in case.hidden_ids
    assert case.query.strip() and case.note.strip()
    if case.kind == "invisibility":
        assert case.hidden_ids, f"{case.id}: an invisibility case must hide something"


# --------------------------------------------------------------------------- retrieval


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_expected_tool_is_in_top_5(case: SelectionCase, request: pytest.FixtureRequest) -> None:
    if case.id in KNOWN_MISSES:
        request.node.add_marker(pytest.mark.xfail(reason=KNOWN_MISSES[case.id], strict=True))
    results = run_case(case)
    assert case.expected_tool_id in results, (
        f"{case.id} ({case.kind}, {case.catalog}): {case.expected_tool_id!r} not in top-{TOP_K}\n"
        f"  query: {case.query!r}\n  note:  {case.note}\n  got:   {list(results)}"
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_hidden_tools_never_surface(case: SelectionCase) -> None:
    """Absolute, for every case — never xfailed, never softened."""
    leaked = set(run_case(case)) & set(case.hidden_ids)
    assert not leaked, f"{case.id}: hidden tool(s) {sorted(leaked)} surfaced for {case.query!r}"


INVISIBILITY_CASES = [c for c in CASES if c.kind == "invisibility"]


@pytest.mark.parametrize(
    "case", INVISIBILITY_CASES, ids=[c.id for c in INVISIBILITY_CASES] or ["none"]
)
def test_invisibility_holds_against_the_hidden_tools_own_text(case: SelectionCase) -> None:
    """The hardest query for a hidden tool is that tool's own name and prose."""
    catalog = load_catalog(case.catalog)
    decision = _decision(catalog, case.hidden_ids)
    hidden_set = set(case.hidden_ids)
    for hidden_id in case.hidden_ids:
        entry = catalog[hidden_id]
        for query in (
            case.query,
            entry.id,
            entry.label,
            entry.compact_hint,
            entry.description,
            " ".join(tokenize(entry.id)),
            " ".join(entry.input_examples),
        ):
            hits = search_tools(query, decision, catalog=catalog, limit=TOP_K)
            leaked = {hit.entry.id for hit in hits} & hidden_set
            assert not leaked, f"{case.id}: {sorted(leaked)} surfaced for {query!r}"


@pytest.mark.parametrize(
    "case", INVISIBILITY_CASES, ids=[c.id for c in INVISIBILITY_CASES] or ["none"]
)
def test_invisibility_cases_actually_hide_a_contender(case: SelectionCase) -> None:
    """An invisibility case only measures something if the hidden tool WOULD have won.

    Hiding a tool the query was never going to retrieve proves nothing — the assertion
    would pass against a broken retriever too. This pins each case to the property that
    makes it evidence: with the hidden set emptied, at least one of its ids surfaces in
    the top-5, so suppressing it is a real result and not a coincidence.
    """
    catalog = load_catalog(case.catalog)
    open_decision = _decision(catalog, ())
    open_hits = {
        hit.entry.id
        for hit in search_tools(case.query, open_decision, catalog=catalog, limit=TOP_K)
    }
    contenders = open_hits & set(case.hidden_ids)
    assert contenders, (
        f"{case.id}: none of {list(case.hidden_ids)} would reach the top-{TOP_K} for "
        f"{case.query!r} even when visible, so hiding them measures nothing.\n"
        f"  visible top-{TOP_K}: {sorted(open_hits)}"
    )


def test_search_is_deterministic() -> None:
    """Two identical runs must agree, or the hit rate is not a measurement."""
    for case in CASES:
        assert run_case(case) == run_case(case), case.id


# --------------------------------------------------------------------------- the number


def _hit_report() -> tuple[float, list[str], str]:
    """Top-5 hit rate, plus top-1 as the headroom signal, sliced by catalog and by kind."""
    misses: list[str] = []
    top5: dict[str, list[int]] = {}
    top1: dict[str, list[int]] = {}
    for case in CASES:
        results = run_case(case)
        hit = case.expected_tool_id in results
        first = bool(results) and results[0] == case.expected_tool_id
        for group in (case.catalog, case.kind, "overall"):
            top5.setdefault(group, []).append(1 if hit else 0)
            top1.setdefault(group, []).append(1 if first else 0)
        if not hit:
            misses.append(
                f"{case.id} [{case.kind}/{case.catalog}] {case.query!r} "
                f"-> want {case.expected_tool_id}, got {list(results)}"
            )
    lines = [
        f"selection fixtures: {len(CASES)} cases over "
        f"{'/'.join(str(CATALOG_SIZES[n]) for n in CATALOG_NAMES)}-tool catalogs",
        f"  {'group':<14} {'top-' + str(TOP_K):>8}   {'top-1':>8}",
    ]
    for group in (*CATALOG_NAMES, "plain", "adversarial", "invisibility", "overall"):
        five, one = top5.get(group), top1.get(group)
        if five and one:
            lines.append(
                f"  {group:<14} {sum(five):>3}/{len(five):<3} {sum(five) / len(five):>4.0%}"
                f"   {sum(one):>3}/{len(one):<3} {sum(one) / len(one):>4.0%}"
            )
    if misses:
        lines.append("misses:")
        lines.extend("  " + m for m in misses)
    overall = top5["overall"]
    return sum(overall) / len(overall), misses, "\n".join(lines)


def test_top5_hit_rate_meets_the_floor() -> None:
    """The headline number, over EVERY case — cases listed in KNOWN_MISSES included.

    Printed on every run (visible with ``-s``) and embedded in the failure message, so
    the miss list is the first thing a retrieval change shows you.
    """
    rate, misses, report = _hit_report()
    print("\n" + report)
    assert rate >= MIN_HIT_RATE, (
        f"top-{TOP_K} hit rate {rate:.0%} is below the {MIN_HIT_RATE:.0%} floor\n{report}"
    )
    assert len(misses) == len(KNOWN_MISSES), (
        "KNOWN_MISSES is stale — it must list exactly the cases that currently miss, so "
        f"the xfail list stays honest.\n{report}\n"
        f"KNOWN_MISSES: {sorted(KNOWN_MISSES)}"
    )


def test_known_misses_are_documented() -> None:
    for case_id, reason in KNOWN_MISSES.items():
        assert case_id in CASE_IDS, f"{case_id} is not a real case"
        assert len(reason.strip()) >= 20, f"{case_id}: give a real reason, not {reason!r}"
