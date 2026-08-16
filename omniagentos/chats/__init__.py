"""M1: Chats, hidden companion tasks, and chat workspace management."""

from __future__ import annotations

from omniagentos.chats.store import (
    FOLDER_COLORS,
    BoardTaskConflictError,
    ChatError,
    ChatStore,
    UnknownBoardTaskError,
    UnknownFolderError,
    UnknownProjectError,
    to_dto,
    to_dto_list,
)

__all__ = [
    "FOLDER_COLORS",
    "BoardTaskConflictError",
    "ChatError",
    "ChatStore",
    "UnknownBoardTaskError",
    "UnknownFolderError",
    "UnknownProjectError",
    "to_dto",
    "to_dto_list",
]
