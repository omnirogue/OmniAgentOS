"""The operator reconcile path for a receipt wedged on UNKNOWN.

A receipt parked on UNKNOWN fails its tick closed for ever: ``idem_release`` is
reached from exactly one place (``receipts._attempt`` on ``EffectUnavailable``),
so there was no way back except SQL by hand. This lane routes ALL ambiguous
transport failures into UNKNOWN deliberately — that is what stops a possibly
billed call being re-issued — so it raises the incidence of the wedge and owes
it a recovery path.

The bar for every test here is the same one the rest of this subsystem is held
to: **releasing a claim is releasing a licence to spend money again**, so the
tool must be impossible to fire by accident, must refuse outright the one case
that would destroy evidence (a COMPLETED receipt), and must leave a record of
who decided and why.

"Impossible to fire by accident" was, for one commit, only true of the CLI: the
provider-check requirement lived in ``_cmd_release``, so an importer could call
the release function directly and skip it. ``THE INTERLOCK`` below is the
regression bar for that — it tests the FUNCTION, not the argparse wrapper,
because the wrapper was never the thing at risk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler import loop_receipts_admin as admin
from tests.support.db_template import make_store

CLAIMED_KEY = "loop:render_probe_tpl:render_probe:call:replicate.generate:2026-08-02"
COMPLETED_KEY = "loop:render_probe_tpl:render_probe:call:replicate.generate:2026-08-01"


@pytest.fixture
def store(tmp_path: Path) -> Any:
    path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, path)
    yield handle
    handle.close()


@pytest.fixture
def wedged(store: Any) -> Any:
    """One claimed-never-completed receipt, and one properly completed one."""
    store.idem_insert(CLAIMED_KEY, "render_probe", "call")
    store.idem_insert(COMPLETED_KEY, "render_probe", "call")
    store.idem_complete(COMPLETED_KEY, json.dumps({"__loop_receipt__": 1, "state": "succeeded"}))
    return store


# --------------------------------------------------------------------------
# Finding the wedge
# --------------------------------------------------------------------------


def test_only_claimed_receipts_are_offered_for_release(wedged: Any) -> None:
    """A COMPLETED receipt is the record of an effect that ran. It is not a wedge."""
    rows = admin._claimed_receipts(wedged)

    keys = {row["key"] for row in rows}
    assert CLAIMED_KEY in keys
    assert COMPLETED_KEY not in keys, (
        "a completed receipt was listed as reconcilable; an operator offered it "
        "would be offered the deletion of a real effect's record"
    )


def test_a_runner_receipt_is_not_a_loop_receipt(wedged: Any) -> None:
    """The tool's blast radius is the loops namespace and nothing else."""
    wedged.idem_insert("run:abc123:step-4", "run-abc123", "step-4")

    keys = {row["key"] for row in admin._claimed_receipts(wedged)}
    assert "run:abc123:step-4" not in keys


def test_listing_can_be_narrowed_to_old_claims(wedged: Any) -> None:
    """A fresh claim is probably a call in flight, not a wedge."""
    assert admin._claimed_receipts(wedged, older_than_minutes=60.0) == []
    assert admin._claimed_receipts(wedged, older_than_minutes=0.0)


# --------------------------------------------------------------------------
# Releasing one, and the four ways it must refuse
# --------------------------------------------------------------------------


def test_a_release_needs_a_reason_and_an_operator(wedged: Any) -> None:
    released, message = admin._release_claim(
        wedged, key=CLAIMED_KEY, checked=admin._ProviderChecked(operator="owner", reason="")
    )
    assert not released
    assert "reason" in message

    released, message = admin._release_claim(
        wedged, key=CLAIMED_KEY, checked=admin._ProviderChecked(operator="", reason="checked")
    )
    assert not released
    assert "operator" in message

    assert wedged.idem_get(CLAIMED_KEY) is not None, "a refused release must change nothing"


