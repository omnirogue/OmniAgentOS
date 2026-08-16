"""harness_audit_guard.py — record parsing and retest-guard keyword matching.

The fixture record below is a small, self-contained analogue of the real
seeded `devtasks/harness-audits/RECORD.md` (same format, same field names) so
these tests stay correct even if the real record grows or is reworded — plus
one integration-shaped test against the real shipped record to prove the
guard actually finds a known D-reject in it.

Several tests here are regression tests for a cross-lineage review round
(2026-08-14, ocrit) that found: a malformed bullet silently overwriting the
PREVIOUS entry's fields instead of closing it; two silent-loss paths (odd
fence count, bullet-variant lines the entry regex missed) both green-lighting
a "not been recorded yet" answer for content that was actually present; a
ranking/banner design that let D-reject outrank a higher-scoring non-reject
and let a single stopword-class token fire "ALREADY REJECTED"; and a few
exit-code gaps (zero-keyword query, non-UTF-8 record).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ops import harness_audit_guard as guard

FIXTURE_RECORD = """\
# Fixture disposition record

## Batch: fixture-harness (2026-01-01)

### A-integrate

- **Sandbox fallback made explicit**
  - disposition: A-integrate
  - north_star: C-03
  - evidence: wrap_command silently returns unchanged argv on fallback.
  - source: dual-lineage convergent

### D-reject

- **Cordis port for omni**
  - disposition: D-reject
  - north_star: NEW:runtime-composability
  - evidence: bus-factor-1 upstream; omni's restart cost is seconds so the
    paper's cost case does not transfer.
  - source: A-plugins/opus, H-external/opus

- **In-memory job registry as a production job system**
  - disposition: D-reject
  - north_star: C-14, C-17
  - evidence: no cross-process durability; omni's workqueue already has
    fencing and TTL reclaim.
  - source: D-orch/grok

## Batch: fixture-harness-v2 (2026-02-01)

- **Config-only live reload**
  - disposition: C-watch
  - north_star: NEW:runtime-composability
  - evidence: not load-bearing today; revisit if the restart-to-reload gap
    becomes measured.
  - source: A-plugins/opus
"""


@pytest.fixture
def fixture_path(tmp_path: Path) -> Path:
    path = tmp_path / "RECORD.md"
    path.write_text(FIXTURE_RECORD)
    return path


def _entries(text: str) -> list[guard.Entry]:
    warnings: list[str] = []
    entries = guard.parse_record(text, warnings)
    assert warnings == [], f"unexpected parse warnings: {warnings}"
    return entries


def _parse(text: str) -> tuple[list[guard.Entry], list[str]]:
    warnings: list[str] = []
    entries = guard.parse_record(text, warnings)
    return entries, warnings


# ------------------------------------------------------------------- parsing --
def test_parse_record_extracts_all_entries() -> None:
    names = [e.mechanism for e in _entries(FIXTURE_RECORD)]
    assert names == [
        "Sandbox fallback made explicit",
        "Cordis port for omni",
        "In-memory job registry as a production job system",
        "Config-only live reload",
    ]


def test_parse_record_captures_fields_and_batch() -> None:
    entries = _entries(FIXTURE_RECORD)
    cordis = next(e for e in entries if e.mechanism == "Cordis port for omni")
    assert cordis.disposition == "D-reject"
    assert cordis.batch == "fixture-harness (2026-01-01)"
    assert cordis.fields["north_star"] == "NEW:runtime-composability"
    assert "bus-factor-1" in cordis.fields["evidence"]


def test_parse_record_second_batch_has_its_own_heading() -> None:
    entries = _entries(FIXTURE_RECORD)
    reload_entry = next(e for e in entries if e.mechanism == "Config-only live reload")
    assert reload_entry.batch == "fixture-harness-v2 (2026-02-01)"
    assert reload_entry.disposition == "C-watch"


def test_parse_record_ignores_prose_and_blank_lines() -> None:
    text = "some prose\n\n" + FIXTURE_RECORD + "\ntrailing prose, not an entry\n"
    assert len(_entries(text)) == 4


def test_parse_record_joins_wrapped_evidence_lines() -> None:
    text = """\
## Batch: wrap-test (2026-01-01)

