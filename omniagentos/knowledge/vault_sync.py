"""Admin-routed, idempotent vault note ingestion."""

from __future__ import annotations

from pathlib import Path

from omniagentos.knowledge.contracts import EpisodeSource, IngestResult, PromotionDenied
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.store import KnowledgeStore


def _previous_episode(store: KnowledgeStore, note_id: str) -> tuple[int, str] | None:
    """Read the latest version for a vault note; the store has no public lookup."""
    with store._lock:  # noqa: SLF001 - frozen store has no source-ref read helper
        with store._conn().cursor() as cur:  # noqa: SLF001
            cur.execute(
                """
                SELECT id, content FROM episodes
                WHERE source = %s AND source_ref = %s
                ORDER BY id DESC LIMIT 1
                """,
                (EpisodeSource.VAULT.value, note_id),
            )
            row = cur.fetchone()
    return (int(row[0]), str(row[1])) if row else None


def _fact_ids_for_episode(store: KnowledgeStore, episode_id: int) -> list[int]:
    with store._lock:  # noqa: SLF001 - see _previous_episode
        with store._conn().cursor() as cur:  # noqa: SLF001
            cur.execute("SELECT id FROM facts WHERE episode_id = %s", (episode_id,))
            return [int(row[0]) for row in cur.fetchall()]


def ingest_vault(
    store: KnowledgeStore,
    *,
    note_id: str,
    content: str,
    title: str = "",
    discipline: str | None = None,
) -> IngestResult:
    """Sync one vault note, returning existing rows for an unchanged note.

    An edited note writes a new quarantined version and supersedes facts from
    its immediate predecessor.  This requires the admin DB role; unchanged
    notes create neither duplicate episodes nor duplicate facts.
    """
    if store._role == "knowledge_agent":  # noqa: SLF001 - role is determined by frozen store
        raise PromotionDenied("vault sync requires an admin KnowledgeStore")
    previous = _previous_episode(store, note_id)
    if previous is not None and previous[1] == content:
        fact_ids = _fact_ids_for_episode(store, previous[0])
        return IngestResult(episode_id=previous[0], fact_ids=fact_ids)

    result = ingest_episode(
        store,
        source=EpisodeSource.VAULT,
        content=content,
        source_ref=note_id,
        discipline=discipline,
    )
    if previous is not None and result.fact_ids:
        winner = result.fact_ids[0]
        for old_fact_id in _fact_ids_for_episode(store, previous[0]):
            store.supersede_fact(old_fact_id, winner)
    return result


def ingest_web(
    store: KnowledgeStore,
    *,
    url: str,
    title: str,
    content: str,
    discipline: str | None = None,
) -> IngestResult:
    """Compatibility name for an admin-provided source/note sync.

    The historical task spelling called this ``ingest_web`` in this module;
    vault ingestion still writes a VAULT episode at 0.9 source trust.
    """
    return ingest_vault(
        store,
        note_id=url,
        title=title,
        content=content,
        discipline=discipline,
    )


def sync_path(store: KnowledgeStore, path: str | Path) -> list[IngestResult]:
    """Sync one Markdown file or every Markdown file under a directory.

    A missing path raises ``FileNotFoundError``. That must not collapse into the
    same ``[]`` returned for an existing directory that simply has no ``.md``
    files — the CLI prints ``synced N vault note(s)`` and exits 0 on success, so
    a typo'd vault path would look like a clean empty sync.
    """
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"vault path does not exist: {root}")
    if root.is_file():
        paths = [root]
    elif root.is_dir():
        paths = sorted(root.rglob("*.md"))
    else:
        # Exists but is neither file nor directory (broken symlink, special node).
        raise FileNotFoundError(f"vault path does not exist as a file or directory: {root}")
    results: list[IngestResult] = []
    for note_path in paths:
        content = note_path.read_text(encoding="utf-8")
        title = next(
            (
                line.removeprefix("# ").strip()
                for line in content.splitlines()
                if line.startswith("# ")
            ),
            note_path.stem,
        )
        results.append(ingest_vault(store, note_id=str(note_path), title=title, content=content))
    return results
