from __future__ import annotations

from omniagentos.sessions.company_map import DEFAULT_MAP, resolve_company


def test_resolve_company_matches_first_known_prefix() -> None:
    prefix, company = DEFAULT_MAP[0]
    assert resolve_company(f"{prefix}/project/subdir", None) == company


def test_resolve_company_override_takes_precedence() -> None:
    prefix, _ = DEFAULT_MAP[0]
    assert resolve_company(prefix, "Special Operations") == "Special Operations"


def test_resolve_company_returns_none_without_match() -> None:
    assert resolve_company("/tmp/no-known-company", None) is None


def test_resolve_company_requires_path_boundary() -> None:
    """~/PersonalFinance must not inherit ~/Personal's label."""
    for prefix, _company in DEFAULT_MAP:
        assert resolve_company(f"{prefix}Finance", None) is None


def test_resolve_company_exact_prefix_dir_matches() -> None:
    prefix, company = DEFAULT_MAP[0]
    assert resolve_company(prefix, None) == company
