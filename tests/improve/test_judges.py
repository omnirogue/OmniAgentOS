from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.improve.judges import (
    CHEAP_PANEL,
    PREMIUM_PANEL,
    SOL_JUDGE_MODEL,
    Attestation,
    Blocker,
    ChangeSet,
    Opinion,
    OwnerPing,
    PairwiseView,
    Vote,
    classify_tier,
    run_cascade,
    tier_policy,
)


class FakeJudge:
    def __init__(self, candidate_text: str = "", main_text: str = "") -> None:
        self.candidate_text = candidate_text
        self.main_text = main_text
        self.calls: list[tuple[str, PairwiseView]] = []
        # maps judge_model -> vote ("candidate" or "main")
        self.votes: dict[str, Vote] = {}
        # maps judge_model -> blocker
        self.blockers: dict[str, Blocker | None] = {}
        # maps judge_model -> critique
        self.critiques: dict[str, str] = {}
        # maps judge_model -> attestation (key present = explicit, including None)
        self.attestations: dict[str, Attestation | None] = {}
        # default vote if not customized
        self.default_vote: Vote = "candidate"

    def __call__(self, judge_model: str, view: PairwiseView) -> Opinion:
        self.calls.append((judge_model, view))

        # Determine candidate and main tokens from blinded view
        tokens: dict[Vote, str] = {}
        for side in view["sides"]:
            text_val = side["output"].get("text", "")
            if text_val == self.candidate_text:
                tokens["candidate"] = side["blind_token"]
            elif text_val == self.main_text:
                tokens["main"] = side["blind_token"]
            else:
                raise ValueError(
                    f"FakeJudge could not match side text {text_val!r} to "
                    f"candidate_text {self.candidate_text!r} or main_text {self.main_text!r}"
                )

        vote = self.votes.get(judge_model, self.default_vote)
        preferred_token = tokens[vote]

        blocker = self.blockers.get(judge_model, None)
        critique = self.critiques.get(judge_model, "")
        # Only auto-fill Sol attestation when the key is absent; explicit None must stick
        # so tests can exercise missing_attestation.
        if judge_model in self.attestations:
            att_val = self.attestations[judge_model]
        else:
            att_val = None
            if judge_model == SOL_JUDGE_MODEL:
                policy = tier_policy(view["tier"])
                if policy.requires_attestation:
                    att_val = Attestation(attested=True, statement="Sol verified")

        return Opinion(
            preferred_token=preferred_token,
            rationale=f"{judge_model} rationale",
            blocker=blocker,
            critique=critique,
            attestation=att_val,
        )


def _t0_docs_changeset(
    *,
    attempt_id: str = "att_t0",
    diff_text: str = "readme changes",
    base_text: str = "readme base",
) -> ChangeSet:
    return ChangeSet(
        attempt_id=attempt_id,
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("docs/readme.md",),
        changed_loc=5,
        diff_text=diff_text,
        summary="update readme",
        base_text=base_text,
        base_summary="readme original summary",
    )


def _t1_module_changeset(
    *,
    attempt_id: str = "att_t1",
    diff_text: str = "some changes",
    base_text: str = "some base",
) -> ChangeSet:
    return ChangeSet(
        attempt_id=attempt_id,
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/my_module/abc.py",),
        changed_loc=10,
        diff_text=diff_text,
        summary="update module",
        base_text=base_text,
        base_summary="some original summary",
    )


def _t2_swarm_changeset(
    *,
    attempt_id: str = "att_t2",
    diff_text: str = "swarm changes",
    base_text: str = "swarm base",
) -> ChangeSet:
    return ChangeSet(
        attempt_id=attempt_id,
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/swarm/agent.py",),
        changed_loc=10,
        diff_text=diff_text,
        summary="update swarm",
        base_text=base_text,
        base_summary="swarm original summary",
    )


def _ready_t1_judge(diff_text: str = "some changes", base_text: str = "some base") -> FakeJudge:
    judge = FakeJudge(candidate_text=diff_text, main_text=base_text)
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")
    return judge


