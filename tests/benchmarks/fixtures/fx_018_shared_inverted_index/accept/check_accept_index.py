"""
FROZEN acceptance check for fx_018_shared_inverted_index

This file is copied into the workspace after the agent completes the task.
It verifies the incremental consistency of the inverted index under various
mutation sequences, correctness of error handling, and routing through apply_change.
"""

from __future__ import annotations

import copy

from ops.add import add_document
from ops.remove import remove_document
from ops.rewrite import rewrite_document
from store import Store, StoreError, lookup, new_store, rebuild_index, terms_of


def _assert_coherent(store: Store, expected_revision: int) -> None:
    assert store.index == rebuild_index(store)
    assert all(ids for ids in store.index.values()), "empty term set leaked"
    assert store.revision == expected_revision
    expected_terms = set()
    for text in store.docs.values():
        expected_terms |= terms_of(text)
    assert set(store.index) == expected_terms


def test_accept_empty_store() -> None:
    store = new_store()
    assert store.docs == {}
    assert store.index == {}
    assert store.revision == 0
    assert lookup(store, "hello") == ()


def test_accept_basic_operations() -> None:
    store = new_store()

    # Add first doc
    add_document(store, "doc1", "The quick brown fox jumps")
    assert store.revision == 1
    assert store.docs["doc1"] == "The quick brown fox jumps"
    assert store.index == rebuild_index(store)
    # Check that terms with length >= 2 are present, and no empty/leaked keys
    assert "" not in store.index
    assert "the" in store.index
    assert "quick" in store.index
    assert "fox" in store.index
    assert lookup(store, "fox") == ("doc1",)

    # Rewrite doc
    rewrite_document(store, "doc1", "The quick red fox")
    assert store.revision == 2
    assert "jumps" not in store.index  # removed term
    assert "brown" not in store.index  # removed term
    assert "red" in store.index  # added term
    assert store.index == rebuild_index(store)

    # Remove doc
    remove_document(store, "doc1")
    assert store.revision == 3
    assert store.docs == {}
    assert store.index == {}  # empty, no leaked keys with empty sets!


def test_accept_interleaved_sequence() -> None:
    """
    Runs an interleaved sequence of at least 12 operations and verifies
    incremental consistency at each step.
    """
    store = new_store()

    # 1. Add doc1
    add_document(store, "doc1", "apple banana cherry")
    _assert_coherent(store, 1)

    # 2. Add doc2
    add_document(store, "doc2", "banana cherry date")
    _assert_coherent(store, 2)

    # 3. Add doc3
    add_document(store, "doc3", "cherry date elderberry")
    _assert_coherent(store, 3)

    # 4. Rewrite doc2 (drops 'banana', keeps 'cherry', 'date', adds 'fig')
    rewrite_document(store, "doc2", "cherry date fig")
    _assert_coherent(store, 4)
    assert store.index["banana"] == {"doc1"}

    # 5. Rewrite doc1 (drops 'banana')
    rewrite_document(store, "doc1", "apple cherry")
    _assert_coherent(store, 5)

    # 6. Remove doc3
    remove_document(store, "doc3")
    _assert_coherent(store, 6)

    # 7. Add doc4
    add_document(store, "doc4", "grape honey grape")
    _assert_coherent(store, 7)

    # 8. Add doc5
    add_document(store, "doc5", "honey melon")
    _assert_coherent(store, 8)

    # 9. Rewrite doc4 (drops 'grape', 'honey', adds 'kiwi')
    rewrite_document(store, "doc4", "kiwi")
    _assert_coherent(store, 9)
    # 'honey' was in doc4 and doc5. Now only in doc5.
    assert store.index["honey"] == {"doc5"}

    # 10. Remove doc5
    remove_document(store, "doc5")
    _assert_coherent(store, 10)

    # 11. Add doc6
    add_document(store, "doc6", "lemon kiwi apple")
    _assert_coherent(store, 11)

    # 12. Rewrite doc1 (drops 'apple', 'cherry', adds 'lemon')
    rewrite_document(store, "doc1", "lemon")
    _assert_coherent(store, 12)
    # apple was in doc1 and doc6. Now only in doc6.
    assert store.index["apple"] == {"doc6"}

    # Check lookup returns sorted doc ids
    assert lookup(store, "lemon") == ("doc1", "doc6")
    assert lookup(store, "kiwi") == ("doc4", "doc6")
    assert lookup(store, "nonexistent") == ()


