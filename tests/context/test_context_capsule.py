"""Context Capsule v1 — shadow manifest facade tests.

Headline claim: enabling the capsule never changes a single delivered prompt
byte. Manifests are derived from the delivered prompt, not the request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.context.capsule import (
    CONTEXT_CAPSULE_ENV,
    CapsuleContractTruncated,
    CapsuleSecretSource,
    CapsuleSource,
    build_capsule_manifest,
    context_capsule_mode,
    observed_sources_from_prompt,
    write_capsule_manifest,
)
from omniagentos.swarm.prompt_safety import fence_data_block
from omniagentos.swarm.spawn import (
    CORAL_FALLBACK_BYTE_CAP,
    CORAL_FALLBACK_PER_REFERENCE_BYTE_CAP,
    CoralExcerpt,
    CoralReferenceExcerpt,
    UnifiedSpawner,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task-1",
        "run_id": "run-1",
        "project_id": "proj-1",
        "contract_version": "1",
        "repo_sha": "abc123",
        "preset_digest": "preset-aa",
        "byte_cap": 10_000,
        "per_slice_cap": 4_000,
        "compression_mode": "off",
        "lease_snapshot": None,
    }
    base.update(overrides)
    return base


def _src(
    name: str,
    content: str,
    *,
    kind: str = "data",
    rank: int = 0,
) -> CapsuleSource:
    return CapsuleSource(name=name, kind=kind, content=content, rank=rank)


def _render_coral(
    *,
    prompt: str,
    references: tuple[Any, ...],
    excerpt_text: str,
    truncated: bool = False,
    dropped: int = 0,
    total_cap: int = CORAL_FALLBACK_BYTE_CAP,
    per_reference_cap: int = CORAL_FALLBACK_PER_REFERENCE_BYTE_CAP,
) -> str:
    excerpt = CoralExcerpt(
        text=excerpt_text,
        truncated=truncated,
        dropped=dropped,
        per_reference=tuple(
            CoralReferenceExcerpt(
                worker_path=str(getattr(r, "worker_path", f"ref-{i}")),
                inlined_bytes=min(len(excerpt_text.encode("utf-8")), per_reference_cap),
                total_bytes=len(excerpt_text.encode("utf-8")),
                truncated=truncated,
            )
            for i, r in enumerate(references)
        )
        if references
        else (),
    )
    return UnifiedSpawner._render_coral_context(
        prompt=prompt,
        references=references,
        excerpt=excerpt,
        total_cap=total_cap,
        per_reference_cap=per_reference_cap,
    )


class _Ref:
    def __init__(self, worker_path: str, size_bytes: int = 10) -> None:
        self.worker_path = worker_path
        self.kind = "skills"
        self.size_bytes = size_bytes


# ---------------------------------------------------------------------------
# (h) mode resolution
# ---------------------------------------------------------------------------


def test_context_capsule_mode_defaults_invalid_and_absent_to_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONTEXT_CAPSULE_ENV, raising=False)
    assert context_capsule_mode() == "off"
    assert context_capsule_mode(None) == "off"
    assert context_capsule_mode("") == "off"
    assert context_capsule_mode("   ") == "off"
    assert context_capsule_mode("garbage") == "off"
    assert context_capsule_mode("SHADOW") == "shadow"
    assert context_capsule_mode("enforce") == "enforce"
    monkeypatch.setenv(CONTEXT_CAPSULE_ENV, "shadow")
    assert context_capsule_mode() == "shadow"


# ---------------------------------------------------------------------------
# (a) Byte-equality / inertness proof — HEADLINE
# ---------------------------------------------------------------------------


def test_context_capsule_flag_does_not_change_delivered_prompt_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling capsule (shadow/enforce) must not change a single prompt byte.

    Builds a realistic CORAL prompt through the real spawn render path under
    off, shadow, and enforce. Asserts byte-identical UTF-8 encodings.
    """
    refs = (_Ref("var/coral/skills/alpha.md", 100),)
    base_prompt = "Implement the widget. Acceptance: tests pass."
    excerpt = "### var/coral/skills/alpha.md\nskill body content for alpha"

    def _prompt_under(mode: str | None) -> bytes:
        if mode is None:
            monkeypatch.delenv(CONTEXT_CAPSULE_ENV, raising=False)
        else:
            monkeypatch.setenv(CONTEXT_CAPSULE_ENV, mode)
        rendered = _render_coral(
            prompt=base_prompt,
            references=refs,
            excerpt_text=excerpt,
            truncated=False,
        )
        return rendered.encode("utf-8")

    prompt_unset = _prompt_under(None)
    prompt_off = _prompt_under("off")
    prompt_shadow = _prompt_under("shadow")
    prompt_enforce = _prompt_under("enforce")

    assert prompt_unset == prompt_off == prompt_shadow == prompt_enforce
    assert prompt_off  # non-empty realistic brief


