"""Static source-account → (owner, company) map for the Executive Decision Center.

Tenancy is aligned to the Team Work OS model (synthesis §8): every Decision and
every owner-stamped ``comms_messages`` row carries an ``owner_employee_id`` (plus
a denormalized ``company_slug``). The mapping from a comms source to its owner is
a STATIC, git-reviewable config block (``configs/steward.yaml`` ``edc.accounts:``)
rather than a mutable cursor blob — the same reasoning that keeps the roster in
config, not guessed at runtime.

The load-bearing invariant: a source that is NOT in the map has no owner.
:func:`owner_for_source` returns ``None`` for it, and every caller MUST skip such
a message loudly (log + count) rather than guess whose private queue it belongs
to. This module never invents an owner.
"""

from __future__ import annotations

from typing import NamedTuple

from omniagentos.steward.config import EdcConfig, StewardConfig, load_steward_config


class SourceOwner(NamedTuple):
    """Resolved ownership for one comms source."""

    owner_employee_id: str
    company_slug: str
    source_account: str


def accounts_map(config: EdcConfig | StewardConfig | None = None) -> dict[str, SourceOwner]:
    """Return ``source name → SourceOwner`` from the EDC config.

    Accepts an :class:`EdcConfig`, a full :class:`StewardConfig`, or ``None``
    (loads the packaged ``configs/steward.yaml``). The result is a plain dict so
    callers can cache it for a whole triage cycle instead of re-reading YAML per
    message.
    """
    edc = _edc_config(config)
    return {
        source: SourceOwner(
            owner_employee_id=account.owner_employee_id,
            company_slug=account.company_slug,
            source_account=account.source_account,
        )
        for source, account in edc.accounts.items()
    }


def owner_for_source(
    source: str,
    config: EdcConfig | StewardConfig | dict[str, SourceOwner] | None = None,
) -> SourceOwner | None:
    """Resolve one comms ``source`` to its owner, or ``None`` if unmapped.

    ``config`` may be a pre-built ``accounts_map`` dict (the hot path during a
    triage cycle), an :class:`EdcConfig`/:class:`StewardConfig`, or ``None`` to
    load from disk. ``None`` is a deliberate, explicit signal — the caller must
    skip the message loudly, never fall back to a default owner.
    """
    table = config if isinstance(config, dict) else accounts_map(config)
    return table.get(source)


def _edc_config(config: EdcConfig | StewardConfig | None) -> EdcConfig:
    if config is None:
        return load_steward_config().edc
    if isinstance(config, StewardConfig):
        return config.edc
    return config


__all__ = ["SourceOwner", "accounts_map", "owner_for_source"]
