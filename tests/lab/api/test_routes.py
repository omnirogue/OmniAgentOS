from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Response

import omniagentos.lab.api.routes.lab as lab_routes
from omniagentos.lab.contracts import (
    ChampionEntry,
    Disposition,
    Experiment,
    ExperimentStatus,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
)
from omniagentos.lab.surfaces import OPERATOR_TOKEN_ENV
from tests.lab.api.conftest import FakeLabStore


def request(client: httpx.AsyncClient, method: str, path: str, **kwargs: object) -> httpx.Response:
    return asyncio.run(client.request(method, path, **kwargs))


def test_experiment_and_surface_routes(asgi_client: httpx.AsyncClient, store: FakeLabStore) -> None:
    listed = request(asgi_client, "GET", "/api/lab/experiments?discipline=writing")
    detail = request(asgi_client, "GET", "/api/lab/experiments/exp_seed")
    created = request(
        asgi_client,
        "POST",
        "/api/lab/experiments",
        json={
            "hypothesis": "Try a focused prompt",
            "discipline": "writing",
            "mutable_surface_kind": "prompt",
            "challenger_surface_id": "srf_challenger",
            "eval_suite_id": "evs_writing",
        },
    )
    run = request(asgi_client, "POST", "/api/lab/experiments/exp_seed/run", json={"dry_run": True})
    disposition = request(
        asgi_client,
        "POST",
        "/api/lab/experiments/exp_seed/disposition",
        json={"decision": "human_review", "note": "review this", "decided_by": "operator"},
    )
    surfaces = request(asgi_client, "GET", "/api/lab/surfaces?kind=prompt")
    surface = request(asgi_client, "GET", "/api/lab/surfaces/srf_champion")

    assert (
        listed.status_code
        == detail.status_code
        == surfaces.status_code
        == surface.status_code
        == 200
    )
    assert created.status_code == 201
    assert created.json()["champion_surface_id"] == "srf_champion"
    assert run.status_code == 202
    assert run.json()["status"] == "queued"
    assert run.json()["job_id"]
    assert disposition.json()["decision_note"] == "review this"
    assert detail.json()["eval_results"][0]["metrics"] == {"quality": 0.9}
    assert "content_hash" in surface.json()
    assert store.events


def test_champion_tournament_logbook_and_curate_routes(asgi_client: httpx.AsyncClient) -> None:
    disciplines = request(asgi_client, "GET", "/api/lab/disciplines")
    champions = request(asgi_client, "GET", "/api/lab/champions?discipline=writing")
    promoted = request(
        asgi_client,
        "POST",
        "/api/lab/experiments/exp_seed/disposition",
        json={"decision": "promote", "note": "approved", "decided_by": "operator"},
    )
    rollback = request(asgi_client, "POST", "/api/lab/champions/writing/prompt/rollback", json={})
    tournaments = request(asgi_client, "GET", "/api/lab/tournaments?subject=writing-arena")
    tournament = request(asgi_client, "GET", "/api/lab/tournaments/tnm_seed")
    created = request(
        asgi_client,
        "POST",
        "/api/lab/tournaments",
        json={
            "subject": "new-arena",
            "discipline": "writing",
            "config_ids": ["srf_champion", "srf_challenger"],
            "arena_task": {"prompt": "write"},
            "dry_run": True,
        },
    )
    leaderboard = request(asgi_client, "GET", "/api/lab/leaderboard?subject=writing-arena")
    playbook = request(asgi_client, "GET", "/api/lab/playbook?discipline=writing")
    curate = request(asgi_client, "POST", "/api/lab/curate", json={"dry_run": True})

    assert disciplines.status_code == champions.status_code == tournaments.status_code == 200
    assert promoted.json()["status"] == "promoted"
    assert rollback.json()["surface_id"] == "srf_champion"
    assert tournament.json()["matches"][0]["judge_notes"] == "better structure"
    assert created.status_code == 201
    assert created.json()["status"] == "done"
    assert leaderboard.json()[0]["rank"] == 1
    assert playbook.json()[0]["trait"] == "Use a concise review pass"
    assert curate.json()["summary"]["status"] == "dry_run"


