"""
Remove document operation.
"""

from __future__ import annotations

from store import Store, apply_change


def remove_document(store: Store, doc_id: str) -> None:
    apply_change(store, doc_id, None)