def test_a_completed_receipt_can_never_be_released(wedged: Any) -> None:
    """The one refusal that protects evidence rather than money.

    ``idem_release``'s SQL carries ``result_json IS NULL``, so this is belt and
    braces — but an operator who types the wrong key deserves to be told why,
    not to get a silent no-op.
    """
    released, message = admin._release_claim(
        wedged,
        key=COMPLETED_KEY,
        checked=admin._ProviderChecked(operator="owner", reason="looks stuck to me"),
    )

    assert not released
    assert "COMPLETED" in message
    assert wedged.idem_get(COMPLETED_KEY) is not None


def test_a_non_loop_key_is_refused(wedged: Any) -> None:
    wedged.idem_insert("run:abc123:step-4", "run-abc123", "step-4")
    released, _ = admin._release_claim(
        wedged,
        key="run:abc123:step-4",
        checked=admin._ProviderChecked(operator="owner", reason="checked"),
    )
    assert not released
    assert wedged.idem_get("run:abc123:step-4") is not None


def test_a_missing_key_is_refused(wedged: Any) -> None:
    released, message = admin._release_claim(
        wedged,
        key="loop:t:i:n:tool:nope",
        checked=admin._ProviderChecked(operator="owner", reason="checked"),
    )
    assert not released
    assert "no receipt" in message


def test_a_release_removes_the_claim_and_records_who_did_it(wedged: Any) -> None:
    released, message = admin._release_claim(
        wedged,
        key=CLAIMED_KEY,
        checked=admin._ProviderChecked(
            operator="owner", reason="replicate dashboard shows no prediction for this key"
        ),
    )

    assert released, message
    assert wedged.idem_get(CLAIMED_KEY) is None, "the claim must actually be gone"

    events = wedged.get_events_after(0, types=[admin.EVENT_TYPE], limit=50)
    releases = [e for e in events if e["action"] == admin.ACTION_RELEASED]
    assert len(releases) == 1, "a manual money decision must leave exactly one record"
    payload = json.loads(releases[0]["payload_json"])
    assert payload["operator"] == "owner"
    assert "replicate dashboard" in payload["reason"]
    assert releases[0]["target_id"] == CLAIMED_KEY