def test_accept_failures_leave_identical() -> None:
    """
    Asserts that every rejected operation leaves docs, index, and revision
    byte-identical.
    """
    store = new_store()
    add_document(store, "doc1", "hello world")

    # Create an identical deep copy to verify rollback/no-change
    snapshot = copy.deepcopy(store)

    # 1. Duplicate Add
    try:
        add_document(store, "doc1", "new text")
    except StoreError:
        pass
    else:
        raise AssertionError("Expected StoreError on duplicate add_document")
    assert store == snapshot

    # 2. Add empty text
    try:
        add_document(store, "doc2", "   ")
    except StoreError:
        pass
    else:
        raise AssertionError("Expected StoreError on empty add_document")
    assert store == snapshot

    # 3. Unknown Rewrite
    try:
        rewrite_document(store, "doc_unknown", "some text")
    except StoreError:
        pass
    else:
        raise AssertionError("Expected StoreError on unknown rewrite_document")
    assert store == snapshot

    # 4. Rewrite with empty text
    try:
        rewrite_document(store, "doc1", "")
    except StoreError:
        pass
    else:
        raise AssertionError("Expected StoreError on rewrite with empty text")
    assert store == snapshot

    # 5. Unknown Remove
    try:
        remove_document(store, "doc_unknown")
    except StoreError:
        pass
    else:
        raise AssertionError("Expected StoreError on unknown remove_document")
    assert store == snapshot


def test_accept_routing() -> None:
    """
    Verifies that all mutations are strictly routed through store.apply_change.
    """
    import ops.add as add_module
    import ops.remove as remove_module
    import ops.rewrite as rewrite_module
    import store as store_module

    original_apply_change = store_module.apply_change
    call_count = 0
    passed_args = []

    def mock_apply_change(s: Store, doc_id: str, new_text: str | None) -> None:
        nonlocal call_count
        call_count += 1
        passed_args.append((doc_id, new_text))
        original_apply_change(s, doc_id, new_text)

    store_module.apply_change = mock_apply_change

    # Also patch in the imported namespaces of ops modules if they imported the function directly
    orig_add = getattr(add_module, "apply_change", None)
    if orig_add is not None:
        add_module.apply_change = mock_apply_change

    orig_remove = getattr(remove_module, "apply_change", None)
    if orig_remove is not None:
        remove_module.apply_change = mock_apply_change

    orig_rewrite = getattr(rewrite_module, "apply_change", None)
    if orig_rewrite is not None:
        rewrite_module.apply_change = mock_apply_change

    try:
        store = new_store()

        # Test routing for add
        add_document(store, "doc1", "apple orange")
        assert call_count == 1
        assert passed_args[-1] == ("doc1", "apple orange")

        # Test routing for rewrite
        rewrite_document(store, "doc1", "banana grape")
        assert call_count == 2
        assert passed_args[-1] == ("doc1", "banana grape")

        # Test routing for remove
        remove_document(store, "doc1")
        assert call_count == 3
        assert passed_args[-1] == ("doc1", None)

    finally:
        store_module.apply_change = original_apply_change
        if orig_add is not None:
            add_module.apply_change = orig_add
        if orig_remove is not None:
            remove_module.apply_change = orig_remove
        if orig_rewrite is not None:
            rewrite_module.apply_change = orig_rewrite
