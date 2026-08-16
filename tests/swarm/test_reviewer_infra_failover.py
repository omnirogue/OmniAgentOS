"""Reviewer infrastructure failures must be named, and must fail over.

Every fixture in this module models a reviewer transcript under
``var/runtime/logs/swarm-review-*/`` for a classic failure mode:
dozens of swarm attempts in one hour ending ``blocked`` with the single
opaque detail

    reviewer infrastructure failed twice: reviewer returned no parseable verdict

while the underlying cause — the reviewer CLI being logged out — was sitting
in the transcript and was discarded by ``CrossLineageSwarmReviewer.review``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, HarnessType, ResultStatus
from omniagentos.swarm.scheduler import (
    CrossLineageSwarmReviewer,
    _extract_verdict_payload,
    _high_blast_radius,
    _reviewer_model_id,
)

# --------------------------------------------------------------------------
# Representative reviewer outputs (do not "clean up" — byte-for-byte fixtures)
# --------------------------------------------------------------------------

# The shape of a grok.log transcript under var/runtime/logs/swarm-review-*/
# when the CLI is logged out. The grok CLI exits
# NON-zero, so CliAdapter.run returns status=ERROR with this text in `error`.
GROK_NOT_SIGNED_IN = (
    '{"type":"error","message":"Not signed in. To authenticate without a browser, run:'
    "\\n  grok login --device-code\\n\\nAlternatively, set the XAI_API_KEY environment "
    'variable or run `grok login` on a machine with a browser."}'
)

# The claude.log shape under var/runtime/logs/swarm-review-*/
# Note `"subtype":"success"` and `"is_error":true` in the SAME envelope: the
# claude adapter's _parse raises, CliAdapter.run converts that to status=ERROR.
CLAUDE_OAUTH_EXPIRED = (
    '{"is_error":true,"duration_api_ms":0,"num_turns":1,"stop_reason":"stop_sequence",'
    '"session_id":"b70b6d6e-70d8-42fa-a375-cdf17c994684","total_cost_usd":0,'
    '"terminal_reason":"api_error","subtype":"success",'
    '"result":"Failed to authenticate: OAuth session expired and could not be refreshed",'
    '"type":"result","duration_ms":64}'
)


def _usage() -> AgentUsage:
    return AgentUsage(input_tokens=0, output_tokens=0, cost_usd=0.0, wall_ms=1)


class _DeadHarness:
    """What CliAdapter.run actually returns for a logged-out CLI."""

    def __init__(self, error: str) -> None:
        self.error = error
        self.calls = 0

    def run(self, agent_input: Any) -> Any:
        del agent_input
        self.calls += 1
        return AgentResult(
            status=ResultStatus.ERROR,
            usage=_usage(),
            log_path="var/runtime/logs/swarm-review-fixture/grok.log",
            error=self.error,
        )


class _HealthyHarness:
    def __init__(self, verdict: str = "confirm") -> None:
        self.verdict = verdict
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, agent_input: Any) -> Any:
        self.calls += 1
        self.prompts.append(agent_input.prompt)
        payload = {"verdict": self.verdict, "feedback": "real reviewer feedback"}
        return AgentResult(
            status=ResultStatus.OK,
            output_text=json.dumps(payload),
            output_json=payload,
            usage=_usage(),
        )


class _DriftingHarness:
    """Runs fine (status OK) but answers in prose until corrected."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0
        self.prompts: list[str] = []

    def run(self, agent_input: Any) -> Any:
        self.prompts.append(agent_input.prompt)
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return AgentResult(status=ResultStatus.OK, output_text=text, usage=_usage())