def test_the_release_is_recorded_even_when_the_delete_finds_nothing(
    wedged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit first, then delete: a crash between them leaves the row, not a mystery.

    The other order would allow an unexplained deletion in a money-adjacent
    table, which is the one outcome an audit trail exists to make impossible.
    """
    monkeypatch.setattr(wedged, "idem_release", lambda _key: False)

    released, message = admin._release_claim(
        wedged, key=CLAIMED_KEY, checked=admin._ProviderChecked(operator="owner", reason="checked")
    )

    assert not released
    assert "records the attempt" in message
    events = wedged.get_events_after(0, types=[admin.EVENT_TYPE], limit=50)
    assert [e for e in events if e["action"] == admin.ACTION_RELEASED]


def test_an_unrecordable_release_does_not_happen(
    wedged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the decision cannot be written down, the row is not touched."""

    def _no_events(**_kwargs: Any) -> int:
        raise RuntimeError("events table is unavailable")

    monkeypatch.setattr(wedged, "insert_event", _no_events)
    released, message = admin._release_claim(
        wedged, key=CLAIMED_KEY, checked=admin._ProviderChecked(operator="owner", reason="checked")
    )

    assert not released
    assert "audit" in message
    assert wedged.idem_get(CLAIMED_KEY) is not None


# --------------------------------------------------------------------------
# THE INTERLOCK: the provider check is a property of the FUNCTION
# --------------------------------------------------------------------------


def test_a_release_is_impossible_without_stating_the_provider_was_checked(wedged: Any) -> None:
    """The acceptance test for this module. A caller cannot skip the human.

    This is the shape of the call that used to work: the confirmation was
    enforced in ``_cmd_release``, so ANY importer could reach straight past it
    and drop a claim that may already have been billed — the double-billing
    defect, re-created from inside the tool built to prevent it.

    It must now be impossible to express. Not "discouraged", not "the CLI stops
    you": the call does not run.
    """
    with pytest.raises(TypeError):
        admin._release_claim(wedged, key=CLAIMED_KEY)  # type: ignore[call-arg]

    # ...including under the exact pre-fix signature, which no longer exists.
    with pytest.raises(TypeError):
        admin._release_claim(  # type: ignore[call-arg]
            wedged, key=CLAIMED_KEY, reason="checked", operator="owner"
        )

    assert wedged.idem_get(CLAIMED_KEY) is not None, (
        "a claim was released by a caller that never said a human checked the provider"
    )


@pytest.mark.parametrize("forgery", [True, 1, "i-have-checked-the-provider", None, object()])
def test_no_truthy_value_stands_in_for_the_human(wedged: Any, forgery: Any) -> None:
    """A bool would let ``checked=some_config_flag`` satisfy a money decision.

    The requirement is a value somebody constructed on purpose, so that every
    assertion in this repo that a human checked the provider is greppable by
    type name. Truthiness is not an assertion about the world.
    """
    with pytest.raises(TypeError):
        admin._release_claim(wedged, key=CLAIMED_KEY, checked=forgery)

    assert wedged.idem_get(CLAIMED_KEY) is not None


def test_the_confirmation_is_checked_before_anything_is_read_or_written(
    wedged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller with no confirmation must not even reach the database.

    If the interlock sat after the lookups, a release attempt would still be a
    write path one reordered edit away from firing.
    """

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the store was touched by a call with no provider check")

    monkeypatch.setattr(wedged, "idem_get", _boom)
    monkeypatch.setattr(wedged, "idem_release", _boom)
    monkeypatch.setattr(wedged, "insert_event", _boom)

    with pytest.raises(TypeError):
        admin._release_claim(wedged, key=CLAIMED_KEY, checked=True)


def test_the_only_public_name_in_this_module_is_main() -> None:
    """``main`` is the entry point; everything else is a part of it.

    A public ``release_claim`` is not merely untidy — it is an importable
    destructive call, and it is what let this module read as "wired" to the
    reachability gate (which matches bare symbol names, and found the unrelated
    ``release_claim`` methods on ``CollabStore`` and the gate-evidence store).
    Keeping the surface at one name is what the exemption in
    ``devtasks/REACHABILITY-EXEMPT.txt`` now asserts, so it is tested here.
    """
    public = {
        name
        for name, value in vars(admin).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == admin.__name__
    }

    assert public == {"main"}, f"unexpected public callables on the admin module: {public}"


# --------------------------------------------------------------------------
# The CLI surface: the dry run is the OUTER interlock, not the only one
# --------------------------------------------------------------------------


def _run_cli(store: Any, argv: list[str]) -> int:
    args = admin._build_parser().parse_args(argv)
    return int(args.func(args, store))


def test_release_without_the_confirmation_flag_is_a_dry_run(
    wedged: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default must be "show me", never "do it"."""
    code = _run_cli(wedged, ["release", "--key", CLAIMED_KEY, "--operator", "owner", "--reason", "x"])

    assert code == 2
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "pays for it twice" in out, "the dry run must state the actual hazard"
    assert wedged.idem_get(CLAIMED_KEY) is not None, "a dry run must change nothing"


def test_the_confirmation_flag_performs_the_release(wedged: Any) -> None:
    code = _run_cli(
        wedged,
        [
            "release",
            "--key",
            CLAIMED_KEY,
            "--operator",
            "owner",
            "--reason",
            "checked the provider; nothing was created",
            "--i-have-checked-the-provider",
        ],
    )

    assert code == 0
    assert wedged.idem_get(CLAIMED_KEY) is None


def test_list_reports_the_wedge_in_json(wedged: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run_cli(wedged, ["list", "--json"])
    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["key"] for row in rows] == [CLAIMED_KEY]


def test_show_prints_the_state_an_operator_has_to_judge(
    wedged: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(wedged, ["show", "--key", CLAIMED_KEY]) == 0
    assert "CLAIMED (never completed)" in capsys.readouterr().out

    assert _run_cli(wedged, ["show", "--key", COMPLETED_KEY]) == 0
    assert "COMPLETED" in capsys.readouterr().out
