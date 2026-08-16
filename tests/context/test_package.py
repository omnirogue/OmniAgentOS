"""ContextPackage pure domain model — hash invariants and validation."""

from __future__ import annotations

import pytest

from omniagentos.context.package import (
    ContextExclusion,
    ContextItem,
    ContextPackage,
    ContextPackageError,
)


def _item(
    item_id: str = "a",
    *,
    source: str = "src/a.py",
    version: str = "v1",
    freshness: str = "2026-07-01T00:00:00Z",
    kind: str = "file",
    **kwargs: object,
) -> ContextItem:
    return ContextItem(
        id=item_id,
        source=source,
        version=version,
        freshness=freshness,
        kind=kind,
        **kwargs,  # type: ignore[arg-type]
    )


def _pkg(
    *items: ContextItem,
    excluded: tuple[ContextExclusion, ...] = (),
    package_id: str = "pkg-1",
    ack_token: str | None = None,
    **kwargs: object,
) -> ContextPackage:
    return ContextPackage(
        package_id=package_id,
        items=items,
        excluded=excluded,
        ack_token=ack_token,
        **kwargs,  # type: ignore[arg-type]
    )


def test_content_hash_is_stable_under_item_reordering() -> None:
    a = _item("a", version="1")
    b = _item("b", version="2")
    p1 = _pkg(a, b)
    p2 = _pkg(b, a)
    assert p1.content_hash() == p2.content_hash()
    # Also stable under exclusion reordering.
    e1 = ContextExclusion(id="x", reason="noise")
    e2 = ContextExclusion(id="y", reason="pii")
    assert _pkg(a, excluded=(e1, e2)).content_hash() == _pkg(a, excluded=(e2, e1)).content_hash()


def test_content_hash_changes_when_any_item_version_changes() -> None:
    base = _pkg(_item("a", version="sha-aaa"))
    changed = _pkg(_item("a", version="sha-bbb"))
    assert base.content_hash() != changed.content_hash()
    # Other fields also change the digest.
    kind_changed = _pkg(_item("a", version="sha-aaa", kind="doc"))
    assert base.content_hash() != kind_changed.content_hash()


def test_the_exclusion_list_is_part_of_the_hashed_manifest() -> None:
    item = _item("a")
    bare = _pkg(item)
    with_excl = _pkg(item, excluded=(ContextExclusion(id="skip", reason="out of scope"),))
    assert bare.content_hash() != with_excl.content_hash()

    reason_changed = _pkg(item, excluded=(ContextExclusion(id="skip", reason="different reason"),))
    assert with_excl.content_hash() != reason_changed.content_hash()

    id_changed = _pkg(item, excluded=(ContextExclusion(id="other", reason="out of scope"),))
    assert with_excl.content_hash() != id_changed.content_hash()


def test_ack_token_does_not_change_content_hash() -> None:
    item = _item("a")
    without = _pkg(item, ack_token=None)
    with_ack = _pkg(item, ack_token="ack-xyz-post-dispatch")
    assert without.content_hash() == with_ack.content_hash()
    # Different ack values still share identity.
    other_ack = _pkg(item, ack_token="ack-other")
    assert without.content_hash() == other_ack.content_hash()


def test_duplicate_item_ids_raise() -> None:
    with pytest.raises(ContextPackageError, match="unique"):
        _pkg(_item("dup"), _item("dup", source="other.py"))


def test_duplicate_exclusion_ids_raise() -> None:
    with pytest.raises(ContextPackageError, match="unique"):
        _pkg(
            _item("a"),
            excluded=(
                ContextExclusion(id="e1", reason="one"),
                ContextExclusion(id="e1", reason="two"),
            ),
        )


def test_empty_id_source_version_raise() -> None:
    with pytest.raises(ContextPackageError, match="id"):
        ContextItem(
            id="",
            source="s",
            version="v",
            freshness="2026-01-01T00:00:00Z",
            kind="file",
        )
    with pytest.raises(ContextPackageError, match="source"):
        ContextItem(
            id="x",
            source="",
            version="v",
            freshness="2026-01-01T00:00:00Z",
            kind="file",
        )
    with pytest.raises(ContextPackageError, match="version"):
        ContextItem(
            id="x",
            source="s",
            version="",
            freshness="2026-01-01T00:00:00Z",
            kind="file",
        )
    with pytest.raises(ContextPackageError, match="package_id"):
        ContextPackage(package_id="", items=())
    with pytest.raises(ContextPackageError, match="size_tokens"):
        ContextItem(
            id="x",
            source="s",
            version="v",
            freshness="2026-01-01T00:00:00Z",
            kind="file",
            size_tokens=-1,
        )


def test_item_lookup_and_total_tokens() -> None:
    a = _item("a", size_tokens=10)
    b = _item("b", size_tokens=25)
    pkg = _pkg(a, b)
    assert pkg.item_ids() == frozenset({"a", "b"})
    assert pkg.item("a") is a
    assert pkg.item("missing") is None
    assert pkg.total_tokens() == 35
    d = pkg.to_dict()
    assert set(d.keys()) == {
        "package_id",
        "items",
        "excluded",
        "task_id",
        "ack_token",
        "created_at",
        "metadata",
    }
