"""HTTP routes for the H2 empirical self-improvement lab."""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from copy import deepcopy
from pathlib import Path, PurePath
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response

from omniagentos.api.routes.control import _emit, fail
from omniagentos.contracts import default_vault_dir, utc_now_iso
from omniagentos.lab import jobs as lab_jobs
from omniagentos.lab import surfaces as lab_surfaces
from omniagentos.lab.api.deps import LabStoreDep
from omniagentos.lab.api.models import (
    CreateExperimentRequest,
    CreateTournamentRequest,
    CurateRequest,
    DispositionRequest,
    RollbackRequest,
    RunExperimentRequest,
)
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    Experiment,
    ExperimentStatus,
    ExplorePolicy,
    LabEvents,
    SurfaceKind,
)
from omniagentos.lab.db import ChampionCASMismatch
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vault import parse_frontmatter
from omniagentos.vault.errors import VaultError

router = APIRouter(prefix="/api/lab", tags=["lab"])

_JSON_COLUMNS = {
    "budgets_json": "budgets",
    "promotion_json": "promotion_threshold",
    "scorecard_json": "scorecard",
    "metrics_json": "metrics",
    "per_case_json": "per_case",
    "config_ids_json": "config_ids",
    "source_experiments_json": "source_experiments",
    "evidence_experiments_json": "evidence_experiments",
    "evidence_tournaments_json": "evidence_tournaments",
    "checkpoint_json": "checkpoint",
    "result_json": "result",
}
_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]]*)?(?:\|[^\]]*)?\]\]")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _decode(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _row(row: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize SQLite-shaped lab rows to the documented JSON field names."""
    if row is None:
        return {}
    result = dict(row)
    for source, target in _JSON_COLUMNS.items():
        if source in result:
            if target in {"scorecard", "result"}:
                fallback: Any = None
            elif target in {
                "config_ids",
                "source_experiments",
                "evidence_experiments",
                "evidence_tournaments",
            }:
                fallback = []
            else:
                fallback = {}
            result[target] = _decode(result.pop(source), fallback)
    for name in ("protected", "deterministic_passed", "blind", "safety_relevant"):
        if name in result and result[name] is not None:
            result[name] = bool(result[name])
    return result


def _safe(value: Any, held_out: bool = False) -> Any:
    """Recursively remove protected expected values before an HTTP response.

    Eval results currently store metrics only, but this guard deliberately also
    protects future result shapes and test doubles that contain a nested case.
    """
    if isinstance(value, dict):
        is_held_out = held_out or value.get("split") == "held_out"
        return {
            key: _safe(item, is_held_out)
            for key, item in value.items()
            if not (is_held_out and key == "expected")
        }
    if isinstance(value, list):
        return [_safe(item, held_out) for item in value]
    return value


def _emit_lab(store: Any, event_type: str, action: str, **kwargs: Any) -> None:
    """LabStore composes H1's event store; test doubles may expose it directly."""
    _emit(getattr(store, "_store", store), event_type, action, **kwargs)


def _surface_content(path: str) -> str | None:
    """Read only safe, relative prompt/genome assets from their fixed roots."""
    requested = PurePath(path)
    if not path or requested.is_absolute() or ".." in requested.parts:
        fail(422, "validation", "surface path must be a safe relative asset path")
    parts = requested.parts
    if parts[:2] == ("vault", "prompts"):
        root = (Path(default_vault_dir()).resolve() / "prompts").resolve()
        relative = Path(*parts[2:])
    elif parts[:2] == ("configs", "genomes"):
        root = (Path(__file__).resolve().parents[4] / "configs" / "genomes").resolve()
        relative = Path(*parts[2:])
    else:
        fail(422, "validation", "surface path is outside the prompt/genome allow-list")
    candidate = (root / relative).resolve()
    if inode_relative_parts_anchored(candidate, root) is None:
        fail(422, "validation", "surface path escapes its asset root")
    if not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8")


