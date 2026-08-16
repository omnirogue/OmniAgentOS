"""Pins bridge/publish_queue.py as the SOLE RUNTIME writer of state/queue.json.

(`bootstrap.sh` still seeds an initial placeholder at repo setup — a
one-time, disclosed exception, not a steady-state race; see
test_F5_bootstrap_sh_is_a_disclosed_seed_writer_not_hidden below.)

The defect this file keeps closed: rebuild_queue() already emitted the right
shape (wip, wip_cap, wip_definition, wip_degraded) but nothing ran it on a cadence, so a
hand-authored queue.json kept overwriting it and dropping `wip` — read as
headroom by both producer loops. An entirely unreadable torn ledger writes no
file; a partial damaged replay may publish diagnostics, but marks
`wip_degraded` so every producer refuses its guessed count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402
from bridge import publish_queue as P  # noqa: E402

# probe discipline (verdict_absence_matrix.py's pattern): refuse to measure
# the wrong tree if some other bridge/ ends up earlier on sys.path.
assert Path(P.__file__).resolve() == (PKG / "bridge" / "publish_queue.py").resolve(), \
    f"imported the WRONG publish_queue.py: {P.__file__}"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "loopqueue"
    for sub in ("proposals", "candidates", "findings", "inquiries", "rejected", "parked", "state"):
        (r / sub).mkdir(parents=True)
    return r


def _write_ledger(root: Path, lines: list[str]) -> None:
    (root / "ledger.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""),
                                        encoding="utf-8")


def _ev(event: str, ident: str, ts: str = "2026-08-08T10:00:00Z", **detail) -> str:
    return json.dumps({"ts": ts, "role": "implementer", "event": event, "id": ident,
                       "detail": detail})


# --------------------------------------------------------------------------
# 1. publish() is the sole path to queue.json and emits the right shape
# --------------------------------------------------------------------------

def test_publish_emits_wip_shape_golden(root: Path):
    _write_ledger(root, [
        _ev("proposed", "sha256:aaa", title="one"),
        _ev("admitted", "sha256:aaa"),
        _ev("proposed", "sha256:bbb", title="two"),
        _ev("admitted", "sha256:bbb"),
        _ev("gated", "sha256:bbb"),
        _ev("proposed", "sha256:ccc", title="three"),
        _ev("merged", "sha256:ccc"),
    ])
    q, msg = P.publish(root)
    assert q is not None, msg

    # golden snapshot of the backpressure fields — the whole point of this file
    assert q["wip"] == 2                # aaa (admitted) + bbb (gated); ccc is terminal
    assert q["wip_cap"] == 4            # no budget.json present -> default
    assert q["wip_degraded"] is False
    # A blocked candidate counts only with WIP-entry provenance. A generic
    # admission-time instrument diagnostic stays visible, but preserves headroom.
    assert q["wip_definition"] == (
        "ids whose status is admitted or gated, plus ids blocked while occupying a slot "
        "(entered WIP via an admitted/gated/unparked event or a blocked-on-human refusal, "
        "no terminal event, not parked); an instrument error on an id that never entered "
        "WIP is visible but not WIP; ledger damage (discarded lines or a torn tail) sets "
        "wip_degraded and omits wip so every reader refuses the guessed count")
    assert q["rebuilt_by"] == "publish_queue"   # names the MECHANICAL publisher, not a hand-edit
    assert "rebuilt_at" in q and "ledger_events" in q

    on_disk = json.loads((root / "state" / "queue.json").read_text())
    assert on_disk == q


def test_publish_reads_wip_cap_from_budget(root: Path):
    (root / "state" / "budget.json").write_text(json.dumps({"wip_cap": 8}), encoding="utf-8")
    _write_ledger(root, [_ev("proposed", "sha256:aaa"), _ev("admitted", "sha256:aaa")])
    q, msg = P.publish(root)
    assert q is not None, msg
    assert q["wip_cap"] == 8


def test_publish_no_admission_no_gating_no_subprocess(root: Path, monkeypatch):
    """publish() must never shell out — it is a pure replay+write."""
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("publish_queue must never invoke subprocess")

    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)
    _write_ledger(root, [_ev("proposed", "sha256:aaa"), _ev("admitted", "sha256:aaa")])
    q, msg = P.publish(root)
    assert q is not None, msg


# --------------------------------------------------------------------------
# 2. torn tail -> LOUD failure, NO file written (RED-FIRST evidence in the
#    build transcript: this test failed before publish() carried the guard)
# --------------------------------------------------------------------------

def test_torn_tail_with_no_valid_events_refuses_and_writes_nothing(root: Path):
    # single line, deliberately truncated mid-object: not valid JSON, and it
    # is the ONLY line, so replay produces zero events with torn_tail=True.
    (root / "ledger.jsonl").write_text('{"ts": "2026-08-08T10:00:00Z", "role": "impl', encoding="utf-8")

    queue_path = root / "state" / "queue.json"
    assert not queue_path.exists()

    q, msg = P.publish(root)

    assert q is None                      # loud refusal, not a degraded object
    assert "torn" in msg.lower() or "unreadable" in msg.lower()
    assert not queue_path.exists(), "a torn-tail replay must write NO file, not a wip:0 lie"
    assert not (queue_path.with_suffix(".json.tmp")).exists(), "no tmp artefact left behind either"


def test_torn_tail_after_valid_events_still_publishes_with_ledger_torn_tail_flag(root: Path):
    """A partial replay may publish diagnostics, but no producer may trust WIP."""
    lines = [_ev("proposed", "sha256:aaa"), _ev("admitted", "sha256:aaa")]
    (root / "ledger.jsonl").write_text(
        "\n".join(lines) + '\n{"ts": "2026-08-08T11:00:00Z", "role": "impl', encoding="utf-8")
    q, msg = P.publish(root)
    assert q is not None, msg
    assert q["ledger_torn_tail"] is True
    assert q["wip_degraded"] is True
    assert "wip" not in q
    detail = q["wip_degraded_detail"]
    assert detail.startswith("torn tail at line 3, byte ")
    assert "impl" in detail
    with pytest.raises(P.WipKeyMissing) as exc:
        P.read_wip(root / "state" / "queue.json")
    assert detail in str(exc.value)


def test_discarded_line_does_not_poison_other_id_and_read_refuses(root: Path) -> None:
    """Damage is global read confidence, never per-id WIP provenance."""
    (root / "ledger.jsonl").write_text(
        '"unrelated malformed history"\n'
        + _ev("instrument_error", "sha256:clean-id", **{"class": "timeout"})
        + "\n",
        encoding="utf-8",
    )
    view = I.LedgerView.build(root)
    assert "sha256:clean-id" not in view.wip_entered

    q, msg = P.publish(root)
    assert q is not None, msg
    assert q["wip_degraded"] is True
    assert "wip" not in q
    detail = q["wip_degraded_detail"]
    assert detail == 'line 1, byte 0: \'"unrelated malformed history"\''
    with pytest.raises(P.WipKeyMissing) as exc:
        P.read_wip(root / "state" / "queue.json")
    assert detail in str(exc.value)


def test_dropped_admitted_line_refuses_instead_of_guessing_wip(root: Path) -> None:
    """A lost entry event may undercount, so readers refuse the whole count."""
    (root / "ledger.jsonl").write_text(
        _ev("proposed", "sha256:damaged-id") + '\n"damaged admitted line"\n',
        encoding="utf-8",
    )
    q, msg = P.publish(root)
    assert q is not None, msg
    assert "wip" not in q
    assert q["wip_degraded"] is True
    assert q["wip_degraded_detail"].startswith("line 2, byte ")
    with pytest.raises(P.WipKeyMissing):
        P.read_wip(root / "state" / "queue.json")


def test_read_wip_refuses_half_applied_degraded_snapshot_with_wip(root: Path) -> None:
    """A stale writer cannot bypass degradation by retaining a guessed number."""
    path = root / "state" / "queue.json"
    detail = "line 9, byte 321: '\"bad\"'"
    path.write_text(json.dumps({
        "wip": 0,
        "wip_cap": 4,
        "wip_definition": "x",
        "wip_degraded": True,
        "wip_degraded_detail": detail,
        "ledger_discarded": 1,
        "ledger_torn_tail": False,
        "rebuilt_at": I._iso(I._now()),
    }), encoding="utf-8")
    with pytest.raises(P.WipKeyMissing) as exc:
        P.read_wip(path)
    assert detail in str(exc.value)


# --------------------------------------------------------------------------
# 3. readers fail closed on a missing wip key (the falsifier's producer half)
# --------------------------------------------------------------------------

def test_read_wip_refuses_missing_key(root: Path):
    path = root / "state" / "queue.json"
    path.write_text(json.dumps({"rebuilt_at": I._iso(I._now()), "rebuilt_by": "someone",
                                "source": "hand", "items": []}), encoding="utf-8")
    with pytest.raises(P.WipKeyMissing, match=r"missing.*wip"):
        P.read_wip(path)


def test_read_wip_refuses_missing_wip_degraded(root: Path) -> None:
    path = root / "state" / "queue.json"
    path.write_text(json.dumps({
        "wip": 0,
        "wip_cap": 4,
        "wip_definition": "x",
        "rebuilt_at": I._iso(I._now()),
    }), encoding="utf-8")
    with pytest.raises(P.WipKeyMissing, match=r"missing.*wip_degraded"):
        P.read_wip(path)


def test_read_wip_refuses_absent_file(root: Path):
    with pytest.raises(P.WipKeyMissing, match="absent"):
        P.read_wip(root / "state" / "queue.json")


def test_read_wip_refuses_stale_snapshot(root: Path):
    stale_ts = "2020-01-01T00:00:00Z"
    path = root / "state" / "queue.json"
    path.write_text(json.dumps({"wip": 1, "wip_cap": 4, "wip_definition": "x",
                                "wip_degraded": False,
                                "rebuilt_at": stale_ts, "rebuilt_by": "publish_queue",
                                "items": []}), encoding="utf-8")
    with pytest.raises(P.WipKeyMissing, match="STALE"):
        P.read_wip(path)


def test_read_wip_accepts_fresh_valid_snapshot(root: Path):
    _write_ledger(root, [_ev("proposed", "sha256:aaa"), _ev("admitted", "sha256:aaa")])
    q, msg = P.publish(root)
    assert q is not None, msg
    got = P.read_wip(root / "state" / "queue.json")
    assert got["wip"] == 1
    assert got["wip_degraded"] is False


def test_degraded_integration_summary_uses_none_instead_of_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integ, root, _repo = _integration_fixture(tmp_path, apply=True)
    (root / "ledger.jsonl").write_text(
        _ev("proposed", "sha256:visible") + '\n"damaged event"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(I, "read_governor", lambda _root: I.Governor(True, wip_cap=4))

    assert integ.iterate() == 0
    assert integ.summary["wip"] is None
    assert integ.summary["wip_degraded"] is True
    assert integ.summary["queue"]["wip"] is None
    assert integ.summary["queue"]["wip_degraded"] is True


# --------------------------------------------------------------------------
# 4. importability
# --------------------------------------------------------------------------

def test_module_is_importable_and_uses_the_real_rebuild_queue():
    assert P.I.rebuild_queue is I.rebuild_queue
    assert P.I.LedgerView is I.LedgerView


# --------------------------------------------------------------------------
# 5. carrier enumeration — publish_queue.py must be the ONLY place that ever
#    writes state/queue.json to disk (the clone-family this proposal closes:
#    integration.py used to carry a second, near-identical rebuild+write).
# --------------------------------------------------------------------------

def test_integration_py_delegates_the_write_to_publish_queue():
    src = (PKG / "bridge" / "integration.py").read_text(encoding="utf-8")
    # the only literal path segment that writes queue.json to disk
    assert 'root / "state" / "queue.json"' not in src, (
        "integration.py must not write state/queue.json directly any more — "
        "it must delegate to bridge.publish_queue so there is exactly one writer")
    assert "publish_queue" in src, "integration.py must delegate to bridge.publish_queue"


def test_publish_queue_is_the_sole_write_atomic_caller_for_queue_json():
    pq_src = (PKG / "bridge" / "publish_queue.py").read_text(encoding="utf-8")
    integ_src = (PKG / "bridge" / "integration.py").read_text(encoding="utf-8")
    assert pq_src.count('write_atomic(root / "state" / "queue.json"') == 1
    assert 'write_atomic(root / "state" / "queue.json"' not in integ_src


# --------------------------------------------------------------------------
# 6. cross-lineage review findings (grok-4.5/xai) — F1-F6, each a regression
#    test that fails on the pre-fix code and passes after it.
# --------------------------------------------------------------------------

def _integration_fixture(tmp_path: Path, *, apply: bool) -> tuple[I.Integration, Path, Path]:
    """A minimal, healthy Integration() an iterate() can actually run end to end."""
    root = tmp_path / "lq"
    for sub in ("proposals", "candidates", "findings", "inquiries", "rejected", "parked",
               "state", "receipts"):
        (root / sub).mkdir(parents=True)
    (root / "ledger.jsonl").write_text("")
    now = I._iso(I._now())
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": now, "wip_cap": 4, "disk_free_gb": 100, "disk_free_gb_min": 20,
        "load_avg_1m": 0.5, "load_avg_1m_max": 20, "metered_usd": {},
        "subscription": {"accounts": {}},
    }), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    schema_dir = PKG / "schema"
    integ = I.Integration(root=root, repo=repo, gate_ws=repo, schema_dir=schema_dir, apply=apply)
    return integ, root, repo


def test_F1_iterate_does_not_raise_nameerror_and_publishes(tmp_path: Path):
    """grok-4.5 (xai) cross-lineage review, live repro: integration.py:1354
    referenced `fresh` after the block that built it was deleted -- every
    --apply tick that passed the governor raised NameError, so the ledger
    could advance while queue.json went stale silently (no traceback
    surfaces to a human in a supervised loop; it just never updates).
    """
    integ, root, _repo = _integration_fixture(tmp_path, apply=True)
    rc = integ.iterate()  # must not raise
    assert rc == 0
    q = json.loads((root / "state" / "queue.json").read_text())
    assert q["rebuilt_by"] == "publish_queue"
    assert "wip" in q and "wip_cap" in q and "wip_definition" in q


def test_F2_dry_run_never_touches_queue_json(tmp_path: Path):
    """grok-4.5 finding: the old write path went through Integration._write_json,
    which no-ops when apply=False. The naive F1 fix (calling publish_from
    unconditionally) would make dry-run mode mutate the live queue.json --
    contradicting the run banner's "writes nothing" promise and racing the
    300s timer publisher. apply=False must leave state/queue.json untouched.
    """
    integ, root, _repo = _integration_fixture(tmp_path, apply=False)
    qpath = root / "state" / "queue.json"
    assert not qpath.exists()
    rc = integ.iterate()
    assert rc == 0
    assert not qpath.exists(), "DRY-RUN must never write state/queue.json"
    assert not qpath.with_suffix(".json.tmp").exists()


def test_F2_dry_run_still_reports_the_computed_queue_in_summary(tmp_path: Path):
    """Dry-run should still COMPUTE (for the report) even though it must not WRITE."""
    integ, root, _repo = _integration_fixture(tmp_path, apply=False)
    integ.iterate()
    assert "queue" in integ.summary
    assert integ.summary["queue"]["wip"] == 0


def test_F3_end_to_end_iterate_regression(tmp_path: Path):
    """The carrier-enumeration lesson: a syntactic "the old string is gone"
    check cannot fail at runtime and is not evidence a code path actually
    runs. This drives the real Integration.iterate() end to end (grok's
    F3_test.py minimal repro, hardened) -- it must exercise the exact call
    site F1 crashed at.
    """
    integ, root, _repo = _integration_fixture(tmp_path, apply=True)
    integ.iterate()
    q = json.loads((root / "state" / "queue.json").read_text())
    assert q["rebuilt_by"] == "publish_queue"


def test_F4_docstrings_do_not_claim_prompts_were_landed():
    """publish_queue.py's module docstring used to claim the PROMPT-*.md files
    say "run publish_queue.py" -- false after the 6146e1c revert. Docs must
    describe what the branch actually contains, not what was drafted and
    withheld.
    """
    src = (PKG / "bridge" / "publish_queue.py").read_text(encoding="utf-8")
    assert 'now say "run publish_queue.py"' not in src
    assert "no `PROMPT-*-loop.md` tells any loop to call this file" in src


def test_F4_integration_comment_does_not_overclaim_prompt_wiring():
    src = (PKG / "bridge" / "integration.py").read_text(encoding="utf-8")
    # must not claim the prompts were updated by this candidate
    assert "PROMPT-implementer-loop.md" not in src
    assert "PROMPT-planning-loop.md" not in src


def test_F5_bootstrap_sh_is_a_disclosed_seed_writer_not_hidden():
    """bootstrap.sh:71 creates queue.json if empty (one-time repo setup, not a
    steady-state race) -- a second, undiffed writer the review flagged. This
    candidate does not touch bootstrap.sh (out of its declared paths), so the
    fix is disclosure: no doc may claim "exactly ONE write_atomic call site
    repo-wide" -- only "sole RUNTIME writer" / "sole steady-state writer".
    """
    bootstrap = (PKG / "bootstrap.sh").read_text(encoding="utf-8")
    assert 'queue.json' in bootstrap, "bootstrap.sh's seed line moved or was removed unexpectedly"
    pq_src = (PKG / "bridge" / "publish_queue.py").read_text(encoding="utf-8")
    assert "bootstrap.sh" in pq_src, "publish_queue.py must disclose the bootstrap seed exception"
    assert "SOLE writer" not in pq_src, (
        "must not claim unqualified sole writer once bootstrap.sh's seed write is known")


def test_F6_read_wip_rejects_non_int_wip():
    """wip: "0" (a string) used to pass read_wip's presence-only check untyped."""
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "queue.json"
    p.write_text(json.dumps({"wip": "0", "wip_cap": 4, "wip_definition": "x",
                             "wip_degraded": False,
                             "rebuilt_at": I._iso(I._now())}), encoding="utf-8")
    with pytest.raises(P.WipKeyMissing, match="not int"):
        P.read_wip(p)


