"""``curate()`` -- the deterministic core of the 2x-daily curation loop
(contracts/lab-interfaces.md §L06-curator).

Reads the run ledger (`ledger_dir`), the experiment ledger + tournament/elo
tables (`store`), and any existing vault notes (`vault_dir`) to recompute, per
subject, the top orchestrations by ELO with a distilled judge-notes digest,
plus a validated-traits playbook rollup. When `dry_run` is False it persists
the recomputed leaderboard rows via `store.replace_leaderboard` and
writes vault leaderboard/log-book + playbook notes via L08 (lazy import,
since L08 is a sibling package built in parallel and not present in every
worktree that runs these tests).

BINDING (design review HD-006): this module NEVER calls `store.set_champion`.
Promotion is L03's job (`surfaces.promote()`, gated by the §11.4 thresholds and
human-review for safety-relevant surfaces) -- `curate()` only RECOMMENDS, by
comparing each subject's ELO leader against the current champion and returning
a human-readable recommendation string in the result dict.
"""

from __future__ import annotations

from typing import Any

import omniagentos.lab.curator.queries as queries
import omniagentos.lab.curator.rollup as rollup
from omniagentos.contracts import utc_now_iso
from omniagentos.lab.contracts import LeaderboardEntry, SurfaceKind
from omniagentos.lab.db import LabStore

#: How many orchestrations the log-book keeps per subject ("top orchestrations").
DEFAULT_TOP_N = 10

#: Judge-notes digests are capped so a row cannot grow without bound across
#: many refresh cycles.
_JUDGE_NOTES_MAX_CHARS = 600

_ORCHESTRATION_GENOME = SurfaceKind.ORCHESTRATION_GENOME.value