def _ready_t2_judge(diff_text: str = "swarm changes", base_text: str = "swarm base") -> FakeJudge:
    judge = FakeJudge(candidate_text=diff_text, main_text=base_text)
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")
    return judge


def test_t0_cannot_merge_without_final_premium_review() -> None:
    changeset = ChangeSet(
        attempt_id="att_t0",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("docs/readme.md",),
        changed_loc=5,
        diff_text="readme changes",
        summary="update readme",
        base_text="readme base",
        base_summary="readme original summary",
    )
    # Verify it is classified as T0
    assert classify_tier(changeset) == "T0"

    judge = FakeJudge(candidate_text="readme changes", main_text="readme base")
    # Run with final review stage skipped
    verdict1 = run_cascade(
        changeset,
        judge_fn=judge,
        skip_stages=frozenset({"final"}),
    )
    assert verdict1.approved is False
    assert verdict1.reason == "missing_premium_review"

    # Run with final review stage included
    verdict2 = run_cascade(
        changeset,
        judge_fn=judge,
    )
    assert verdict2.approved is True
    assert verdict2.reason == "approved"


def test_reproduced_severity1_blocker_vetoes_unanimous_branch() -> None:
    changeset = ChangeSet(
        attempt_id="att_t2",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/swarm/agent.py",),
        changed_loc=10,
        diff_text="swarm changes",
        summary="update swarm",
        base_text="swarm base",
        base_summary="swarm original summary",
    )
    assert classify_tier(changeset) == "T2"

    from omniagentos.improve.judges import PREMIUM_PANEL
    veto_model = PREMIUM_PANEL[0]

    # Scenario 1: A premium panel judge attaches a reproduced severity-1 blocker -> vetoed!
    judge1 = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge1.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge1.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge1.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    judge1.blockers[veto_model] = Blocker(
        severity=1,
        citation="omniagentos/x/y.py:42",
        reproduced=True,
    )

    owner_ping = OwnerPing(sent_at=datetime.now(UTC), response="approve")

    verdict1 = run_cascade(
        changeset,
        judge_fn=judge1,
        owner_ping=owner_ping,
    )
    assert verdict1.approved is False
    assert verdict1.reason == "vetoed_severity1_blocker"
    assert verdict1.veto_by == veto_model
    # Assert the panel really was unanimous before the veto fired
    assert len(verdict1.verdicts) > 0
    for v in verdict1.verdicts:
        assert v.vote == "candidate"

    # Scenario 2: Identical scenario but reproduced=False -> not vetoed (approved!)
    judge2 = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge2.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge2.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge2.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    judge2.blockers[veto_model] = Blocker(
        severity=1,
        citation="omniagentos/x/y.py:42",
        reproduced=False,
    )

    verdict2 = run_cascade(
        changeset,
        judge_fn=judge2,
        owner_ping=owner_ping,
    )
    assert verdict2.approved is True


def test_unreproduced_preference_dissent_does_not_block() -> None:
    changeset = ChangeSet(
        attempt_id="att_t2",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/swarm/agent.py",),
        changed_loc=10,
        diff_text="swarm changes",
        summary="update swarm",
        base_text="swarm base",
        base_summary="swarm original summary",
    )
    assert classify_tier(changeset) == "T2"

    from omniagentos.improve.judges import PREMIUM_PANEL
    dissent_model = PREMIUM_PANEL[0]

    # Scenario 1: One judge dissents but has an unreproduced/unsubstantiated blocker -> approved!
    judge1 = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge1.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge1.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge1.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    judge1.votes[dissent_model] = "main"
    judge1.critiques[dissent_model] = "unsubstantiated dissent"
    judge1.blockers[dissent_model] = Blocker(severity=2, citation="feels risky")

    owner_ping = OwnerPing(sent_at=datetime.now(UTC), response="approve")

    verdict1 = run_cascade(
        changeset,
        judge_fn=judge1,
        owner_ping=owner_ping,
    )
    assert verdict1.approved is True

    # Scenario 2: Flip dissent to a substantiated blocker (but unreproduced, severity 2) -> blocks!
    judge2 = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge2.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge2.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge2.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    judge2.votes[dissent_model] = "main"
    judge2.critiques[dissent_model] = "substantiated dissent"
    judge2.blockers[dissent_model] = Blocker(
        severity=2,
        citation="omniagentos/x/y.py:9",
        reproduced=False,
    )

    verdict2 = run_cascade(
        changeset,
        judge_fn=judge2,
        owner_ping=owner_ping,
    )
    assert verdict2.approved is False
    assert verdict2.reason == "premium_panel_failed"