- **Wrapped evidence mechanism**
  - disposition: D-reject
  - evidence: first line of the evidence
    continues here on a second indented line
    and a third one too.
  - source: some-lane
"""
    entries = _entries(text)
    assert len(entries) == 1
    evidence = entries[0].fields["evidence"]
    assert "first line of the evidence" in evidence
    assert "continues here on a second indented line" in evidence
    assert "third one too" in evidence


def test_parse_record_horizontal_rule_does_not_pollute_field() -> None:
    """A literal '---' right after a field line must not become part of that
    field's value (RECORD.md uses --- as a section separator)."""
    text = """\
## Batch: rule-test (2026-01-01)

- **Some mechanism**
  - disposition: D-reject
  - source: some-lane
---

## Batch: next-batch (2026-01-02)

- **Other mechanism**
  - disposition: C-watch
"""
    entries, warnings = _parse(text)
    assert warnings == []
    first = next(e for e in entries if e.mechanism == "Some mechanism")
    assert "---" not in first.fields["source"]
    assert first.fields["source"] == "some-lane"


def test_parse_record_skips_fenced_format_spec_example() -> None:
    text = """\
## Format

```
- **<mechanism name>**
  - disposition: A-integrate | B-prototype | C-watch | D-reject
```

## Batch: real-batch (2026-01-01)

- **A real mechanism**
  - disposition: C-watch
"""
    entries = _entries(text)
    assert len(entries) == 1
    assert entries[0].mechanism == "A real mechanism"


# ---------------------------------------------------- regression: BLOCKER 1/2 --
def test_malformed_bullet_does_not_mutate_neighbour() -> None:
    """A bullet the entry regex cannot fully parse must never let its fields
    land on the PREVIOUS entry — that previous D-reject must survive intact,
    and the malformed line must surface as a warning, not silent absorption.
    """
    text = """\
## Batch: b1 (2026-01-01)

- **Default-allow pre-execute waterfall**
  - disposition: D-reject
  - north_star: C-03
  - evidence: adopting it would delete omni's deny-by-default classify_shell.
  - source: C-toolsec/grok

- **Default-allow waterfall, revisited (bold span left unclosed by mistake
  - disposition: A-integrate
  - evidence: this must never overwrite the entry above.
"""
    entries, warnings = _parse(text)
    first = next(e for e in entries if e.mechanism == "Default-allow pre-execute waterfall")
    assert first.disposition == "D-reject"
    assert "deny-by-default" in first.fields["evidence"]
    matches = guard.search(entries, "default-allow pre-execute waterfall")
    assert any(m.entry.disposition == "D-reject" for m in matches)
    assert any(w.startswith("unparsed bullet") for w in warnings)


def test_malformed_bullet_variant_from_real_supersedes_shape() -> None:
    """The exact shape RECORD.md's own header invites for a supersession
    entry — a bullet with a trailing parenthetical note — must parse as its
    OWN entry, not corrupt the entry above it."""
    text = """\
## Batch: b1 (2026-01-01)

- **Default-allow pre-execute waterfall**
  - disposition: D-reject
  - evidence: original rejection.
  - source: C-toolsec/grok

- **Default-allow waterfall, revisited** (supersedes the 2026-01-01 entry)
  - disposition: A-integrate
  - evidence: brand new claim from the next audit.
  - source: some-lane
"""
    entries, warnings = _parse(text)
    assert warnings == []
    assert len(entries) == 2
    first, second = entries
    assert first.mechanism == "Default-allow pre-execute waterfall"
    assert first.disposition == "D-reject"
    assert second.mechanism == "Default-allow waterfall, revisited"
    assert second.disposition == "A-integrate"


@pytest.mark.parametrize(
    ("label", "bullet"),
    [
        ("asterisk bullet", "* **Cordis port for omni**"),
        ("trailing period", "- **Cordis port for omni**."),
        ("trailing note", "- **Cordis port for omni** (superseded)"),
        ("indented under section", "  - **Cordis port for omni**"),
    ],
)
def test_parse_record_common_bullet_variants_are_not_dropped(label: str, bullet: str) -> None:
    variant = FIXTURE_RECORD.replace("- **Cordis port for omni**", bullet)
    entries = _entries(variant)
    names = [e.mechanism for e in entries]
    assert any("Cordis" in n for n in names), f"{label}: entry lost entirely"


