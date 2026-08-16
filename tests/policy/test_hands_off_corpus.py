"""AUTO-APPROVE Phase 6 — classifier + standing roots corpus.

Replays the measured shapes from HANDOFF/AUTO-APPROVE-PLAN.md and asserts
safe in-scope work auto-runs while hard-stops still park.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import AutonomyTier, evaluate_action, load_policy
from omniagentos.policy.roots import clear_roots_cache, merge_standing_roots, standing_write_roots
from omniagentos.policy.shell import classify_shell


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "qa.mjs").write_text("console.log(1)\n", encoding="utf-8")
    (tmp_path / "index.php").write_text("<?php\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_phase2_safe_shapes_auto_run(workspace: Path) -> None:
    d = str(workspace)
    cases = [
        "mkdir -p outputs/delivery/KIT",
        "touch outputs/x.txt",
        "git add index.php",
        "git commit -m x",
        f"cd {d} && git commit -m x",
        "cd outputs && node qa.mjs",
        "NODE_PATH=/tmp/node_modules node outputs/qa.mjs",
        "cp outputs/qa.mjs outputs/qa2.mjs",
        "node outputs/qa.mjs",
    ]
    for cmd in cases:
        cls = classify_shell(cmd, d)
        assert cls in (
            ActionClass.INTERNAL_REVERSIBLE,
            ActionClass.READ_ONLY,
        ), f"{cmd!r} -> {cls}"


def test_phase2_dangerous_siblings_still_park(workspace: Path) -> None:
    d = str(workspace)
    cases = [
        "PATH=/evil ls",
        "mkdir -p /System/x",
        "git push",
        "cd /etc && rm -rf /",
        "LD_PRELOAD=/evil.so node outputs/qa.mjs",
    ]
    for cmd in cases:
        cls = classify_shell(cmd, d)
        assert cls == ActionClass.IRREVERSIBLE, f"{cmd!r} -> {cls}"


def test_standing_roots_include_desktop_and_var() -> None:
    clear_roots_cache()
    roots = standing_write_roots()
    joined = " ".join(roots)
    assert "Desktop" in joined
    assert "var" in joined
    # Product SOURCE must not be a standing write root.
    assert not any(r.endswith("/omniagentos") and "var" not in r for r in roots)


def test_merge_standing_roots_dedupes_working_dir(tmp_path: Path) -> None:
    clear_roots_cache()
    merged = merge_standing_roots([str(tmp_path)], working_dir=str(tmp_path))
    # standing roots still present even when working_dir is excluded from list
    assert any("Desktop" in r or "var" in r for r in merged)


def test_unknown_containment_does_not_admit_standing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tri-state unknown must not become an admitted standing write root.

    ``inode_path_is_within_anchored`` returns ``None`` when identity cannot be
    proved.  Secret and ``never`` filters are exclusion gates: unknown must
    block, not grant.  Failing-on-revert: ``is True`` polarity treats None as
    "not secret / not under never" and admits the root.
    """
    from omniagentos.policy import roots as roots_mod

    candidate = str(tmp_path / "standing-candidate")
    Path(candidate).mkdir()

    # Force every containment probe to unknown — the defect surface.
    monkeypatch.setattr(
        roots_mod,
        "inode_path_is_within_anchored",
        lambda candidate, root: None,
    )

    assert roots_mod._is_or_under_secret(candidate) is True
    assert roots_mod._is_under(candidate, str(tmp_path / "never-block")) is True
    assert roots_mod._is_blocked_by_never(candidate, {str(tmp_path / "never-block")}) is True

    # Positive control: proved-outside still admits through the secret filter.
    monkeypatch.setattr(
        roots_mod,
        "inode_path_is_within_anchored",
        lambda candidate, root: False,
    )
    assert roots_mod._is_or_under_secret(candidate) is False
    assert roots_mod._is_under(candidate, str(tmp_path / "never-block")) is False


def test_policy_hands_off_and_expiry() -> None:
    cfg = load_policy()
    assert cfg.autonomy == AutonomyTier.HANDS_OFF
    assert cfg.approval_expiry_hours >= 72
    # In-scope irreversible auto under hands_off
    decision = evaluate_action(ActionClass.IRREVERSIBLE, cfg, in_granted_scope=True)
    assert decision.requires_approval is False
    # Out-of-scope irreversible still hard-stops
    decision2 = evaluate_action(ActionClass.IRREVERSIBLE, cfg, in_granted_scope=False)
    assert decision2.requires_approval is True


def test_formation_selector() -> None:
    from omniagentos.formation import select_formation

    assert select_formation(goal="fix the login bug").id == "coding"
    assert select_formation(goal="launch FB ad campaign").id == "marketing"
    assert select_formation(task_class="research").id == "research"