def test_tier_routing_picks_t2_for_agent_core() -> None:
    # 1. T2 for a single small file under an agent-core prefix
    cs1 = ChangeSet(
        attempt_id="att1", base_sha="b", head_sha="h", tree_hash="t", diff_hash="d",
        files=("omniagentos/swarm/abc.py",), changed_loc=5
    )
    assert classify_tier(cs1) == "T2"

    # 2. T0 for a docs-only change
    cs2 = ChangeSet(
        attempt_id="att2", base_sha="b", head_sha="h", tree_hash="t", diff_hash="d",
        files=("docs/readme.md", "README.txt"), changed_loc=100
    )
    assert classify_tier(cs2) == "T0"

    # 3. T1 for one small single-module non-core code file
    cs3 = ChangeSet(
        attempt_id="att3", base_sha="b", head_sha="h", tree_hash="t", diff_hash="d",
        files=("omniagentos/my_module/abc.py",), changed_loc=100
    )
    assert classify_tier(cs3) == "T1"

    # 4. T2 for that same T1 file at 300+ LOC
    cs4 = ChangeSet(
        attempt_id="att4", base_sha="b", head_sha="h", tree_hash="t", diff_hash="d",
        files=("omniagentos/my_module/abc.py",), changed_loc=300
    )
    assert classify_tier(cs4) == "T2"

    # 5. T2 for a dependency-file change
    cs5 = ChangeSet(
        attempt_id="att5", base_sha="b", head_sha="h", tree_hash="t", diff_hash="d",
        files=("pyproject.toml",), changed_loc=5
    )
    assert classify_tier(cs5) == "T2"


def test_judging_is_pairwise_against_main() -> None:
    changeset = ChangeSet(
        attempt_id="att_t0",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("docs/readme.md",),
        changed_loc=5,
        diff_text="readme changes",
        summary="update readme",
        base_text="readme base",
        base_summary="readme original summary",
    )

    judge1 = FakeJudge(candidate_text="readme changes", main_text="readme base")
    run_cascade(changeset, judge_fn=judge1)

    assert len(judge1.calls) > 0
    first_call_tokens: set[str] = set()

    for _model, view in judge1.calls:
        sides = view["sides"]
        assert len(sides) == 2

        token_a = sides[0]["blind_token"]
        token_b = sides[1]["blind_token"]
        assert token_a != token_b

        # check key sets
        assert sides[0].keys() == sides[1].keys()
        assert sides[0]["output"].keys() == sides[1]["output"].keys()

        # check forbidden labels in payload values
        forbidden = ["current main", "main branch", "is main", "candidate", "baseline"]
        for side in sides:
            payload_str = " ".join(str(v).lower() for v in side["output"].values())
            for f in forbidden:
                assert f not in payload_str, f"Found forbidden term {f!r} in payload: {payload_str}"

        # both output dicts are non-empty and neither is empty-vs-populated
        assert sides[0]["output"]
        assert sides[1]["output"]
        assert sides[0]["output"]["text"] != ""
        assert sides[1]["output"]["text"] != ""

        first_call_tokens.add(token_a)
        first_call_tokens.add(token_b)

    # Second run should have completely disjoint fresh tokens
    judge2 = FakeJudge(candidate_text="readme changes", main_text="readme base")
    run_cascade(changeset, judge_fn=judge2)

    second_call_tokens: set[str] = set()
    for _model, view in judge2.calls:
        for side in view["sides"]:
            second_call_tokens.add(side["blind_token"])

    assert first_call_tokens.isdisjoint(second_call_tokens)