def test_promote_route_rejects_audit_flagged_experiment(
    asgi_client: httpx.AsyncClient, store: FakeLabStore
) -> None:
    store.experiments["exp_seed"]["disposition"] = "human_review"
    store.experiments["exp_seed"]["scorecard_json"] = json.dumps(
        {"audit_flags": ["suspicious_perfect:accuracy"], "safety_regression": False}
    )

    response = request(
        asgi_client,
        "POST",
        "/api/lab/experiments/exp_seed/disposition",
        json={"decision": "promote", "decided_by": "operator"},
    )

    assert response.status_code == 409
    assert "unresolved audit flags" in response.json()["error"]["message"]
    assert store.get_champion("writing", "prompt")["surface_id"] == "srf_champion"  # type: ignore[index]


def test_disposition_route_requires_operator_token_for_safety_relevant_promotion(
    asgi_client: httpx.AsyncClient,
    store: FakeLabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
    champion = Surface(
        id="srf_rubric_champion",
        kind=SurfaceKind.REVIEW_RUBRIC,
        discipline="writing",
        path="vault/prompts/rubric/v01.md",
        content_hash="rubric-champion",
        status=SurfaceStatus.CHAMPION,
    )
    challenger = champion.model_copy(
        update={
            "id": "srf_rubric_challenger",
            "version": 2,
            "path": "vault/prompts/rubric/v02.md",
            "content_hash": "rubric-challenger",
            "status": SurfaceStatus.CHALLENGER,
        }
    )
    store.create_surface(champion)
    store.create_surface(challenger)
    store.set_champion(
        ChampionEntry(
            discipline="writing",
            surface_kind=SurfaceKind.REVIEW_RUBRIC,
            surface_id=champion.id,
            surface_version=champion.version,
            cas_version=0,
        )
    )
    store.create_experiment(
        Experiment(
            id="exp_rubric",
            hypothesis="tighter rubric",
            discipline="writing",
            mutable_surface_kind=SurfaceKind.REVIEW_RUBRIC,
            champion_surface_id=champion.id,
            challenger_surface_id=challenger.id,
            eval_suite_id="evs_writing",
            primary_metric="accuracy",
            status=ExperimentStatus.DECIDED,
            disposition=Disposition.HUMAN_REVIEW,
            scorecard=Scorecard(audit_flags=[], safety_regression=False),
        )
    )
    path = "/api/lab/experiments/exp_rubric/disposition"

    no_token = request(
        asgi_client,
        "POST",
        path,
        json={"decision": "promote", "decided_by": "operator"},
    )
    assert no_token.status_code == 409
    assert OPERATOR_TOKEN_ENV in no_token.json()["error"]["message"]
    assert store.get_champion("writing", "review_rubric")["surface_id"] == champion.id  # type: ignore[index]

    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "correct-token")
    wrong_token = request(
        asgi_client,
        "POST",
        path,
        json={
            "decision": "promote",
            "decided_by": "operator",
            "operator_token": "nope",
        },
    )
    assert wrong_token.status_code == 409
    assert "operator token" in wrong_token.json()["error"]["message"]
    assert store.get_champion("writing", "review_rubric")["surface_id"] == champion.id  # type: ignore[index]

    right_token = request(
        asgi_client,
        "POST",
        path,
        json={
            "decision": "promote",
            "decided_by": "operator",
            "operator_token": "correct-token",
        },
    )
    assert right_token.status_code == 200
    assert "correct-token" not in right_token.text
    assert store.get_champion("writing", "review_rubric")["surface_id"] == challenger.id  # type: ignore[index]

    rollback_path = "/api/lab/champions/writing/review_rubric/rollback"
    no_rollback_token = request(asgi_client, "POST", rollback_path, json={})
    assert no_rollback_token.status_code == 409
    assert "operator token" in no_rollback_token.json()["error"]["message"]
    wrong_rollback_token = request(
        asgi_client, "POST", rollback_path, json={"operator_token": "nope"}
    )
    assert wrong_rollback_token.status_code == 409
    assert "operator token" in wrong_rollback_token.json()["error"]["message"]
    right_rollback_token = request(
        asgi_client,
        "POST",
        rollback_path,
        json={"operator_token": "correct-token"},
    )
    assert right_rollback_token.status_code == 200
    assert right_rollback_token.json()["surface_id"] == champion.id


