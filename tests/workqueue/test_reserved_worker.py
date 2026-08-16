"""Reserved workers: WORKER-DECLARED and FAIL CLOSED.

A reserved worker declares `--reserved-for <owner>` on its OWN command line and
then claims ONLY units whose `submitted_by` equals that owner. There is NO
server-side `reserved_machines` config-map (it was dropped: it FAILED OPEN on
every unresolved-owner path — empty owner, missing/malformed YAML swallowed to
{}, machine_id drift vs the box's real hostname, null, and a non-dict config that
raised AttributeError on EVERY claim = a pool-wide claim outage).

HONEST SCOPE — resource hygiene, NOT a security boundary. A reserved worker
claims ONLY its declared owner's OWN submissions, to keep other people's pool
work off a machine reserved for one person under COOPERATIVE use. It is NOT a
hard boundary: attribution (`submitted_by`) is self-declared under the single
shared pool token, so a token-holder who deliberately submits AS the owner CAN
still land work on the box (see ``test_forged_submitted_by_lands_on_reserved_box_known_limitation``).
This prevents accidental contention, not deliberate forging. A hard boundary
requires per-submitter auth tokens (out of scope).

The tests below convert the two independent cross-lineage REQUEST-CHANGES repros
(opus F1/F2/F3/F5, gemini finding-2/3) into passing regressions proving the
redesign FAILS CLOSED: a broken reservation claims NOTHING, a garbage owner
value is coerced to the safe side, and a worker with a blank reservation refuses
to start rather than silently become unrestricted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omniagentos.workqueue import worker as worker_mod
from omniagentos.workqueue.server import create_app
from tests.workqueue.conftest import at, submit

_TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


# --------------------------------------------------------------------------- #
# (a) FAIL CLOSED at startup: a blank reservation refuses to start.            #
#     opus F1 (empty owner) — repro turned into a passing regression.          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t \n "])
def test_worker_refuses_to_start_on_blank_reserved_for(blank):
    # resolve_reserved_for is the single fail-closed chokepoint main() calls
    # BEFORE opening the queue, so a broken reservation claims NOTHING.
    with pytest.raises(SystemExit) as exc:
        worker_mod.resolve_reserved_for(blank)
    assert exc.value.code != 0


def test_main_exits_nonzero_before_claiming_on_blank_reserved_for(tmp_path, monkeypatch):
    """A worker invoked with --reserved-for '' aborts before it can claim.

    We prove "claims nothing" structurally: open_queue is patched to explode, so
    if main() reached the claim path at all the test would see that error instead
    of the fail-closed SystemExit.
    """

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("open_queue reached: worker did not fail closed")

    monkeypatch.setattr(worker_mod, "open_queue", _boom)
    with pytest.raises(SystemExit) as exc:
        worker_mod.main(["--db", str(tmp_path / "q.sqlite3"), "--reserved-for", "   ", "--once"])
    assert exc.value.code != 0


def test_absent_reserved_for_is_a_normal_unrestricted_worker():
    # Omitting the flag (None) is an EXPLICIT, visible operator choice — not a
    # silent default — and yields a normal unrestricted worker.
    assert worker_mod.resolve_reserved_for(None) is None


def test_valid_reserved_for_is_trimmed():
    assert worker_mod.resolve_reserved_for("  owner ") == "owner"


# --------------------------------------------------------------------------- #
# (a2) THE ENV-VAR CHANNEL must fail closed exactly like the CLI channel.      #
#      gemini F1 + opus B-ENV regression: the argparse default used            #
#      `os.environ.get("WQ_RESERVED_FOR") or None`, which collapsed a          #
#      SET-BUT-EMPTY env value ("") to None == flag-absent == UNRESTRICTED,     #
#      bypassing resolve_reserved_for entirely (fail OPEN — the classic         #
#      `export WQ_RESERVED_FOR="$UNSET_VAR"` accident). The default is now      #
#      plain os.environ.get(...) so "" survives to the fail-closed chokepoint.  #
# --------------------------------------------------------------------------- #
def test_env_empty_wq_reserved_for_survives_to_parser(monkeypatch):
    # The set-but-empty env value must NOT be collapsed to None by the parser.
    monkeypatch.setenv("WQ_RESERVED_FOR", "")
    args = worker_mod.build_parser().parse_args([])
    assert args.reserved_for == ""  # NOT None — "" is a broken reservation, not absence


def test_env_empty_wq_reserved_for_fails_closed(monkeypatch):
    # End-to-end: set-but-empty env resolves like --reserved-for '' → SystemExit,
    # symmetric with the CLI blank path (never a silent unrestricted worker).
    monkeypatch.setenv("WQ_RESERVED_FOR", "")
    args = worker_mod.build_parser().parse_args([])
    with pytest.raises(SystemExit) as exc:
        worker_mod.resolve_reserved_for(args.reserved_for)
    assert exc.value.code != 0


def test_env_unset_wq_reserved_for_is_unrestricted(monkeypatch):
    # UNSET (flag genuinely absent) resolves to None = a normal unrestricted
    # worker: a VISIBLE choice, not a broken reservation. This is the ONLY
    # unrestricted path — distinct from set-but-empty above.
    monkeypatch.delenv("WQ_RESERVED_FOR", raising=False)
    args = worker_mod.build_parser().parse_args([])
    assert args.reserved_for is None
    assert worker_mod.resolve_reserved_for(args.reserved_for) is None


def test_env_set_wq_reserved_for_reserves(monkeypatch):
    monkeypatch.setenv("WQ_RESERVED_FOR", "owner")
    args = worker_mod.build_parser().parse_args([])
    assert worker_mod.resolve_reserved_for(args.reserved_for) == "owner"


# --------------------------------------------------------------------------- #
# (b) DENY PATH (adversarial): reserved-for owner claims ONLY owner's units.       #
# --------------------------------------------------------------------------- #
def test_reserved_worker_claims_only_its_owner_never_a_dev(store):
    store.enqueue(submit("dev-work", submitted_by="bob"))
    store.enqueue(submit("owner-work", submitted_by="owner"))

    claimed = store.claim("any-machine", "w1", [], now=at(0), reserved_for="owner")
    assert claimed is not None
    assert claimed["unit"]["submitted_by"] == "owner"
    assert claimed["unit"]["idempotency_key"] == "owner-work"

    # owner's only unit is now in flight — the dev unit is NOT claimable by a
    # owner-reserved worker, ever, even though it is the only thing left queued.
    assert store.claim("any-machine", "w2", [], now=at(1), reserved_for="owner") is None


def test_reserved_worker_claims_nothing_when_only_a_dev_unit_is_queued(store):
    # gemini finding-2 repro: the deny path must hold even when the reserved box
    # is the ONLY worker and a dev unit is the ONLY work — fail closed, claim None.
    store.enqueue(submit("dev-only", submitted_by="alice"))
    assert store.claim("any-machine", "w1", [], now=at(0), reserved_for="owner") is None


# --------------------------------------------------------------------------- #
# (c) DEFAULT: an unreserved worker claims anything.                           #
# --------------------------------------------------------------------------- #
def test_unreserved_worker_claims_anything(store):
    store.enqueue(submit("dev-work", submitted_by="bob"))
    store.enqueue(submit("owner-work", submitted_by="owner"))

    first = store.claim("any-machine", "w1", [], now=at(0))
    second = store.claim("any-machine", "w2", [], now=at(1))
    assert first is not None and second is not None
    assert {first["unit"]["submitted_by"], second["unit"]["submitted_by"]} == {"bob", "owner"}


def test_owner_units_stay_claimable_by_an_unreserved_worker(store):
    # Reservation restricts the reserved WORKER; it does not pin the owner's unit
    # to it. An unreserved box may still claim owner's work.
    store.enqueue(submit("owner-work", submitted_by="owner"))
    claimed = store.claim("any-machine", "w1", [], now=at(0))
    assert claimed is not None
    assert claimed["unit"]["submitted_by"] == "owner"


# --------------------------------------------------------------------------- #
# (d) COERCE TO THE SAFE SIDE: a garbage reserved_for is treated as absent,    #
#     never crashes. This is the structural cure for the dropped config-map's  #
#     fail-open paths — opus F2 (malformed YAML → {}), F3 (machine_id drift),  #
#     F5 (null), and gemini finding-3 (non-dict config → AttributeError DoS).  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("garbage", [None, "", "   ", 123, 12.5, [], {}, object()])
def test_store_claim_coerces_non_string_reserved_for_to_unrestricted(store, garbage):
    store.enqueue(submit("dev-work", submitted_by="bob"))
    # A non-str / blank owner restricts NOTHING and never raises: the reserved
    # box behaves exactly like an unrestricted one, which is the safe direction.
    claimed = store.claim("any-machine", "w1", [], now=at(0), reserved_for=garbage)  # type: ignore[arg-type]
    assert claimed is not None
    assert claimed["unit"]["submitted_by"] == "bob"


def test_server_claim_coerces_non_string_reserved_for_and_never_crashes(store):
    # The wire path: server.py coerces with isinstance(str). A non-str body value
    # must not reach the SQL as a parameter and must not 500 — there is no config
    # -map left for a malformed value to DoS the whole pool with.
    store.enqueue(submit("dev-work", submitted_by="bob"))
    with TestClient(create_app(store=store, token=_TOKEN, reaper=False)) as client:
        resp = client.post(
            "/v1/claim",
            json={"machine_id": "m1", "worker_id": "w1", "labels": [], "reserved_for": 12345},
            headers=_AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["unit"]["submitted_by"] == "bob"


def test_server_claim_reserved_for_string_restricts(store):
    store.enqueue(submit("dev-work", submitted_by="bob"))
    store.enqueue(submit("owner-work", submitted_by="owner"))
    with TestClient(create_app(store=store, token=_TOKEN, reaper=False)) as client:
        resp = client.post(
            "/v1/claim",
            json={"machine_id": "m1", "worker_id": "w1", "labels": [], "reserved_for": "owner"},
            headers=_AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["unit"]["submitted_by"] == "owner"


# --------------------------------------------------------------------------- #
# (e) THE KNOWN, DOCUMENTED LIMITATION — asserted, so it is explicit, not      #
#     accidental. This is resource hygiene, NOT a security boundary.           #
# --------------------------------------------------------------------------- #
def test_forged_submitted_by_lands_on_reserved_box_known_limitation(store):
    """A unit whose submitted_by was set to 'owner' by ANOTHER submitter DOES land
    on a owner-reserved worker. This is INTENDED and DOCUMENTED, not a bug.

    submitted_by is self-declared under the single shared pool token — the queue
    cannot tell the operator's submission from a token-holder who typed
    ``wq enqueue --by owner``. Reservation stops ACCIDENTAL contention (a dev's own
    work, honestly attributed, stays off the box); it does not stop DELIBERATE
    forging. Closing this gap needs per-submitter auth tokens, which are out of
    scope for the resource-hygiene threat model.
    """
    # "forged" only in the sense that someone OTHER than owner submitted it as owner.
    store.enqueue(submit("forged-as-owner", submitted_by="owner"))
    claimed = store.claim("any-machine", "w1", [], now=at(0), reserved_for="owner")
    assert claimed is not None  # it lands — by design, under cooperative use
    assert claimed["unit"]["submitted_by"] == "owner"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
