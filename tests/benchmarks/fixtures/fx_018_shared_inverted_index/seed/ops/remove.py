"""
Remove document operation.
"""

from __future__ import annotations

from store import Store, StoreError


def remove_document(store: Store, doc_id: str) -> None:
    if doc_id not in store.docs:
        raise StoreError(f"Document {doc_id} does not exist")
    # BUG: Directly mutates docs without updating index or revision!
    del store.docs[doc_id]