def test_surface_content_is_limited_to_prompt_and_genome_roots(
    asgi_client: httpx.AsyncClient,
    store: FakeLabStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    prompt = vault / "prompts" / "allowed.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("allowed prompt", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    store.surfaces["srf_champion"]["path"] = "vault/prompts/allowed.md"

    allowed = request(asgi_client, "GET", "/api/lab/surfaces/srf_champion")
    store.surfaces["srf_champion"]["path"] = "pyproject.toml"
    arbitrary = request(asgi_client, "GET", "/api/lab/surfaces/srf_champion")
    store.surfaces["srf_champion"]["path"] = "vault/prompts/../../pyproject.toml"
    traversal = request(asgi_client, "GET", "/api/lab/surfaces/srf_champion")

    assert allowed.status_code == 200
    assert allowed.json()["content"] == "allowed prompt"
    assert arbitrary.status_code == 422
    assert traversal.status_code == 422


def test_vault_routes_are_confined(
    asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    note = notes / "one.md"
    note.write_text(
        "---\nid: note-1\ntype: run\ndiscipline: writing\ncreated: '2026-01-01T00:00:00Z'\nsource_run: null\nconfidence: high\nstatus: active\nsupersedes: null\n---\n# A useful note\n\nFind [[other]].",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))

    tree = request(asgi_client, "GET", "/api/lab/vault/tree")
    read = request(asgi_client, "GET", "/api/lab/vault/note?path=notes/one.md")
    search = request(asgi_client, "GET", "/api/lab/vault/search?q=useful")
    traversal = request(asgi_client, "GET", "/api/lab/vault/note?path=../outside.md")

    assert tree.status_code == read.status_code == search.status_code == 200
    assert tree.json()["folders"][0]["notes"][0]["id"] == "note-1"
    assert tree.json()["folders"][0]["notes"][0]["path"] == "notes/one.md"
    assert read.json()["links"] == ["other"]
    assert search.json()[0]["path"] == "notes/one.md"
    assert traversal.status_code == 422
    assert traversal.json()["error"]["code"] == "validation"


def test_vault_search_bounds_scanned_notes_and_results(
    asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes: list[tuple[Path, dict[str, str]]] = []
    for index in range(lab_routes._VAULT_TREE_DEFAULT_LIMIT + 1):
        path = tmp_path / f"note-{index}.md"
        path.write_text(f"needle {index}", encoding="utf-8")
        notes.append((path, {"path": path.name, "title": path.stem, "type": "note"}))

    calls: list[dict[str, object]] = []

    def fake_vault_notes(root: Path, **kwargs: object) -> tuple[list[tuple[Path, dict[str, str]]], bool]:
        calls.append(kwargs)
        return notes, True

    monkeypatch.setattr(lab_routes, "_vault_notes", fake_vault_notes)

    bounded = request(asgi_client, "GET", "/api/lab/vault/search?q=needle")
    limited = request(asgi_client, "GET", "/api/lab/vault/search?q=needle&limit=2")
    invalid = request(asgi_client, "GET", "/api/lab/vault/search?q=needle&limit=0")

    assert bounded.status_code == 200
    assert len(bounded.json()) <= lab_routes._VAULT_TREE_DEFAULT_LIMIT
    assert calls[0]["limit"] == lab_routes._VAULT_TREE_DEFAULT_LIMIT
    assert limited.status_code == 200
    assert len(limited.json()) == 2
    assert invalid.status_code == 422


def test_surfaces_and_tournaments_do_not_declare_offset() -> None:
    assert "offset" not in inspect.signature(lab_routes.list_surfaces).parameters
    assert "offset" not in inspect.signature(lab_routes.list_tournaments).parameters


def test_vault_notes_bounds_candidate_scan_when_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    yielded = 0
    paths = [vault / f"note-{index:04d}.md" for index in range(20)]

    def counted_rglob(root: Path, pattern: str):
        nonlocal yielded
        assert root == vault
        assert pattern == "*.md"
        for path in paths:
            yielded += 1
            yield path

    monkeypatch.setattr(Path, "rglob", counted_rglob)
    monkeypatch.setattr(lab_routes, "_note_summary", lambda root, path: {"path": path.name})

    notes, truncated = lab_routes._vault_notes(vault, limit=3)

    assert len(notes) == 3
    assert truncated is True
    assert yielded <= 3 * lab_routes._VAULT_NOTES_CANDIDATE_FACTOR + 1


def test_vault_notes_bounds_visited_count_even_when_most_paths_are_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A depth filter that rejects almost everything must not defeat the cap.

    Every ``rglob`` yield counts against the visited cap BEFORE the
    inode/depth filters run -- counting post-filter ``candidates`` lets a deep
    tree under a shallow ``depth`` walk every path while the cap never fills
    (Sol repro: depth=1, limit=3 over 100 deeper paths -> all 100 visited).

    When the cap cuts the raw walk off entirely before any eligible note is
    reached, that must surface as ``truncated=True`` with an empty result --
    NOT a false-complete empty/partial response with no signal that eligible
    notes exist beyond the cap (Sol's second-round repro: yielded=12,
    notes=0, truncated=False while 3 eligible shallow notes exist past it).
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    yielded = 0
    # 100 paths two levels deep (rejected by depth=1), all ahead of the 3
    # shallow notes in walk order -- the cap is exhausted before any shallow
    # note is even visited.
    deep_paths = [vault / f"deep-{index:04d}" / "note.md" for index in range(100)]
    shallow_paths = [vault / f"note-{index:04d}.md" for index in range(3)]
    all_paths = deep_paths + shallow_paths

    def counted_rglob(root: Path, pattern: str):
        nonlocal yielded
        assert root == vault
        assert pattern == "*.md"
        for path in all_paths:
            yielded += 1
            yield path

    monkeypatch.setattr(Path, "rglob", counted_rglob)
    monkeypatch.setattr(lab_routes, "_note_summary", lambda root, path: {"path": path.name})

    limit = 3
    cap = limit * lab_routes._VAULT_NOTES_CANDIDATE_FACTOR
    notes, truncated = lab_routes._vault_notes(vault, depth=1, limit=limit)

    # islice pulls at most cap+1 raw items (the +1 is a cut-off sentinel).
    assert yielded <= cap + 1
    assert notes == []
    assert truncated is True


def test_vault_notes_selects_eligible_notes_reached_within_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eligible notes reached within the raw-walk cap are actually returned."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # 3 shallow notes ahead of a pile of deeper, depth-filtered notes: the
    # shallow notes are within the cap and must be selected, even though the
    # cap still cuts the (much larger) deep tail off.
    shallow_paths = [vault / f"note-{index:04d}.md" for index in range(3)]
    deep_paths = [vault / f"deep-{index:04d}" / "note.md" for index in range(100)]
    all_paths = shallow_paths + deep_paths

    def fake_rglob(root: Path, pattern: str):
        assert root == vault
        assert pattern == "*.md"
        yield from all_paths

    monkeypatch.setattr(Path, "rglob", fake_rglob)
    monkeypatch.setattr(lab_routes, "_note_summary", lambda root, path: {"path": path.name})

    limit = 3
    notes, truncated = lab_routes._vault_notes(vault, depth=1, limit=limit)

    assert sorted(summary["path"] for _resolved, summary in notes) == sorted(
        path.name for path in shallow_paths
    )
    # The deep tail past the cap is real (more matches exist), so this is
    # truthfully truncated even though exactly `limit` notes were selected.
    assert truncated is True


def _real_vault(tmp_path: Path, *, shallow: int = 0, deep: int = 0) -> Path:
    def frontmatter(note_id: str) -> str:
        return (
            "---\nid: " + note_id + "\ntype: run\ndiscipline: writing\n"
            "created: '2026-01-01T00:00:00Z'\nsource_run: null\n"
            "confidence: high\nstatus: active\nsupersedes: null\n---\n# Note\n"
        )

    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(shallow):
        (vault / f"note-{index:04d}.md").write_text(
            frontmatter(f"shallow-{index:04d}"), encoding="utf-8"
        )
    for index in range(deep):
        nested = vault / f"deep-{index:04d}"
        nested.mkdir()
        (nested / "note.md").write_text(
            frontmatter(f"deep-{index:04d}"), encoding="utf-8"
        )
    return vault


def test_vault_notes_exhausted_and_complete_reports_not_truncated(
    tmp_path: Path,
) -> None:
    """Fewer files than the cap, all surviving filters within limit -> False."""
    vault = _real_vault(tmp_path, shallow=2)
    notes, truncated = lab_routes._vault_notes(vault, limit=5)
    assert len(notes) == 2
    assert truncated is False


def test_vault_notes_exhausted_but_limit_trimmed_reports_truncated(
    tmp_path: Path,
) -> None:
    """Raw walk exhausts naturally (no cap cutoff) but the result count still
    exceeds ``limit`` -- truncated must be True from the post-filter branch
    alone, independent of the candidate-cap/cutoff branch."""
    limit = 2
    cap = limit * lab_routes._VAULT_NOTES_CANDIDATE_FACTOR
    vault = _real_vault(tmp_path, shallow=5)
    assert 5 < cap  # raw walk exhausts naturally, well under the cap
    notes, truncated = lab_routes._vault_notes(vault, limit=limit)
    assert len(notes) == limit
    assert truncated is True


def test_vault_notes_exact_candidate_cap_boundary_reports_not_truncated(
    tmp_path: Path,
) -> None:
    """Exactly ``candidate_cap`` raw files exist (no cap+1 sentinel arrives)
    and at most ``limit`` survive filtering -- the off-by-one boundary must
    read as complete, not cut off."""
    limit = 1
    cap = limit * lab_routes._VAULT_NOTES_CANDIDATE_FACTOR
    deep = cap - 1
    vault = _real_vault(tmp_path, shallow=1, deep=deep)
    notes, truncated = lab_routes._vault_notes(vault, depth=1, limit=limit)
    assert len(notes) == 1
    assert truncated is False


def test_vault_notes_sentinel_exactly_at_cap_plus_one_is_never_summarized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the EXACT boundary: precisely ``candidate_cap`` deep (filtered-out)
    files followed by exactly ONE shallow, otherwise-eligible file as the
    (cap+1)'th raw item -- the sentinel islice pulls to detect a cutoff, then
    discards. If the sentinel leaked into ``candidates`` it would be the
    ONLY surviving candidate (everything else is depth-filtered) and would
    obviously be found; asserting an empty, truncated result proves it never
    does, deterministically -- not just bounded by a weaker count."""
    limit = 3
    cap = limit * lab_routes._VAULT_NOTES_CANDIDATE_FACTOR
    vault = _real_vault(tmp_path, deep=cap)
    sentinel = vault / "sentinel.md"
    sentinel.write_text(
        "---\nid: sentinel\ntype: run\ndiscipline: writing\n"
        "created: '2026-01-01T00:00:00Z'\nsource_run: null\n"
        "confidence: high\nstatus: active\nsupersedes: null\n---\n# Note\n",
        encoding="utf-8",
    )
    deep_paths = sorted((vault / f"deep-{index:04d}" / "note.md" for index in range(cap)), key=str)
    ordered_paths = [*deep_paths, sentinel]
    assert len(ordered_paths) == cap + 1

    def fake_rglob(root: Path, pattern: str):
        assert root == vault
        assert pattern == "*.md"
        yield from ordered_paths

    monkeypatch.setattr(Path, "rglob", fake_rglob)

    summarized: list[str] = []
    real_note_summary = lab_routes._note_summary

    def counting_note_summary(root: Path, path: Path) -> dict[str, Any] | None:
        summarized.append(path.name)
        return real_note_summary(root, path)

    monkeypatch.setattr(lab_routes, "_note_summary", counting_note_summary)

    # depth=1 rejects every deep-*/note.md candidate; only the sentinel
    # (shallow) would survive filtering if it were ever admitted as a
    # candidate at all.
    notes, truncated = lab_routes._vault_notes(vault, depth=1, limit=limit)

    assert sentinel.name not in summarized
    assert notes == []
    assert truncated is True


def test_vault_search_limit_hit_on_last_candidate_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching `limit` matches EXACTLY when the last candidate note is
    searched must NOT report truncated -- nothing was left unsearched.
    (Sol repro: one complete candidate, one match, limit=1 previously
    returned X-Vault-Search-Truncated: true -- a false positive at the
    exact boundary.)"""
    vault = _real_vault(tmp_path, shallow=1)
    monkeypatch.setattr(lab_routes, "_vault_root", lambda: vault)
    response = Response()

    lab_routes.vault_search(response, "Note", limit=1)

    assert response.headers["x-vault-search-truncated"] == "false"


def test_vault_search_truncated_reflects_results_cap_independent_of_notes_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OR is genuinely two-sided: results_capped alone (candidate notes
    remained unsearched when `limit` matches were reached) must set
    truncated True even when `_vault_notes` itself reports untruncated."""
    vault = _real_vault(tmp_path, shallow=2)
    monkeypatch.setattr(lab_routes, "_vault_root", lambda: vault)
    response = Response()

    lab_routes.vault_search(response, "Note", limit=1)

    # Two real, un-truncated candidate notes exist (well under the candidate
    # cap), both match "Note"; limit=1 stops after the first with one
    # candidate left unsearched -- results_capped alone must drive this True.
    assert response.headers["x-vault-search-truncated"] == "true"


def test_vault_search_cache_hit_preserves_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache hit must not launder an incomplete result into a complete one:
    the truncated signal recorded at write time has to survive and ride the
    response through every subsequent cache-served call within the TTL.
    Isolates the notes-level OR branch specifically: exactly one candidate
    note (so results_capped is False by the last-candidate boundary above),
    with _vault_notes itself reporting truncated=True -- only the
    `notes_truncated` half of the OR can be driving this True."""
    lab_routes._VAULT_SEARCH_CACHE.clear()
    note = tmp_path / "note.md"
    note.write_text("needle", encoding="utf-8")
    monkeypatch.setattr(lab_routes, "_vault_root", lambda: tmp_path)
    monkeypatch.setattr(
        lab_routes,
        "_vault_notes",
        lambda root, **kwargs: (
            [(note, {"path": "note.md", "title": "note", "type": "note"})],
            True,  # notes-level truncated, e.g. candidate cap was hit
        ),
    )

    first_response = Response()
    first = lab_routes.vault_search(first_response, "needle", limit=1)
    # A single candidate note reaching `limit` is the last-candidate boundary
    # (see test_vault_search_limit_hit_on_last_candidate_is_not_truncated),
    # so results_capped is False here -- this True can only come from
    # notes_truncated surviving the OR.
    assert first_response.headers["x-vault-search-truncated"] == "true"

    key = (str(tmp_path), "needle", 1)
    assert lab_routes._VAULT_SEARCH_CACHE[key][2] is True

    second_response = Response()
    second = lab_routes.vault_search(second_response, "needle", limit=1)
    assert second == first
    assert second_response.headers["x-vault-search-truncated"] == "true"


def test_vault_search_cache_has_a_hard_cardinality_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_routes._VAULT_SEARCH_CACHE.clear()
    note = tmp_path / "note.md"
    note.write_text("shared content", encoding="utf-8")
    monkeypatch.setattr(lab_routes, "_vault_root", lambda: tmp_path)
    monkeypatch.setattr(
        lab_routes,
        "_vault_notes",
        lambda root, **kwargs: ([(note, {"path": "note.md", "title": "note", "type": "note"})], False),
    )

    for index in range(257):
        lab_routes.vault_search(Response(), f"query-{index}", limit=1)

    assert len(lab_routes._VAULT_SEARCH_CACHE) <= lab_routes._VAULT_SEARCH_CACHE_MAX_ENTRIES


def test_vault_search_cache_timestamp_is_captured_after_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab_routes._VAULT_SEARCH_CACHE.clear()
    note = tmp_path / "note.md"
    note.write_text("needle", encoding="utf-8")
    calls = 0
    clock = iter((0.0, lab_routes._VAULT_SEARCH_TTL_SECONDS + 1, lab_routes._VAULT_SEARCH_TTL_SECONDS + 1.1))

    def fake_vault_notes(root: Path, **kwargs: object):
        nonlocal calls
        calls += 1
        return [(note, {"path": "note.md", "title": "note", "type": "note"})], False

    monkeypatch.setattr(lab_routes, "_vault_root", lambda: tmp_path)
    monkeypatch.setattr(lab_routes, "_vault_notes", fake_vault_notes)
    monkeypatch.setattr(lab_routes.time, "monotonic", lambda: next(clock))

    assert lab_routes.vault_search(Response(), "needle", limit=1)
    assert lab_routes.vault_search(Response(), "needle", limit=1)
    assert calls == 1
