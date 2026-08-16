"""Skill router selection bounds."""

from __future__ import annotations

from omniagentos.skills.select import select_skills


def test_selects_by_domain_and_caps_at_8() -> None:
    reg = [
        {"name": f"skill-{i}", "version": "1", "domains": ["coding"], "status": "active"}
        for i in range(12)
    ]
    hits = select_skills(reg, domain="coding", max_skills=8)
    assert 1 <= len(hits) <= 8
    assert all(h.score > 0 for h in hits)


def test_tool_overlap_ranks_higher() -> None:
    reg = [
        {"name": "generic", "domains": ["coding"], "status": "active"},
        {
            "name": "shellish",
            "domains": ["coding"],
            "tools": ["bash", "shell"],
            "status": "active",
        },
    ]
    hits = select_skills(reg, domain="coding", allowed_tools=["shell"])
    assert hits[0].name == "shellish"


def test_min_skills_fills_with_zero_score_approved_not_deprecated() -> None:
    """min_skills must pad with zero-score ACTIVE skills when matches are few.

    Counterfeits this catches:
    - bare `pass` on the under-min branch (returns only the 1 match)
    - padding with deprecated/experimental (flattering "we filled" using junk)
    - padding with archived/quarantined/rejected/unknown/absent status
    - padding with a producerless status word (approved/canary): U-16 collapsed
      the selector onto the `skills.status` CHECK vocabulary, so a row spelled
      "approved" is now exactly as unknown as "rejected"
    - defaulting missing status to a favourable one then filling with it
    - inventing skill names not present in the registry
    - giving fillers a positive score so they look like real matches
    - returning min_skills copies of the same matched skill
    """
    reg = [
        {"name": "match-a", "version": "1", "domains": ["coding"], "status": "active"},
        {"name": "pad-b", "version": "1", "domains": ["writing"], "status": "active"},
        {"name": "pad-c", "version": "1", "domains": ["ops"], "status": "active"},
        {"name": "deprecated-d", "version": "1", "domains": ["ops"], "status": "deprecated"},
        {"name": "experimental-e", "version": "1", "domains": ["ops"], "status": "experimental"},
        {"name": "archived", "version": "1", "domains": ["ops"], "status": "archived"},
        {"name": "quarantined", "version": "1", "domains": ["ops"], "status": "quarantined"},
        {"name": "approved-nonvocab", "version": "1", "domains": ["ops"], "status": "approved"},
        {"name": "canary-nonvocab", "version": "1", "domains": ["ops"], "status": "canary"},
        {"name": "rejected", "version": "1", "domains": ["ops"], "status": "rejected"},
        {"name": "unknown-status", "version": "1", "domains": ["ops"], "status": "unknown"},
        {"name": "missing-status", "version": "1", "domains": ["ops"]},  # no status key
        {"name": "empty-status", "version": "1", "domains": ["ops"], "status": ""},
        {"name": "null-status", "version": "1", "domains": ["ops"], "status": None},
    ]
    hits = select_skills(reg, domain="coding", min_skills=3, max_skills=8)
    assert len(hits) == 3
    assert hits[0].name == "match-a"
    assert hits[0].score > 0
    pad = hits[1:]
    pad_names = {h.name for h in pad}
    assert pad_names == {"pad-b", "pad-c"}
    assert all(h.score == 0.0 for h in pad)
    assert all(h.reason == "min_skills_fill" for h in pad)
    # Unfavourable / unknown / absent statuses must never appear as fillers.
    forbidden = {
        "deprecated-d",
        "experimental-e",
        "archived",
        "quarantined",
        "approved-nonvocab",
        "canary-nonvocab",
        "rejected",
        "unknown-status",
        "missing-status",
        "empty-status",
        "null-status",
    }
    assert pad_names.isdisjoint(forbidden)
    assert {h.name for h in hits}.isdisjoint(forbidden)
    # Uniqueness: no duplicate padding of the matched skill.
    assert len({h.name for h in hits}) == 3
    # Only registry names.
    assert {h.name for h in hits} <= {row["name"] for row in reg}


def test_min_skills_does_not_fill_with_bad_status_when_only_junk_remains() -> None:
    """If remaining rows are non-fillable, under-select rather than promote junk."""
    reg = [
        {"name": "match", "version": "1", "domains": ["coding"], "status": "active"},
        {"name": "archived", "version": "1", "domains": ["ops"], "status": "archived"},
        {"name": "rejected", "version": "1", "domains": ["ops"], "status": "rejected"},
        {"name": "unknown-status", "version": "1", "domains": ["ops"], "status": "unknown"},
        {"name": "missing-status", "version": "1", "domains": ["ops"]},
    ]
    hits = select_skills(reg, domain="coding", min_skills=3, max_skills=8)
    assert len(hits) == 1
    assert hits[0].name == "match"
    assert {h.name for h in hits} == {"match"}


def test_min_skills_filler_names_are_unique() -> None:
    """Duplicate registry rows must not pad min_skills with the same name twice."""
    reg = [
        {"name": "match", "version": "1", "domains": ["coding"], "status": "active"},
        {"name": "aaa-pad", "version": "1", "domains": ["writing"], "status": "active"},
        {"name": "aaa-pad", "version": "1", "domains": ["ops"], "status": "active"},
        {"name": "bbb-pad", "version": "1", "domains": ["ops"], "status": "active"},
    ]
    hits = select_skills(reg, domain="coding", min_skills=3, max_skills=8)
    names = [h.name for h in hits]
    assert names[0] == "match"
    assert len(names) == 3
    assert len(set(names)) == 3
    assert set(names[1:]) == {"aaa-pad", "bbb-pad"}


def test_absent_or_unknown_status_not_selected_as_match() -> None:
    """Domain matches with absent/unknown/archived/rejected status are not selected."""
    reg = [
        {"name": "good", "version": "1", "domains": ["coding"], "status": "active"},
        {"name": "archived", "version": "1", "domains": ["coding"], "status": "archived"},
        {"name": "rejected", "version": "1", "domains": ["coding"], "status": "rejected"},
        {"name": "unknown-status", "version": "1", "domains": ["coding"], "status": "unknown"},
        {"name": "missing-status", "version": "1", "domains": ["coding"]},
    ]
    hits = select_skills(reg, domain="coding", max_skills=8)
    assert [h.name for h in hits] == ["good"]


def test_min_skills_exhausted_registry_returns_what_exists() -> None:
    """When the registry cannot satisfy min_skills, do not invent rows."""
    reg = [
        {"name": "only", "version": "1", "domains": ["coding"], "status": "active"},
    ]
    hits = select_skills(reg, domain="coding", min_skills=5, max_skills=8)
    assert len(hits) == 1
    assert hits[0].name == "only"


def test_min_skills_noop_when_enough_positive_matches() -> None:
    """Enough real matches must not be diluted with zero-score fillers."""
    reg = [
        {"name": f"s{i}", "version": "1", "domains": ["coding"], "status": "active"}
        for i in range(5)
    ] + [
        {"name": "unrelated", "version": "1", "domains": ["writing"], "status": "active"},
    ]
    hits = select_skills(reg, domain="coding", min_skills=3, max_skills=8)
    assert len(hits) == 5
    assert all(h.score > 0 for h in hits)
    assert "unrelated" not in {h.name for h in hits}
