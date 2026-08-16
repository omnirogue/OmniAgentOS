"""Tests for hand-rolled BM25 tool catalog search.

W3 requirements tested:
- Tokenizer splitting on camelCase, snake_case, dotted ids, stopwords, and short tokens.
- ID matches ranking first due to field weights.
- Description matches ranking below ID matches.
- Result capping at limit, and default to max_definitions().
- Deterministic alphabetical tie-breaking by ID.
- Empty or all-stopword queries returning ().
- Empty exposure decisions returning ().
- Robust security property tests ensuring hidden tools are structurally invisible.
- Indexing zero entries returning () safely without division by zero.
- Return hits with strictly positive scores.
- Real catalog search integration.
"""

from __future__ import annotations

import pytest

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.exposure import ExposureDecision
from omniagentos.toolplane.search import (
    build_index,
    search_tools,
    tokenize,
)


def make_catalog_entry(
    id: str,
    namespace: str = "default_namespace",
    label: str = "Default Label",
    compact_hint: str = "Default Hint",
    description: str = "Default Description",
    parameter_names: tuple[str, ...] = (),
    input_examples: tuple[str, ...] = (),
) -> CatalogEntry:
    """Create a synthetic CatalogEntry with reasonable defaults for testing."""
    return CatalogEntry(
        id=id,
        namespace=namespace,
        label=label,
        compact_hint=compact_hint,
        description=description,
        source="builtin",
        action_class=ActionClass.READ_ONLY,
        read_only=True,
        side_effect_class=SideEffectClass.NONE,
        resource_keys=(),
        idempotent=True,
        parallel_safe=True,
        cancellation_group="default_group",
        credential_scope="default_scope",
        result_size_class=ResultSizeClass.SMALL,
        risk="low",
        requires_scope=False,
        input_examples=input_examples,
        parameter_names=parameter_names,
        callable_now=True,
        classified=True,
    )


def make_exposure_decision(
    core_tools: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
    deferred: tuple[str, ...] = (),
    hidden: tuple[str, ...] = (),
) -> ExposureDecision:
    """Create an ExposureDecision directly for testing search isolation."""
    return ExposureDecision(
        core_tools=core_tools,
        allowed=allowed,
        deferred=deferred,
        hidden=hidden,
        reason="test",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )


class TestTokenize:
    """Validate tokenizer splitting, normalization, and filtering."""

    def test_tokenize_splitting(self) -> None:
        """Tokenize splits snake_case, dotted ids, and camelCase."""
        assert tokenize("stripe_acmeuni.read") == ["stripe", "acmeuni", "read"]
        assert tokenize("readFile") == ["readfile", "read", "file"]
        assert tokenize("stripe_acmeuni.readMyFile") == [
            "stripe",
            "acmeuni",
            "readmyfile",
            "read",
            "file",
        ]

    def test_tokenize_filtering(self) -> None:
        """Tokenize drops stopwords and tokens shorter than _MIN_TOKEN_LEN."""
        # 'the', 'a', 'with' are stopwords, 'x' is 1-character
        assert tokenize("the file") == ["file"]
        assert tokenize("x file") == ["file"]
        assert tokenize("a with the") == []

    def test_tokenize_determinism(self) -> None:
        """Tokenize is fully deterministic."""
        text = "Stripe_AcmeUni.readSomeData"
        assert tokenize(text) == tokenize(text)


class TestSearchRankingAndWeighting:
    """Validate query matching, ranking, and weighting logic."""

    def test_find_query_also_matches_search_without_expanding_documents(self) -> None:
        """The one-way query synonym finds search tools without mutating indexed text."""
        search_entry = make_catalog_entry(id="workspace.search", description="query records")
        find_entry = make_catalog_entry(id="workspace.find", description="locate records")

        catalog = {search_entry.id: search_entry, find_entry.id: find_entry}
        decision = make_exposure_decision(deferred=tuple(catalog))

        find_hits = search_tools("find", decision, catalog=catalog)
        assert {hit.entry.id for hit in find_hits} == {"workspace.find", "workspace.search"}

        search_hits = search_tools("search", decision, catalog=catalog)
        assert tuple(hit.entry.id for hit in search_hits) == ("workspace.search",)

    def test_id_match_ranks_first(self) -> None:
        """A query matching a tool's id ranks that tool first."""
        entry_1 = make_catalog_entry(id="stripe.refund", description="this tool does refunds")
        entry_2 = make_catalog_entry(id="other.tool", description="stripe refund tool helper")

        catalog = {entry_1.id: entry_1, entry_2.id: entry_2}
        decision = make_exposure_decision(deferred=("stripe.refund", "other.tool"))

        hits = search_tools("stripe refund", decision, catalog=catalog)
        assert len(hits) == 2
        assert hits[0].entry.id == "stripe.refund"

    def test_description_match_vs_id_match(self) -> None:
        """A description match finds the tool, but ranks below an id match."""
        entry_id_match = make_catalog_entry(id="stripe", description="some utility")
        entry_desc_match = make_catalog_entry(id="other", description="stripe refund tool")

        catalog = {entry_id_match.id: entry_id_match, entry_desc_match.id: entry_desc_match}
        decision = make_exposure_decision(deferred=("stripe", "other"))

        hits = search_tools("stripe", decision, catalog=catalog)
        assert len(hits) == 2
        assert hits[0].entry.id == "stripe"
        assert hits[1].entry.id == "other"


