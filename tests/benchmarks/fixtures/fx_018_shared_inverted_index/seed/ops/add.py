"""
Add document operation.
"""

from __future__ import annotations

from store import Store, StoreError


def add_document(store: Store, doc_id: str, text: str) -> None:
    if doc_id in store.docs:
        raise StoreError(f"Document {doc_id} already exists")
    # BUG: Directly mutates docs without updating index or revision!
    store.docs[doc_id] = text
