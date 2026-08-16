"""
Add document operation.
"""

from __future__ import annotations

from store import Store, StoreError, apply_change


def add_document(store: Store, doc_id: str, text: str) -> None:
    if doc_id in store.docs:
        raise StoreError(f"Document {doc_id} already exists")
    apply_change(store, doc_id, text)
