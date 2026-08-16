"""The artifact-download allowlist must match on a DOMAIN LABEL, not on characters.

`_download_artifact` fetches the rendered image from a URL that arrived in
Replicate's prediction body — the module says so itself, at the declaration of
``ARTIFACT_HOST_SUFFIXES``: "The URL comes back in the API's response, i.e. it
is attacker-influenceable in the general case, so it is checked against this
list". That check is therefore the whole control between an API-supplied name
and 32 MiB written into ``<var>/loops/artifacts/<instance>/``.

It was matched with a bare ``host.endswith(suffix)`` over a tuple one of whose
members carried no leading dot, so ``evilreplicate.delivery`` — a registrable
name under the ``.delivery`` gTLD — satisfied it. ``str.endswith`` is not a
domain-label operation.

These tests are written against the CONSTANT rather than a second hand-copied
list of hosts: a hard-coded list of nine has the same failure mode as the
allowlist it is checking. Adding a host to ``ARTIFACT_HOST_SUFFIXES`` therefore
extends this guard automatically, in either spelling, dotted or not.
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import pytest

from omniagentos.scheduler import loop_effects


class _Reached(BaseException):
    """Raised the instant the guard has let a URL through to the transport.

    Deliberately a ``BaseException``: ``_download_artifact`` catches
    ``httpx.HTTPError`` and re-raises it as a seam error, and catching this as
    "the fetch was attempted" must not be confused with the real transport's
    own failure classification.
    """


class _FakeClient:
    """Stands in for ``httpx.Client`` so no test here touches the network."""

    def __init__(self, **_: Any) -> None: ...

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def stream(self, _method: str, url: str) -> None:
        raise _Reached(url)


@pytest.fixture
def fetch_attempted(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Call ``_download_artifact`` and report only whether the guard admitted the URL."""
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    def attempt(url: str) -> bool:
        try:
            loop_effects._download_artifact(url, tmp_path / "out.png")
        except _Reached:
            return True
        except loop_effects.SeamRefused as exc:
            assert exc.reason == "artifact_host_not_allowed", (
                f"{url} was refused, but for {exc.reason!r} rather than the host check"
            )
            return False
        raise AssertionError(f"{url} neither reached the transport nor was refused")

    return attempt


def _allowed_domains() -> list[str]:
    """The bare domains the seam means to admit, in either declared spelling."""
    return sorted({suffix.lstrip(".") for suffix in loop_effects.ARTIFACT_HOST_SUFFIXES})


def test_the_allowlist_is_not_empty() -> None:
    """A guard over an empty list would pass every assertion below vacuously."""
    assert _allowed_domains(), "ARTIFACT_HOST_SUFFIXES declares no host to admit"


@pytest.mark.parametrize("domain", _allowed_domains())
def test_an_allowlisted_domain_and_its_subdomains_are_admitted(domain, fetch_attempted) -> None:
    """The guard must still let the real CDN through — the control for the rest."""
    assert fetch_attempted(f"https://{domain}/pbxt/abc/out.png")
    assert fetch_attempted(f"https://cdn.{domain}/pbxt/abc/out.png")


@pytest.mark.parametrize("domain", _allowed_domains())
def test_a_host_merely_ENDING_in_an_allowlisted_domain_is_refused(domain, fetch_attempted) -> None:
    """`evil<domain>` is a different registrable name and must not be admitted.

    This is the case the guard missed. The label boundary is the whole point:
    ``evilreplicate.delivery`` ends in the characters ``replicate.delivery`` and
    is owned by whoever registers it.
    """
    for hostile in (f"evil{domain}", f"x{domain}", f"not-{domain}"):
        assert not fetch_attempted(f"https://{hostile}/out.png"), (
            f"{hostile} was admitted: the allowlist matched characters, not a domain label"
        )


@pytest.mark.parametrize("domain", _allowed_domains())
def test_an_allowlisted_domain_used_as_a_PREFIX_is_refused(domain, fetch_attempted) -> None:
    """The near miss the previous test suite did cover; kept so it cannot regress."""
    assert not fetch_attempted(f"https://{domain}.attacker.example/out.png")


@pytest.mark.parametrize("domain", _allowed_domains())
def test_plaintext_is_refused_even_on_an_allowlisted_domain(domain, fetch_attempted) -> None:
    """https-only is part of the same condition and must not be lost to a rewrite."""
    assert not fetch_attempted(f"http://{domain}/out.png")


def test_an_unrelated_host_is_refused(fetch_attempted) -> None:
    assert not fetch_attempted("https://evil.example.com/x.png")
