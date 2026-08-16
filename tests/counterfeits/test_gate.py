"""W4-10 counterfeit corpus gate — pytest entry.

Runs every corpus entry as apply → must_fail RED → restore. Surviving
counterfeits fail the gate loudly; they are findings, not soft skips.

The corpus is ``corpus.toml`` **plus** every ``corpus.d/*.toml`` fragment (see
``harness.load_corpus``), so a lane's fragment is gated exactly like an entry
appended to the base manifest — otherwise fragments would be a way to add
counterfeits that nothing runs.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.counterfeits.harness import (
    REPO_ROOT,
    CounterfeitApplyError,
    CounterfeitCollectionError,
    CounterfeitControlError,
    CounterfeitSurvived,
    CounterfeitWrongFailure,
    assert_entry_caught,
    format_report,
    load_corpus,
    run_control,
    run_entry,
)

# Corpus is a release-style gate: keep it out of the default unit selection if a
# marker filter is used, but always runnable via `make counterfeit-gate`.
pytestmark = pytest.mark.counterfeit_gate


def test_manifest_loads_and_patches_exist() -> None:
    entries = load_corpus()
    assert len(entries) >= 9, "seed corpus must include the nine real counterfeits"
    ids = {e.id for e in entries}
    required = {
        "score-zero-work-special-case",
        "path-startswith-containment",
        "favourable-default-swap",
        "hardcoded-none-empty-denom",
        "width-inflation-max-units",
        "seq-only-escalation",
        "token-path-copy",
        "sleep-hides-leak",
        "assert-terminal-accepts-failed",
    }
    missing = required - ids
    assert not missing, f"corpus missing seed ids: {sorted(missing)}"
    for entry in entries:
        assert entry.patch_path.is_file(), entry.patch_path
        assert entry.must_fail
        assert entry.failure_re
        # TASK 3: Detect bit-rotted patches — they must apply cleanly
        result = subprocess.run(
            ["git", "apply", "--verbose", "--whitespace=nowarn", "--check", str(entry.patch_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{entry.id} patch does not apply cleanly (bit-rotted entry):\n"
            f"  patch: {entry.patch_path}\n"
            f"  error: {result.stderr.split(chr(10))[0] if result.stderr else result.stdout}"
        )


def test_control_unpatched_must_fail_set_is_green() -> None:
    """Without the control, a corpus entry aimed at an already-broken test looks green."""
    entries = load_corpus()
    run_control(entries)


@pytest.mark.parametrize(
    "entry_id",
    [e.id for e in load_corpus()],
    ids=[e.id for e in load_corpus()],
)
def test_counterfeit_is_caught(entry_id: str) -> None:
    """Each counterfeit must make its named tests fail for the recorded reason."""
    entries = {e.id: e for e in load_corpus()}
    entry = entries[entry_id]
    result = run_entry(entry)
    # Surface the report line even on success for `pytest -s` forensics.
    print(f"{result.status}: {entry.id}: {result.detail}")
    try:
        assert_entry_caught(result)
    except (
        CounterfeitSurvived,
        CounterfeitApplyError,
        CounterfeitCollectionError,
        CounterfeitWrongFailure,
        CounterfeitControlError,
    ) as exc:
        pytest.fail(str(exc))


# This test re-runs EVERY corpus entry (a second full pass on top of the
# parametrized one above), so its runtime grows linearly with the corpus while
# the suite-wide 180s budget in pyproject.toml does not. It was measured at 170s
# with 47 entries — 94% of the budget — so the next entry any lane added was
# always going to fail it on time rather than on evidence. An explicit budget
# keeps a genuine hang failing loudly without making "the corpus grew" a red.
@pytest.mark.timeout(1800)
def test_full_corpus_report_and_operator_tree_clean() -> None:
    """End-to-end: report every entry, then prove production paths stay pristine."""
    entries = load_corpus()
    results = [run_entry(e) for e in entries]
    report = format_report(results)
    print(report)

    # Restoration proof: scratch trees only — never mutate omniagentos/**.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    production_dirt = [
        line
        for line in status.stdout.splitlines()
        if "omniagentos/" in line or line.rstrip().endswith("omniagentos")
    ]
    assert not production_dirt, (
        "harness left omniagentos/** dirty — mutations must be scratch-only:\n"
        + "\n".join(production_dirt)
    )

    survived = [r for r in results if r.status == "survived"]
    if survived:
        ids = ", ".join(r.entry.id for r in survived)
        pytest.fail(f"SURVIVING counterfeits (coverage is decoration): {ids}\n{report}")

    bad = [r for r in results if not r.ok]
    if bad:
        pytest.fail(f"corpus gate red:\n{report}")