def test_verdicts_persist_to_improve_verdicts(tmp_path: Path) -> None:
    db = str(tmp_path / "test_improve.db")
    migrate(db)

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row

    changeset = ChangeSet(
        attempt_id="att_persist",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("docs/readme.md",),
        changed_loc=5,
        diff_text="readme changes",
        summary="update readme",
        base_text="readme base",
        base_summary="readme original summary",
    )

    judge = FakeJudge(candidate_text="readme changes", main_text="readme base")
    from omniagentos.improve.judges import CHEAP_PANEL
    model_a = CHEAP_PANEL[0]
    judge.blockers[model_a] = Blocker(
        severity=2,
        citation="omniagentos/x/y.py:10",
        reproduced=True,
    )

    run_cascade(changeset, judge_fn=judge, connection=connection)

    rows = connection.execute("SELECT * FROM improve_verdicts").fetchall()
    # 3 cheap + 1 final = 4 verdicts
    assert len(rows) == 4

    row_a = [r for r in rows if r["judge_model"] == model_a][0]
    assert row_a["tier"] == "T0"
    assert row_a["stage"] == "cheap"
    assert row_a["vote"] == "candidate"
    assert row_a["blocker_cited"] == 1
    assert row_a["blocker_reproduced"] == 1
    assert row_a["base_sha"] == "sha_b"
    assert row_a["head_sha"] == "sha_h"
    assert row_a["tree_hash"] == "tree"
    assert row_a["diff_hash"] == "diff"
    assert len(row_a["judge_config_hash"]) == 64
    assert datetime.fromisoformat(row_a["created_at"]) is not None

    hashes = {r["judge_config_hash"] for r in rows}
    assert len(hashes) == 1

    connection.close()


def test_t1_dissenter_must_write_forced_critique() -> None:
    changeset = ChangeSet(
        attempt_id="att_t1",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/my_module/abc.py",),
        changed_loc=10,
        diff_text="some changes",
        summary="update module",
        base_text="some base",
        base_summary="some original summary",
    )
    assert classify_tier(changeset) == "T1"

    from omniagentos.improve.judges import PREMIUM_PANEL
    dissent_model = PREMIUM_PANEL[0]

    # Scenario 1: Dissenter writes empty critique -> missing_forced_critique!
    judge = FakeJudge(candidate_text="some changes", main_text="some base")
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    judge.votes[dissent_model] = "main"
    judge.critiques[dissent_model] = ""

    verdict1 = run_cascade(changeset, judge_fn=judge)
    assert verdict1.approved is False
    assert verdict1.reason == "missing_forced_critique"

    # Scenario 2: Dissenter writes a non-empty critique -> approved!
    judge.critiques[dissent_model] = "this critique details why I dissented"
    verdict2 = run_cascade(changeset, judge_fn=judge)
    assert verdict2.approved is True