def _review(
    reviewer: CrossLineageSwarmReviewer,
    workspace: Path,
    *,
    formation_reviewer: str = "grok",
    implementer: str = "gemini-3.6-flash",
    surface: str = "standard",
) -> Any:
    return reviewer.review(
        task={"id": "btk_00000000000000000000", "title": "trim contract"},
        swarm_json={
            "implementer_model": implementer,
            "formation_reviewer": formation_reviewer,
            "review_surface": surface,
            "acceptance": "tests/simprobe/test_trim.py exists",
        },
        session={"id": "ses_00000000000000000000", "project_dir": str(workspace)},
        verify_output="",
        flags=[],
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# --------------------------------------------------------------------------
# 1. The real adapters really do produce these AgentResults
# --------------------------------------------------------------------------


def test_real_grok_stdout_is_not_a_verdict_envelope() -> None:
    """Proof the fixture is a genuine failure, not a straw man."""
    from omniagentos.adapters.grok import GrokAdapter

    with pytest.raises(ValueError, match="text string|error envelope"):
        GrokAdapter()._parse(GROK_NOT_SIGNED_IN)


def test_real_claude_stdout_raises_with_the_auth_message() -> None:
    from omniagentos.adapters.claude import ClaudeAdapter

    with pytest.raises(ValueError, match="OAuth session expired"):
        ClaudeAdapter()._parse(CLAUDE_OAUTH_EXPIRED)


# --------------------------------------------------------------------------
# 2. The reviewer must NAME the infrastructure failure
# --------------------------------------------------------------------------


def test_logged_out_harness_is_reported_as_an_auth_failure_not_a_bad_verdict(
    workspace: Path,
) -> None:
    """`reviewer returned no parseable verdict` hid a logged-out CLI for a full
    day of blocked attempts. The adapter's own status/error is the truth."""
    dead = _DeadHarness("Not signed in. To authenticate without a browser, run:\n  grok login")
    outcome = _review(CrossLineageSwarmReviewer(adapter=dead), workspace)

    assert outcome.verdict == "error"  # fail-closed preserved
    assert "Not signed in" in outcome.feedback
    assert "no parseable verdict" not in outcome.feedback


def test_infra_failure_feedback_names_the_transcript(workspace: Path) -> None:
    dead = _DeadHarness("Not signed in.")
    outcome = _review(CrossLineageSwarmReviewer(adapter=dead), workspace)
    assert "swarm-review-fixture/grok.log" in outcome.feedback


# --------------------------------------------------------------------------
# 3. A dead primary harness must fail over to a live cross-lineage reviewer
# --------------------------------------------------------------------------


def test_dead_primary_harness_fails_over_to_a_live_lineage(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live killer: formations pin `reviewer: grok`, the grok CLI is logged
    out, and the retry re-ran the SAME dead CLI. Every review blocked."""
    dead_grok = _DeadHarness(GROK_NOT_SIGNED_IN)
    healthy_codex = _HealthyHarness("confirm")
    by_harness = {
        HarnessType.CLI_GROK: dead_grok,
        HarnessType.CLI_CODEX: healthy_codex,
        HarnessType.CLI_CLAUDE: _DeadHarness(CLAUDE_OAUTH_EXPIRED),
    }
    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        lambda harness: by_harness[harness],
    )

    outcome = _review(CrossLineageSwarmReviewer(), workspace)

    assert dead_grok.calls == 1, "the down harness must be tried, then abandoned"
    assert healthy_codex.calls == 1
    assert outcome.verdict == "confirm"
    assert outcome.reviewer == "cli-codex", "the reviewer reported must be the one that ran"


def test_failover_never_reuses_the_implementer_lineage(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-lineage independence is not negotiable, even under failover."""
    seen: list[HarnessType] = []

    def fake_resolve(harness: HarnessType) -> Any:
        seen.append(harness)
        return _DeadHarness("down")

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", fake_resolve)

    outcome = _review(
        CrossLineageSwarmReviewer(), workspace, formation_reviewer="grok", implementer="codex"
    )

    assert HarnessType.CLI_CODEX not in seen, "openai implementer reviewed by an openai CLI"
    assert outcome.verdict == "error"


def test_all_harnesses_down_still_blocks_and_names_every_failure(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: a review that could not happen is never an auto-confirm."""
    by_harness = {
        HarnessType.CLI_GROK: _DeadHarness(GROK_NOT_SIGNED_IN),
        HarnessType.CLI_CODEX: _DeadHarness("codex: stream disconnected"),
        HarnessType.CLI_CLAUDE: _DeadHarness(CLAUDE_OAUTH_EXPIRED),
    }
    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        lambda harness: by_harness[harness],
    )

    outcome = _review(CrossLineageSwarmReviewer(), workspace)

    assert outcome.verdict == "error"
    assert "Not signed in" in outcome.feedback
    assert "stream disconnected" in outcome.feedback
    assert all(h.calls == 1 for h in by_harness.values())


def test_verification_surface_does_not_silently_fail_over(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-reviewer surface's reviewers are declared, never auto-substituted."""
    tried: list[HarnessType] = []

    def fake_resolve(harness: HarnessType) -> Any:
        tried.append(harness)
        return _DeadHarness(GROK_NOT_SIGNED_IN)

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", fake_resolve)

    outcome = _review(
        CrossLineageSwarmReviewer(),
        workspace,
        formation_reviewer="grok",
        implementer="gemini-3.6-flash",
        surface="verification",
    )

    assert outcome.verdict == "error"
    assert tried == [HarnessType.CLI_GROK]


# --------------------------------------------------------------------------
# 4. Format drift: tolerant extraction, then a genuine corrective re-prompt
# --------------------------------------------------------------------------


def test_fenced_verdict_is_accepted(workspace: Path) -> None:
    """Exact JSON stays preferred; a fenced/prose-wrapped verdict is not an
    infrastructure failure and must not burn a retry."""
    drift = _DriftingHarness(
        [
            'Here is my review.\n\n```json\n{"verdict": "deny", '
            '"feedback": "tests/simprobe/test_trim.py is missing"}\n```\n'
        ]
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)

    assert outcome.verdict == "deny"
    assert outcome.feedback == "tests/simprobe/test_trim.py is missing"
    assert drift.calls == 1


def test_trailing_verdict_object_on_its_own_line_is_accepted(
    workspace: Path,
) -> None:
    """A preamble followed by the verdict alone on the final line is the other
    shape a cooperating model produces."""
    drift = _DriftingHarness(
        [
            'I was asked for {"verdict": "confirm"|"deny", "feedback": "..."}.\n'
            '{"verdict": "deny", "feedback": "no red evidence"}'
        ]
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)
    assert (outcome.verdict, outcome.feedback) == ("deny", "no red evidence")
    assert drift.calls == 1


# -- BLOCKER regression: quoted verdicts must never be read as the reviewer's --

# Probe shape seen in cross-lineage review transcripts. Under
# last-balanced-object extraction this parsed as verdict=confirm and would have
# merged work the reviewer explicitly refused to confirm.
QUOTED_VERDICT_PROBE = (
    "I cannot verify this. The worker provided "
    '{"verdict":"confirm","feedback":"ship it"} but that is not my verdict.'
)


def test_quoted_verdict_mid_sentence_is_not_the_reviewers_verdict() -> None:
    """Fail-OPEN regression: reading a quotation as a CONFIRM merges unreviewed
    work. Refusing a real verdict only costs a corrective re-prompt."""
    assert _extract_verdict_payload(QUOTED_VERDICT_PROBE) is None


def test_quoted_verdict_reaches_the_corrective_reprompt_not_a_confirm(
    workspace: Path,
) -> None:
    drift = _DriftingHarness([QUOTED_VERDICT_PROBE])
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)

    assert outcome.verdict == "error", "a quoted verdict must never confirm"
    assert drift.calls == 2, "it must be re-asked, not guessed at"


def test_quoted_verdict_is_recovered_by_the_corrective_reprompt(
    workspace: Path,
) -> None:
    """Fail-closed must not mean fail-stuck: the re-prompt gets the real answer."""
    drift = _DriftingHarness(
        [QUOTED_VERDICT_PROBE, '{"verdict": "deny", "feedback": "cannot verify"}']
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)
    assert (outcome.verdict, outcome.feedback) == ("deny", "cannot verify")


@pytest.mark.parametrize(
    "body",
    [
        'The worker claims {"verdict":"confirm"} but I disagree.',
        'Do NOT return {"verdict": "confirm", "feedback": "x"} like the last one did.',
        'prefix {"verdict":"confirm"} suffix',
    ],
)
def test_embedded_verdicts_surrounded_by_prose_are_all_refused(body: str) -> None:
    assert _extract_verdict_payload(body) is None


def test_pretty_printed_trailing_object_is_accepted() -> None:
    """Multi-line JSON at the end still stands alone on its own lines."""
    payload = _extract_verdict_payload(
        'Summary of my review.\n\n{\n  "verdict": "confirm",\n  "feedback": "ok"\n}\n'
    )
    assert payload == {"verdict": "confirm", "feedback": "ok"}


def test_unparseable_output_triggers_a_corrective_reprompt_before_blocking(
    workspace: Path,
) -> None:
    """The retry path must GENUINELY retry with a corrective prompt — the old
    code re-sent the identical prompt to the identical (dead) harness."""
    drift = _DriftingHarness(
        [
            "Looks good to me overall, ship it.",
            '{"verdict": "confirm", "feedback": "recovered"}',
        ]
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)

    assert drift.calls == 2
    assert drift.prompts[0] != drift.prompts[1]
    assert '"verdict"' in drift.prompts[1]
    assert outcome.verdict == "confirm"


def test_prose_only_reviewer_still_blocks_after_the_corrective_reprompt(
    workspace: Path,
) -> None:
    drift = _DriftingHarness(["Looks good to me overall, ship it."])
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)

    assert drift.calls == 2, "must genuinely re-ask before blocking"
    assert outcome.verdict == "error"
    assert "Looks good to me overall" in outcome.feedback


def test_invalid_verdict_word_is_corrected_not_accepted(workspace: Path) -> None:
    drift = _DriftingHarness(
        [
            '{"verdict": "approve", "feedback": "fine"}',
            '{"verdict": "confirm", "feedback": "fine"}',
        ]
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=drift), workspace)
    assert drift.calls == 2
    assert outcome.verdict == "confirm"


