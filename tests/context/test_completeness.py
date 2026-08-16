"""Completeness evaluate — six independent checks, fail-closed."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.context.completeness import (
    CHECK_NAMES,
    CompletenessVerdict,
    FreshnessPolicy,
    evaluate,
)
from omniagentos.context.package import ContextItem, ContextPackage

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
FRESH = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
STALE = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
CRIT_STALE = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(
    item_id: str,
    *,
    source: str = "src/app/mod.py",
    version: str = "v1",
    freshness: str = FRESH,
    kind: str = "file",
    **kwargs: object,
) -> ContextItem:
    return ContextItem(
        id=item_id,
        source=source,
        version=version,
        freshness=freshness,
        kind=kind,
        **kwargs,  # type: ignore[arg-type]
    )


def _pkg(
    *items: ContextItem,
    ack_token: str | None = "ack-ok",
    package_id: str = "pkg-1",
) -> ContextPackage:
    return ContextPackage(package_id=package_id, items=items, ack_token=ack_token)


def _eval(
    package: ContextPackage,
    *,
    contract_required_ids: list[str] | None = None,
    token_budget: int = 10_000,
    read_set: list[str] | None = None,
    authorized_secret_ids: list[str] | None = None,
    policy: FreshnessPolicy | None = None,
    safety_margin: float = 0.1,
) -> CompletenessVerdict:
    return evaluate(
        package,
        contract_required_ids=contract_required_ids
        if contract_required_ids is not None
        else [it.id for it in package.items],
        token_budget=token_budget,
        now=NOW,
        read_set=read_set if read_set is not None else ["src/app"],
        authorized_secret_ids=authorized_secret_ids or (),
        policy=policy,
        safety_margin=safety_margin,
    )


def test_a_missing_required_artifact_fails_presence_and_names_the_id() -> None:
    pkg = _pkg(_item("present"))
    v = _eval(pkg, contract_required_ids=["present", "missing-req"])
    presence = v.check("presence")
    assert presence.passed is False
    assert "missing-req" in presence.failed_item_ids
    assert "missing-req" in presence.detail
    assert "present" not in presence.failed_item_ids


def test_an_unlabeled_stale_item_fails_freshness() -> None:
    pkg = _pkg(_item("stale-one", freshness=STALE, stale_labeled=False))
    v = _eval(pkg)
    fr = v.check("freshness")
    assert fr.passed is False
    assert fr.failed_item_ids == ("stale-one",)


def test_a_labeled_stale_item_passes_freshness_with_the_label_surfaced() -> None:
    pkg = _pkg(_item("stale-ok", freshness=STALE, stale_labeled=True, critical=False))
    v = _eval(pkg)
    fr = v.check("freshness")
    assert fr.passed is True
    assert fr.data.get("stale_labeled") == ["stale-ok"]
    assert (
        "stale-ok" in fr.detail or "stale_labeled" in fr.detail.lower() or "accepted" in fr.detail
    )


def test_critical_over_age_fails_even_when_stale_labeled() -> None:
    pkg = _pkg(
        _item(
            "crit",
            freshness=CRIT_STALE,
            critical=True,
            stale_labeled=True,
        )
    )
    v = _eval(pkg, policy=FreshnessPolicy(max_age_seconds=86_400, critical_max_age_seconds=3_600))
    fr = v.check("freshness")
    assert fr.passed is False
    assert fr.failed_item_ids == ("crit",)


def test_unresolved_contradicting_pair_fails_consistency() -> None:
    a = _item("a", contradicts=("b",), resolved=False)
    b = _item("b", resolved=False)
    v = _eval(_pkg(a, b))
    cons = v.check("consistency")
    assert cons.passed is False
    assert set(cons.failed_item_ids) == {"a", "b"}
    assert cons.data["pairs"] == [["a", "b"]]


def test_resolved_contradicting_pair_passes_consistency() -> None:
    a = _item("a", contradicts=("b",), resolved=True)
    b = _item("b", resolved=False)
    v = _eval(_pkg(a, b))
    cons = v.check("consistency")
    assert cons.passed is True
    assert cons.failed_item_ids == ()

    a2 = _item("a", contradicts=("b",), resolved=False)
    b2 = _item("b", resolved=True)
    v2 = _eval(_pkg(a2, b2))
    assert v2.check("consistency").passed is True


def test_an_item_outside_the_contract_read_set_fails_authorization() -> None:
    pkg = _pkg(_item("out", source="other/tree/file.py"))
    v = _eval(pkg, read_set=["src/app"])
    auth = v.check("authorization")
    assert auth.passed is False
    assert auth.failed_item_ids == ("out",)


def test_read_set_path_boundary_does_not_authorize_prefix_sibling() -> None:
    """read_set=['src/app'] must NOT authorize src/application/secrets.py."""
    pkg = _pkg(_item("leak", source="src/application/secrets.py"))
    v = _eval(pkg, read_set=["src/app"])
    auth = v.check("authorization")
    assert auth.passed is False
    assert auth.failed_item_ids == ("leak",)
    # Positive control: path-boundary child is authorized.
    ok = _pkg(_item("child", source="src/app/mod.py"))
    assert _eval(ok, read_set=["src/app"]).check("authorization").passed is True


def test_read_set_path_boundary_does_not_authorize_dotdot_traversal() -> None:
    """read_set=['src/app'] must NOT authorize src/app/../../etc/passwd.

    Defect class: non-result (failed containment) presented as authorized.
    Bare ``source.startswith(entry + '/')`` admits ``..`` traversal spellings
    that leave the boundary after normalization.

    Named counterfeit: ``boundary = entry.rstrip('/') + '/'; return
    source.startswith(boundary)`` without collapsing ``..`` / consulting
    path containment. This test must fail under that counterfeit.
    Positive control: real child under the boundary remains authorized.
    """
    pkg = _pkg(_item("trav", source="src/app/../../etc/passwd"))
    v = _eval(pkg, read_set=["src/app"])
    auth = v.check("authorization")
    assert auth.passed is False, (
        "path traversal spelling must not be authorized by string-prefix boundary"
    )
    assert auth.failed_item_ids == ("trav",)

    ok = _pkg(_item("child", source="src/app/mod.py"))
    assert _eval(ok, read_set=["src/app"]).check("authorization").passed is True


def test_read_set_exact_match_does_not_authorize_traversal_bearing_entry() -> None:
    """Exact source==entry must not bypass path_containment fail-closed rules.

    Defect class: string comparison used for a path security decision.
    ``_path_boundary_authorized`` refuses read_set entries that themselves
    traverse (``..`` components), but ``_source_authorized`` previously
    returned True on bare ``source == entry`` before calling it.

    Named counterfeit: ``if source == entry: return True`` ahead of
    path-boundary / path_containment. This test must fail under that
    counterfeit. Positive control: same spelling without ``..`` still
    authorizes via path-boundary exact containment.
    """
    trav = "src/app/../../etc/passwd"
    pkg = _pkg(_item("trav-exact", source=trav))
    v = _eval(pkg, read_set=[trav])
    auth = v.check("authorization")
    assert auth.passed is False, (
        "traversal-bearing exact read_set entry must not authorize via string equality"
    )
    assert auth.failed_item_ids == ("trav-exact",)

    ok = _pkg(_item("exact-ok", source="src/app/mod.py"))
    assert _eval(ok, read_set=["src/app/mod.py"]).check("authorization").passed is True


def test_dangling_contradiction_target_fails_consistency() -> None:
    """A contradiction pointing at a missing item is unknown, not clean.

    Defect class: missing/unknown mapped to favourable success. Silently
    skipping ``by_id.get(bid) is None`` yields ready=True and
    ``\"no unresolved contradictions\"`` for a package with a dangling
    contradiction reference.

    Named counterfeit: ``if b is None: continue`` so absence is treated
    identically to a resolved/non-contradicting package. This test must
    fail under that counterfeit. Positive control: both sides present and
    resolved still pass.
    """
    a = _item("a", contradicts=("missing-b",), resolved=False)
    v = _eval(_pkg(a))
    cons = v.check("consistency")
    assert cons.passed is False, (
        "dangling contradiction target must fail consistency, not report clean"
    )
    assert "a" in cons.failed_item_ids
    assert (
        "missing" in cons.detail.lower()
        or "dangling" in cons.detail.lower()
        or "missing-b" in cons.detail
    )
    assert v.ready is False

    # Positive control: real pair with one side resolved remains clean.
    ok_a = _item("a", contradicts=("b",), resolved=True)
    ok_b = _item("b", resolved=False)
    assert _eval(_pkg(ok_a, ok_b)).check("consistency").passed is True


def test_empty_read_set_authorizes_nothing() -> None:
    pkg = _pkg(_item("any", source="src/app/mod.py"))
    v = _eval(pkg, read_set=[])
    auth = v.check("authorization")
    assert auth.passed is False
    assert auth.failed_item_ids == ("any",)


def test_secret_kind_requires_authorized_secret_ids() -> None:
    secret = _item("sec-1", kind="secret", source="vault://x")
    denied = _eval(_pkg(secret), read_set=["src/app"], authorized_secret_ids=[])
    auth = denied.check("authorization")
    assert auth.passed is False
    assert auth.failed_item_ids == ("sec-1",)

    allowed = _eval(_pkg(secret), read_set=[], authorized_secret_ids=["sec-1"])
    assert allowed.check("authorization").passed is True


def test_over_budget_package_fails_token_budget_with_margin_arithmetic_shown() -> None:
    pkg = _pkg(_item("big", size_tokens=1000))
    # effective = int(1000 * 0.9) = 900; total 1000 > 900
    v = _eval(pkg, token_budget=1000, safety_margin=0.1)
    tb = v.check("token_budget")
    assert tb.passed is False
    assert tb.data["total_tokens"] == 1000
    assert tb.data["token_budget"] == 1000
    assert tb.data["safety_margin"] == 0.1
    assert tb.data["effective_budget"] == 900
    assert tb.data["overflow"] == 100
    assert "900" in tb.detail or "effective" in tb.detail
    assert "1000" in tb.detail


def test_missing_acknowledgment_blocks_ready() -> None:
    pkg = _pkg(_item("a"), ack_token=None)
    v = _eval(pkg)
    ack = v.check("acknowledgment")
    assert ack.passed is False
    assert "not-yet-acknowledged" in ack.detail or "ack" in ack.detail.lower()
    assert v.ready is False

    blank = _pkg(_item("a"), ack_token="   ")
    assert _eval(blank).check("acknowledgment").passed is False


def test_all_six_checks_report_independently() -> None:
    """One package trips all six; each reports passed is False with its own ids."""
    # presence: missing "required-missing"
    # freshness: unlabeled stale "stale"
    # consistency: a contradicts b, both unresolved
    # authorization: bad source "auth-bad"
    # token_budget: huge size_tokens
    # acknowledgment: no ack
    a = _item(
        "a",
        source="forbidden/path.py",
        freshness=STALE,
        stale_labeled=False,
        contradicts=("b",),
        resolved=False,
        size_tokens=50_000,
    )
    b = _item(
        "b",
        source="also/forbidden.py",
        freshness=STALE,
        stale_labeled=False,
        resolved=False,
        size_tokens=50_000,
    )
    pkg = ContextPackage(
        package_id="all-fail",
        items=(a, b),
        ack_token=None,
    )
    v = evaluate(
        pkg,
        contract_required_ids=["a", "b", "required-missing"],
        token_budget=100,
        now=NOW,
        read_set=["src/app"],
        safety_margin=0.1,
    )
    assert len(v.checks) == 6
    assert [c.name for c in v.checks] == list(CHECK_NAMES)
    assert v.ready is False

    presence = v.check("presence")
    assert presence.passed is False
    assert "required-missing" in presence.failed_item_ids

    freshness = v.check("freshness")
    assert freshness.passed is False
    assert "a" in freshness.failed_item_ids
    assert "b" in freshness.failed_item_ids

    consistency = v.check("consistency")
    assert consistency.passed is False
    assert set(consistency.failed_item_ids) == {"a", "b"}

    authorization = v.check("authorization")
    assert authorization.passed is False
    assert set(authorization.failed_item_ids) == {"a", "b"}

    token_budget = v.check("token_budget")
    assert token_budget.passed is False
    assert token_budget.data["total_tokens"] == 100_000

    acknowledgment = v.check("acknowledgment")
    assert acknowledgment.passed is False

    # Every check failed independently — no masking.
    assert all(c.passed is False for c in v.checks)
    assert set(v.failed_checks()) == set(CHECK_NAMES)


def test_happy_path_all_pass() -> None:
    pkg = _pkg(
        _item("r1", source="src/app/x.py", size_tokens=10),
        ack_token="ack-1",
    )
    v = _eval(pkg, contract_required_ids=["r1"], token_budget=1000, read_set=["src/app"])
    assert v.ready is True
    assert v.failed_checks() == ()
    assert all(c.passed for c in v.checks)


def test_safety_margin_and_budget_validation() -> None:
    pkg = _pkg(_item("a"))
    with pytest.raises(ValueError, match="safety_margin"):
        _eval(pkg, safety_margin=1.0)
    with pytest.raises(ValueError, match="safety_margin"):
        _eval(pkg, safety_margin=-0.1)
    with pytest.raises(ValueError, match="token_budget"):
        _eval(pkg, token_budget=-1)


def test_unparseable_and_future_freshness_fail() -> None:
    bad = _pkg(_item("bad", freshness="not-a-date"))
    assert _eval(bad).check("freshness").passed is False
    assert "bad" in _eval(bad).check("freshness").failed_item_ids

    future = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fut = _pkg(_item("fut", freshness=future))
    assert _eval(fut).check("freshness").passed is False
    assert "fut" in _eval(fut).check("freshness").failed_item_ids


def test_verdict_check_unknown_raises() -> None:
    pkg = _pkg(_item("a"))
    v = _eval(pkg)
    with pytest.raises(KeyError):
        v.check("nope")
    d = v.to_dict()
    assert d["ready"] is True
    assert len(d["checks"]) == 6


def test_modules_import_nothing_from_swarm_db_or_api() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden = {
        "omniagentos.swarm",
        "omniagentos.db",
        "omniagentos.api",
    }
    for rel in (
        "omniagentos/context/package.py",
        "omniagentos/context/completeness.py",
    ):
        path = root / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        for mod in imported:
            for ban in forbidden:
                assert not (mod == ban or mod.startswith(ban + ".")), (
                    f"{rel} imports forbidden module {mod!r}"
                )
        # Also no I/O-ish stdlib imports that would violate pure domain.
        for bad in ("socket", "urllib", "http", "sqlite3", "pathlib", "subprocess"):
            assert bad not in imported, f"{rel} imports {bad}"
