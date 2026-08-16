"""PKG-REQUEST-SUBTASKS: worker-initiated fan-out via an ATTEMPT-BOUND,
coordinator-validated ``subtasks_request.<attempt>.json``.

A worker that discovers its task decomposes writes a per-attempt request beside
its workbook and ends the attempt; the coordinator validates it (six guards in a
fixed order) and, on success, registers the children through the SAME atomic
split machinery the timeout path uses. Registration is DURABLE-FIRST: only after
``split_task`` commits are the advisory events emitted and the attempt
terminalized ``"split"`` (review skipped). Every failure mode — absent /
stale-from-prior-attempt / malformed / any guard denial / registration fault /
feature off — degrades to today's review flow.

These tests drive ``_quality_gate`` directly (the seam where detection lives),
reusing the shared scheduler fakes/harness, so the completion path is exercised
end-to-end without spinning the whole DAG.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.dispatch.gate import text_hits_risk
from omniagentos.swarm.scheduler import (
    _RunState,
    build_worker_brief,
    subtasks_request_protocol_lines,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler
from tests.swarm.test_spawn import make_request, make_spawner


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Route every DB touch through the harness and redirect the DEFAULT workbook
    root into tmp_path (the fake spawner exposes no ``workbook_dir``, so the
    scheduler falls back to the default root — which we patch)."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))
    var_root = tmp_path / "var" / "swarm"
    monkeypatch.setattr("omniagentos.swarm.spawn.default_swarm_var_root", lambda: var_root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_REQUEST = {
    "reason": "splits cleanly into two disjoint files",
    "subtasks": [
        {
            "title": "part one",
            "description": "implement the one half",
            "owned_paths": ["src/a/one.txt"],
            "est_minutes": 12,
        },
        {
            "title": "part two",
            "description": "implement the other half",
            "owned_paths": ["src/a/two.txt"],
        },
    ],
}


def _parent_swarm_json(task_key: str = "a", **overrides: object) -> dict[str, object]:
    swarm_json: dict[str, object] = {
        "task_key": task_key,
        "owned_paths": ["src/a"],
        "risk_class": "review",
        "plan_version": 7,
        "plan_hash": "deadbeefcafe",
        "acceptance": "ok",
        "verify_command": "true",
    }
    swarm_json.update(overrides)
    return swarm_json


def _make(tmp_path: Path, **scheduler_kwargs: object):
    h = make_harness(
        tmp_path,
        [{"id": "a", "owned_paths": ["src/a"]}],
        max_concurrency=1,
        budget=scheduler_kwargs.pop("budget", None),  # type: ignore[arg-type]
    )
    scheduler = make_scheduler(h, **scheduler_kwargs)
    return h, scheduler


def _workbook_dir(h, task_id: str) -> Path:
    from omniagentos.swarm.spawn import swarm_workbook_path

    return swarm_workbook_path(h.run_id, task_id).parent


def _write_file(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(body, encoding="utf-8")


def _drive_gate(
    h,
    scheduler,
    *,
    payload: object | None = None,
    extra_files: dict[str, object] | None = None,
    swarm_json: dict[str, object] | None = None,
    task_key: str = "a",
    session: dict[str, object] | None = None,
):
    """Open a fresh attempt, write THIS attempt's request file (and any extra
    files), then run the quality gate exactly as settlement would."""
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
    task_row = h.task_row(task_key)
    task_id = str(task_row["id"])
    swarm_json = swarm_json if swarm_json is not None else _parent_swarm_json(task_key)
    attempt = h.dal.open_attempt(
        h.run_id, task_id, provider="claude", model="sonnet", tier="simple", account_id="acct", source="test")
    attempt_id = str(attempt["id"])
    workbook_dir = _workbook_dir(h, task_id)
    if payload is not None:
        _write_file(workbook_dir / f"subtasks_request.{attempt_id}.json", payload)
    for name, body in (extra_files or {}).items():
        _write_file(workbook_dir / name, body)
    session = session or {"id": f"sess-{task_key}", "output_text": "what remains", "cost_usd": 0.0}
    result = scheduler._quality_gate(
        state,
        dict(task_row),
        attempt_id,
        dict(attempt),
        session,
        dict(swarm_json),
        "simple",
        "snap0",
    )
    return result, task_id, attempt_id


def _child_swarm_jsons(h) -> list[dict[str, object]]:
    out = []
    for task in h.dal.tasks_for_run(h.run_id):
        sj = h.dal.get_swarm_json(task["id"]) or {}
        if str(sj.get("task_key") or "").startswith("a."):
            out.append(sj)
    return out


# ---------------------------------------------------------------------------
# The happy path: register children, skip review, emit both events
# ---------------------------------------------------------------------------


class TestValidRequest:
    def test_valid_request_registers_children_and_skips_review(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)

            # Parent terminalizes as a split; its review is SKIPPED entirely.
            assert result == "split"
            assert h.reviewer.calls == []
            assert h.dal.list_attempts(task_id)[-1]["end_reason"] == "split"

            # Both events fire; the reused TASK_SPLIT is tagged source=worker_request.
            requested = h.emitter.of("subtasks_requested")
            assert requested and requested[0]["task_id"] == task_id
            assert requested[0]["count"] == 2
            assert requested[0]["reason"] == VALID_REQUEST["reason"][:200]
            splits = h.emitter.of("task_split")
            assert splits and splits[0]["source"] == "worker_request"
            assert len(splits[0]["subtask_ids"]) == 2

            # Children inherit the parent's risk class and carry the POST-bump
            # plan lineage (the split itself bumps the plan — see
            # TestSplitPlanLineage); NORMALIZED paths registered.
            plan = json.loads(str(h.dal.get_run(h.run_id)["plan_json"]))
            children = _child_swarm_jsons(h)
            assert {str(sj["task_key"]) for sj in children} == {"a.1", "a.2"}
            for sj in children:
                assert sj["risk_class"] == "review"
                assert sj["plan_version"] == plan["version"]
                assert sj["plan_hash"] == plan["plan_hash"]
                assert sj["split_from"] == task_id
            owned = sorted(str(p) for sj in children for p in sj["owned_paths"])  # type: ignore[union-attr]
            assert owned == ["src/a/one.txt", "src/a/two.txt"]

            # Dependents (integration) were rewired onto the children atomically.
            child_ids = {
                str(t["id"])
                for t in h.dal.tasks_for_run(h.run_id)
                if str((h.dal.get_swarm_json(t["id"]) or {}).get("task_key") or "").startswith("a.")
            }
            integ_deps = {
                d["depends_on_task_id"]
                for d in h.dal.deps_for_run(h.run_id)
                if d["task_id"] == h.task_id("integration")
            }
            assert integ_deps == child_ids
            assert h.swarm_json_of("a").get("split") is True
        finally:
            h.close()

    def test_normalized_paths_are_registered(self, tmp_path: Path) -> None:
        """A messy-but-valid path (``src/./a//one.txt``) is normalized before
        registration, not stored raw."""
        h, scheduler = _make(tmp_path)
        payload = {
            "reason": "ok",
            "subtasks": [
                {"title": "one", "description": "d", "owned_paths": ["src/./a//one.txt"]},
                {"title": "two", "description": "d", "owned_paths": ["src/a/sub/../two.txt"]},
            ],
        }
        try:
            result, _, _ = _drive_gate(h, scheduler, payload=payload)
            assert result == "split"
            owned = sorted(
                str(p)
                for sj in _child_swarm_jsons(h)
                for p in sj["owned_paths"]  # type: ignore[union-attr]
            )
            assert owned == ["src/a/one.txt", "src/a/two.txt"]
        finally:
            h.close()


# ---------------------------------------------------------------------------
# Absent / stale-from-prior-attempt / feature off: today's flow
# ---------------------------------------------------------------------------


class TestAbsentStaleAndDisabled:
    def test_absent_request_reviews_as_today(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)  # no request file written
        try:
            result, task_id, _ = _drive_gate(h, scheduler)
            assert result == "done"
            assert [c["key"] for c in h.reviewer.calls] == ["a"]
            assert h.dal.list_attempts(task_id)[-1]["end_reason"] == "completed"
            assert h.emitter.of("subtasks_requested") == []
            assert h.emitter.of("subtasks_denied") == []
            assert h.emitter.of("task_split") == []
        finally:
            h.close()

    def test_stale_prior_attempt_file_is_swept_not_consumed(self, tmp_path: Path) -> None:
        """B1: a prior attempt wrote a request, then died on a path that never
        settled. A successor with NO request of its own must NOT split — the
        stale file is ignored and swept."""
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h, scheduler, extra_files={"subtasks_request.priorATT.json": VALID_REQUEST}
            )
            assert result == "done"
            assert [c["key"] for c in h.reviewer.calls] == ["a"]
            assert h.emitter.of("subtasks_requested") == []
            assert h.emitter.of("task_split") == []
            assert _child_swarm_jsons(h) == []
            assert not (_workbook_dir(h, task_id) / "subtasks_request.priorATT.json").exists()
        finally:
            h.close()

    def test_current_file_consumed_stale_swept(self, tmp_path: Path) -> None:
        """The current attempt's file registers; a co-resident stale file is swept."""
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h,
                scheduler,
                payload=VALID_REQUEST,
                extra_files={"subtasks_request.priorATT.json": VALID_REQUEST},
            )
            assert result == "split"
            assert not (_workbook_dir(h, task_id) / "subtasks_request.priorATT.json").exists()
        finally:
            h.close()

    def test_feature_off_ignores_a_valid_file(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path, worker_subtask_requests=False)
        try:
            result, _, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            assert result == "done"
            assert [c["key"] for c in h.reviewer.calls] == ["a"]
            assert h.emitter.of("subtasks_requested") == []
            assert h.emitter.of("subtasks_denied") == []
            assert h.emitter.of("task_split") == []
            assert _child_swarm_jsons(h) == []
        finally:
            h.close()