# --------------------------------------------------------------------------
# 5. status=ERROR is overloaded: repair-failure drift vs a dead harness
# --------------------------------------------------------------------------


def _repair_failure_result(text: str) -> AgentResult:
    """Exactly what CliAdapter.run returns when structured-output repair fails.

    Built by driving the REAL claude adapter parse + the real schema validator,
    so the shape cannot drift away from adapters/common.py by fabrication.
    """
    from omniagentos.adapters.claude import ClaudeAdapter
    from omniagentos.orchestrator.review import _REVIEW_SCHEMA

    adapter = ClaudeAdapter()
    envelope = json.dumps({"is_error": False, "subtype": "success", "result": text})
    parsed = adapter._parse(envelope)
    output_json, validation_error = adapter._validate_json(parsed.text, _REVIEW_SCHEMA)
    assert output_json is None, "fixture must be a genuine schema-validation failure"
    return AgentResult(
        status=ResultStatus.ERROR,
        output_text=parsed.text,
        usage=_usage(),
        error=validation_error,
    )


class _RepairFailureHarness:
    """status=ERROR carrying the model's own text — a BAD ANSWER, not an outage."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    def run(self, agent_input: Any) -> Any:
        del agent_input
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if text.lstrip().startswith("{"):
            payload = json.loads(text)
            return AgentResult(
                status=ResultStatus.OK, output_text=text, output_json=payload, usage=_usage()
            )
        return _repair_failure_result(text)


def test_error_status_with_model_text_is_drift_and_gets_the_reprompt(
    workspace: Path,
) -> None:
    """Real CLIs return status=ERROR with the model's text after repair fails.
    Treating that as infra sent format drift to another lineage, which would
    drift too — the corrective re-prompt is what actually fixes it."""
    harness = _RepairFailureHarness(
        ["I think this is fine, ship it.", '{"verdict": "confirm", "feedback": "recovered"}']
    )
    outcome = _review(CrossLineageSwarmReviewer(adapter=harness), workspace)

    assert harness.calls == 2
    assert outcome.verdict == "confirm"


def test_error_status_with_model_text_never_fails_over_to_another_lineage(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[HarnessType] = []
    # One instance per harness, as the real registry returns — otherwise the
    # corrective re-prompt would reach a fresh object that replays reply #1.
    harness = _RepairFailureHarness(["prose only", '{"verdict": "deny", "feedback": "recovered"}'])

    def fake_resolve(harness_type: HarnessType) -> Any:
        tried.append(harness_type)
        return harness

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", fake_resolve)
    outcome = _review(CrossLineageSwarmReviewer(), workspace)

    assert outcome.verdict == "deny"
    # Resolved twice (initial probe + corrective re-prompt) but always the SAME
    # harness: drift is corrected in place, never handed to another lineage.
    assert set(tried) == {HarnessType.CLI_GROK}
    assert harness.calls == 2


def test_error_status_with_empty_output_is_still_infra(workspace: Path) -> None:
    """The auth/exit-code case must keep failing over, not burn a re-prompt."""
    dead = _DeadHarness(GROK_NOT_SIGNED_IN)
    outcome = _review(CrossLineageSwarmReviewer(adapter=dead), workspace)
    assert dead.calls == 1, "no corrective re-prompt for a harness that never ran"
    assert outcome.verdict == "error"
    assert "Not signed in" in outcome.feedback


# --------------------------------------------------------------------------
# 6. Bounded invocations, model attribution, per-harness diagnosis budget
# --------------------------------------------------------------------------


def test_exhausted_is_set_once_the_reviewer_has_already_tried_more_than_once(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        lambda harness: _DeadHarness("down"),
    )
    outcome = _review(CrossLineageSwarmReviewer(), workspace)
    assert outcome.verdict == "error"
    assert outcome.exhausted is True


def test_single_attempt_failure_is_not_exhausted_so_a_blip_still_retries(
    workspace: Path,
) -> None:
    """One injected harness, one infra failure: the outer retry is still the
    right call, because a single transient blip deserves a second look."""
    dead = _DeadHarness("transient")
    outcome = _review(CrossLineageSwarmReviewer(adapter=dead), workspace)
    assert outcome.exhausted is False


def test_reviewer_model_is_pinned_so_attribution_matches_execution(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording reviewer 'opus' while the adapter ran its 'sonnet' default made
    the audit trail claim a review that did not happen."""
    seen: list[str | None] = []

    class _Recorder:
        def run(self, agent_input: Any) -> Any:
            seen.append(agent_input.model)
            payload = {"verdict": "confirm", "feedback": "ok"}
            return AgentResult(
                status=ResultStatus.OK,
                output_text=json.dumps(payload),
                output_json=payload,
                usage=_usage(),
            )

    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter", lambda harness: _Recorder()
    )
    outcome = _review(
        CrossLineageSwarmReviewer(), workspace, formation_reviewer="opus", implementer="codex"
    )
    assert outcome.reviewer == "opus"
    assert seen == ["opus"]


