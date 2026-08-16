"""Tests for ChatStore and ProjectStore org-linked folder projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.chats import ChatError, ChatStore
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore


def test_chat_store_lifecycle(store: SqliteStore, collab_store: CollabStore) -> None:
    chat_store = ChatStore(store)
    project_store = ProjectStore(store)

    # Create project to satisfy foreign key constraint
    project_store.create_project({"id": "proj_xyz", "name": "XYZ Project"})

    # 1. Create a chat
    chat = chat_store.create_chat(
        title="My Feature Discussion",
        project_id="proj_xyz",
        meta={"some_key": "some_value"},
    )

    assert chat["id"].startswith("cht_")
    assert chat["title"] == "My Feature Discussion"
    assert chat["project_id"] == "proj_xyz"
    assert chat["status"] == "active"
    assert chat["promoted_at"] is None
    assert chat["meta"] == {"some_key": "some_value"}

    board_task_id = chat["board_task_id"]
    assert board_task_id.startswith("btk_")

    # Verify hidden companion task was created with origin='chat'
    task = collab_store.get_board_task(board_task_id)
    assert task is not None
    assert task["origin"] == "chat"
    assert "My Feature Discussion" in task["title"]

    # 2. Get the chat
    fetched = chat_store.get_chat(chat["id"])
    assert fetched is not None
    assert fetched["id"] == chat["id"]

    # 3. List chats
    chats = chat_store.list_chats()
    assert len(chats) == 1
    assert chats[0]["id"] == chat["id"]

    chats_filtered = chat_store.list_chats(project_id="proj_xyz")
    assert len(chats_filtered) == 1

    chats_filtered_empty = chat_store.list_chats(project_id="proj_other")
    assert len(chats_filtered_empty) == 0

    # 4. Update the chat
    updated = chat_store.update_chat(
        chat["id"],
        title="New Title",
        status="active",
        meta={"another": "field"},
    )
    assert updated["title"] == "New Title"
    assert updated["meta"] == {"some_key": "some_value", "another": "field"}

    # 5. Promote the chat
    promoted = chat_store.promote_chat(chat["id"])
    assert promoted["status"] == "promoted"
    assert promoted["promoted_at"] is not None


def test_create_chat_empty_title_fails(store: SqliteStore) -> None:
    chat_store = ChatStore(store)
    with pytest.raises(ChatError):
        chat_store.create_chat(title="  ")


def test_ensure_org_folder_projects(
    store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_store = ProjectStore(store)

    # Seed org taxonomy in DB
    store._connection.execute(
        "INSERT INTO org_companies (id, name, slug, status, created_at) "
        "VALUES ('co_click', 'Globex', 'click', 'active', 'now')"
    )
    store._connection.execute(
        "INSERT INTO org_products (id, company_id, name, slug, status, created_at) "
        "VALUES ('prd_studio', 'co_click', 'Studio', 'studio', 'active', 'now')"
    )
    store._connection.commit()

    # Set up base directories env
    base_dir = tmp_path / "MyBases"
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_BASES", str(base_dir))

    # Run ensure_org_folder_projects
    projects = project_store.ensure_org_folder_projects()
    assert len(projects) == 1
    proj = projects[0]

    assert proj["id"] == "proj_studio"
    assert proj["name"] == "Globex - Studio"
    assert proj["org_company_id"] == "co_click"
    assert proj["org_product_id"] == "prd_studio"
    assert proj["vault_subfolder"] == "click/studio"

    # Verify directory was physically created
    expected_path = base_dir / "click" / "studio"
    assert expected_path.exists()
    assert proj["root_dirs"] == [str(expected_path.resolve())]

    # Idempotency check: running again should not create new projects
    more_projects = project_store.ensure_org_folder_projects()
    assert len(more_projects) == 0


def test_list_folders(store: SqliteStore) -> None:
    """list_folders returns distinct non-empty folder names."""
    chat_store = ChatStore(store)

    # No chats yet
    assert chat_store.list_folders() == []

    chat_store.create_chat(title="A", meta={"folder": "design"})
    chat_store.create_chat(title="B", meta={"folder": "design"})
    chat_store.create_chat(title="C", meta={"folder": "backend"})
    chat_store.create_chat(title="D", meta={})
    chat_store.create_chat(title="E")

    folders = chat_store.list_folders()
    assert folders == ["backend", "design"]


def test_list_chats_folder_filter(store: SqliteStore) -> None:
    """list_chats filters by folder, including empty-string for no-folder."""
    chat_store = ChatStore(store)

    chat_store.create_chat(title="A", meta={"folder": "design"})
    chat_store.create_chat(title="B", meta={"folder": "backend"})
    chat_store.create_chat(title="C", meta={})

    assert len(chat_store.list_chats(folder="design")) == 1
    assert len(chat_store.list_chats(folder="backend")) == 1
    assert len(chat_store.list_chats(folder="")) == 1
    assert len(chat_store.list_chats(folder="nonexistent")) == 0


def test_soft_delete(store: SqliteStore) -> None:
    """soft_delete_chat marks status='deleted', excluded from lists."""
    chat_store = ChatStore(store)

    chat = chat_store.create_chat(title="Delete Me")
    chat_id = chat["id"]

    deleted = chat_store.soft_delete_chat(chat_id)
    assert deleted["status"] == "deleted"

    # Excluded from default list
    assert len(chat_store.list_chats()) == 0

    # Visible with include_deleted
    assert len(chat_store.list_chats(include_deleted=True)) == 1

    # Get still works
    fetched = chat_store.get_chat(chat_id)
    assert fetched is not None
    assert fetched["status"] == "deleted"

    # Double delete is fine (idempotent)
    chat_store.soft_delete_chat(chat_id)


def test_soft_delete_closes_companion_board_task(
    store: SqliteStore, collab_store: CollabStore
) -> None:
    chat_store = ChatStore(store)
    chat = chat_store.create_chat(title="Close Companion")
    board_task_id = str(chat["board_task_id"])

    task = collab_store.get_board_task(board_task_id)
    assert task is not None
    assert task["status"] == "open"

    chat_store.soft_delete_chat(str(chat["id"]))

    task = collab_store.get_board_task(board_task_id)
    assert task is not None
    assert task["status"] == "cancelled"


def test_soft_delete_nonexistent_raises(store: SqliteStore) -> None:
    chat_store = ChatStore(store)
    with pytest.raises(ChatError):
        chat_store.soft_delete_chat("cht_nonexistent")


def test_find_chat_by_board_task_id(store: SqliteStore) -> None:
    """find_chat_by_board_task_id resolves companion task to chat."""
    chat_store = ChatStore(store)

    chat = chat_store.create_chat(title="Find Me")
    btk_id = chat["board_task_id"]

    found = chat_store.find_chat_by_board_task_id(btk_id)
    assert found is not None
    assert found["id"] == chat["id"]

    # Non-matching returns None
    assert chat_store.find_chat_by_board_task_id("btk_nonexistent") is None

    # Deleted chat is excluded
    chat_store.soft_delete_chat(chat["id"])
    assert chat_store.find_chat_by_board_task_id(btk_id) is None


def test_create_spawn_task(store: SqliteStore, collab_store: CollabStore) -> None:
    """create_spawn_task creates a hidden board task parented to companion."""
    chat_store = ChatStore(store)

    chat = chat_store.create_chat(title="Spawn Parent")
    chat_id = chat["id"]
    companion_id = chat["board_task_id"]

    task_id = chat_store.create_spawn_task(
        chat_id=chat_id,
        title="Sub-agent: Build widget",
        description="Build the main widget component.",
    )

    assert task_id.startswith("btk_")
    bt = collab_store.get_board_task(task_id)
    assert bt is not None
    assert bt["origin"] == "chat"
    assert bt["title"] == "Sub-agent: Build widget"

    # Verify parent link in org (CollabStore._task_dict parses org_json into `org`)
    org = bt["org"]
    assert org.get("parent_task_id") == companion_id
    assert org.get("chat_id") == chat_id
    assert org.get("spawned") is True


def test_create_spawn_task_nonexistent_chat(store: SqliteStore) -> None:
    chat_store = ChatStore(store)
    with pytest.raises(ChatError):
        chat_store.create_spawn_task(
            chat_id="cht_nonexistent",
            title="Nope",
            description="Nope",
        )


def test_deleted_excluded_from_folders(store: SqliteStore) -> None:
    """Soft-deleted chats' folders are excluded from list_folders."""
    chat_store = ChatStore(store)

    chat = chat_store.create_chat(title="Only One", meta={"folder": "archive"})
    assert chat_store.list_folders() == ["archive"]

    chat_store.soft_delete_chat(chat["id"])
    assert chat_store.list_folders() == []