def test_t2_requires_owner_response() -> None:
    changeset = ChangeSet(
        attempt_id="att_t2",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/swarm/agent.py",),
        changed_loc=10,
        diff_text="swarm changes",
        summary="update swarm",
        base_text="swarm base",
        base_summary="swarm original summary",
    )
    assert classify_tier(changeset) == "T2"

    judge = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    from omniagentos.improve.judges import PREMIUM_PANEL
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    # 1. no ping -> "owner_response_required"
    v1 = run_cascade(changeset, judge_fn=judge, owner_ping=None)
    assert v1.approved is False
    assert v1.reason == "owner_response_required"

    # 2. ping sent 25h ago with no response -> "owner_no_response"
    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    sent_at = now - timedelta(hours=25)
    owner_ping_timeout = OwnerPing(sent_at=sent_at, response=None)
    v2 = run_cascade(changeset, judge_fn=judge, owner_ping=owner_ping_timeout, now=now)
    assert v2.approved is False
    assert v2.reason == "owner_no_response"

    # 3. within deadline, no response -> awaiting_owner
    sent_at_await = now - timedelta(hours=10)
    owner_ping_await = OwnerPing(sent_at=sent_at_await, response=None)
    v3 = run_cascade(changeset, judge_fn=judge, owner_ping=owner_ping_await, now=now)
    assert v3.approved is False
    assert v3.reason == "awaiting_owner"

    # 4. reject -> owner_rejected
    owner_ping_reject = OwnerPing(sent_at=sent_at_await, response="reject")
    v4 = run_cascade(changeset, judge_fn=judge, owner_ping=owner_ping_reject, now=now)
    assert v4.approved is False
    assert v4.reason == "owner_rejected"

    # 5. approve -> approved
    owner_ping_approve = OwnerPing(sent_at=sent_at_await, response="approve")
    v5 = run_cascade(changeset, judge_fn=judge, owner_ping=owner_ping_approve, now=now)
    assert v5.approved is True


def test_premium_panel_cannot_be_skipped() -> None:
    changeset = ChangeSet(
        attempt_id="att_t1",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/my_module/abc.py",),
        changed_loc=10,
        diff_text="some changes",
        summary="update module",
        base_text="some base",
        base_summary="some original summary",
    )
    assert classify_tier(changeset) == "T1"

    judge = FakeJudge(candidate_text="some changes", main_text="some base")
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    from omniagentos.improve.judges import PREMIUM_PANEL
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    # Run with premium stage skipped
    verdict1 = run_cascade(
        changeset,
        judge_fn=judge,
        skip_stages=frozenset({"premium"}),
    )
    assert verdict1.approved is False
    assert verdict1.reason == "missing_premium_review"

    # Run with premium stage included
    verdict2 = run_cascade(
        changeset,
        judge_fn=judge,
    )
    assert verdict2.approved is True
    assert verdict2.reason == "approved"


def test_veto_outranks_missing_owner_response() -> None:
    changeset = ChangeSet(
        attempt_id="att_t2",
        base_sha="sha_b",
        head_sha="sha_h",
        tree_hash="tree",
        diff_hash="diff",
        files=("omniagentos/swarm/agent.py",),
        changed_loc=10,
        diff_text="swarm changes",
        summary="update swarm",
        base_text="swarm base",
        base_summary="swarm original summary",
    )
    assert classify_tier(changeset) == "T2"

    judge = FakeJudge(candidate_text="swarm changes", main_text="swarm base")
    judge.critiques[SOL_JUDGE_MODEL] = "sol critique"
    judge.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    from omniagentos.improve.judges import PREMIUM_PANEL
    for model in PREMIUM_PANEL:
        judge.attestations[model] = Attestation(attested=True, statement=f"{model} verified")

    veto_model = PREMIUM_PANEL[0]
    judge.blockers[veto_model] = Blocker(
        severity=1,
        citation="omniagentos/x/y.py:42",
        reproduced=True,
    )

    verdict = run_cascade(
        changeset,
        judge_fn=judge,
        owner_ping=None,  # No owner ping!
    )
    assert verdict.approved is False
    assert verdict.reason == "vetoed_severity1_blocker"
    assert verdict.veto_by == veto_model
    assert len(judge.calls) > 0


