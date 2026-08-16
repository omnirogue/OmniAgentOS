from __future__ import annotations

import os

import pytest

from omniagentos.lab.curator.env import is_protected_key, scrubbed_env, scrubbed_environ


def test_is_protected_key_matches_the_documented_var_case_insensitively() -> None:
    assert is_protected_key("OMNIAGENTOS_EVAL_PROTECTED")
    assert is_protected_key("omniagentos_eval_protected")


def test_is_protected_key_also_catches_any_key_naming_protected_as_defense_in_depth() -> None:
    assert is_protected_key("SOME_OTHER_PROTECTED_PATH")


def test_is_protected_key_leaves_ordinary_keys_alone() -> None:
    assert not is_protected_key("OMNIAGENTOS_DB")
    assert not is_protected_key("PATH")


def test_scrubbed_env_strips_protected_keys_from_a_given_mapping() -> None:
    base = {
        "PATH": "/bin",
        "OMNIAGENTOS_EVAL_PROTECTED": "/var/eval_protected.db",
        "OMNIAGENTOS_DB": "x",
    }
    assert scrubbed_env(base) == {"PATH": "/bin", "OMNIAGENTOS_DB": "x"}


def test_scrubbed_env_does_not_mutate_the_input_mapping() -> None:
    base = {"OMNIAGENTOS_EVAL_PROTECTED": "leak"}
    scrubbed_env(base)
    assert base == {"OMNIAGENTOS_EVAL_PROTECTED": "leak"}


def test_scrubbed_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_EVAL_PROTECTED", "leak")
    monkeypatch.setenv("KEEP_ME", "1")
    result = scrubbed_env()
    assert "OMNIAGENTOS_EVAL_PROTECTED" not in result
    assert result["KEEP_ME"] == "1"


def test_scrubbed_environ_removes_and_restores_the_protected_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_EVAL_PROTECTED", "leak")
    with scrubbed_environ():
        assert "OMNIAGENTOS_EVAL_PROTECTED" not in os.environ
    assert os.environ["OMNIAGENTOS_EVAL_PROTECTED"] == "leak"


def test_scrubbed_environ_restores_even_if_the_block_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_EVAL_PROTECTED", "leak")
    with pytest.raises(ValueError, match="boom"), scrubbed_environ():
        assert "OMNIAGENTOS_EVAL_PROTECTED" not in os.environ
        raise ValueError("boom")
    assert os.environ["OMNIAGENTOS_EVAL_PROTECTED"] == "leak"


def test_scrubbed_environ_is_a_noop_when_nothing_is_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_EVAL_PROTECTED", raising=False)
    with scrubbed_environ():
        assert "OMNIAGENTOS_EVAL_PROTECTED" not in os.environ
    assert "OMNIAGENTOS_EVAL_PROTECTED" not in os.environ