# ---------------------------------------------------------------------------
# Malformed + every guard: deny with the right reason, then review proceeds
# ---------------------------------------------------------------------------


class TestDenials:
    def _assert_denied(self, h, result, task_id, reason: str, *, key: str = "a") -> None:
        assert result == "done"  # fell through to the normal review + confirm
        assert [c["key"] for c in h.reviewer.calls] == [key]
        denied = h.emitter.of("subtasks_denied")
        assert denied and denied[0]["reason"] == reason
        assert denied[0]["task_id"] == task_id
        assert h.emitter.of("subtasks_requested") == []
        assert h.emitter.of("task_split") == []
        assert _child_swarm_jsons(h) == []
        # A denial writes NO durable marker (B2c).
        assert "subtasks_requested" not in h.swarm_json_of("a")

    def test_malformed_json_denied(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(h, scheduler, payload="{ this is not valid json ]")
            self._assert_denied(h, result, task_id, "malformed")
        finally:
            h.close()

    def test_bad_shape_denied_as_malformed(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h, scheduler, payload={"reason": "x", "subtasks": [{"title": "only a title"}]}
            )
            self._assert_denied(h, result, task_id, "malformed")
        finally:
            h.close()

    def test_count_too_few_denied(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h,
                scheduler,
                payload={
                    "reason": "x",
                    "subtasks": [
                        {"title": "solo", "description": "d", "owned_paths": ["src/a/one.txt"]}
                    ],
                },
            )
            self._assert_denied(h, result, task_id, "count")
        finally:
            h.close()

    def test_count_too_many_denied(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h,
                scheduler,
                payload={
                    "reason": "x",
                    "subtasks": [
                        {"title": f"p{i}", "description": "d", "owned_paths": [f"src/a/{i}.txt"]}
                        for i in range(5)
                    ],
                },
            )
            self._assert_denied(h, result, task_id, "count")
        finally:
            h.close()

    def test_depth_denied_for_already_split_task_key(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h,
                scheduler,
                payload=VALID_REQUEST,
                swarm_json=_parent_swarm_json(task_key="parser.1"),
            )
            self._assert_denied(h, result, task_id, "depth", key="parser.1")
        finally:
            h.close()

    def test_repeat_denied_when_split_already_recorded(self, tmp_path: Path) -> None:
        """B2a: the once-guard reads split_task's durable ``split`` flag, not a
        separate best-effort marker."""
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h, scheduler, payload=VALID_REQUEST, swarm_json=_parent_swarm_json(split=True)
            )
            self._assert_denied(h, result, task_id, "repeat")
        finally:
            h.close()

    def test_risky_subtask_denied(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            result, task_id, _ = _drive_gate(
                h,
                scheduler,
                payload={
                    "reason": "x",
                    "subtasks": [
                        {
                            "title": "clean",
                            "description": "add a helper",
                            "owned_paths": ["src/a/one.txt"],
                        },
                        {
                            "title": "cleanup",
                            "description": "truncate the production users table",
                            "owned_paths": ["src/a/two.txt"],
                        },
                    ],
                },
            )
            self._assert_denied(h, result, task_id, "risk")
        finally:
            h.close()

    def test_risk_check_unavailable_fails_closed(self, tmp_path: Path) -> None:
        """B4: an all-invalid configured risk_patterns must DENY (fail closed),
        never silently match nothing."""
        h, scheduler = _make(tmp_path)

        import omniagentos.dispatch.gate as gate

        original = gate.load_config
        gate.load_config = lambda path=None: {"risk_patterns": ["(unclosed"]}
        try:
            result, task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            self._assert_denied(h, result, task_id, "risk_check_unavailable")
        finally:
            gate.load_config = original
            h.close()

    def test_budget_exhausted_denied(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path, budget=1.0)
        h.dal.add_cost(h.run_id, 5.0)
        try:
            result, task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            self._assert_denied(h, result, task_id, "budget")
        finally:
            h.close()


class TestOwnershipGuardB3:
    """B3: proper path-component normalization + containment + disjointness."""

    def _deny_paths(self, tmp_path: Path, subtasks, *, parent=("src/a",)) -> None:
        h, scheduler = _make(tmp_path)
        payload = {"reason": "x", "subtasks": subtasks}
        try:
            result, _, _ = _drive_gate(
                h,
                scheduler,
                payload=payload,
                swarm_json=_parent_swarm_json(owned_paths=list(parent)),
            )
            assert result == "done"
            denied = h.emitter.of("subtasks_denied")
            assert denied and denied[0]["reason"] == "ownership"
            assert h.emitter.of("task_split") == []
            assert _child_swarm_jsons(h) == []
        finally:
            h.close()

    def test_dotfile_prefix_escape_denied(self, tmp_path: Path) -> None:
        # parent .github/workflows must NOT accept child github/workflows/pwn.yml
        self._deny_paths(
            tmp_path,
            [
                {"title": "in", "description": "d", "owned_paths": [".github/workflows/ci.yml"]},
                {"title": "out", "description": "d", "owned_paths": ["github/workflows/pwn.yml"]},
            ],
            parent=(".github/workflows",),
        )

    def test_parent_traversal_denied(self, tmp_path: Path) -> None:
        self._deny_paths(
            tmp_path,
            [
                {"title": "in", "description": "d", "owned_paths": ["src/a/one.txt"]},
                {"title": "out", "description": "d", "owned_paths": ["../escape.txt"]},
            ],
        )

    def test_absolute_path_denied(self, tmp_path: Path) -> None:
        self._deny_paths(
            tmp_path,
            [
                {"title": "in", "description": "d", "owned_paths": ["src/a/one.txt"]},
                {"title": "abs", "description": "d", "owned_paths": ["/etc/passwd"]},
            ],
        )

    def test_duplicate_children_denied(self, tmp_path: Path) -> None:
        self._deny_paths(
            tmp_path,
            [
                {"title": "a", "description": "d", "owned_paths": ["src/a/dup.txt"]},
                {"title": "b", "description": "d", "owned_paths": ["src/a/dup.txt"]},
            ],
        )

    def test_ancestor_descendant_overlap_denied(self, tmp_path: Path) -> None:
        # src/a + src/a/sub overlap (ancestor/descendant), both inside parent src
        self._deny_paths(
            tmp_path,
            [
                {"title": "a", "description": "d", "owned_paths": ["src/a"]},
                {"title": "b", "description": "d", "owned_paths": ["src/a/sub"]},
            ],
            parent=("src",),
        )

    def test_empty_ownership_denied(self, tmp_path: Path) -> None:
        self._deny_paths(
            tmp_path,
            [
                {"title": "in", "description": "d", "owned_paths": ["src/a/one.txt"]},
                {"title": "empty", "description": "d", "owned_paths": []},
            ],
        )


# ---------------------------------------------------------------------------
# B2: registration fault is transactional — no marker, no split, review runs
# ---------------------------------------------------------------------------


class TestRegistrationFault:
    def test_split_task_fault_falls_through_to_review(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)

        def _boom(*_a, **_k):
            raise RuntimeError("split transaction failed")

        scheduler._dal.split_task = _boom  # type: ignore[method-assign]
        try:
            result, task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            # Durable state unchanged → normal review ran and confirmed.
            assert result == "done"
            assert [c["key"] for c in h.reviewer.calls] == ["a"]
            assert h.dal.list_attempts(task_id)[-1]["end_reason"] == "completed"
            # No advisory events fired; parent never marked split.
            assert h.emitter.of("subtasks_requested") == []
            assert h.emitter.of("task_split") == []
            assert h.swarm_json_of("a").get("split") is not True
        finally:
            h.close()


# ---------------------------------------------------------------------------
# B4: text_hits_risk strict-mode obfuscation resistance + fail-closed compile
# ---------------------------------------------------------------------------


class TestRiskScanStrict:
    def test_zero_width_split_denied(self) -> None:
        assert text_hits_risk("de​lete all users", config={}, strict=True) is True

    def test_full_width_variant_denied(self) -> None:
        assert text_hits_risk("ＤＥＬＥＴＥ ＡＬＬ ＵＳＥＲＳ", config={}, strict=True) is True

    def test_newline_split_keyword_denied(self) -> None:
        assert text_hits_risk("del\nete all users", config={}, strict=True) is True

    def test_clean_text_passes(self) -> None:
        assert text_hits_risk("add a small helper function", config={}, strict=True) is False

    def test_invalid_regex_raises_in_strict_mode(self) -> None:
        import re

        with pytest.raises(re.error):
            text_hits_risk("truncate all", config={"risk_patterns": ["(unclosed"]}, strict=True)

    def test_lenient_mode_unchanged_and_never_raises(self) -> None:
        # Lenient path drops bad patterns silently (lane-gate behavior preserved).
        assert text_hits_risk("truncate all", config={"risk_patterns": ["(unclosed"]}) is False
        assert text_hits_risk("truncate the users table", config={}) is True


# ---------------------------------------------------------------------------
# B5: relay/continuation prompts still carry the protocol with the successor path
# ---------------------------------------------------------------------------


class TestRelayPromptPersistence:
    def test_timeout_relay_prompt_keeps_protocol_and_successor_path(self, tmp_path: Path) -> None:
        spawner, supervisor, _, swarm_dal, sessions_dal, _ = make_spawner(tmp_path)
        swarm_dal.attempts["task1"] = [
            {
                "id": "swa_prior",
                "seq": 0,
                "session_id": "ses_prior",
                "provider": "claude",
                "ended_at": "2026-07-23T11:00:00Z",
                "end_reason": "timeout",
            },
            {"id": "swa_new", "seq": 1, "session_id": None, "ended_at": None},
        ]
        sessions_dal.sessions["ses_prior"] = {
            "id": "ses_prior",
            "todos_json": "[]",
            "files_json": "[]",
        }
        request_path = "/x/var/swarm/swr1/task1/subtasks_request.swa_new.json"
        spawner.spawn(make_request(attempt_id="swa_new", subtasks_request_path=request_path))
        prompt = supervisor.calls[0]["prompt"]
        # It IS the relay (continuation) prompt, and it STILL carries the section.
        assert "taking over from a colleague" in prompt
        assert "worker-initiated fan-out" in prompt
        assert request_path in prompt


# ---------------------------------------------------------------------------
# M6: the timeout-split TASK_SPLIT payload stays byte-identical (no "source")
# ---------------------------------------------------------------------------


class TestTimeoutSplitPayloadParity:
    def test_timeout_split_emits_no_source_key(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a"]}], max_concurrency=1)
        specs = [
            {"title": "one", "description": "d", "owned_paths": ["src/a/one.txt"]},
            {"title": "two", "description": "d", "owned_paths": ["src/a/two.txt"]},
        ]
        scheduler = make_scheduler(h, splitter=lambda task, sj: specs)
        try:
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            result = scheduler._split_task(state, dict(h.task_row("a")), _parent_swarm_json())
            assert result == "split"
            splits = h.emitter.of("task_split")
            assert len(splits) == 1
            assert set(splits[0].keys()) == {"task_id", "subtask_ids", "rewired_dependents"}
            assert "source" not in splits[0]
        finally:
            h.close()


# ---------------------------------------------------------------------------
# The worker brief carries the protocol section + the exact request path
# ---------------------------------------------------------------------------


class TestWorkerBrief:
    def test_section_and_exact_path_present_when_threaded(self) -> None:
        swarm_json = {
            "plan_version": 3,
            "plan_hash": "abcdef123456ff00",
            "owned_paths": ["src/a.py"],
            "acceptance": "ok",
            "verify_command": "true",
        }
        path = "/tmp/var/swarm/run7/taskA/subtasks_request.swa_9.json"
        brief = build_worker_brief({}, {"title": "T", "description": "D"}, swarm_json, {}, path)
        assert "worker-initiated fan-out" in brief
        assert path in brief
        assert "2-4 INDEPENDENT" in brief
        # Single source of truth: the section is exactly the shared helper's lines.
        assert "\n".join(subtasks_request_protocol_lines(path)) in brief

    def test_section_absent_when_not_threaded(self) -> None:
        swarm_json = {"owned_paths": ["src/a.py"]}
        brief = build_worker_brief({}, {"title": "T", "description": "D"}, swarm_json, {})
        assert "subtasks_request" not in brief
        assert "worker-initiated fan-out" not in brief


# ---------------------------------------------------------------------------
# Redteam addendum (b): split children carry the POST-bump plan lineage
# ---------------------------------------------------------------------------
#
# ``_provision_split`` stamped each child with the plan_version/plan_hash it
# read off the PARENT's swarm_json, then bumped ``plan.version`` afterwards and
# never wrote the new lineage back into the children. Every split therefore
# minted cards claiming a plan revision that no longer existed.


def _plan_of(h) -> dict[str, object]:
    run = h.dal.get_run(h.run_id)
    assert run is not None
    return json.loads(str(run["plan_json"] or "{}"))


class TestSplitPlanLineage:
    def test_children_carry_the_post_bump_plan_version_and_hash(self, tmp_path: Path) -> None:
        h, scheduler = _make(tmp_path)
        try:
            before = _plan_of(h)
            assert before["version"] == 1

            result, _task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            assert result == "split"

            after = _plan_of(h)
            assert after["version"] == 2
            assert after["plan_hash"] != before.get("plan_hash")
            children = _child_swarm_jsons(h)
            assert len(children) == 2
            for sj in children:
                assert sj["plan_version"] == after["version"]
                assert sj["plan_hash"] == after["plan_hash"]
        finally:
            h.close()

    def test_a_failed_split_transaction_leaves_the_plan_unbumped(self, tmp_path: Path) -> None:
        """Adversarial ORDERING: stamping the children with the post-bump
        lineage must not move the bump AHEAD of the split. If the split
        transaction dies, the plan must still be the pre-split plan — otherwise
        the run advertises a revision whose children were never registered."""
        h, scheduler = _make(tmp_path)

        def _boom(*_a: object, **_k: object) -> dict[str, object]:
            raise RuntimeError("split transaction failed")

        scheduler._dal.split_task = _boom  # type: ignore[method-assign]
        try:
            before = _plan_of(h)

            result, _task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)

            assert result == "done"  # fell through to the normal review
            assert _plan_of(h) == before
            assert _child_swarm_jsons(h) == []
        finally:
            h.close()

    def test_children_keep_the_parent_lineage_when_the_plan_cannot_be_rebuilt(
        self, tmp_path: Path
    ) -> None:
        """Adversarial STATE: the derived plan no longer holds the parent spec,
        so there is no bump to inherit. The children must fall back to the
        parent's lineage — never to a null/blank stamp — and the projection must
        be left exactly as it was."""
        h, scheduler = _make(tmp_path)
        try:
            plan = _plan_of(h)
            plan["tasks"] = [t for t in plan["tasks"] if t["id"] != "a"]  # type: ignore[index,union-attr]
            h.dal.set_plan(h.run_id, plan)
            before = _plan_of(h)

            result, _task_id, _ = _drive_gate(h, scheduler, payload=VALID_REQUEST)
            assert result == "split"

            assert _plan_of(h) == before  # no bump happened, none was written
            children = _child_swarm_jsons(h)
            assert len(children) == 2
            for sj in children:
                assert sj["plan_version"] == 7
                assert sj["plan_hash"] == "deadbeefcafe"
        finally:
            h.close()