def test_sol_dissent_blocks_regardless_of_panels() -> None:
    """Sol voting main must block even when every cheap/premium judge is unanimous for candidate."""
    # T0: Sol is the only premium review
    t0 = _t0_docs_changeset()
    assert classify_tier(t0) == "T0"
    judge_t0 = FakeJudge(candidate_text="readme changes", main_text="readme base")
    judge_t0.votes[SOL_JUDGE_MODEL] = "main"
    v_t0 = run_cascade(t0, judge_fn=judge_t0)
    assert v_t0.approved is False
    assert v_t0.reason == "final_review_failed"
    assert all(v.vote == "candidate" for v in v_t0.verdicts if v.stage != "final")
    assert any(v.stage == "final" and v.vote == "main" for v in v_t0.verdicts)

    # T2: unanimous cheap + premium, Sol still blocks
    t2 = _t2_swarm_changeset()
    assert classify_tier(t2) == "T2"
    judge_t2 = _ready_t2_judge()
    judge_t2.votes[SOL_JUDGE_MODEL] = "main"
    owner = OwnerPing(sent_at=datetime.now(UTC), response="approve")
    v_t2 = run_cascade(t2, judge_fn=judge_t2, owner_ping=owner)
    assert v_t2.approved is False
    assert v_t2.reason == "final_review_failed"
    assert all(v.vote == "candidate" for v in v_t2.verdicts if v.stage != "final")
    assert any(v.stage == "final" and v.vote == "main" for v in v_t2.verdicts)


def test_sol_must_write_forced_critique() -> None:
    """Forced critique is Sol's own (not the premium dissenter branch)."""
    for tier_builder, make_judge, owner_ping in (
        (_t1_module_changeset, _ready_t1_judge, None),
        (
            _t2_swarm_changeset,
            _ready_t2_judge,
            OwnerPing(sent_at=datetime.now(UTC), response="approve"),
        ),
    ):
        changeset = tier_builder()
        expected_tier = "T1" if owner_ping is None else "T2"
        assert classify_tier(changeset) == expected_tier

        for empty in ("", "   "):
            judge = make_judge()
            judge.critiques[SOL_JUDGE_MODEL] = empty
            kwargs = {"owner_ping": owner_ping} if owner_ping is not None else {}
            failed = run_cascade(changeset, judge_fn=judge, **kwargs)
            assert failed.approved is False
            assert failed.reason == "missing_forced_critique"

        judge_ok = make_judge()
        judge_ok.critiques[SOL_JUDGE_MODEL] = "sol forced critique of the candidate"
        kwargs = {"owner_ping": owner_ping} if owner_ping is not None else {}
        ok = run_cascade(changeset, judge_fn=judge_ok, **kwargs)
        assert ok.approved is True
        assert ok.reason == "approved"


def test_attestation_is_required() -> None:
    """Sol attestation (T1/T2) and full premium attestation (T2 only)."""
    t1 = _t1_module_changeset()
    assert classify_tier(t1) == "T1"

    # a. T1, Sol's attestation is None
    judge_a = _ready_t1_judge()
    judge_a.attestations[SOL_JUDGE_MODEL] = None
    v_a = run_cascade(t1, judge_fn=judge_a)
    assert v_a.approved is False
    assert v_a.reason == "missing_attestation"
    judge_a.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="Sol verified")
    assert run_cascade(t1, judge_fn=judge_a).approved is True

    # b. T1, Sol attests with attested=False
    judge_b = _ready_t1_judge()
    judge_b.attestations[SOL_JUDGE_MODEL] = Attestation(attested=False, statement="no")
    v_b = run_cascade(t1, judge_fn=judge_b)
    assert v_b.approved is False
    assert v_b.reason == "missing_attestation"
    judge_b.attestations[SOL_JUDGE_MODEL] = Attestation(attested=True, statement="yes")
    assert run_cascade(t1, judge_fn=judge_b).approved is True

    # c. T2: Sol attests, but one premium panelist has no attestation -> fail.
    #    Same scenario on T1 is approved (full panel attestation is T2-only).
    missing_premium = PREMIUM_PANEL[0]

    t2 = _t2_swarm_changeset()
    assert classify_tier(t2) == "T2"
    judge_t2 = _ready_t2_judge()
    judge_t2.attestations[missing_premium] = None
    owner = OwnerPing(sent_at=datetime.now(UTC), response="approve")
    v_t2 = run_cascade(t2, judge_fn=judge_t2, owner_ping=owner)
    assert v_t2.approved is False
    assert v_t2.reason == "missing_attestation"
    judge_t2.attestations[missing_premium] = Attestation(
        attested=True, statement=f"{missing_premium} verified"
    )
    assert run_cascade(t2, judge_fn=judge_t2, owner_ping=owner).approved is True

    # Same missing premium attestation on T1 is approved (full panel attestation is T2-only)
    judge_t1_partial = _ready_t1_judge()
    del judge_t1_partial.attestations[missing_premium]
    v_t1_partial = run_cascade(t1, judge_fn=judge_t1_partial)
    assert v_t1_partial.approved is True


