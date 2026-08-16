"""Non-result presented as favourable result — orgdims sweep regressions.

Defect class: inventing a concrete favourable label when the classifier had no
signal, or reporting bulk/health success when measurement failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.orgdims.classify import ClassificationService
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.store import OrgDimsStore


@pytest.fixture()
def svc(tmp_path: Path) -> OrgDimsService:
    s = OrgDimsService(db_path=str(tmp_path / "nr.db"))
    s.ensure_seeded()
    return s


@pytest.fixture()
def clf(tmp_path: Path) -> ClassificationService:
    store = OrgDimsStore(str(tmp_path / "nr-clf.db"))
    store.seed_taxonomy()
    return ClassificationService(store)


# ---------------------------------------------------------------------------
# Defect 1: unmatched workstream must NOT invent "engineering"
# Counterfeit that must still fail the test: return "product" / "operations"
# (any other concrete workstream) or empty string instead of None.
# ---------------------------------------------------------------------------


def test_unmatched_text_does_not_invent_workstream(clf: ClassificationService) -> None:
    """No vocabulary signal → primary_workstream is unknown, not a default label.

    Historical bug: ``return "engineering", 0.55, ["default:engineering"]`` so a
    zero-signal card was routed as engineering, persisted, and counted in the
    matrix engineering column (optimizer-friendly non-result).
    """
    # Avoid substrings of short aliases/domains (bi/dev/ux/cs/ops/ads) so this
    # case isolates the default-workstream invent, not the substring matcher.
    result = clf.classify_text(
        object_type="board_task",
        object_id="noise-1",
        title="zzzz pure noise aaabb",
        description="no vocabulary signal at all",
        apply=False,
    )
    ws = result.bundle.classification.primary_workstream
    assert ws is None, (
        f"unmatched text must leave primary_workstream unknown (None), "
        f"not invent a concrete workstream; got {ws!r}"
    )
    assert "primary_workstream" not in result.field_confidences or (
        result.field_confidences.get("primary_workstream", 0) == 0
    ), "unknown workstream must not carry a flattering confidence"
    # Prefer explicit unmatched evidence over a fake default label.
    evidence = result.bundle.provenance.evidence_refs or []
    assert not any(str(e).startswith("default:") for e in evidence), (
        f"evidence must not claim a default workstream label: {evidence}"
    )
    # Three-valued path: unknown must not invent routing defaults either.
    agents = result.bundle.execution_links.get("preferred_agent_profiles") or []
    assert agents == ["grok-orchestrator"], (
        f"unknown workstream must not invent domain co-agents; got {agents!r}"
    )
    assert result.bundle.execution_links.get("loop_template_id") is None, (
        "unknown workstream must not invent solo_executor (or any) loop template"
    )
    assert "loop_template_id" in result.needs_review


def test_unmatched_workstream_not_persisted_as_engineering(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    """apply=True must not write a fabricated engineering label onto the card."""
    collab = CollabStore(str(tmp_path / "nr.db"))
    task = BoardTask(
        title="zzzz pure noise aaabb",
        description="no vocabulary signal at all",
    )
    collab.create_board_task(task)

    result = svc.classify_board_task(
        task_id=task.id,
        title=task.title,
        description=task.description or "",
        apply=True,
    )
    assert result.bundle.classification.primary_workstream is None

    persisted = svc.get_board_dimensions(task.id)
    assert persisted is not None
    assert persisted.classification.primary_workstream is None

    matrix = svc.matrix_view()
    eng_count = 0
    for row in matrix["rows"]:
        eng_count += len((row.get("cells") or {}).get("engineering") or [])
    assert eng_count == 0, (
        "zero-signal card must not appear under matrix engineering column; "
        f"got engineering cells={eng_count}, uncategorized={matrix.get('uncategorized')}"
    )
    # Must land in uncategorized (or not appear as a known workstream).
    uncategorized_ids = {c["id"] for c in matrix.get("uncategorized") or []}
    assert task.id in uncategorized_ids


# ---------------------------------------------------------------------------
# Defect 2a: short alias substring match invents a workstream
# Counterfeit that must still fail: keep bare ``a in text`` for aliases, or
# only fix slugs and leave aliases as substring matches.
# ---------------------------------------------------------------------------


def test_short_alias_does_not_match_inside_unrelated_word(clf: ClassificationService) -> None:
    """``bi`` must not fire inside ``webinar``; ``dev`` must not fire inside ``device``.

    Channels already use word boundaries so ``meta`` ≠ ``metacog``. Aliases did
    not, so creative/webinar work was labelled data-analytics via alias ``bi``.
    """
    webinar = clf.classify_text(
        object_type="board_task",
        object_id="web-1",
        title="Create webinar skill",
        description="Generate webinar scripts and emails",
        apply=False,
    )
    assert webinar.bundle.classification.primary_workstream != "data-analytics", (
        "alias 'bi' must not match as a substring of 'webinar' "
        f"(evidence={webinar.bundle.provenance.evidence_refs})"
    )
    # Real creative signal should still land on creative when present.
    assert webinar.bundle.classification.primary_workstream in {
        "creative",
        None,
    } or "webinar" in (webinar.bundle.classification.channels or [])

    device = clf.classify_text(
        object_type="board_task",
        object_id="dev-1",
        title="Device inventory",
        description="List devices in the lab closet",
        apply=False,
    )
    assert device.bundle.classification.primary_workstream != "engineering", (
        "alias 'dev' must not match as a substring of 'device' "
        f"(evidence={device.bundle.provenance.evidence_refs})"
    )
    assert device.bundle.classification.primary_workstream is None


def test_real_alias_still_matches_as_whole_token(clf: ClassificationService) -> None:
    """Word-boundary fix must not break genuine alias hits (coding → engineering)."""
    result = clf.classify_text(
        object_type="board_task",
        object_id="code-1",
        title="Something about coding",
        description="implement feature in the service",
        apply=False,
    )
    assert result.bundle.classification.primary_workstream == "engineering"
    evidence = " ".join(result.bundle.provenance.evidence_refs or [])
    assert "coding" in evidence or "engineering" in evidence


# ---------------------------------------------------------------------------
# Defect 2b: workstream SLUG substring must not invent a workstream
# Counterfeit that must fail: restore ``if slug in text or slug.replace(...)``
# while keeping alias/domain phrase fixes. Reviewer proved prior tests stayed
# green under that mutation — this case must bind the slug site.
# ---------------------------------------------------------------------------


def test_workstream_slug_does_not_match_inside_unrelated_word(
    clf: ClassificationService,
) -> None:
    """Slug ``sales`` must not fire inside ``wholesales``; ``product`` not in ``byproduct``.

    Historical matcher: ``if slug in text or slug.replace("-", " ") in text``.
    """
    wholesales = clf.classify_text(
        object_type="board_task",
        object_id="ws-slug-1",
        title="Wholesales inventory count",
        description="zzzz pure noise aaabb",
        apply=False,
    )
    assert wholesales.bundle.classification.primary_workstream != "sales", (
        "workstream slug 'sales' must not match as a substring of 'wholesales' "
        f"(evidence={wholesales.bundle.provenance.evidence_refs})"
    )
    assert wholesales.bundle.classification.primary_workstream is None

    byproduct = clf.classify_text(
        object_type="board_task",
        object_id="ws-slug-2",
        title="Byproduct analysis notes",
        description="zzzz pure noise aaabb",
        apply=False,
    )
    assert byproduct.bundle.classification.primary_workstream != "product", (
        "workstream slug 'product' must not match as a substring of 'byproduct' "
        f"(evidence={byproduct.bundle.provenance.evidence_refs})"
    )
    assert byproduct.bundle.classification.primary_workstream is None


# ---------------------------------------------------------------------------
# Defect 2c: DOMAIN hint substring must not invent a workstream
# Counterfeit that must fail: restore
# ``if d_s in text or d_s.replace("-", " ") in text`` (and the post-match
# domain extractor ``if d in text or ...``) while leaving alias/slug fixes.
# ---------------------------------------------------------------------------


def test_domain_hint_does_not_match_inside_unrelated_word(
    clf: ClassificationService,
) -> None:
    """Domain ``model`` must not fire inside ``remodeling`` → research.

    Domain hits alone reach the 0.35 threshold and invent a workstream label.
    """
    remodel = clf.classify_text(
        object_type="board_task",
        object_id="dom-1",
        title="Remodeling kitchen schedule",
        description="zzzz pure noise aaabb",
        apply=False,
    )
    evidence = remodel.bundle.provenance.evidence_refs or []
    assert remodel.bundle.classification.primary_workstream != "research", (
        "domain 'model' must not match as a substring of 'remodeling' "
        f"(evidence={evidence})"
    )
    assert not any("domain-hint:model" in str(e) for e in evidence), evidence
    assert remodel.bundle.classification.primary_workstream is None

    imagery = clf.classify_text(
        object_type="board_task",
        object_id="dom-2",
        title="Imagery assets folder",
        description="zzzz pure noise aaabb",
        apply=False,
    )
    evidence2 = imagery.bundle.provenance.evidence_refs or []
    assert imagery.bundle.classification.primary_workstream != "creative", (
        "domain 'image' must not match as a substring of 'imagery' "
        f"(evidence={evidence2})"
    )
    assert not any("domain-hint:image" in str(e) for e in evidence2), evidence2


def test_domain_under_workstream_does_not_match_substring(
    clf: ClassificationService,
) -> None:
    """Even after workstream is known, domain list must use whole-token match.

    Counterfeit: restore ``if d in text or d.replace("-", " ") in text or d in tokens``
    under the workstream domain extractor while leaving _match_workstream fixed.
    """
    result = clf.classify_text(
        object_type="board_task",
        object_id="dom-under-1",
        title="Brandishing a torch metaphor",
        description="creative narrative for the torch scene",
        discipline="creatives",
        apply=False,
    )
    assert result.bundle.classification.primary_workstream == "creative"
    domains = result.bundle.classification.domains or []
    assert "brand" not in domains, (
        "domain 'brand' must not match as a substring of 'brandishing'; "
        f"got domains={domains}"
    )


# ---------------------------------------------------------------------------
# Defect 3: bulk_reclassify reports ok:True when every row failed
# Counterfeit that must still fail: always set ok=True while listing errors.
# ---------------------------------------------------------------------------


def test_bulk_reclassify_ok_false_when_all_rows_fail(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    """Total classify failure is not a successful bulk run.

    Historical bug: return dict always included ``ok: True`` even when
    ``classified=0`` and every scanned row was in ``errors``.
    """
    now = utc_now_iso()
    with svc.store._lock:
        for i in range(2):
            svc.store._connection.execute(
                "INSERT INTO board_tasks "
                "(id, title, description, discipline, priority, status, org_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"fail_{i:03d}",
                    f"t{i}",
                    "d",
                    None,
                    "normal",
                    "open",
                    "{}",
                    now,
                    now,
                ),
            )
        svc.store._connection.commit()

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("classify exploded")

    svc.classifier.classify_text = _boom  # type: ignore[method-assign]

    out = svc.bulk_reclassify(only_missing=True, limit=10)
    assert out["scanned"] >= 2
    assert out["classified"] == 0
    assert len(out["errors"]) >= 2
    assert out["ok"] is False, (
        f"bulk_reclassify must not report ok=True when every row failed; got {out!r}"
    )


def test_bulk_reclassify_ok_true_when_clean_success(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    """Happy path still reports ok=True (guards against always-False counterfeit)."""
    collab = CollabStore(str(tmp_path / "nr.db"))
    task = BoardTask(
        title="Implement backend API endpoint",
        description="Add REST route and unit tests",
        discipline="coding",
    )
    collab.create_board_task(task)
    out = svc.bulk_reclassify(only_missing=True, limit=20)
    assert out["errors"] == []
    assert out["ok"] is True
    assert out["classified"] >= 1


# ---------------------------------------------------------------------------
# Defect 4: zero-signal must not invent priority/lifecycle/risk + confidence
# Counterfeit: restore pri="normal"/lifecycle="ready"/risk="reversible_internal"
# with field_conf > 0.
# ---------------------------------------------------------------------------


def test_zero_signal_does_not_invent_priority_lifecycle_risk(
    clf: ClassificationService,
) -> None:
    """No evidence → priority/lifecycle/risk stay unknown (not flattering defaults)."""
    result = clf.classify_text(
        object_type="board_task",
        object_id="soft-1",
        title="zzzz pure noise aaabb",
        description="no vocabulary signal at all",
        apply=False,
    )
    c = result.bundle.classification
    assert c.primary_workstream is None
    assert c.priority is None, f"invented priority={c.priority!r}"
    assert c.lifecycle is None, f"invented lifecycle={c.lifecycle!r}"
    assert c.risk_class is None, f"invented risk_class={c.risk_class!r}"
    for field in ("priority", "lifecycle", "risk_class", "primary_workstream"):
        assert field in result.needs_review, result.needs_review
        conf = result.field_confidences.get(field)
        assert conf is None or conf == 0, (
            f"{field} must not carry flattering confidence; got {conf!r}"
        )
    assert result.bundle.provenance.confidence == 0.0, (
        f"zero-signal provenance confidence must be 0.0; "
        f"got {result.bundle.provenance.confidence}"
    )
    assert result.status == "suggested"


# ---------------------------------------------------------------------------
# Defect 5: health must not report ok when no primary agents are available
# Counterfeit: hardcode ``"ok": True`` and static primary_orchestrator.
# ---------------------------------------------------------------------------


def test_health_not_ok_when_no_primary_agents(svc: OrgDimsService) -> None:
    """Empty primary-agent roster is not a healthy orgdims service."""
    before = svc.health()
    assert before["ok"] is True
    assert "grok-orchestrator" in before["grok_metacog_agents"]

    with svc.store._lock:
        svc.store._connection.execute(
            "UPDATE org_agent_profiles SET enabled = 0 WHERE metacog_primary = 1"
        )
        svc.store._connection.commit()

    after = svc.health()
    assert after["grok_metacog_agents"] == [], after
    assert after["ok"] is False, (
        f"health must not report ok=True when primary agents are empty; got {after!r}"
    )
    assert after["primary_orchestrator"] is None, after


def test_health_ok_when_primary_agents_present(svc: OrgDimsService) -> None:
    """Healthy seed still reports ok (guards always-False counterfeit)."""
    h = svc.health()
    assert h["ok"] is True
    assert h["primary_orchestrator"] == "grok-orchestrator"
    assert "grok-orchestrator" in h["grok_metacog_agents"]


# ---------------------------------------------------------------------------
# Defect 6: recommend_loop must not present a zero-evidence pick as ranked
# Counterfeit that must still fail: refuse only when workstream is None AND
# risk is None, but return LOOP_TEMPLATES[0] when every candidate scores 0
# (unrecognised workstream + risk_class=None).
# ---------------------------------------------------------------------------


def test_recommend_loop_refuses_when_both_signals_unset(svc: OrgDimsService) -> None:
    """workstream=None risk=None is insufficient signal, not a ranked pick."""
    rec = svc.recommend_loop(workstream=None, risk_class=None)
    assert rec["recommended"] is None, (
        f"unset workstream+risk must not invent a loop; got {rec!r}"
    )
    assert "insufficient" in rec["reason"].lower() or "unset" in rec["reason"].lower()


def test_recommend_loop_refuses_unrecognised_workstream_with_no_risk(
    svc: OrgDimsService,
) -> None:
    """Unrecognised workstream + risk=None must not surface LOOP_TEMPLATES[0].

    Historical bug: every candidate scored 0, stable reverse=True sort left
    order untouched, and best = candidates[0][1] returned solo_executor as
    ``"recommended"`` under ``"ranked by Grok strategy selector applicability"``.
    """
    from omniagentos.orgdims.service import LOOP_TEMPLATES

    assert LOOP_TEMPLATES, "registry must be non-empty for this defect to exist"
    first_id = LOOP_TEMPLATES[0]["id"]

    rec = svc.recommend_loop(
        workstream="no-such-workstream-zzzz",
        risk_class=None,
    )
    assert rec["recommended"] is None, (
        f"zero-evidence ranking must refuse, not return {first_id!r} "
        f"(or any template) as recommended; got {rec!r}"
    )
    # Must not claim a successful ranked applicability result.
    assert "ranked by" not in (rec.get("reason") or "").lower(), rec
    assert rec.get("recommended") is not first_id


def test_recommend_loop_refuses_when_risk_also_unmatched(
    svc: OrgDimsService,
) -> None:
    """Unrecognised workstream + unrecognised risk still has zero evidence."""
    rec = svc.recommend_loop(
        workstream="no-such-workstream-zzzz",
        risk_class="no-such-risk-zzzz",
    )
    assert rec["recommended"] is None, (
        f"zero-score candidates must not invent a recommended loop; got {rec!r}"
    )


def test_recommend_loop_still_ranks_when_workstream_matches(
    svc: OrgDimsService,
) -> None:
    """Happy path still returns a real template (guards always-None counterfeit)."""
    rec = svc.recommend_loop(workstream="creative", risk_class="bounded_external")
    assert rec["recommended"] is not None
    assert rec["recommended"]["id"] == "generate_critique_repair_verify"
    assert "ranked" in rec["reason"].lower()


def test_recommend_loop_risk_only_match_is_not_zero_evidence(
    svc: OrgDimsService,
) -> None:
    """workstream unset but a recognised risk_class is real applicability signal."""
    rec = svc.recommend_loop(workstream=None, risk_class="irreversible")
    assert rec["recommended"] is not None, (
        f"risk-only match must still recommend; got {rec!r}"
    )
    assert rec["recommended"]["id"] in {
        "generator_critic",
        "verify_then_execute",
    }


# ---------------------------------------------------------------------------
# Defect 7: malformed org_json must not look like genuine empty dimensions
# Counterfeit: ``_loads`` collapses parse errors onto the empty default.
# ---------------------------------------------------------------------------


def test_malformed_org_json_not_same_as_empty(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    """Unreadable org_json is distinct from a genuine empty ``{}`` envelope."""
    now = utc_now_iso()
    with svc.store._lock:
        for task_id, org_json in (
            ("empty_org_1", "{}"),
            ("bad_org_1", "{unparseable"),
        ):
            svc.store._connection.execute(
                "INSERT INTO board_tasks "
                "(id, title, description, discipline, priority, status, org_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    task_id,
                    "d",
                    None,
                    "normal",
                    "open",
                    org_json,
                    now,
                    now,
                ),
            )
        svc.store._connection.commit()

    empty = svc.get_board_dimensions("empty_org_1")
    bad = svc.get_board_dimensions("bad_org_1")
    assert empty is not None and bad is not None
    assert empty.provenance.source != "unreadable", empty.provenance
    assert bad.provenance.source == "unreadable", (
        f"malformed org_json must surface source=unreadable, not look empty; "
        f"got source={bad.provenance.source!r} conf={bad.provenance.confidence}"
    )
    assert empty.provenance.source == "empty", empty.provenance
    assert "parse_error:org_json" in (bad.provenance.evidence_refs or [])
    # Must not claim a successful AI classification provenance on unreadable.
    assert bad.provenance.source != "ai_classification"
    assert bad.provenance.confidence == 0.0
