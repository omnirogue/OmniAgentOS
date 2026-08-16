"""Candidate collection + prompt.md policy parsing (selection inputs)."""

from __future__ import annotations

from pathlib import Path

TODO_FIXTURE = """\
# Some plan

| Pkg | What | Owner | Status |
|---|---|---|---|
| A5 | Headless stale sweep in supervisor | GPT-5.6 | ⬜ TODO (Phase 1 tail) |
| WP2 | limit_state.py | Fable | ✅ MERGED (450 tests green) |
| — | Follow-ups queued: worktree live drill; golden-suite scripting | — | ⬜ QUEUED |
| C2 | Metrics panels | Opus 4.8 | ✅ MERGED + DEPLOYED |
"""

REPORT_FIXTURE = """\
# Nightly report 2026-07-24

## What I mined

- transcripts, 36h

## Deferred

- Add a skill for the repeated release-note workflow.
- Consolidate the two overlapping git-hook scripts.

## Proposed permission additions

- `Bash(gh pr view:*)` — observed 4 denials.
"""

PLAYBOOK_FIXTURE = """\
# Playbook

### Routing hints

| a | b |
|---|---|

### Improvement opportunities (Fable)

- **Bump default effort toward `high`.** Evidence attached.
- Nudge `large` plan concurrency above 5.

_Advisory only._
"""


def test_parse_todo_candidates_only_open_rows(executor):
    candidates = executor.parse_todo_candidates(TODO_FIXTURE)
    assert len(candidates) == 2
    assert candidates[0].id.startswith("todo-a5")
    assert "Headless stale sweep" in candidates[0].title
    assert candidates[0].source == "devtasks/SWARM-EXECUTION-TODO.md"
    assert candidates[0].raw_line  # kept verbatim for the ✅ flip
    assert "Follow-ups queued" in candidates[1].title


def test_parse_report_candidates_deferred_and_proposed_sections(executor):
    candidates = executor.parse_report_candidates(REPORT_FIXTURE, "2026-07-24.md")
    texts = [c.text for c in candidates]
    assert len(candidates) == 3  # 2 deferred + 1 proposed; mining bullets excluded
    assert any("release-note workflow" in t for t in texts)
    assert any("gh pr view" in t for t in texts)
    assert all(c.source == "curator-report:2026-07-24.md" for c in candidates)
    assert not any("transcripts, 36h" in t for t in texts)


def test_parse_playbook_candidates_tail_section(executor):
    candidates = executor.parse_playbook_candidates(PLAYBOOK_FIXTURE)
    assert len(candidates) == 2
    assert "Bump default effort" in candidates[0].title
    assert candidates[0].source == "vault/swarm/playbook.md"


def test_collect_candidates_uses_latest_three_reports(executor, sandbox):
    sandbox["todo"].parent.mkdir(parents=True, exist_ok=True)
    sandbox["todo"].write_text(TODO_FIXTURE, encoding="utf-8")
    sandbox["reports"].mkdir(parents=True, exist_ok=True)
    for day in ("2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"):
        sandbox["reports"].joinpath(f"{day}.md").write_text(
            f"## Deferred\n\n- item from {day}\n", encoding="utf-8"
        )
    sandbox["playbook"].parent.mkdir(parents=True, exist_ok=True)
    sandbox["playbook"].write_text(PLAYBOOK_FIXTURE, encoding="utf-8")

    candidates = executor.collect_candidates()
    report_texts = [c.text for c in candidates if c.source.startswith("curator-report:")]
    assert len(report_texts) == 3
    assert not any("2026-07-20" in t for t in report_texts)  # oldest dropped
    assert sum(1 for c in candidates if c.source == "vault/swarm/playbook.md") == 2
    assert sum(1 for c in candidates if c.id.startswith("todo-")) == 2


def test_collect_candidates_survives_missing_sources(executor, sandbox):
    assert executor.collect_candidates() == []


# --- prompt.md policy block (Addendum 2) -----------------------------------


def test_policy_and_criteria_load_from_real_prompt_file(executor):
    prompt_path = Path(executor.__file__).parent / "prompt.md"
    text = prompt_path.read_text(encoding="utf-8")
    policy, problem = executor.parse_policy(text)
    assert problem is None
    assert policy.max_items == 3
    assert policy.auto_merge_max_files == 6
    assert policy.merge_deadline_hour == 5
    # prompt deny_list is unioned with the immutable code layer
    assert set(executor.CODE_DENY_PATTERNS) <= set(policy.deny_list)
    assert "credential" in policy.deny_list
    # the criteria text itself rides into the selection prompt
    built = executor.build_selection_prompt(text, [], 3)
    assert "STRICT selection criteria" in built
    assert "risk_class none" in built


def test_malformed_policy_block_falls_back_to_code_defaults(executor):
    policy, problem = executor.parse_policy("```yaml\npolicy: [broken\n```\n")
    assert problem is not None
    assert policy.max_items == executor.HARD_MAX_ITEMS
    assert policy.auto_merge_max_files == executor.DEFAULT_AUTO_MERGE_MAX_FILES
    assert policy.merge_deadline_hour == executor.DEFAULT_MERGE_DEADLINE_HOUR
    assert policy.deny_list == executor.CODE_DENY_PATTERNS

    policy2, problem2 = executor.parse_policy("no yaml block at all")
    assert problem2 is not None
    assert policy2.max_items == executor.HARD_MAX_ITEMS


def test_policy_max_items_is_hard_capped_in_code(executor):
    text = "```yaml\npolicy:\n  max_items: 10\n```\n"
    policy, _ = executor.parse_policy(text)
    assert policy.max_items == 10
    assert policy.effective_max_items == executor.HARD_MAX_ITEMS  # never above 3