def test_model_pinning_stays_silent_for_names_that_are_not_cli_model_ids() -> None:
    """A wrong --model makes the CLI refuse to start, which is worse than the
    attribution gap it would close. Unknown spellings fall back to the default."""
    assert _reviewer_model_id("opus", "anthropic") == "opus"
    assert _reviewer_model_id("gpt-5.6-sol", "openai") == "gpt-5.6-sol"
    assert _reviewer_model_id("grok-4.5", "xai") == "grok-4.5"
    assert _reviewer_model_id("cli-codex", "openai") is None
    assert _reviewer_model_id("sol", "openai") is None
    assert _reviewer_model_id("cli-claude", "anthropic") is None


# --------------------------------------------------------------------------
# 7. The no-substitution rule must be REACHABLE from the planner, not injected
# --------------------------------------------------------------------------


def _provisioned_swarm_json(tmp_path: Path, risk_class: str) -> dict[str, Any]:
    """A child card's swarm_json built by the REAL planner -> provision_run path.

    `review_surface` is never written by production code, so a rule that keys
    only off it can never fire on a real task. This walks the path an actual
    task takes so the gate is proven reachable rather than asserted.
    """
    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import build_plan, provision_run

    db = str(tmp_path / "planner.db")
    CollabStore(db)
    dal = SwarmDal(db)
    workspace = tmp_path / "plan-ws"
    workspace.mkdir()
    try:
        plan = build_plan(
            "ship it",
            [
                {
                    "id": "a",
                    "title": "A",
                    "description": "do a",
                    "depends_on": [],
                    "owned_paths": ["src/a.py"],
                    "est_agent_minutes": 10,
                    "est_manual_minutes": 30,
                    "acceptance": "a done",
                    "verify_command": "git diff --check",
                    "risk_class": risk_class,
                }
            ],
        )
        result = provision_run(plan, dal=dal, working_dir=str(workspace), write_plan_doc=False)
        card = CollabStore(db).get_board_task(result["card_ids"]["a"])
        swarm_json: dict[str, Any] = json.loads(card["swarm_json"] or "{}")
        assert swarm_json.get("task_key") == "a"
        return swarm_json
    finally:
        dal.close()


