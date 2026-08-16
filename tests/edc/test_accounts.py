"""The static source-account → owner map, and its loud-skip on unmapped sources."""

from __future__ import annotations

from omniagentos.edc.accounts import accounts_map, owner_for_source
from omniagentos.steward.config import EdcAccountCfg, EdcConfig


def _cfg() -> EdcConfig:
    return EdcConfig(
        accounts={
            "gmail_ownera": EdcAccountCfg(
                owner_employee_id="emp_owner",
                company_slug="initech",
                source_account="gmail_ownera",
            )
        }
    )


def test_mapped_source_resolves() -> None:
    owner = owner_for_source("gmail_ownera", _cfg())
    assert owner is not None
    assert owner.owner_employee_id == "emp_owner"
    assert owner.company_slug == "initech"
    assert owner.source_account == "gmail_ownera"


def test_unmapped_source_is_none() -> None:
    # None is the explicit "skip loudly, never guess an owner" signal.
    assert owner_for_source("gmail_unknown", _cfg()) is None


def test_accounts_map_is_prebuildable() -> None:
    table = accounts_map(_cfg())
    assert set(table) == {"gmail_ownera"}
    # A pre-built dict is accepted directly (the triage-cycle hot path).
    assert owner_for_source("gmail_ownera", table) is not None
    assert owner_for_source("nope", table) is None


def test_packaged_config_loads() -> None:
    # The default configs/steward.yaml edc block parses and maps the known accounts.
    table = accounts_map()
    assert table["gmail_ownera"].owner_employee_id == "emp_owner"