def _vault_root() -> Path:
    return Path(default_vault_dir()).resolve()


def _vault_note_path(path: str) -> Path:
    """Resolve a caller path and reject both lexical and symlink traversal."""
    requested = PurePath(path)
    if not path or requested.is_absolute() or ".." in requested.parts:
        fail(422, "validation", "vault path must stay within vault_dir", {"path": path})
    root = _vault_root()
    candidate = (root / requested).resolve()
    if inode_relative_parts_anchored(candidate, root) is None:
        fail(422, "validation", "vault path must stay within vault_dir", {"path": path})
    return candidate


def _links(content: str) -> list[str]:
    return list(dict.fromkeys(match.group(1).strip() for match in _LINK_RE.finditer(content)))


def _note_summary(root: Path, path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(content)
    except (OSError, VaultError):
        return None
    body = content.split("---", 2)[-1]
    title = _TITLE_RE.search(body)
    return {
        "id": frontmatter.id,
        "type": frontmatter.type.value,
        "title": title.group(1) if title else path.stem.replace("-", " "),
        "links": _links(content),
        "path": path.relative_to(root).as_posix(),
    }


def _vault_notes(
    root: Path, *, depth: int | None = None, limit: int | None = None
) -> tuple[list[tuple[Path, dict[str, Any]]], bool]:
    """Every readable note under ``root``, path-sorted, optionally bounded.

    Returns ``(notes, truncated)``. ``truncated`` is True whenever the result
    may be INCOMPLETE, for either of two independent reasons: the raw
    candidate walk was cut off by the candidate cap before it could confirm
    no more matches exist (even if every candidate seen within the cap was
    then filtered out), OR the post-filter result count exceeded ``limit``.
    A vault genuinely exhausted within the cap, with at most ``limit`` notes
    surviving filtering, reports False.

    ``depth`` counts directory levels below ``root`` (``1`` = notes sitting
    directly in the vault root); ``None`` walks the whole tree. ``limit`` caps
    how many notes are OPENED, which is what the walk actually costs — each note
    is read and frontmatter-parsed. Exactly ONE note beyond the limit is opened,
    which is the cheapest possible way to tell "the limit bit" from "the vault
    ended": the alternative, counting candidates, cannot tell a skipped note
    from an unreadable one. The candidate walk itself is also bounded when
    ``limit`` is set: every ``rglob`` yield counts against the visited cap
    BEFORE any inode/depth filtering, so a bounded request does not scan
    the entire vault even when most paths are filtered out (e.g. a deep
    tree under a shallow ``depth``).

    Selection is against the path-sorted candidate list, not raw ``rglob``
    order, so the notes actually returned are deterministic given a fixed
    candidate set. The candidate SET itself is filesystem-order-dependent
    when ``limit`` is set: the raw walk is capped before sorting (see the
    cap-then-sort comment below), so which notes make the bounded candidate
    set can vary with directory traversal order, even though which of those
    candidates get selected does not.
    """
    if not root.is_dir():
        return [], False
    candidates: list[Path] = []
    candidate_cap = None if limit is None else limit * _VAULT_NOTES_CANDIDATE_FACTOR
    scan_cut_off = False
    if candidate_cap is None:
        walk: list[Path] = list(root.rglob("*.md"))
    else:
        # Pull one MORE than the cap. If that sentinel is actually delivered,
        # the raw rglob walk had more matches beyond the cap and was cut off
        # rather than exhausted naturally -- so the candidate set (and
        # therefore ``notes``) may be missing eligible entries with no other
        # signal. The sentinel itself is discarded, never treated as a real
        # candidate.
        raw_walk = list(itertools.islice(root.rglob("*.md"), candidate_cap + 1))
        scan_cut_off = len(raw_walk) > candidate_cap
        walk = raw_walk[:candidate_cap]
    for path in walk:
        resolved = path.resolve()
        if inode_relative_parts_anchored(resolved, root) is None:
            continue
        if depth is not None and len(resolved.relative_to(root).parts) > depth:
            continue
        candidates.append(resolved)
    # ``candidates`` here is an ``rglob``-order prefix of at most ``candidate_cap``
    # visited entries, sorted afterward -- a bounded walk necessarily selects a
    # filesystem-order-dependent prefix before sorting; this is a deliberate
    # cap-then-sort trade-off, not a correctness bug.
    candidates.sort(key=lambda item: item.relative_to(root).as_posix())
    notes: list[tuple[Path, dict[str, Any]]] = []
    for resolved in candidates:
        if limit is not None and len(notes) > limit:
            break
        if summary := _note_summary(root, resolved):
            notes.append((resolved, summary))
    if limit is not None and len(notes) > limit:
        return notes[:limit], True
    return notes, scan_cut_off


def _vault_tree(
    root: Path, *, depth: int | None = None, limit: int | None = None
) -> dict[str, Any]:
    tree: dict[str, Any] = {"name": root.name, "folders": [], "notes": []}
    folders: dict[tuple[str, ...], dict[str, Any]] = {(): tree}
    notes, truncated = _vault_notes(root, depth=depth, limit=limit)
    for path, summary in notes:
        parent = tree
        parts = path.relative_to(root).parts[:-1]
        for index, part in enumerate(parts):
            key = parts[: index + 1]
            if key not in folders:
                child = {"name": part, "folders": [], "notes": []}
                folders[key] = child
                parent["folders"].append(child)
            parent = folders[key]
        parent["notes"].append(dict(summary))
    # Truthful shape: say what the bounds were and whether they bit, so a client
    # rendering a graph knows it is looking at a slice of the vault, not the
    # vault. ``truncated`` is True when the result MAY be incomplete -- either
    # the raw candidate walk was cut off by the cap, or the post-filter count
    # exceeded ``limit``. A vault exhausted within the cap with at most
    # ``limit`` surviving notes reports False.
    tree["depth"] = depth
    tree["limit"] = limit
    tree["note_count"] = len(notes)
    tree["truncated"] = truncated
    return tree


# Short in-process cache for the tree walk. The vault is ~1k files and the walk
# opens and parses every one of them (up to 50s measured); the graph panel
# re-fetches it on every mount, and the route is unauthenticated. A few seconds
# of staleness is invisible in a knowledge graph; a 50s hold on the event loop
# is not. Deliberately NOT keyed on mtime: stat-ing the tree to decide whether
# to walk the tree is most of the cost we are avoiding.
_VAULT_TREE_TTL_SECONDS = 30.0
_VAULT_TREE_CACHE: dict[tuple[str, int | None, int | None], tuple[float, dict[str, Any]]] = {}
_VAULT_TREE_CACHE_LOCK = threading.Lock()


def _vault_tree_cached(
    root: Path, *, depth: int | None, limit: int | None, now: float | None = None
) -> dict[str, Any]:
    key = (str(root), depth, limit)
    moment = time.monotonic() if now is None else now
    with _VAULT_TREE_CACHE_LOCK:
        hit = _VAULT_TREE_CACHE.get(key)
        if hit is not None and (moment - hit[0]) < _VAULT_TREE_TTL_SECONDS:
            return deepcopy(hit[1])
    tree = _vault_tree(root, depth=depth, limit=limit)
    with _VAULT_TREE_CACHE_LOCK:
        _VAULT_TREE_CACHE[key] = (moment, tree)
    return deepcopy(tree)


def _reset_vault_tree_cache() -> None:
    """Private test seam: drop the cached trees for deterministic cache tests.

    Production never needs this — entries self-expire via _VAULT_TREE_TTL_SECONDS.
    """
    with _VAULT_TREE_CACHE_LOCK:
        _VAULT_TREE_CACHE.clear()


@router.get("/disciplines")
def list_disciplines(
    store: LabStoreDep, limit: int = Query(100, ge=1, le=500)
) -> list[dict[str, Any]]:
    rows = [_row(row) for row in store.discipline_summaries(limit)]
    for row in rows:
        champions = store.list_champions(row["discipline"])
        row["champion_surfaces"] = [_row(champion) for champion in champions]
    return _safe(rows)


@router.get("/experiments")
def list_experiments(
    store: LabStoreDep,
    discipline: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _safe([_row(row) for row in store.list_experiments(discipline, status, limit)])


@router.post("/experiments", status_code=201)
def create_experiment(body: CreateExperimentRequest, store: LabStoreDep) -> dict[str, Any]:
    champion = store.get_champion(body.discipline, body.mutable_surface_kind.value)
    if champion is None:
        fail(
            409,
            "invalid_state",
            "no champion is registered for this surface",
            {"discipline": body.discipline, "kind": body.mutable_surface_kind.value},
        )
    if store.get_surface(body.challenger_surface_id) is None:
        fail(404, "not_found", "challenger surface not found", {"id": body.challenger_surface_id})
    suite = store.get_eval_suite(body.eval_suite_id)
    if suite is None:
        fail(404, "not_found", "evaluation suite not found", {"id": body.eval_suite_id})
    experiment = Experiment(
        hypothesis=body.hypothesis,
        discipline=body.discipline,
        mutable_surface_kind=body.mutable_surface_kind,
        champion_surface_id=str(champion["surface_id"]),
        challenger_surface_id=body.challenger_surface_id,
        eval_suite_id=body.eval_suite_id,
        dataset_hash=str(suite.get("dataset_hash", "")),
        budgets=body.budgets if body.budgets is not None else Budgets(),
        explore_policy=body.explore_policy or ExplorePolicy.EXPLOIT,
    )
    store.create_experiment(experiment)
    _emit_lab(
        store,
        LabEvents.EXPERIMENT_UPDATED,
        "experiment.created",
        target_type="experiment",
        target_id=experiment.id,
        payload={"experiment_id": experiment.id, "discipline": experiment.discipline},
    )
    return _safe(experiment.model_dump(mode="json"))


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, store: LabStoreDep) -> dict[str, Any]:
    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        fail(404, "not_found", "experiment not found", {"id": experiment_id})
    result = _row(experiment)
    result["eval_results"] = [_row(row) for row in store.eval_results(experiment_id)]
    result["judge_notes"] = [_row(row) for row in store.judge_records(experiment_id)]
    return _safe(result)


@router.post("/experiments/{experiment_id}/run", status_code=202)
def run_experiment(
    experiment_id: str,
    body: RunExperimentRequest,
    store: LabStoreDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if store.get_experiment(experiment_id) is None:
        fail(404, "not_found", "experiment not found", {"id": experiment_id})

    job, created = lab_jobs.enqueue(
        store, experiment_id, idempotency_key=idempotency_key, dry_run=body.dry_run
    )

    _emit_lab(
        store,
        LabEvents.EXPERIMENT_UPDATED,
        "experiment.run_enqueued",
        target_type="experiment",
        target_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "job_id": job.job_id,
            "created": created,
        },
    )

    return {
        "status": "queued",
        "job_id": job.job_id,
        "experiment_id": experiment_id,
        "idempotency_key": job.idempotency_key,
        "created": created,
        "state": job.state,
    }


@router.post("/experiments/{experiment_id}/disposition")
def set_disposition(
    experiment_id: str, body: DispositionRequest, store: LabStoreDep
) -> dict[str, Any]:
    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        fail(404, "not_found", "experiment not found", {"id": experiment_id})
    fields: dict[str, Any] = {"disposition": body.decision.value, "updated_at": utc_now_iso()}
    if body.decision is Disposition.PROMOTE:
        try:
            lab_surfaces.promote(
                store,
                experiment_id,
                str(experiment["challenger_surface_id"]),
                human_decided_by=body.decided_by,
                operator_token=body.operator_token,
            )
        except ChampionCASMismatch:
            fail(409, "conflict", "champion changed while promoting")
        except ValueError as exc:
            fail(409, "invalid_state", str(exc))
        fields["status"] = ExperimentStatus.PROMOTED.value
    elif body.decision in {Disposition.REJECT, Disposition.INVALID}:
        fields["status"] = ExperimentStatus.DECIDED.value
    store.update_experiment(experiment_id, fields)
    updated = store.get_experiment(experiment_id)
    assert updated is not None
    result = _row(updated)
    result["decision_note"] = body.note
    result["decided_by"] = body.decided_by
    _emit_lab(
        store,
        LabEvents.EXPERIMENT_UPDATED,
        "experiment.disposition",
        target_type="experiment",
        target_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "discipline": result["discipline"],
            "decision": body.decision.value,
        },
    )
    if body.decision is Disposition.PROMOTE:
        _emit_lab(
            store,
            LabEvents.SURFACE_PROMOTED,
            "surface.promoted",
            target_type="surface",
            target_id=str(experiment["challenger_surface_id"]),
            payload={"experiment_id": experiment_id, "discipline": result["discipline"]},
        )
    return _safe(result)


