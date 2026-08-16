"""
Rewrite document operation.
"""

from __future__ import annotations

from store import Store, StoreError, apply_change


def rewrite_document(store: Store, doc_id: str, text: str) -> None:
    if doc_id not in store.docs:
        raise StoreError(f"Document {doc_id} does not exist")
    apply_change(store, doc_id, text)
