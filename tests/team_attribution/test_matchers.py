from __future__ import annotations

from omniagentos.team.attribution import match_task_detailed


def _candidate(**fields: object) -> dict:
    candidate = {
        "kind": "commit",
        "ref": "abc123",
        "repo": "repo",
        "actor": "owner@example.com",
        "title": "ordinary change",
        "occurred_at": "2026-08-10T12:00:00Z",
        "files": [],
        "branch_hint": "main",
        "meta": {},
    }
    candidate.update(fields)
    return candidate


def test_explicit_ref_precedes_paths_and_actor_window(make_open_task) -> None:
    explicit = make_open_task(id="btk_explicit", ref="U3")
    path = make_open_task(id="btk_path", owned_paths=["owned.py"])
    window = make_open_task(id="btk_window", status="in_progress", owner_employee_id="emp_owner")
    result = match_task_detailed(
        _candidate(title="refs U3", files=["owned.py"], actor="owner"),
        [explicit, path, window],
        actor_employee_map={"owner": "emp_owner"},
    )
    assert result == ("btk_explicit", "explicit_ref")


def test_short_ref_requires_a_word_boundary(make_open_task) -> None:
    task = make_open_task(id="btk_u3", ref="U3")
    assert match_task_detailed(_candidate(title="fixes U33 bug"), [task]) == (None, None)
    assert match_task_detailed(_candidate(title="fixes U3 bug"), [task]) == (
        "btk_u3",
        "explicit_ref",
    )


def test_two_path_matches_are_unattributed(make_open_task) -> None:
    tasks = [
        make_open_task(id="btk_a", owned_paths=["shared.py"]),
        make_open_task(id="btk_b", owned_paths=["shared.py"]),
    ]
    assert match_task_detailed(_candidate(files=["shared.py"]), tasks) == (None, None)


def test_branch_name_contains_short_ref(make_open_task) -> None:
    # Branch matching binds ONLY to the candidate's own branch (meta.head_branch,
    # i.e. a PR's head) — never to branch_hint, which for commits is the repo's
    # CURRENT checkout branch and would bulk-attribute the whole window.
    task = make_open_task(id="btk_s5", ref="S5")
    pr = _candidate(kind="pr", meta={"head_branch": "feature/S5-thing"})
    assert match_task_detailed(pr, [task]) == ("btk_s5", "explicit_ref")


def test_commit_branch_hint_never_matches(make_open_task) -> None:
    # A checkout parked on fix/U3-resume must not attribute unrelated commits
    # to U3 (measured misattribution vector B1-b).
    task = make_open_task(id="btk_u3", ref="U3")
    parked = _candidate(title="chore(deps): bump pydantic", branch_hint="fix/U3-resume")
    assert match_task_detailed(parked, [task]) == (None, None)


def test_full_board_task_id_in_commit_title(make_open_task) -> None:
    task = make_open_task(id="btk_alpha_42")
    assert match_task_detailed(_candidate(title="finish btk_alpha_42"), [task]) == (
        "btk_alpha_42",
        "explicit_ref",
    )


def test_pr_body_contains_short_ref(make_open_task) -> None:
    task = make_open_task(id="btk_s5", ref="S5")
    candidate = _candidate(kind="pr", meta={"body": "refs S5", "head_branch": "feature"})
    assert match_task_detailed(candidate, [task]) == ("btk_s5", "explicit_ref")


def test_existing_pr_branch_link(make_open_task) -> None:
    task = make_open_task(id="btk_branch", branches=["feature/no-card-ref"])
    candidate = _candidate(
        kind="pr",
        branch_hint="feature/no-card-ref",
        meta={"head_branch": "feature/no-card-ref"},
    )
    assert match_task_detailed(candidate, [task]) == ("btk_branch", "existing_link")


def test_actor_window_matches_one_in_progress_card(make_open_task) -> None:
    task = make_open_task(
        id="btk_active",
        status="in_progress",
        owner_employee_id="emp_owner",
        updated_at="2026-08-10T23:30:00Z",
    )
    assert match_task_detailed(
        _candidate(actor="owner"), [task], actor_employee_map={"owner": "emp_owner"}
    ) == ("btk_active", "actor_window")


def test_explicit_ambiguity_does_not_fall_through(make_open_task) -> None:
    tasks = [
        make_open_task(id="btk_u3", ref="U3"),
        make_open_task(id="btk_s5", ref="S5", owned_paths=["only.py"]),
    ]
    assert match_task_detailed(
        _candidate(title="refs U3 and fixes S5", files=["only.py"]), tasks
    ) == (
        None,
        None,
    )


def test_short_refs_require_markers_and_delimited_branch_segments(make_open_task) -> None:
    s3 = make_open_task(id="btk_s3", ref="S3")
    u3 = make_open_task(id="btk_u3", ref="U3")
    s5 = make_open_task(id="btk_s5", ref="S5")

    assert match_task_detailed(_candidate(title="move uploads bucket to S3"), [s3]) == (
        None,
        None,
    )
    assert match_task_detailed(_candidate(title="security(U-S1): x"), [s3]) == (None, None)
    assert match_task_detailed(_candidate(title="refs U3"), [u3]) == (
        "btk_u3",
        "explicit_ref",
    )
    assert match_task_detailed(_candidate(title="closes S5"), [s5]) == (
        "btk_s5",
        "explicit_ref",
    )
    assert match_task_detailed(_candidate(branch_hint="feat/s3-uploads"), [s3]) == (
        None,
        None,
    )
    assert match_task_detailed(
        _candidate(kind="pr", meta={"head_branch": "fix/U3-resume"}), [u3]
    ) == (
        "btk_u3",
        "explicit_ref",
    )


def test_ambiguous_existing_link_does_not_fall_through_to_actor_window(make_open_task) -> None:
    tasks = [
        make_open_task(
            id="btk_active",
            status="in_progress",
            owner_employee_id="emp_owner",
            updated_at="2026-08-10T12:00:00Z",
            branches=["feature/shared"],
        ),
        make_open_task(id="btk_other", branches=["feature/shared"]),
    ]
    candidate = _candidate(
        kind="pr",
        actor="owner",
        meta={"head_branch": "feature/shared"},
    )
    assert match_task_detailed(candidate, tasks, actor_employee_map={"owner": "emp_owner"}) == (
        None,
        None,
    )


def test_ambiguous_owned_paths_does_not_fall_through_to_actor_window(make_open_task) -> None:
    tasks = [
        make_open_task(
            id="btk_active",
            status="in_progress",
            owner_employee_id="emp_owner",
            updated_at="2026-08-10T12:00:00Z",
            owned_paths=["shared/"],
        ),
        make_open_task(id="btk_other", owned_paths=["shared"]),
    ]
    assert match_task_detailed(
        _candidate(actor="owner", files=["shared/change.py"]),
        tasks,
        actor_employee_map={"owner": "emp_owner"},
    ) == (None, None)


def test_owned_directory_matches_normalized_descendant_only(make_open_task) -> None:
    task = make_open_task(id="btk_team", owned_paths=["./omniagentos/team/"])
    assert match_task_detailed(
        _candidate(files=["omniagentos/team/ingest.py"]), [task]
    ) == ("btk_team", "owned_paths")
    assert match_task_detailed(
        _candidate(files=["omniagentos/teamwork/ingest.py"]), [task]
    ) == (None, None)
