"""TN.3 — artifact protocols, ad-copy platform profiles, rubrics.

Two tests here matter more than the rest:

``test_duration_degrades_gracefully_without_ffprobe``
    A host without ffmpeg installed must produce a WARNING, never a violation. A
    validator that read "no probe" as "zero seconds" would fail every video on
    such a host, i.e. would make the feature depend on an undeclared system
    package.

``test_negated_banned_claim_is_not_flagged``
    "results are not guaranteed" is the disclaimer compliance ASKS for. Flagging
    it is the false positive that gets a banned-claims lexicon switched off, and
    a lexicon nobody runs catches nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.contracts import TaskMode
from omniagentos.workmodes import protocols
from omniagentos.workmodes.protocols import (
    AD_DISCLAIMER_PRESETS,
    MANIFEST_FILENAME,
    PROMPT_FILENAME,
    acceptance_to_json,
    build_acceptance,
    check_files,
    platform_profile,
    probe_duration_s,
    protocol_for,
    validate_ad_copy,
)

# --- the protocol table ----------------------------------------------------


def test_code_has_no_artifact_protocol() -> None:
    """A code task is graded against its diff; a second, weaker path would rot."""
    assert protocol_for(TaskMode.CODE) is None
    report = check_files(TaskMode.CODE, [])
    assert report.violations == ()
    assert "grade the diff" in report.warnings[0]


@pytest.mark.parametrize(
    "mode",
    [TaskMode.REPORT, TaskMode.CONTENT, TaskMode.IMAGE, TaskMode.VIDEO, TaskMode.INTAKE_PROCESSING],
)
def test_every_non_code_mode_has_a_protocol(mode: TaskMode) -> None:
    protocol = protocol_for(mode)
    assert protocol is not None
    assert protocol.default_format in protocol.allowed_formats
    assert MANIFEST_FILENAME in protocol.manifest_files


def test_image_protocol_shape() -> None:
    protocol = protocol_for(TaskMode.IMAGE)
    assert protocol is not None
    assert protocol.require_prompt is True
    assert protocol.min_bytes >= 1024
    assert protocol.allowed_formats == ("png", "jpg", "jpeg", "webp")
    assert PROMPT_FILENAME in protocol.manifest_files


def test_video_protocol_shape() -> None:
    protocol = protocol_for(TaskMode.VIDEO)
    assert protocol is not None
    assert protocol.allowed_formats == ("mp4", "webm", "mov")
    assert protocol.require_prompt is True
    assert protocol.min_duration_s is not None


def test_image_and_video_declare_they_are_unwired() -> None:
    """No provider is wired for these in this repo; a fake model id would be a lie."""
    for mode in (TaskMode.IMAGE, TaskMode.VIDEO):
        protocol = protocol_for(mode)
        assert protocol is not None
        assert protocol.requires_wiring is True
    report = check_files(
        TaskMode.IMAGE, [("/x/a.png", "a.png", 4096)], prompt="a logo", probe=False
    )
    assert any("no provider is wired" in warning for warning in report.warnings)


def test_provider_and_model_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = protocol_for(TaskMode.IMAGE)
    assert protocol is not None
    assert protocol.env_provider() is None
    monkeypatch.setenv("OMNIAGENTOS_WORKMODE_IMAGE_PROVIDER", "cli-gemini")
    monkeypatch.setenv("OMNIAGENTOS_WORKMODE_IMAGE_MODEL", "some-image-model")
    assert protocol.env_provider() == "cli-gemini"
    assert protocol.env_model() == "some-image-model"


# --- file checks -----------------------------------------------------------


def test_disallowed_format_is_a_violation() -> None:
    report = check_files(TaskMode.IMAGE, [("/x/a.gif", "a.gif", 4096)], prompt="p", probe=False)
    assert any("not allowed" in v for v in report.violations)


def test_under_min_bytes_is_a_violation() -> None:
    report = check_files(TaskMode.IMAGE, [("/x/a.png", "a.png", 200)], prompt="p", probe=False)
    assert any("below the 1024-byte floor" in v for v in report.violations)


def test_missing_prompt_is_a_violation_for_image() -> None:
    report = check_files(TaskMode.IMAGE, [("/x/a.png", "a.png", 4096)], prompt="  ", probe=False)
    assert any("requires a generation prompt" in v for v in report.violations)


def test_no_deliverables_is_a_violation() -> None:
    report = check_files(TaskMode.REPORT, [("/x/manifest.json", MANIFEST_FILENAME, 10)])
    assert any("no deliverable files" in v for v in report.violations)


def test_max_outputs_is_enforced() -> None:
    files = [(f"/x/{i}.png", f"{i}.png", 4096) for i in range(50)]
    report = check_files(TaskMode.IMAGE, files, prompt="p", probe=False)
    assert any("exceeds max_outputs" in v for v in report.violations)


def test_executable_format_warns_but_does_not_fail() -> None:
    report = check_files(
        TaskMode.CONTENT, [("/x/run.sh", "run.sh", 400), ("/x/a.md", "a.md", 400)], probe=False
    )
    assert any("executable format" in w for w in report.warnings)
    # the .sh is still a format violation for content; the WARNING is the point
    assert all("executable format" not in v for v in report.violations)


def test_manifest_and_prompt_files_are_not_deliverables() -> None:
    report = check_files(
        TaskMode.REPORT,
        [
            ("/x/manifest.json", MANIFEST_FILENAME, 10),
            ("/x/prompt.txt", PROMPT_FILENAME, 10),
            ("/x/r.md", "r.md", 900),
        ],
        probe=False,
    )
    assert report.ok
    assert [check.rel_path for check in report.files] == ["r.md"]


# --- duration probing ------------------------------------------------------


def test_duration_degrades_gracefully_without_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocols.shutil, "which", lambda _name: None)
    assert probe_duration_s("/x/a.mp4") is None
    report = check_files(TaskMode.VIDEO, [("/x/a.mp4", "a.mp4", 20000)], prompt="p")
    assert report.ok is True
    assert any("duration not checked" in w for w in report.warnings)


def test_duration_out_of_range_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocols, "probe_duration_s", lambda *_a, **_k: 0.2)
    report = check_files(TaskMode.VIDEO, [("/x/a.mp4", "a.mp4", 20000)], prompt="p")
    assert any("under the 1.0s minimum" in v for v in report.violations)


def test_duration_in_range_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocols, "probe_duration_s", lambda *_a, **_k: 30.0)
    report = check_files(TaskMode.VIDEO, [("/x/a.mp4", "a.mp4", 20000)], prompt="p")
    assert report.ok is True
    assert report.files[0].duration_s == 30.0


def test_probe_parses_ffprobe_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(protocols.shutil, "which", lambda _name: "/usr/bin/ffprobe")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=(), returncode=0, stdout="12.5\n", stderr="")

    monkeypatch.setattr(protocols.subprocess, "run", fake_run)
    assert probe_duration_s(str(media)) == 12.5


def test_probe_of_a_broken_container_is_unknown_not_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"not a video")
    monkeypatch.setattr(protocols.shutil, "which", lambda _name: "/usr/bin/ffprobe")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout="", stderr="bad")

    monkeypatch.setattr(protocols.subprocess, "run", fake_run)
    assert probe_duration_s(str(media)) is None


# --- ad-copy profiles ------------------------------------------------------


def test_meta_character_limits() -> None:
    report = validate_ad_copy(
        "meta",
        {"headline": ["x" * 41], "primary_text": ["y" * 126]},
    )
    kinds = {(f.kind, f.slot) for f in report.violations}
    assert ("over_limit", "headline") in kinds
    assert ("over_limit", "primary_text") in kinds


def test_meta_at_the_limit_passes() -> None:
    report = validate_ad_copy("meta", {"headline": ["x" * 40], "primary_text": ["y" * 125]})
    assert report.ok is True


def test_google_rsa_slot_counts() -> None:
    report = validate_ad_copy("google_rsa", {"headline": ["a", "b"], "description": ["d" * 91]})
    kinds = {f.kind for f in report.violations}
    assert "too_few" in kinds  # RSA needs 3+ headlines
    assert "over_limit" in kinds  # 91 > 90
    # descriptions: only one supplied, minimum is 2
    assert any(f.kind == "too_few" and f.slot == "description" for f in report.violations)


def test_google_rsa_headline_limit_is_30() -> None:
    profile = platform_profile("google-RSA")
    assert profile is not None
    headline = profile.slot("headline")
    assert headline is not None
    assert (headline.max_chars, headline.min_count, headline.max_count) == (30, 3, 15)


def test_unknown_slot_and_platform() -> None:
    assert validate_ad_copy("tiktok", {"headline": ["x"]}).violations[0].kind == "unknown_platform"
    report = validate_ad_copy("meta", {"headline": ["ok"], "primary_text": ["ok"], "cta": ["Buy"]})
    assert any(f.kind == "unknown_slot" for f in report.violations)


def test_missing_required_slot() -> None:
    report = validate_ad_copy("meta", {"headline": ["ok"]})
    assert any(f.kind == "missing_slot" and f.slot == "primary_text" for f in report.violations)


def test_banned_claim_is_flagged() -> None:
    report = validate_ad_copy(
        "meta",
        {"headline": ["Guaranteed income"], "primary_text": ["Risk free, get rich quick"]},
    )
    banned = [f for f in report.violations if f.kind == "banned_claim"]
    assert len(banned) >= 2
    assert any("guaranteed income" in f.detail.lower() for f in banned)


def test_negated_banned_claim_is_not_flagged() -> None:
    """'results are not guaranteed' is the DISCLAIMER, not the claim."""
    report = validate_ad_copy(
        "meta",
        {
            "headline": ["A real skill, real work"],
            "primary_text": ["Results are not guaranteed and individual results vary."],
        },
    )
    assert [f.kind for f in report.violations] == []


def test_overlapping_banned_rules_report_once() -> None:
    report = validate_ad_copy("meta", {"headline": ["Guaranteed returns"], "primary_text": ["ok"]})
    banned = [f for f in report.violations if f.kind == "banned_claim"]
    assert len(banned) == 1


def test_required_disclaimer() -> None:
    variants = {"headline": ["Earn more"], "primary_text": ["A repeatable process."]}
    report = validate_ad_copy("meta", variants, disclaimers=AD_DISCLAIMER_PRESETS["income"])
    assert {f.kind for f in report.violations} == {"missing_disclaimer"}
    with_disclaimer = {
        "headline": ["Earn more"],
        "primary_text": ["A repeatable process. Results not typical; individual results vary."],
    }
    assert validate_ad_copy("meta", with_disclaimer, disclaimers=AD_DISCLAIMER_PRESETS["income"]).ok


def test_duplicate_variant_is_a_warning_not_a_violation() -> None:
    report = validate_ad_copy(
        "meta", {"headline": ["Same", "same"], "primary_text": ["Body copy here."]}
    )
    assert report.ok is True
    assert [f.kind for f in report.warnings] == ["duplicate"]


def test_empty_variant_is_a_violation() -> None:
    report = validate_ad_copy("meta", {"headline": ["  "], "primary_text": ["ok"]})
    assert any(f.kind == "empty" for f in report.violations)


def test_extra_banned_rules_can_be_supplied() -> None:
    report = validate_ad_copy(
        "meta",
        {"headline": ["Our secret sauce"], "primary_text": ["ok"]},
        banned=[r"\bsecret sauce\b"],
    )
    assert any(f.kind == "banned_claim" for f in report.violations)


# --- rubrics + acceptance --------------------------------------------------


def test_every_artifact_mode_has_a_rubric() -> None:
    for mode in (
        TaskMode.REPORT,
        TaskMode.CONTENT,
        TaskMode.IMAGE,
        TaskMode.VIDEO,
        TaskMode.INTAKE_PROCESSING,
    ):
        assert protocols.MODE_RUBRICS[mode]


def test_build_acceptance_carries_rubric_and_criteria() -> None:
    acceptance = build_acceptance(
        TaskMode.CONTENT,
        expected_files=["ads.json"],
        must_include=["webinar"],
        must_not_include=["guaranteed"],
        platform="meta",
        notes="three angles",
    )
    payload = acceptance_to_json(acceptance)
    assert payload["mode"] == "content"
    assert payload["expected_files"] == ["ads.json"]
    assert payload["platform"] == "meta"
    assert {c["key"] for c in payload["rubric"]} >= {"compliance", "limits"}
    assert acceptance_to_json(None) == {}


def test_extra_criteria_are_appended() -> None:
    extra = protocols.RubricCriterion("brand_voice", "Matches the brand voice doc.", 2)
    acceptance = build_acceptance(TaskMode.CONTENT, extra_criteria=[extra])
    assert acceptance.rubric[-1] is extra