def test_odd_fence_count_warns_and_still_parses_content_after_it() -> None:
    """An unclosed ``` fence must not silently discard every entry after it —
    it must emit a warning and still surface the real content."""
    text = (
        "# Record\n\n"
        "Example (note the unclosed fence a future author leaves behind):\n\n"
        "```sh\n"
        'python scripts/ops/harness_audit_guard.py "x"\n\n' + FIXTURE_RECORD
    )
    entries, warnings = _parse(text)
    assert len(entries) == 4, "content after the unclosed fence must not be dropped"
    assert any("unbalanced fenced code block" in w for w in warnings)


def test_strict_flag_exits_nonzero_on_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "RECORD.md"
    bad.write_text(
        "## Batch: b1 (2026-01-01)\n\n"
        "- **Good entry**\n"
        "  - disposition: C-watch\n\n"
        "- **Bad entry with unclosed bold span\n"
        "  - disposition: A-integrate\n"
    )
    code_lenient = guard.main(["good entry", "--record", str(bad)])
    assert code_lenient == 0
    code_strict = guard.main(["good entry", "--record", str(bad), "--strict"])
    assert code_strict == 2
    assert "WARNING" in capsys.readouterr().err


# ------------------------------------------------------------------ matching --
def test_search_finds_exact_mechanism_by_keyword() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "cordis dependency injection")
    assert matches
    assert matches[0].entry.mechanism == "Cordis port for omni"


def test_search_is_case_insensitive_and_substring_based() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "SANDBOX FALLBACK")
    assert [m.entry.mechanism for m in matches] == ["Sandbox fallback made explicit"]


def test_search_no_match_returns_empty() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "quantum teleportation compiler")
    assert matches == []


def test_search_ranks_d_reject_first_only_on_tied_score() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "runtime-composability")
    dispositions = [m.entry.disposition for m in matches]
    assert dispositions[0] == "D-reject"  # Cordis port sorts ahead on a tie
    assert "C-watch" in dispositions


def test_search_scores_more_keyword_hits_higher() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "in-memory job registry production")
    assert matches[0].entry.mechanism == "In-memory job registry as a production job system"
    assert matches[0].score >= 3


def test_search_score_is_the_primary_sort_key_not_disposition() -> None:
    """Regression for MAJOR 4: a HIGHER-scoring non-reject must outrank a
    LOWER-scoring D-reject — is_reject is a tie-break, never the primary
    key."""
    text = """\
## Batch: rank-test (2026-01-01)

- **Weakly related reject**
  - disposition: D-reject
  - evidence: mentions widget only once in passing.
  - source: some-lane

- **Strongly matching prototype about widgets**
  - disposition: B-prototype
  - evidence: widget widget everywhere, a direct and thorough widget match.
  - source: some-lane
"""
    entries = _entries(text)
    matches = guard.search(entries, "widget")
    assert matches[0].entry.mechanism == "Strongly matching prototype about widgets"
    assert matches[0].score > matches[1].score


def test_weak_evidence_only_hit_does_not_trigger_already_rejected_banner() -> None:
    """Regression for MAJOR 4: a query that hits a D-reject's EVIDENCE text
    with only a single weak keyword (score 1, no mechanism-name hit) must not
    fire the alarm banner — the match still appears in the listing, but the
    banner needs real signal (score >= 2 or a name hit)."""
    text = """\
## Batch: banner-test (2026-01-01)

- **Totally unrelated mechanism**
  - disposition: D-reject
  - evidence: this reject only happens to mention gadget once in passing.
  - source: some-lane
"""
    entries = _entries(text)
    matches = guard.search(entries, "gadget")
    assert matches and matches[0].entry.disposition == "D-reject"
    assert matches[0].score == 1
    assert matches[0].name_hit is False
    report = guard.render_report("gadget", matches, Path("<mem>"))
    assert "ALREADY REJECTED" not in report


def test_weak_stopword_only_query_yields_zero_matches_not_a_stray_banner() -> None:
    """A query made entirely of stopword-class connector words must not
    surface any match at all against the fixture record, let alone a
    banner."""
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "this mechanism is not yet implemented")
    report = guard.render_report("this mechanism is not yet implemented", matches, Path("<mem>"))
    assert "ALREADY REJECTED" not in report