# ── Folder registry (088) ──────────────────────────────────────────────────


def test_folder_registry_union_and_order(store: SqliteStore) -> None:
    """Registry rows and chat-derived folders union; position sorts first."""
    chat_store = ChatStore(store)

    chat_store.create_chat(title="A", meta={"folder": "zeta"})
    chat_store.create_chat(title="B", meta={"folder": "zeta"})
    chat_store.create_chat(title="C", meta={"folder": "alpha"})
    # Registered folders get positions in creation order (0, 1, …)
    chat_store.set_folder_color("beta", "blue")
    chat_store.set_folder_color("zeta", "red")

    entries = chat_store.list_folder_registry()
    assert [e["name"] for e in entries] == ["beta", "zeta", "alpha"]
    by_name = {e["name"]: e for e in entries}
    # Positioned rows first (beta=0, zeta=1), unregistered alphabetical after
    assert by_name["beta"] == {"name": "beta", "color": "blue", "position": 0, "chat_count": 0}
    assert by_name["zeta"] == {"name": "zeta", "color": "red", "position": 1, "chat_count": 2}
    assert by_name["alpha"] == {"name": "alpha", "color": "gray", "position": None, "chat_count": 1}


def test_set_folder_color_validates(store: SqliteStore) -> None:
    """Only the 8 named palette tokens are accepted; names are normalized."""
    chat_store = ChatStore(store)

    with pytest.raises(ChatError):
        chat_store.set_folder_color("x", "#ff0000")
    with pytest.raises(ChatError):
        chat_store.set_folder_color("x", "crimson")
    with pytest.raises(ChatError):
        chat_store.set_folder_color("   ", "red")
    with pytest.raises(ChatError):
        chat_store.set_folder_color("a/b", "red")

    entry = chat_store.set_folder_color("  padded  ", "teal")
    assert entry["name"] == "padded"
    assert entry["color"] == "teal"


