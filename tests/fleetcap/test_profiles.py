from pathlib import Path

from omniagentos.fleetcap.profiles import enumerate_profiles


def test_profile_enumeration_handles_all_claude_layouts_and_trap(tmp_path: Path) -> None:
    for relative in (
        ".claude",
        ".claude-account-",
        ".claude-account-1",
        ".claude-account-2",
        ".claude-accounts/work",
        ".codex-alt/sessions",
    ):
        (tmp_path / relative).mkdir(parents=True)
    profiles = enumerate_profiles(tmp_path)
    labels = {(profile.cli, profile.account_label) for profile in profiles}
    assert ("claude", "default") in labels
    assert ("claude", "account-1") in labels
    assert ("claude", "account-2") in labels
    assert ("claude", "work") in labels
    assert ("claude", "account-") not in labels
    assert ("codex", "codex-alt") in labels