def test_F6_read_wip_cap_honours_an_explicit_zero(tmp_path: Path):
    """wip_cap: 0 (deliberately halt every producer) used to fall back to the
    default 4 via `budget.get("wip_cap") or default`, silently ignoring an
    operator's zero -- the exact favourable-absence shape this whole
    candidate exists to close, one function away.
    """
    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    (root / "state" / "budget.json").write_text(json.dumps({"wip_cap": 0}), encoding="utf-8")
    assert P.read_wip_cap(root) == 0


# --------------------------------------------------------------------------
# 7. F7 (round-3 cross-lineage review) -- the same falsy-zero shape F6 closed
#    in publish_queue.read_wip_cap(), found in its sibling integration.
#    read_governor(): the two publishers (300s timer vs. this adapter's own
#    --apply tick) could stamp disagreeing wip_cap into the same queue.json.
# --------------------------------------------------------------------------

def test_F7_read_governor_honours_an_explicit_wip_cap_zero(tmp_path: Path):
    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": I._iso(I._now()), "wip_cap": 0,
        "disk_free_gb": 100, "disk_free_gb_min": 20,
        "load_avg_1m": 0.5, "load_avg_1m_max": 20,
        "metered_usd": {}, "subscription": {"accounts": {}},
    }), encoding="utf-8")
    gov = I.read_governor(root)
    assert gov.wip_cap == 0, f"read_governor collapsed an explicit wip_cap:0 to {gov.wip_cap}"


def test_F7_timer_path_and_apply_tick_path_agree_on_an_explicit_zero_cap(tmp_path: Path):
    """The actual failure mode: two independently-triggered publishers
    (the 300s timer via publish_queue.read_wip_cap, and Integration.iterate
    via read_governor) must not stamp disagreeing wip_cap into the same file
    depending on which happened to run last.
    """
    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": I._iso(I._now()), "wip_cap": 0,
        "disk_free_gb": 100, "disk_free_gb_min": 20,
        "load_avg_1m": 0.5, "load_avg_1m_max": 20,
        "metered_usd": {}, "subscription": {"accounts": {}},
    }), encoding="utf-8")
    gov = I.read_governor(root)
    assert P.read_wip_cap(root) == gov.wip_cap == 0