def curate(
    store: LabStore,
    ledger_dir: str,
    vault_dir: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Recompute the leaderboard/log-book + judge-notes digest + playbook rollup.

    Returns a summary dict (see the keys built below) that is always safe to
    `json.dumps`. When `dry_run` is True, no store mutation and no vault write
    ever happens -- the function is read-only end to end.
    """
    from omniagentos.ledger import read_manifests  # lazy: H1 leaf, mirrors omniagentos.scheduler

    manifests = read_manifests(ledger_dir, limit=500)
    experiments = store.list_experiments(limit=500)
    playbook_entries = store.list_playbook()
    subjects = queries.distinct_subjects(store)

    leaderboard: dict[str, list[dict[str, Any]]] = {}
    judge_notes_digest: dict[str, list[dict[str, Any]]] = {}
    tournaments_by_subject: dict[str, list[dict[str, Any]]] = {}
    recommendations: list[str] = []

    for entry in subjects:
        subject, discipline = entry["subject"], entry["discipline"]
        previous = {str(row["config_id"]): row for row in store.leaderboard(subject)}
        rows = _recompute_subject(store, subject, previous, experiments)
        leaderboard[subject] = rows
        judge_notes_digest[subject] = [
            {
                "rank": row["rank"],
                "config_id": row["config_id"],
                "elo": row["elo"],
                "notes": row["judge_notes"],
            }
            for row in rows
            if row["judge_notes"]
        ]
        tournaments_by_subject[subject] = queries.recent_tournaments(store, subject)
        recommendations.extend(_recommend_promotion(store, subject, discipline, rows))

        if not dry_run:
            store.replace_leaderboard(
                subject,
                [
                    LeaderboardEntry(
                        subject=subject,
                        discipline=discipline,
                        rank=row["rank"],
                        config_id=row["config_id"],
                        elo=row["elo"],
                        summary=row["summary"],
                        judge_notes=row["judge_notes"],
                        source_experiments=row["source_experiments"],
                    )
                    for row in rows
                ],
            )

    playbook_by_discipline: dict[str, list[dict[str, Any]]] = {}
    for playbook_row in playbook_entries:
        playbook_by_discipline.setdefault(str(playbook_row.get("discipline") or ""), []).append(
            playbook_row
        )

    notes_written: list[str] = []
    if not dry_run:
        # Lazy: L08 (omniagentos.lab.vault) is a sibling package built in
        # parallel and not present in every worktree/test environment.
        from omniagentos.lab.vault import (  # type: ignore[attr-defined]
            render_leaderboard_note,
            render_playbook_note,
        )
        from omniagentos.vault import write_note

        for subject, rows in leaderboard.items():
            if not rows:
                continue
            relpath, content = render_leaderboard_note(subject, rows)
            write_note(vault_dir, relpath, content)
            notes_written.append(relpath)
        for discipline, discipline_entries in sorted(playbook_by_discipline.items()):
            if not discipline:
                continue
            relpath, content = render_playbook_note(discipline, discipline_entries)
            write_note(vault_dir, relpath, content)
            notes_written.append(relpath)

    return {
        "dry_run": dry_run,
        "generated_at": utc_now_iso(),
        "subjects": [entry["subject"] for entry in subjects],
        "runs": rollup.summarize_runs(manifests),
        "experiments": rollup.summarize_experiments(experiments),
        "playbook": rollup.summarize_playbook(playbook_entries),
        "leaderboard": leaderboard,
        "judge_notes_digest": judge_notes_digest,
        "tournaments": tournaments_by_subject,
        "recommendations": recommendations,
        "vault_context": rollup.scan_vault_context(vault_dir),
        "notes_written": notes_written,
    }


def _recompute_subject(
    store: LabStore,
    subject: str,
    previous: dict[str, dict[str, Any]],
    experiments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The recomputed, ranked rows for one subject: current top `DEFAULT_TOP_N`
    configs by ELO, each enriched with a surface-derived summary, a distilled
    judge-notes digest, and the experiments that produced/challenged it."""
    top = queries.top_elo(store, subject, DEFAULT_TOP_N)
    rows: list[dict[str, Any]] = []
    for rank, elo_row in enumerate(top, start=1):
        config_id = str(elo_row["config_id"])
        surface = store.get_surface(config_id)
        notes = queries.recent_judge_notes(store, subject, config_id)
        prior = previous.get(config_id)
        rows.append(
            {
                "rank": rank,
                "previous_rank": int(prior["rank"]) if prior is not None else None,
                "is_new": prior is None,
                "config_id": config_id,
                "elo": float(elo_row["rating"]),
                "summary": _summary_for(surface, elo_row),
                "judge_notes": _truncate("; ".join(notes)),
                "source_experiments": _source_experiments_for(experiments, config_id),
            }
        )
    return rows


def _summary_for(surface: dict[str, Any] | None, elo_row: dict[str, Any]) -> str:
    """A human-readable one-liner for a leaderboard row, from real data only:
    the surface's own label (if any) and its elo_ratings win/loss record."""
    label = str(surface["label"]) if surface and surface.get("label") else str(elo_row["config_id"])
    record = f"{int(elo_row['wins'])}W/{int(elo_row['losses'])}L/{int(elo_row['draws'])}D"
    return f"{label} — {record} across {int(elo_row['matches'])} matches (elo {float(elo_row['rating']):.1f})"


def _source_experiments_for(experiments: list[dict[str, Any]], config_id: str) -> list[str]:
    return [
        str(experiment["id"])
        for experiment in experiments
        if experiment.get("champion_surface_id") == config_id
        or experiment.get("challenger_surface_id") == config_id
    ]


def _truncate(text: str, limit: int = _JUDGE_NOTES_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _recommend_promotion(
    store: LabStore, subject: str, discipline: str, ranked_rows: list[dict[str, Any]]
) -> list[str]:
    """RECOMMEND only (BINDING HD-006) -- never calls `store.set_champion`.
    Promotion is `surfaces.promote()`'s job (L03), with a human required for
    `SURFACE_REQUIRES_HUMAN`/`safety_relevant` surfaces."""
    if not ranked_rows:
        return []
    leader = ranked_rows[0]
    champion = store.get_champion(discipline, _ORCHESTRATION_GENOME)
    if champion is None:
        return [
            f"subject={subject} discipline={discipline}: no seeded champion yet; "
            f"RECOMMEND seeding {leader['config_id']} (elo {leader['elo']:.1f}) via "
            "surfaces.seed_champion() -- the curator does not set it."
        ]
    if str(champion["surface_id"]) != leader["config_id"]:
        return [
            f"subject={subject} discipline={discipline}: leaderboard leader "
            f"{leader['config_id']} (elo {leader['elo']:.1f}) outranks current champion "
            f"{champion['surface_id']}; RECOMMEND review for promotion via "
            "surfaces.promote() -- the curator does not call set_champion."
        ]
    return []