def test_judge_cannot_invent_a_blind_token() -> None:
    """Judges may only vote tokens from the blinded view they were given."""
    changeset = _t0_docs_changeset()
    assert classify_tier(changeset) == "T0"

    def inventing_judge(judge_model: str, view: PairwiseView) -> Opinion:
        return Opinion(preferred_token="not-a-real-token", rationale="forged")

    with pytest.raises(LookupError) as exc_info:
        run_cascade(changeset, judge_fn=inventing_judge)
    # Must be the explicit gate (LookupError), not bare dict KeyError (a LookupError subclass)
    assert type(exc_info.value) is LookupError
    msg = str(exc_info.value)
    assert "not-a-real-token" in msg
    assert "unknown token" in msg


def test_cheap_screen_threshold_is_enforced() -> None:
    """Cheap panel in_favour < cheap_required stops the cascade (tier-sensitive)."""
    substantiated = Blocker(
        severity=2,
        citation="omniagentos/x/y.py:7",
        reproduced=False,
    )
    cheap_dissenter = CHEAP_PANEL[0]
    second_dissenter = CHEAP_PANEL[1]

    # a. T2 (cheap_required=3): one substantiated cheap dissent fails and stops cascade
    t2 = _t2_swarm_changeset()
    assert classify_tier(t2) == "T2"
    assert tier_policy("T2").cheap_required == 3
    judge_t2 = _ready_t2_judge()
    judge_t2.votes[cheap_dissenter] = "main"
    judge_t2.blockers[cheap_dissenter] = substantiated
    owner = OwnerPing(sent_at=datetime.now(UTC), response="approve")
    v_t2 = run_cascade(t2, judge_fn=judge_t2, owner_ping=owner)
    assert v_t2.approved is False
    assert v_t2.reason == "cheap_panel_failed"
    assert all(v.stage == "cheap" for v in v_t2.verdicts)
    assert not any(v.stage == "premium" for v in v_t2.verdicts)
    assert not any(v.stage == "final" for v in v_t2.verdicts)
    assert not any(model == SOL_JUDGE_MODEL for model, _ in judge_t2.calls)
    assert not any(model in PREMIUM_PANEL for model, _ in judge_t2.calls)

    # b. T1 (cheap_required=2): same single dissent still passes cheap and can approve
    t1 = _t1_module_changeset()
    assert classify_tier(t1) == "T1"
    assert tier_policy("T1").cheap_required == 2
    judge_t1_ok = _ready_t1_judge()
    judge_t1_ok.votes[cheap_dissenter] = "main"
    judge_t1_ok.blockers[cheap_dissenter] = substantiated
    v_t1_ok = run_cascade(t1, judge_fn=judge_t1_ok)
    assert v_t1_ok.approved is True
    assert any(v.stage == "premium" for v in v_t1_ok.verdicts)
    assert any(v.stage == "final" for v in v_t1_ok.verdicts)

    # c. T1: two substantiated cheap dissents fail the screen
    judge_t1_fail = _ready_t1_judge()
    judge_t1_fail.votes[cheap_dissenter] = "main"
    judge_t1_fail.blockers[cheap_dissenter] = substantiated
    judge_t1_fail.votes[second_dissenter] = "main"
    judge_t1_fail.blockers[second_dissenter] = substantiated
    v_t1_fail = run_cascade(t1, judge_fn=judge_t1_fail)
    assert v_t1_fail.approved is False
    assert v_t1_fail.reason == "cheap_panel_failed"
    assert all(v.stage == "cheap" for v in v_t1_fail.verdicts)
