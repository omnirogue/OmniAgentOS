"""
Inverted index store module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class StoreError(ValueError):
    """Raised when a store operation is invalid."""

    pass


@dataclass
class Store:
    docs: dict[str, str]  # doc_id -> text
    index: dict[str, set[str]]  # term -> doc ids containing it
    revision: int


def new_store() -> Store:
    return Store(docs={}, index={}, revision=0)


def terms_of(text: str) -> set[str]:
    # lowercase, split on non-alphanumeric, drop tokens shorter than 2 characters
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return {token for token in tokens if len(token) >= 2}


def rebuild_index(store: Store) -> dict[str, set[str]]:
    # brute force, from docs alone
    new_idx: dict[str, set[str]] = {}
    for doc_id, text in store.docs.items():
        for term in terms_of(text):
            new_idx.setdefault(term, set()).add(doc_id)
    return new_idx


def lookup(store: Store, term: str) -> tuple[str, ...]:
    # sorted doc ids for a term
    if term not in store.index:
        return ()
    return tuple(sorted(store.index[term]))


def apply_change(store: Store, doc_id: str, new_text: str | None) -> None:
    """
    Applies a change incrementally to the store and its index.
    """
    if new_text is None:
        # Deletion
        if doc_id not in store.docs:
            raise StoreError(f"Document {doc_id} does not exist")
        old_text = store.docs[doc_id]
        for term in terms_of(old_text):
            if term in store.index:
                store.index[term].discard(doc_id)
                if not store.index[term]:
                    del store.index[term]
        del store.docs[doc_id]
        store.revision += 1
    else:
        # Insertion or Update
        if not new_text.strip():
            raise StoreError("Document text cannot be empty or whitespace-only")

        if doc_id in store.docs:
            # Update (Rewrite)
            old_text = store.docs[doc_id]
            old_terms = terms_of(old_text)
            new_terms = terms_of(new_text)

            for term in old_terms - new_terms:
                if term in store.index:
                    store.index[term].discard(doc_id)
                    if not store.index[term]:
                        del store.index[term]

            for term in new_terms - old_terms:
                store.index.setdefault(term, set()).add(doc_id)

            store.docs[doc_id] = new_text
            store.revision += 1
        else:
            # Insert
            new_terms = terms_of(new_text)
            for term in new_terms:
                store.index.setdefault(term, set()).add(doc_id)
            store.docs[doc_id] = new_text
            store.revision += 1