def test_rename_folder_moves_all_statuses(store: SqliteStore) -> None:
    """Rename rewrites every member chat — deleted ones included — and
    re-keys the registry row, keeping its color."""
    chat_store = ChatStore(store)

    live = chat_store.create_chat(title="Live", meta={"folder": "old"})
    doomed = chat_store.create_chat(title="Doomed", meta={"folder": "old"})
    chat_store.soft_delete_chat(doomed["id"])
    chat_store.set_folder_color("old", "orange")

    entry = chat_store.rename_folder("old", "new")
    assert entry["name"] == "new"
    assert entry["color"] == "orange"
    assert entry["chat_count"] == 1  # counts exclude the deleted chat

    assert chat_store.get_chat(live["id"])["meta"]["folder"] == "new"
    # The deleted chat moved too: undelete can never resurrect "old"
    assert chat_store.get_chat(doomed["id"])["meta"]["folder"] == "new"
    assert "old" not in {e["name"] for e in chat_store.list_folder_registry()}


def test_rename_folder_merges_into_existing(store: SqliteStore) -> None:
    """Renaming onto an existing folder merges; the target keeps its color."""
    chat_store = ChatStore(store)

    chat_store.create_chat(title="A", meta={"folder": "src"})
    chat_store.create_chat(title="B", meta={"folder": "dst"})
    chat_store.set_folder_color("src", "red")
    chat_store.set_folder_color("dst", "green")

    entry = chat_store.rename_folder("src", "dst")
    assert entry == {"name": "dst", "color": "green", "position": 1, "chat_count": 2}
    names = {e["name"] for e in chat_store.list_folder_registry()}
    assert names == {"dst"}


def test_rename_folder_unknown_and_noop(store: SqliteStore) -> None:
    """Unknown source raises UnknownFolderError; same-name rename is a no-op."""
    from omniagentos.chats import UnknownFolderError

    chat_store = ChatStore(store)
    with pytest.raises(UnknownFolderError):
        chat_store.rename_folder("ghost", "anything")

    chat_store.create_chat(title="A", meta={"folder": "same"})
    entry = chat_store.rename_folder("same", "same")
    assert entry["name"] == "same"
    assert entry["chat_count"] == 1


def test_delete_folder_falls_back_to_inbox(store: SqliteStore) -> None:
    """Delete clears meta.folder on members (deleted chats too) and drops
    the registry row; unknown names raise UnknownFolderError."""
    from omniagentos.chats import UnknownFolderError

    chat_store = ChatStore(store)

    live = chat_store.create_chat(title="Live", meta={"folder": "bin", "keep": "me"})
    doomed = chat_store.create_chat(title="Doomed", meta={"folder": "bin"})
    chat_store.soft_delete_chat(doomed["id"])
    chat_store.set_folder_color("bin", "yellow")

    moved = chat_store.delete_folder("bin")
    assert moved == 2

    live_meta = chat_store.get_chat(live["id"])["meta"]
    assert "folder" not in live_meta
    assert live_meta["keep"] == "me"  # unrelated meta survives
    assert "folder" not in chat_store.get_chat(doomed["id"])["meta"]
    assert chat_store.list_folder_registry() == []

    with pytest.raises(UnknownFolderError):
        chat_store.delete_folder("bin")