class TestLimitsAndTies:
    """Validate limit constraints and deterministic tie-breaking."""

    def test_limit_and_max_definitions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Results are capped at limit, and at max_definitions() when limit is None."""
        entry_1 = make_catalog_entry(id="tool1", description="stripe helper")
        entry_2 = make_catalog_entry(id="tool2", description="stripe helper")
        entry_3 = make_catalog_entry(id="tool3", description="stripe helper")

        catalog = {entry_1.id: entry_1, entry_2.id: entry_2, entry_3.id: entry_3}
        decision = make_exposure_decision(deferred=("tool1", "tool2", "tool3"))

        # Explicit limit
        hits = search_tools("stripe", decision, catalog=catalog, limit=2)
        assert len(hits) == 2

        # Default limit from max_definitions
        monkeypatch.setattr("omniagentos.toolplane.search.max_definitions", lambda: 1)
        hits_default = search_tools("stripe", decision, catalog=catalog)
        assert len(hits_default) == 1

    def test_ties_break_deterministically(self) -> None:
        """Ties break deterministically by id alphabetically."""
        entry_b = make_catalog_entry(id="tool_b", description="stripe helper")
        entry_a = make_catalog_entry(id="tool_a", description="stripe helper")

        catalog = {entry_b.id: entry_b, entry_a.id: entry_a}
        decision = make_exposure_decision(deferred=("tool_b", "tool_a"))

        hits = search_tools("stripe", decision, catalog=catalog)
        assert len(hits) == 2
        assert hits[0].entry.id == "tool_a"
        assert hits[1].entry.id == "tool_b"


class TestEdgeQueries:
    """Validate edge case queries and empty states."""

    def test_empty_or_stopword_query(self) -> None:
        """An empty or all-stopword query returns ()."""
        entry = make_catalog_entry(id="tool", description="stripe helper")
        catalog = {entry.id: entry}
        decision = make_exposure_decision(deferred=("tool",))

        assert search_tools("", decision, catalog=catalog) == ()
        assert search_tools("the with and", decision, catalog=catalog) == ()

    def test_empty_decision(self) -> None:
        """An empty exposure decision returns ()."""
        entry = make_catalog_entry(id="tool", description="stripe helper")
        catalog = {entry.id: entry}
        decision = make_exposure_decision()

        assert search_tools("stripe", decision, catalog=catalog) == ()


class TestSecurityIsolation:
    """Exhaustively validate the construction-not-filtering security property."""

    def test_security_property_thorough(self) -> None:
        """Verify that hidden tools can never be surfaced by any query because they are not indexed."""
        hidden_entry = make_catalog_entry(
            id="secret.refund_everything",
            description="super secret refund everything stripe tool back-door root access bypass security check supercalifragilistic",
        )
        allowed_entry = make_catalog_entry(
            id="public.tool", description="standard public helper tool"
        )

        catalog = {hidden_entry.id: hidden_entry, allowed_entry.id: allowed_entry}
        decision = make_exposure_decision(
            allowed=("public.tool",), hidden=("secret.refund_everything",)
        )

        hidden_tokens = tokenize(hidden_entry.id) + tokenize(hidden_entry.description)
        all_hidden_joined = " ".join(hidden_tokens)
        long_string = "a" * 500

        queries = [
            *hidden_tokens,
            "secret.refund_everything",
            "",
            "*",
            long_string,
            all_hidden_joined,
            "public tool secret refund",
        ]

        for query in queries:
            hits = search_tools(query, decision, catalog=catalog)
            for hit in hits:
                assert hit.entry.id != "secret.refund_everything", (
                    f"Security breached for query: {query!r}"
                )
                assert hit.entry.id not in decision.hidden, (
                    f"Security breached for query: {query!r}"
                )

    def test_hidden_id_in_catalog_never_returned(self) -> None:
        """A hidden id present in the catalog is never returned."""
        hidden_entry = make_catalog_entry(id="hidden_tool", description="stripe helper")
        catalog = {hidden_entry.id: hidden_entry}
        decision = make_exposure_decision(hidden=("hidden_tool",))

        hits = search_tools("stripe", decision, catalog=catalog)
        assert hits == ()


class TestBM25IndexInternal:
    """Validate internal BM25 index behaviors."""

    def test_zero_entries_index(self) -> None:
        """A BM25Index over zero entries returns () and does not raise."""
        index = build_index([])
        assert index.size == 0
        assert index.search("stripe", limit=5) == ()

    def test_scores_strictly_positive(self) -> None:
        """Returned hits always have scores strictly positive."""
        entry_1 = make_catalog_entry(id="tool1", description="stripe helper")
        entry_2 = make_catalog_entry(id="tool2", description="unrelated info")

        catalog = {entry_1.id: entry_1, entry_2.id: entry_2}
        decision = make_exposure_decision(deferred=("tool1", "tool2"))

        hits = search_tools("stripe", decision, catalog=catalog)
        assert len(hits) == 1
        assert hits[0].entry.id == "tool1"
        assert hits[0].score > 0.0


class TestRealCatalog:
    """Validate integration with the real unified catalog."""

    def test_real_catalog_integration(self) -> None:
        """search_tools works against the default_catalog() and limits results correctly."""
        real_cat = default_catalog()
        assert len(real_cat) > 0

        all_ids = tuple(real_cat.keys())
        decision = make_exposure_decision(deferred=all_ids)

        hits = search_tools("refund a stripe charge", decision)
        assert len(hits) <= 5
        for hit in hits:
            assert hit.entry.id in real_cat


class TestRealCatalogSecurityProperty:
    """The construction-not-filtering property, exercised over the REAL catalog.

    The synthetic security test above proves the property for a hand-built pair of
    entries. This one proves it where it actually has to hold: over every tool the
    system really has, with a partition chosen so that the hidden half is exactly the
    half an attacker would most want, and with EVERY tool's own identifying text used
    as a query. If any indexing path could leak a hidden tool, a query built from that
    tool's own name is the query that would do it.
    """

    def test_no_hidden_tool_is_ever_returned_for_any_self_query(self) -> None:
        catalog = dict(default_catalog())
        ids = sorted(catalog)
        assert len(ids) > 50, "expected a substantial real catalog"

        # Deterministic partition: every other id is hidden. Alternating over a sorted
        # list splits sibling capabilities of the same connector across the boundary,
        # so near-identical text sits on both sides -- the hardest case for a scorer.
        hidden = tuple(ids[0::2])
        deferred = tuple(ids[1::2])
        decision = ExposureDecision(
            core_tools=(),
            allowed=(),
            deferred=deferred,
            hidden=hidden,
            reason="test",
            mode="shadow",
            bypassed=False,
            fallback=False,
            estimated_tokens=0,
        )
        hidden_set = set(hidden)

        for tool_id in ids:
            entry = catalog[tool_id]
            for query in (
                tool_id,
                entry.label,
                entry.compact_hint,
                entry.description,
                " ".join(tokenize(tool_id)),
            ):
                for hit in search_tools(query, decision, catalog=catalog):
                    assert hit.entry.id not in hidden_set, (
                        f"hidden tool {hit.entry.id!r} surfaced for query {query!r}"
                    )
                    assert hit.entry.id in deferred

    def test_hidden_high_risk_capabilities_stay_invisible_to_their_own_names(self) -> None:
        """The money-moving capabilities, hidden, are unreachable by their own names."""
        catalog = dict(default_catalog())
        dangerous = tuple(
            sorted(i for i, e in catalog.items() if e.risk == "high" and e.source == "connector")
        )
        assert dangerous, "expected the real catalog to contain high-risk capabilities"

        decision = ExposureDecision(
            core_tools=(),
            allowed=tuple(sorted(set(catalog) - set(dangerous))),
            deferred=(),
            hidden=dangerous,
            reason="test",
            mode="enforce",
            bypassed=False,
            fallback=False,
            estimated_tokens=0,
        )
        for tool_id in dangerous:
            for query in (tool_id, "refund", "launch an ad", "delete everything", "payout"):
                for hit in search_tools(query, decision, catalog=catalog):
                    assert hit.entry.id not in set(dangerous)