@router.get("/jobs/{job_id}")
def get_lab_job(job_id: str, store: LabStoreDep) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        fail(404, "not_found", "job not found", {"id": job_id})
    return _safe(_row(job))


@router.post("/jobs/{job_id}/cancel")
def cancel_lab_job(job_id: str, store: LabStoreDep) -> dict[str, Any]:
    job = lab_jobs.request_cancel(store, job_id)
    if job is None:
        fail(404, "not_found", "job not found", {"id": job_id})
    row = store.get_job(job_id)
    assert row is not None
    return _safe(_row(row))


@router.get("/surfaces")
def list_surfaces(
    store: LabStoreDep,
    discipline: str | None = None,
    kind: SurfaceKind | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    rows = store.list_surfaces(discipline, None if kind is None else kind.value, limit)
    return _safe([_row(row) for row in rows])


@router.get("/surfaces/{surface_id}")
def get_surface(surface_id: str, store: LabStoreDep) -> dict[str, Any]:
    surface = store.get_surface(surface_id)
    if surface is None:
        fail(404, "not_found", "surface not found", {"id": surface_id})
    result = _row(surface)
    result["content"] = _surface_content(str(result["path"]))
    return _safe(result)


@router.get("/champions")
def list_champions(
    store: LabStoreDep, discipline: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict[str, Any]]:
    champions = [_row(row) for row in store.list_champions(discipline, limit)]
    for champion in champions:
        champion["history"] = [
            _row(row)
            for row in store.champion_history(champion["discipline"], champion["surface_kind"])
        ]
    return _safe(champions)


@router.post("/champions/{discipline}/{kind}/rollback")
def rollback_champion(
    discipline: str,
    kind: SurfaceKind,
    body: RollbackRequest,
    store: LabStoreDep,
) -> dict[str, Any]:
    try:
        lab_surfaces.rollback(store, discipline, kind, operator_token=body.operator_token)
    except ChampionCASMismatch:
        fail(409, "conflict", "champion changed while rolling back")
    except ValueError as exc:
        fail(409, "invalid_state", str(exc))
    updated = store.get_champion(discipline, kind.value)
    assert updated is not None
    result = _row(updated)
    _emit_lab(
        store,
        LabEvents.SURFACE_PROMOTED,
        "champion.rolled_back",
        target_type="surface",
        target_id=str(updated["surface_id"]),
        payload={"discipline": discipline, "kind": kind.value},
    )
    return _safe(result)


@router.get("/tournaments")
def list_tournaments(
    store: LabStoreDep,
    subject: str | None = None,
    discipline: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _safe([_row(row) for row in store.list_tournaments(subject, discipline, limit)])


@router.post("/tournaments", status_code=201)
def create_tournament(body: CreateTournamentRequest, store: LabStoreDep) -> dict[str, Any]:
    from omniagentos.lab.tournament.core import run_tournament
    from omniagentos.lab.tournament.driver import TournamentDriver

    tournament = run_tournament(
        store,
        TournamentDriver(store),
        body.subject,
        body.discipline,
        body.config_ids,
        body.arena_task,
        dry_run=body.dry_run,
    )
    _emit_lab(
        store,
        LabEvents.TOURNAMENT_UPDATED,
        "tournament.created",
        target_type="tournament",
        target_id=tournament.id,
        payload={
            "tournament_id": tournament.id,
            "subject": tournament.subject,
            "discipline": tournament.discipline,
        },
    )
    return _safe(tournament.model_dump(mode="json"))


@router.get("/tournaments/{tournament_id}")
def get_tournament(tournament_id: str, store: LabStoreDep) -> dict[str, Any]:
    tournament = store.get_tournament(tournament_id)
    if tournament is None:
        fail(404, "not_found", "tournament not found", {"id": tournament_id})
    result = _row(tournament)
    result["matches"] = [_row(row) for row in store.tournament_matches(tournament_id)]
    result["elo"] = [_row(row) for row in store.elo_for_subject(str(result["subject"]))]
    return _safe(result)


@router.get("/leaderboard")
def get_leaderboard(
    store: LabStoreDep, subject: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict[str, Any]]:
    return _safe([_row(row) for row in store.leaderboard(subject, limit)])


@router.get("/playbook")
def get_playbook(
    store: LabStoreDep, discipline: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict[str, Any]]:
    return _safe([_row(row) for row in store.list_playbook(discipline, limit)])


@router.post("/curate")
def curate(body: CurateRequest, store: LabStoreDep) -> dict[str, Any]:
    summary = {"status": "dry_run" if body.dry_run else "queued", "dry_run": body.dry_run}
    _emit_lab(
        store, LabEvents.CURATION_RAN, "curation.requested", target_type="curation", payload=summary
    )
    return {"summary": summary}


_VAULT_TREE_DEFAULT_DEPTH = 3
_VAULT_TREE_DEFAULT_LIMIT = 500
# Sort a bounded superset so skipped depth/inode candidates do not disturb order.
_VAULT_NOTES_CANDIDATE_FACTOR = 4
_VAULT_SEARCH_DEFAULT_LIMIT = 500
_VAULT_SEARCH_TTL_SECONDS = 30.0
_VAULT_SEARCH_CACHE_MAX_ENTRIES = 256
# (write_moment, results, truncated) -- truncated MUST survive a cache hit,
# or a cache-served incomplete result becomes indistinguishable from a
# genuinely complete one for the whole TTL window (mirrors _VAULT_TREE_CACHE,
# which embeds truncated in the cached tree dict itself).
_VAULT_SEARCH_CACHE: dict[
    tuple[str, str, int], tuple[float, list[dict[str, Any]], bool]
] = {}


@router.get("/vault/tree")
def vault_tree(
    depth: int = Query(_VAULT_TREE_DEFAULT_DEPTH, ge=1, le=32),
    limit: int = Query(_VAULT_TREE_DEFAULT_LIMIT, ge=1, le=20000),
    full: bool = Query(False),
) -> dict[str, Any]:
    """The vault note tree, BOUNDED by default (depth 3, 500 notes).

    This route walked ~1k files and parsed every one of them on every request —
    up to 50s, unauthenticated, with no caller-side bound. It is now bounded by
    default and served from a short in-process cache
    (``_VAULT_TREE_TTL_SECONDS``).

    The full walk is still available, but only when a caller ASKS for it:
    ``?full=1`` (or raise ``depth``/``limit``). The response echoes ``depth``,
    ``limit`` and ``note_count`` and sets ``truncated`` when the limit bit, so a
    bounded tree is never mistaken for the whole vault.
    """
    return _vault_tree_cached(
        _vault_root(),
        depth=None if full else depth,
        limit=None if full else limit,
    )


@router.get("/vault/note")
def vault_note(path: str = Query(min_length=1)) -> dict[str, Any]:
    note_path = _vault_note_path(path)
    if not note_path.is_file():
        fail(404, "not_found", "vault note not found", {"path": path})
    try:
        content = note_path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(content)
    except (OSError, VaultError) as exc:
        fail(422, "validation", "invalid vault note", {"path": path, "reason": str(exc)})
    body = content.split("---", 2)[-1]
    return {
        "frontmatter": frontmatter.model_dump(mode="json"),
        "body": body,
        "links": _links(content),
    }


@router.get("/vault/search")
def vault_search(
    response: Response,
    q: str = Query(min_length=1),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Search a bounded, cached slice of vault notes.

    The search opens at most ``_VAULT_SEARCH_DEFAULT_LIMIT`` notes by default
    and returns at most the requested number of matches. Results are cached
    briefly so repeated unauthenticated searches do not rescan the vault.

    The result may be incomplete (the candidate cap was hit, OR the
    ``limit`` results cap was hit while candidate notes remained unsearched).
    Reaching ``limit`` on the very last candidate note is NOT truncation --
    nothing was left unsearched, so that boundary reports False. That
    signal is NOT dropped on the floor: it rides the ``X-Vault-Search-Truncated``
    response header and is preserved through a cache hit, matching how
    ``vault_tree`` keeps ``truncated`` inside its cached response body.
    """
    root = _vault_root()
    key = (str(root), q.casefold(), limit)
    moment = time.monotonic()
    with _VAULT_TREE_CACHE_LOCK:
        hit = _VAULT_SEARCH_CACHE.get(key)
        if hit is not None and (moment - hit[0]) < _VAULT_SEARCH_TTL_SECONDS:
            response.headers["X-Vault-Search-Truncated"] = "true" if hit[2] else "false"
            return deepcopy(hit[1])

    needle = q.casefold()
    results: list[dict[str, Any]] = []
    notes, notes_truncated = _vault_notes(root, limit=_VAULT_SEARCH_DEFAULT_LIMIT)
    results_capped = False
    note_count = len(notes)
    for note_index, (path, summary) in enumerate(notes):
        content = path.read_text(encoding="utf-8")
        index = content.casefold().find(needle)
        if index < 0:
            continue
        start, end = max(0, index - 80), min(len(content), index + len(q) + 120)
        results.append(
            {
                "path": summary["path"],
                "title": summary["title"],
                "type": summary["type"],
                "snippet": content[start:end].replace("\n", " "),
            }
        )
        if len(results) >= limit:
            # Truncation only if candidate notes remained unsearched -- hitting
            # `limit` on the LAST candidate left nothing out.
            results_capped = note_index < note_count - 1
            break
    truncated = notes_truncated or results_capped
    write_moment = time.monotonic()
    with _VAULT_TREE_CACHE_LOCK:
        expired = [
            cache_key
            for cache_key, (timestamp, _results, _truncated) in _VAULT_SEARCH_CACHE.items()
            if write_moment - timestamp >= _VAULT_SEARCH_TTL_SECONDS
        ]
        for cache_key in expired:
            del _VAULT_SEARCH_CACHE[cache_key]
        if len(_VAULT_SEARCH_CACHE) >= _VAULT_SEARCH_CACHE_MAX_ENTRIES:
            evict_count = len(_VAULT_SEARCH_CACHE) - _VAULT_SEARCH_CACHE_MAX_ENTRIES + 1
            oldest = sorted(_VAULT_SEARCH_CACHE.items(), key=lambda item: item[1][0])[:evict_count]
            for cache_key, _entry in oldest:
                del _VAULT_SEARCH_CACHE[cache_key]
        _VAULT_SEARCH_CACHE[key] = (write_moment, results, truncated)
    response.headers["X-Vault-Search-Truncated"] = "true" if truncated else "false"
    return deepcopy(results)
