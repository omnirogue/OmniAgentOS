"""Federated file search: ranking, dedup, noise filtering, snippets, graceful degrade.

Hermetic — the Spotlight backend's command runner is injected, and results point at
real tmp files (so os.stat / content snippets work) via a monkeypatched ``resolve_roots``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.filesearch import render_hits, search_files, service


def _runner(mdfind_paths: list[str]) -> Callable[[list[str]], str]:
    def run(argv: list[str]) -> str:
        if argv and argv[0] == "mdfind":
            return "\n".join(mdfind_paths)
        if argv and argv[0] == "mdls":
            return 'kMDItemKind = "PDF document"\nkMDItemTitle = (null)\n'
        return ""

    return run


def test_query_terms_drops_stopwords_and_singletons() -> None:
    assert service._query_terms("find the invoice report a") == ["find", "invoice", "report"]


def test_is_noise_local_vs_cloud() -> None:
    assert service._is_noise("/Users/x/Library/Caches/foo.md", "local") is True
    assert service._is_noise("/Users/x/node_modules/y.js", "local") is True
    # A cloud drive lives UNDER ~/Library/CloudStorage — must not be filtered as noise.
    assert service._is_noise("/Users/x/Library/CloudStorage/Dropbox/deal.md", "dropbox") is False
    assert service._is_noise("/Users/x/Documents/report.md", "local") is False


def test_search_ranks_filename_match_first_with_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "invoice-2026.md"
    named.write_text("client invoice total due next week\n", encoding="utf-8")
    mentioned = tmp_path / "notes.txt"
    mentioned.write_text("a passing invoice mention buried in notes\n", encoding="utf-8")

    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "local")])
    hits = search_files("invoice", scopes=["local"], runner=_runner([str(named), str(mentioned)]))

    assert [h.name for h in hits] == ["invoice-2026.md", "notes.txt"]  # name match ranks first
    assert all(h.source == "local" for h in hits)
    assert "invoice" in hits[0].snippet.lower()  # content snippet from the local text file


def test_recency_breaks_ties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = tmp_path / "deal-fresh.md"
    fresh.write_text("deal\n", encoding="utf-8")
    stale = tmp_path / "deal-stale.md"
    stale.write_text("deal\n", encoding="utf-8")
    old = time.time() - 400 * 86_400
    os.utime(stale, (old, old))

    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "local")])
    hits = search_files("deal", scopes=["local"], runner=_runner([str(stale), str(fresh)]))
    assert hits[0].name == "deal-fresh.md"


def test_dedup_by_realpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    only = tmp_path / "a.md"
    only.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "local")])
    hits = search_files("x", scopes=["local"], runner=_runner([str(only), str(only)]))
    assert len(hits) == 1


def test_noise_paths_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keep = tmp_path / "keep.md"
    keep.write_text("q\n", encoding="utf-8")
    junkdir = tmp_path / "node_modules"
    junkdir.mkdir()
    junk = junkdir / "pkg.md"
    junk.write_text("q\n", encoding="utf-8")

    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "local")])
    hits = search_files("q", scopes=["local"], runner=_runner([str(keep), str(junk)]))
    assert [h.name for h in hits] == ["keep.md"]


def test_graceful_when_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [("/no/such/root", "local")])
    assert search_files("anything", runner=lambda argv: "") == []
    assert render_hits([]) == "No files found."


def test_render_is_agent_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "spec.md"
    doc.write_text("the spec\n", encoding="utf-8")
    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "local")])
    text = render_hits(search_files("spec", scopes=["local"], runner=_runner([str(doc)])))
    assert "[local] spec.md" in text
    assert str(doc) in text


def test_gdrive_api_empty_must_not_suppress_mount_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 'available' + empty/failure must not present as 'no Drive files'.

    Defect class: non-result as favourable result. ``is_available()`` is a cheap
    local token-file check (not a live API probe). When True, ``_scope_paths``
    used to skip the bounded mount walk and return ``[]``, trusting the cloud
    API path alone. A dead/expired token, network blip, or swallowed API error
    then yields the same empty list as a genuine no-match — search looks
    successful and complete while matching files on the mount are invisible.

    Counterfeits this test rejects:
    - Flip ``is_available`` to False so the walk runs only when API is "down"
      (does not prove the True path still walks).
    - Make the API return the file (proves API wiring, not walk fallback).
    - Assert only that search does not raise (empty still "succeeds").
    """
    mount_hit = tmp_path / "quarterly-invoice.md"
    mount_hit.write_text("local mount copy of the invoice\n", encoding="utf-8")

    monkeypatch.setattr(service, "resolve_roots", lambda scopes: [(str(tmp_path), "gdrive")])
    # Force the pre-fix short-circuit condition: is_available() True (token file
    # present). Without this, a machine with no token never exercises the bug path
    # and a revert would silently pass.
    from omniagentos.filesearch import gdrive_api

    monkeypatch.setattr(gdrive_api, "is_available", lambda: True)
    # Cloud API yields nothing (auth death, outage, or true empty — indistinguishable
    # today because search_drive swallows errors into []).
    monkeypatch.setattr(
        service,
        "_gdrive_api_hits",
        lambda query, terms, now, limit, seen: [],
    )
    # Spotlight empty → cloud scopes rely on walk (or API). Pre-fix, is_available()
    # True short-circuited to [] and never walked.
    empty_spotlight = _runner([])
    hits = search_files(
        "quarterly-invoice",
        scopes=["gdrive"],
        runner=empty_spotlight,
        time_budget=5.0,
    )

    assert [h.name for h in hits] == ["quarterly-invoice.md"], (
        "mount walk must still surface filename matches when the Drive API "
        "path is available-but-empty; empty API must not erase the mount"
    )
    assert hits[0].source == "gdrive"
    assert hits[0].path == str(mount_hit)