def test_observe_context_capsule_is_inert_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spawn observation hook must not mutate prompt/request and returns None."""
    monkeypatch.setenv(CONTEXT_CAPSULE_ENV, "shadow")
    spawner = UnifiedSpawner(var_root=tmp_path)
    workbook = tmp_path / "run-x" / "task-y" / "WORKBOOK.md"
    workbook.parent.mkdir(parents=True)
    workbook.write_text("# wb\n", encoding="utf-8")

    class _Req:
        task_id = "task-y"
        run_id = "run-x"
        working_dir = str(tmp_path)

    prompt = "stable brief for observation"
    task = {"project_id": "p1", "title": "t"}
    swarm_json = {"project_id": "p1", "contract_version": 1}
    prompt_before = prompt
    task_before = dict(task)
    swarm_before = dict(swarm_json)

    result = spawner._observe_context_capsule(
        prompt=prompt,
        request=_Req(),  # type: ignore[arg-type]
        task=task,
        workbook=workbook,
        swarm_json=swarm_json,
    )
    assert result is None
    assert prompt == prompt_before
    assert task == task_before
    assert swarm_json == swarm_before
    manifest_path = workbook.parent / "context-capsule.json"
    assert manifest_path.is_file()


# ---------------------------------------------------------------------------
# (b) Determinism
# ---------------------------------------------------------------------------


def test_manifest_to_json_is_byte_identical_across_builds() -> None:
    prompt = "brief line\n" + fence_data_block("SKILL_PLAYBOOK_RUN_NOTE_CONTENT", "skill-body")
    sources = (
        _src("base", "brief line\n", kind="base_brief", rank=0),
        _src("skill", "skill-body", kind="fenced", rank=1),
    )
    m1 = build_capsule_manifest(prompt=prompt, sources=sources, **_base_kwargs())
    m2 = build_capsule_manifest(prompt=prompt, sources=sources, **_base_kwargs())
    assert m1.to_json().encode("utf-8") == m2.to_json().encode("utf-8")

    # Reordering an input *set* of the same sources must not change the JSON.
    reordered = (sources[1], sources[0])
    m3 = build_capsule_manifest(prompt=prompt, sources=reordered, **_base_kwargs())
    assert m1.to_json().encode("utf-8") == m3.to_json().encode("utf-8")


# ---------------------------------------------------------------------------
# (c) Reflects the delivered prompt — decisive test for doctrine
# ---------------------------------------------------------------------------


def test_manifest_reflects_delivered_prompt_not_request() -> None:
    """brief_digest/brief_bytes measure the delivered prompt; one byte flips both.

    Two different real CORAL configurations must yield different manifests.
    """
    prompt_a = "Implement feature A. Done when green."
    sources_a = (_src("base", prompt_a, kind="base_brief", rank=0),)
    m_a = build_capsule_manifest(prompt=prompt_a, sources=sources_a, **_base_kwargs())

    # Force a true single-byte flip from prompt_a:
    prompt_flip = prompt_a[:-1] + ("X" if prompt_a[-1] != "X" else "Y")
    assert prompt_flip.encode("utf-8") != prompt_a.encode("utf-8")
    assert abs(len(prompt_flip.encode("utf-8")) - len(prompt_a.encode("utf-8"))) in (0, 1)

    m_flip = build_capsule_manifest(
        prompt=prompt_flip,
        sources=(_src("base", prompt_flip, kind="base_brief", rank=0),),
        **_base_kwargs(),
    )
    assert m_a.brief_digest != m_flip.brief_digest
    # When length stays equal, bytes may match — still require digest change always.
    assert m_a.brief_digest != m_flip.brief_digest
    if len(prompt_flip.encode("utf-8")) != len(prompt_a.encode("utf-8")):
        assert m_a.brief_bytes != m_flip.brief_bytes

    # Stronger single-byte length change: append one ASCII byte.
    prompt_plus = prompt_a + "!"
    m_plus = build_capsule_manifest(
        prompt=prompt_plus,
        sources=(_src("base", prompt_plus, kind="base_brief", rank=0),),
        **_base_kwargs(),
    )
    assert m_a.brief_digest != m_plus.brief_digest
    assert m_a.brief_bytes + 1 == m_plus.brief_bytes

    # Two different CORAL configurations → different manifests.
    refs_one = (_Ref("var/coral/skills/one.md"),)
    refs_two = (
        _Ref("var/coral/skills/one.md"),
        _Ref("var/coral/skills/two.md"),
    )
    coral_full = _render_coral(
        prompt="worker brief",
        references=refs_one,
        excerpt_text="FULL_EXCERPT_CONTENT_AAA",
        truncated=False,
        total_cap=CORAL_FALLBACK_BYTE_CAP,
    )
    coral_trunc = _render_coral(
        prompt="worker brief",
        references=refs_two,
        excerpt_text="TRUNC_EXCERPT_CONTENT_BBB",
        truncated=True,
        total_cap=512,  # tighter budget configuration signal
    )
    assert coral_full.encode("utf-8") != coral_trunc.encode("utf-8")

    sources_full = observed_sources_from_prompt(coral_full)
    sources_trunc = observed_sources_from_prompt(coral_trunc)
    m_full = build_capsule_manifest(
        prompt=coral_full, sources=sources_full, **_base_kwargs(byte_cap=50_000)
    )
    m_trunc = build_capsule_manifest(
        prompt=coral_trunc, sources=sources_trunc, **_base_kwargs(byte_cap=50_000)
    )
    assert m_full.brief_digest != m_trunc.brief_digest
    assert m_full.to_json() != m_trunc.to_json()

    # present_in_brief must actually inspect the prompt for included slices.
    for slice_rec in m_full.slices:
        if slice_rec.included and slice_rec.content_for_test:
            assert slice_rec.present_in_brief is True


# ---------------------------------------------------------------------------
# (d) Contract never truncated
# ---------------------------------------------------------------------------


def test_contract_kind_source_over_cap_raises() -> None:
    big = "C" * 500
    with pytest.raises(CapsuleContractTruncated):
        build_capsule_manifest(
            prompt=big,
            sources=(_src("contract", big, kind="contract", rank=0),),
            **_base_kwargs(byte_cap=10_000, per_slice_cap=100),
        )


def test_contract_kind_that_cannot_fit_total_cap_raises() -> None:
    body = "C" * 200
    with pytest.raises(CapsuleContractTruncated):
        build_capsule_manifest(
            prompt=body,
            sources=(_src("contract", body, kind="contract", rank=0),),
            **_base_kwargs(byte_cap=50, per_slice_cap=10_000),
        )


# ---------------------------------------------------------------------------
# (e) Secret rejected
# ---------------------------------------------------------------------------


def test_secret_kind_source_raises_without_digesting() -> None:
    with pytest.raises(CapsuleSecretSource):
        build_capsule_manifest(
            prompt="no secrets here",
            sources=(_src("api_key", "sk-live-secret", kind="secret", rank=0),),
            **_base_kwargs(),
        )


# ---------------------------------------------------------------------------
# (f) Budget compliance + reason codes
# ---------------------------------------------------------------------------


def test_over_budget_optional_slices_excluded_lowest_rank_first() -> None:
    # rank 0 highest priority; rank 2 lowest — exclude lowest first under tight cap.
    s0 = _src("high", "AAAA", kind="data", rank=0)  # 4 bytes
    s1 = _src("mid", "BBBB", kind="data", rank=1)  # 4 bytes
    s2 = _src("low", "CCCC", kind="data", rank=2)  # 4 bytes
    prompt = "AAAABBBBCCCC"
    # Cap fits only two 4-byte slices.
    m = build_capsule_manifest(
        prompt=prompt,
        sources=(s0, s1, s2),
        **_base_kwargs(byte_cap=8, per_slice_cap=100),
    )
    by_name = {s.name: s for s in m.slices}
    assert by_name["high"].included is True
    assert by_name["high"].reason_code == "included"
    assert by_name["mid"].included is True
    assert by_name["mid"].reason_code == "included"
    assert by_name["low"].included is False
    assert by_name["low"].reason_code == "excluded_budget"
    total_included = sum(s.bytes for s in m.slices if s.included)
    assert total_included <= 8


def test_excluded_slice_present_in_brief_is_measured_not_assumed() -> None:
    """Budget exclusion must not hardcode present_in_brief=False.

    Defect class: unmeasured outcome mapped to a favourable clean signal.
    ``present_in_brief`` is an observation of the *delivered* prompt. When a
    source's content is actually in that prompt, the field must report True
    even if the shadow budget model marks the slice excluded.

    Named counterfeit: on the excluded_budget branch, set
    ``present_in_brief=False`` without inspecting ``prompt`` — so "kept out of
    the brief" is claimed without measurement. This test must fail under that
    counterfeit. Positive control: content absent from prompt → False.
    """
    body = "PRESENT_IN_DELIVERED_BRIEF_CONTENT_XYZ"
    prompt_with = f"prefix {body} suffix"
    src = _src("data", body, kind="data", rank=0)
    # byte_cap too small → excluded_budget while content is still in the brief.
    m_present = build_capsule_manifest(
        prompt=prompt_with,
        sources=(src,),
        **_base_kwargs(byte_cap=5, per_slice_cap=10_000),
    )
    assert len(m_present.slices) == 1
    slice_present = m_present.slices[0]
    assert slice_present.included is False
    assert slice_present.reason_code == "excluded_budget"
    assert slice_present.present_in_brief is True, (
        "content is in the delivered prompt; present_in_brief must be measured True, "
        "not hardcoded False because the budget model excluded the slice"
    )

    m_absent = build_capsule_manifest(
        prompt="no matching body here at all",
        sources=(src,),
        **_base_kwargs(byte_cap=5, per_slice_cap=10_000),
    )
    assert m_absent.slices[0].included is False
    assert m_absent.slices[0].present_in_brief is False


def test_empty_slice_content_is_not_reported_present_in_brief() -> None:
    """Empty content must not be reported present_in_brief=True.

    Defect class: non-result presented as favourable result.
    Vacuous ``"" in prompt`` is always True, so treating empty content as
    verified-present claims a measurement that was never made.

    Named counterfeit: ``if not content: return True`` (or the older
    ``content in prompt if content else True``) on the included branch.
    This test must fail under that counterfeit.
    """
    src = _src("empty", "", kind="data", rank=0)
    m = build_capsule_manifest(
        prompt="any delivered brief text that is non-empty",
        sources=(src,),
        **_base_kwargs(byte_cap=10_000, per_slice_cap=10_000),
    )
    assert len(m.slices) == 1
    assert m.slices[0].included is True
    assert m.slices[0].bytes == 0
    assert m.slices[0].present_in_brief is False, (
        "empty content must not be reported as present_in_brief=True "
        "(vacuous empty-string membership is not a measurement)"
    )


def test_per_slice_over_cap_flagged_truncated_budget() -> None:
    body = "Z" * 200
    m = build_capsule_manifest(
        prompt=body,
        sources=(_src("big", body, kind="data", rank=0),),
        **_base_kwargs(byte_cap=10_000, per_slice_cap=50),
    )
    assert len(m.slices) == 1
    assert m.slices[0].included is True
    assert m.slices[0].reason_code == "truncated_budget"
    assert m.slices[0].bytes == 200


# ---------------------------------------------------------------------------
# (g) Flag default off — no manifest file
# ---------------------------------------------------------------------------


def test_write_capsule_manifest_respects_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = "hello capsule"
    sources = (_src("base", prompt, kind="base_brief", rank=0),)
    manifest = build_capsule_manifest(prompt=prompt, sources=sources, **_base_kwargs())

    monkeypatch.delenv(CONTEXT_CAPSULE_ENV, raising=False)
    evidence_off = tmp_path / "off"
    assert write_capsule_manifest(manifest, evidence_dir=evidence_off) is None
    assert not (evidence_off / "context-capsule.json").exists()

    monkeypatch.setenv(CONTEXT_CAPSULE_ENV, "shadow")
    evidence_shadow = tmp_path / "shadow"
    path_shadow = write_capsule_manifest(manifest, evidence_dir=evidence_shadow)
    assert path_shadow is not None
    assert path_shadow == evidence_shadow / "context-capsule.json"
    assert path_shadow.is_file()
    payload = json.loads(path_shadow.read_text(encoding="utf-8"))
    assert payload["brief_digest"] == manifest.brief_digest

    monkeypatch.setenv(CONTEXT_CAPSULE_ENV, "enforce")
    evidence_enforce = tmp_path / "enforce"
    path_enforce = write_capsule_manifest(manifest, evidence_dir=evidence_enforce)
    assert path_enforce is not None
    assert path_enforce.is_file()


# ---------------------------------------------------------------------------
# Discovery helper
# ---------------------------------------------------------------------------


def test_observed_sources_from_prompt_extracts_fences_and_base() -> None:
    fenced = fence_data_block("SKILL_PLAYBOOK_RUN_NOTE_CONTENT", "inner-skill-data")
    prompt = f"[CORAL context]\n{fenced}\n\nworker brief body"
    sources = observed_sources_from_prompt(prompt)
    names = [s.name for s in sources]
    assert "SKILL_PLAYBOOK_RUN_NOTE_CONTENT" in names
    assert any(n == "base_brief" or n.startswith("base_brief_") for n in names)
    # Ranks are stable by first-byte offset.
    ranks = [s.rank for s in sources]
    assert ranks == sorted(ranks)
    skill = next(s for s in sources if s.name == "SKILL_PLAYBOOK_RUN_NOTE_CONTENT")
    assert "inner-skill-data" in skill.content
    # Unfenced regions are true substrings of the delivered prompt.
    for source in sources:
        if source.kind == "base_brief":
            assert source.content in prompt


def test_to_json_is_sorted_deterministic() -> None:
    m = build_capsule_manifest(
        prompt="p",
        sources=(_src("a", "p", kind="base_brief", rank=0),),
        **_base_kwargs(),
    )
    raw = m.to_json()
    # sort_keys=True → keys alphabetically at top level
    assert raw == json.dumps(json.loads(raw), sort_keys=True, indent=2, ensure_ascii=False)