def test_name_hit_alone_triggers_banner_even_at_low_raw_score() -> None:
    entries = _entries(FIXTURE_RECORD)
    matches = guard.search(entries, "cordis")
    report = guard.render_report("cordis", matches, Path("<mem>"))
    assert "ALREADY REJECTED" in report
    assert "triggered by keywords: cordis" in report


def test_quoted_short_token_is_forced_through_min_length_filter() -> None:
    keywords = guard._keywords('"AI" safety framework')
    assert "ai" in keywords
    keywords_unquoted = guard._keywords("ai safety framework")
    assert "ai" not in keywords_unquoted  # too short unless explicitly quoted


# --------------------------------------------------------------------- CLI ---
def test_main_exits_zero_with_matches(
    fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = guard.main(["cordis in-process DI", "--record", str(fixture_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "ALREADY REJECTED" in out
    assert "Cordis port for omni" in out


def test_main_exits_zero_with_no_matches(
    fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = guard.main(["completely novel mechanism xyz", "--record", str(fixture_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no prior adjudicated disposition found" in out


def test_main_prints_parse_census_line(
    fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = guard.main(["cordis", "--record", str(fixture_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "entries=4" in out
    assert "unparsed_bullets=0" in out


def test_main_json_payload_shape(fixture_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = guard.main(["job registry", "--record", str(fixture_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "job registry"
    assert payload["matches"]
    assert payload["matches"][0]["disposition"] == "D-reject"
    assert "census" in payload
    assert payload["warnings"] == []


def test_main_missing_record_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope" / "RECORD.md"
    code = guard.main(["anything", "--record", str(missing)])
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_main_empty_query_exits_nonzero(fixture_path: Path) -> None:
    code = guard.main(["", "--record", str(fixture_path)])
    assert code == 2


def test_main_zero_keyword_query_exits_nonzero(
    fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for F6(a): a query that reduces to zero searchable keywords
    (all stopwords, or all sub-minimum-length tokens) must not silently
    report a clean "not recorded" green result — it cannot search at all."""
    code = guard.main(["the and of a", "--record", str(fixture_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "no searchable keywords" in err


def test_main_non_utf8_record_exits_two_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for F6(b): UnicodeDecodeError is a ValueError, not an
    OSError — it must be caught and reported as a read failure, not crash."""
    binpath = tmp_path / "bad.md"
    binpath.write_bytes(b"\xff\xfe- **widget**\n  - disposition: D-reject\n")
    code = guard.main(["widget", "--record", str(binpath)])
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


# ------------------------------------------------------ real shipped record --
def test_real_record_finds_known_d_reject() -> None:
    """Integration check: the actual seeded RECORD.md parses cleanly (zero
    parse warnings) and the guard finds the Cordis-port D-reject as its top
    match — proof the guard works against the real file, not just the
    fixture's simplified shape.

    ``devtasks/harness-audits/RECORD.md`` is a historical, estate-specific
    audit log (45 real disposition entries from a past seed batch). This
    checkout's ``devtasks/`` was trimmed to the generic benchmark corpus for
    the public release, so that lane directory never shipped here -- same
    category as the harness-adapter/DAL-settlement patch lanes that are
    estate-bound for the same reason. Skip explicitly rather than fail on a
    FileNotFoundError that names nothing actionable in this checkout.
    """
    if not guard.DEFAULT_RECORD.is_file():
        pytest.skip(
            f"{guard.DEFAULT_RECORD} not present in this checkout (devtasks/ ships "
            "only the benchmark corpus here) -- this integration check reads a "
            "historical, estate-specific audit record not carried into this release"
        )
    entries, warnings = _parse(guard.DEFAULT_RECORD.read_text())
    assert warnings == []
    assert len(entries) == 45  # 5 A + 13 B + 9 C + 18 D from the seed batch
    matches = guard.search(entries, "cordis in-process DI runtime")
    assert matches
    assert matches[0].entry.disposition == "D-reject"
    assert matches[0].entry.mechanism.startswith("Cordis port")
    assert "bus-factor-1" in matches[0].entry.fields["evidence"]
