from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagentos.sessions import agents_view


@pytest.fixture(autouse=True)
def _fake_agent_view_registry() -> None:
    """Override the central offline fake: this file tests the REAL collector
    (subprocess is mocked per-test, so no provider CLI is ever spawned)."""


@pytest.fixture(autouse=True)
def _clear_agent_view_cache() -> None:
    # Reset the module-level TTL cache directly: a production-side reset helper
    # would be test-only code and refused by the reachability gate.
    with agents_view._CACHE_LOCK:
        agents_view._cache_at = 0.0
        agents_view._cache_value = None


def test_list_profiles_skips_empty_account_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude-account-1").mkdir()
    (home / ".claude-account-").mkdir()
    monkeypatch.setattr(agents_view.Path, "home", lambda: home)

    assert agents_view.list_profiles() == [home / ".claude", home / ".claude-account-1"]


def test_fetch_profile_tolerates_bad_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / ".claude"
    profile.mkdir()
    monkeypatch.setattr(
        agents_view.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not json"),
    )
    assert agents_view.fetch_profile(profile) == []

    def missing_binary(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(agents_view.subprocess, "run", missing_binary)
    assert agents_view.fetch_profile(profile) == []


def test_fetch_profile_sets_profile_env_and_reads_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / ".claude-account-2"
    profile.mkdir()
    seen: dict[str, str] = {}

    def fake_run(_argv: list[str], **kwargs: object) -> SimpleNamespace:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen["profile"] = str(env["CLAUDE_CONFIG_DIR"])
        seen["timeout"] = str(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout='[{"pid": 41, "name": "Scout"}]')

    monkeypatch.setattr(agents_view.subprocess, "run", fake_run)
    assert agents_view.fetch_profile(profile) == [
        {"pid": 41, "name": "Scout", "profile": ".claude-account-2"}
    ]
    assert seen == {"profile": str(profile), "timeout": "8.0"}


# The historically-leaked secret set the _scrubbed_env docstring names, plus an
# arbitrary UNKNOWN secret name. Planting only a fixed 3-name subset would let a
# future revert to `{**os.environ}` that pops just those three stay green while
# re-leaking everything else (grok F1) -- so the test is allowlist-shaped below,
# not a denylist of these specific names.
_PLANTED_SECRETS = (
    "ANTHROPIC_API_KEY",
    "ACMEUNI_STRIPE_SECRET_KEY",
    "OPERATOR_TOKEN",
    "DATABASE_URL",
    "SLASH_API_KEY",
    "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
    "SOME_RANDOM_SECRET",
)


def test_fetch_profile_scrubs_secrets_from_child_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-policy money boundary (#499 F1): the subscription CLI never inherits
    payment/bank/infra credentials, model-provider API keys, OPERATOR_TOKEN, or
    any unknown secret; it gets ONLY the allowlisted env plus the profile
    pointer. Asserted as a subset of what _scrubbed_env legitimately admits so a
    non-allowlisted leak fails, not just the specific names planted here."""
    from omniagentos.adapters.common import _scrubbed_env

    profile = tmp_path / ".claude-account-9"
    profile.mkdir()
    for name in _PLANTED_SECRETS:
        monkeypatch.setenv(name, "leak-me")
    # PATH must survive scrubbing so the CLI is still resolvable.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # A WRONG parent config dir: fetch_profile must OVERRIDE it with the profile,
    # not inherit it. Catches a `{profile, **_scrubbed_env()}` merge-order
    # inversion that would run the CLI as the wrong account (grok F2 /
    # wrong-account api_error class, 2026-07-26).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/parent/wrong/dir")
    seen: dict[str, str] = {}

    def fake_run(_argv: list[str], **kwargs: object) -> SimpleNamespace:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.update(env)
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(agents_view.subprocess, "run", fake_run)
    agents_view.fetch_profile(profile)

    # F2: the profile wins over the (wrong) parent CLAUDE_CONFIG_DIR.
    assert seen.get("CLAUDE_CONFIG_DIR") == str(profile)
    assert seen.get("CLAUDE_CONFIG_DIR") != "/parent/wrong/dir"
    assert seen.get("PATH") == "/usr/bin:/bin"

    # F1a: every planted secret -- known and arbitrary-unknown -- is absent.
    for name in _PLANTED_SECRETS:
        assert name not in seen, f"{name} leaked into the subscription CLI env"

    # F1b (decisive): child env keys are a SUBSET of what the canonical scrubber
    # admits plus CLAUDE_CONFIG_DIR. A revert to a hand-rolled denylist that
    # forwards os.environ minus a few names would admit an extra key and fail.
    allowed = set(_scrubbed_env()) | {"CLAUDE_CONFIG_DIR"}
    leaked = set(seen) - allowed
    assert not leaked, f"non-allowlisted keys handed to the subscription CLI: {leaked}"


def test_collect_all_merges_pids_and_uses_ttl_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = [Path("/profiles/a"), Path("/profiles/b")]
    calls: list[Path] = []
    monkeypatch.setattr(agents_view, "list_profiles", lambda: profiles)

    def fake_fetch(profile: Path) -> list[dict[str, object]]:
        calls.append(profile)
        if profile.name == "a":
            return [{"pid": 7, "name": "first"}, {"pid": 8, "name": "only"}]
        return [{"pid": 7, "name": "last"}]

    monkeypatch.setattr(agents_view, "fetch_profile", fake_fetch)
    assert agents_view.collect_all() == {
        7: {"pid": 7, "name": "last"},
        8: {"pid": 8, "name": "only"},
    }
    assert agents_view.collect_all()[7]["name"] == "last"
    assert calls == profiles