def test_planner_emits_the_risk_class_the_no_substitution_rule_reads(
    tmp_path: Path,
) -> None:
    """Proof the signal exists end-to-end: planner -> provision_run -> swarm_json."""
    destructive = _provisioned_swarm_json(tmp_path, "destructive")
    assert destructive["risk_class"] == "destructive"
    assert "review_surface" not in destructive, (
        "nothing in production writes review_surface; the rule must not depend on it"
    )
    assert _high_blast_radius(destructive) is True
    assert _high_blast_radius(_provisioned_swarm_json(tmp_path / "b", "none")) is False


def test_destructive_task_from_the_planner_never_substitutes_a_reviewer(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real planner-provisioned destructive card must block on its
    declared reviewer rather than quietly promoting a stand-in."""
    swarm_json = _provisioned_swarm_json(tmp_path, "destructive")
    swarm_json.update({"implementer_model": "gemini-3.6-flash", "formation_reviewer": "grok"})

    tried: list[HarnessType] = []

    def fake_resolve(harness_type: HarnessType) -> Any:
        tried.append(harness_type)
        return _DeadHarness(GROK_NOT_SIGNED_IN)

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", fake_resolve)

    outcome = CrossLineageSwarmReviewer().review(
        task={"id": "btk_x", "title": "t"},
        swarm_json=swarm_json,
        session={"id": "ses_x", "project_dir": str(workspace)},
        verify_output="",
        flags=[],
    )

    assert outcome.verdict == "error"
    assert tried == [HarnessType.CLI_GROK], "no stand-in on irreversible work"


def test_ordinary_task_from_the_planner_does_fail_over(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same path with risk_class=none must still be rescued by failover —
    otherwise the gate would have quietly disabled the fix everywhere."""
    swarm_json = _provisioned_swarm_json(tmp_path, "none")
    swarm_json.update({"implementer_model": "gemini-3.6-flash", "formation_reviewer": "grok"})

    healthy = _HealthyHarness("confirm")
    by_harness = {
        HarnessType.CLI_GROK: _DeadHarness(GROK_NOT_SIGNED_IN),
        HarnessType.CLI_CODEX: healthy,
        HarnessType.CLI_CLAUDE: _DeadHarness(CLAUDE_OAUTH_EXPIRED),
    }
    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", lambda h: by_harness[h])

    outcome = CrossLineageSwarmReviewer().review(
        task={"id": "btk_x", "title": "t"},
        swarm_json=swarm_json,
        session={"id": "ses_x", "project_dir": str(workspace)},
        verify_output="",
        flags=[],
    )

    assert outcome.verdict == "confirm"
    assert outcome.reviewer == "cli-codex"


# --------------------------------------------------------------------------
# 8. Bounded invocations through the REAL scheduler retry path
# --------------------------------------------------------------------------


class _CountingDeadHarness:
    total = 0

    def run(self, agent_input: Any) -> Any:
        del agent_input
        _CountingDeadHarness.total += 1
        return AgentResult(status=ResultStatus.ERROR, usage=_usage(), error=GROK_NOT_SIGNED_IN)


def test_scheduler_does_not_rerun_an_exhausted_failover_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer 'retry the reviewer once' used to re-run the ENTIRE chain: 2x
    the invocations for infra, 4x for drift, all to re-learn a known answer."""
    from tests.swarm.scheduler_fakes import make_harness, make_scheduler

    _CountingDeadHarness.total = 0
    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        lambda harness_type: _CountingDeadHarness(),
    )

    harness = make_harness(tmp_path, [{"id": "t"}], max_concurrency=1, integration=False)
    swarm_json = harness.swarm_json_of("t")
    swarm_json["formation_reviewer"] = "grok"
    assert harness.dal.set_swarm_json(harness.task_id("t"), swarm_json)
    try:
        scheduler = make_scheduler(harness, reviewer=CrossLineageSwarmReviewer())
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=30)

        attempts = harness.attempts_of("t")
        assert attempts[0]["end_reason"] == "blocked"
        # The chain is walked exactly ONCE. Before the exhausted marker the
        # outer retry doubled this.
        assert 1 < _CountingDeadHarness.total <= 3, (
            f"chain walked more than once: {_CountingDeadHarness.total} invocations"
        )
        assert "Not signed in" in attempts[0]["detail"]
    finally:
        harness.close()


def test_every_harness_diagnosis_survives_into_the_feedback(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wholesale tail-trim let a verbose first harness delete the later lines
    — exactly the ones that say whether failover found anything better."""
    by_harness = {
        HarnessType.CLI_GROK: _DeadHarness("G" * 4000),
        HarnessType.CLI_CODEX: _DeadHarness("codex-distinctive-error"),
        HarnessType.CLI_CLAUDE: _DeadHarness("claude-distinctive-error"),
    }
    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter", lambda harness: by_harness[harness]
    )
    outcome = _review(CrossLineageSwarmReviewer(), workspace)

    assert "codex-distinctive-error" in outcome.feedback
    assert "claude-distinctive-error" in outcome.feedback
