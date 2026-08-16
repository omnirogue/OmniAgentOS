"""A park sidecar's ID FORMAT must never take the whole build fan-out to zero.

`spawn_builders._queue_state` used to require every file in `parked/` to carry
a full `sha256:<64 hex>` id agreeing byte-for-byte with its filename, and to
raise `_SelectionRefused` — which aborts the ENTIRE selection — for any that
did not. Measured against the live queue on 2026-08-11: 24 of 64 sidecars use
the estate's other, sanctioned id conventions (short hex, descriptive, and
`parked/`-namespaced slugs), so ONE ordinary artifact meant zero builders for
44 otherwise-selectable proposals.

These tests bind the fix in both directions:

  * an unrecognised id FORMAT excludes conservatively (both the body id and
    the filename-derived id are parked) and selection proceeds;
  * a sidecar that cannot be INTERPRETED at all — unreadable, corrupt,
    non-object, or naming no id — still refuses, because there is no way to
    know what it parks and guessing favourably is the failure this queue
    exists to refuse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from bridge import canonical  # noqa: E402
from bridge import spawn_builders as sb  # noqa: E402

# The three id shapes the live `parked/` directory actually carries alongside
# full sha256s, verbatim from var/loopqueue/parked (2026-08-11).
SHORT_ID = "sha256:d4b10e97d5e3f"
DESCRIPTIVE_ID = "sha256:loop-accounts-weekly-exhausted-20260810"
NAMESPACED_ID = "parked/billing-harm-9eddb04dd5-0810"


@pytest.fixture(autouse=True)
def _no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """`alert_once` pushes to ntfy when OMNI_NTFY_URL is set; tests must not."""
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)


@pytest.fixture
def loops_root(tmp_path: Path) -> Path:
    root = tmp_path / "loopqueue"
    for name in ("claims", "state", "candidates", "proposals", "parked"):
        (root / name).mkdir(parents=True)
    (root / "ledger.jsonl").touch()
    return root


def _proposal(tag: str) -> dict:
    payload = {
        "urgency": "p1", "benefit_class": "throughput", "impact": "high",
        "risk_level": "medium", "problem": f"build {tag}",
        "falsifier": f"{tag} is implemented", "implementation_plan": "implement it",
        "effort": "s", "new_paths": [], "repo": "pipeline",
    }
    ident = canonical.content_id(payload)
    return {
        "contract": "v1.1", "kind": "proposal", "title": f"proposal {tag}",
        "created_at": "2026-08-10T00:00:00Z",
        "producer": {"role": "external", "actor": "test", "lineage": "test"},
        "paths": ["README.md"], "payload": payload, "id": ident, "priority": 0,
    }


def _write_proposal(root: Path, item: dict) -> None:
    name = f"{item['id'].replace(':', '_', 1)}.json"
    (root / "proposals" / name).write_text(json.dumps(item))


def _write_park(root: Path, filename: str, body: object) -> Path:
    path = root / "parked" / filename
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def _select(root: Path, *, alerts: list[str] | None = None,
            persist: bool = False) -> list[dict]:
    return sb._admitted_unclaimed(root, persist_alerts=persist,
                                  alerts=[] if alerts is None else alerts)


def _parked(root: Path) -> set[str]:
    return sb._queue_state(root)[1]


# --------------------------------------------------------------------------
# RED-FIRST: one odd-format sidecar used to abort the whole selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("ident", "filename"), [
    (SHORT_ID, "sha256_d4b10e97d5e3f.json"),
    (DESCRIPTIVE_ID, "sha256_loop-accounts-weekly-exhausted-20260810.json"),
    (NAMESPACED_ID, "billing-harm-9eddb04dd5-0810.json"),
])
def test_odd_id_sidecar_does_not_starve_the_fan_out(
        loops_root: Path, ident: str, filename: str) -> None:
    """The whole defect in one assertion: an unrelated proposal must still be
    selectable when a sidecar's id is not a full sha256. On the base commit
    every parametrisation raises `_SelectionRefused` and selects NOTHING."""
    item = _proposal(f"survivor-{filename}")
    _write_proposal(loops_root, item)
    _write_park(loops_root, filename,
                {"id": ident, "kind": "park", "reason": "operator ruling owed"})

    alerts: list[str] = []
    assert [row["id"] for row in _select(loops_root, alerts=alerts)] == [item["id"]]
    # A conventional filename for that id is not an anomaly -- no alert noise.
    assert alerts == []
    # ...and the odd id is still excluded, which is the point of reading parked/.
    assert ident in _parked(loops_root)


@pytest.mark.parametrize(("ident", "filename"), [
    (SHORT_ID, "sha256_d4b10e97d5e3f.json"),
    (NAMESPACED_ID, "billing-harm-9eddb04dd5-0810.json"),
])
def test_odd_id_sidecar_parks_both_body_and_filename_keys(
        loops_root: Path, ident: str, filename: str) -> None:
    _write_park(loops_root, filename, {"id": ident, "kind": "park"})
    parked = _parked(loops_root)
    assert ident in parked
    assert sb._park_ident_from_name(filename) in parked


def test_park_sidecar_excludes_by_body_id_when_filename_disagrees(
        loops_root: Path) -> None:
    """Body id names a real proposal under a filename that does not encode it.
    Over-excluding costs one build; under-excluding builds a parked item."""
    item = _proposal("body-key")
    _write_proposal(loops_root, item)
    _write_park(loops_root, "operator-ruling-owed-0811.json",
                {"id": item["id"], "kind": "park"})

    alerts: list[str] = []
    assert _select(loops_root, alerts=alerts) == []
    assert len(alerts) == 1
    assert "disagrees with body id" in alerts[0]
    assert "BOTH ids are treated as parked" in alerts[0]


def test_park_sidecar_excludes_by_filename_when_body_id_differs(
        loops_root: Path) -> None:
    """Mirror case: the FILENAME encodes a real proposal id while the body
    names something else entirely. Both keys are parked, so the proposal is
    still withheld from a builder."""
    item = _proposal("name-key")
    _write_proposal(loops_root, item)
    _write_park(loops_root, f"{item['id'].replace(':', '_', 1)}.json",
                {"id": NAMESPACED_ID, "kind": "park"})

    alerts: list[str] = []
    assert _select(loops_root, alerts=alerts) == []
    assert len(alerts) == 1
    parked = _parked(loops_root)
    assert item["id"] in parked and NAMESPACED_ID in parked


def test_conventional_live_filenames_raise_no_alerts(loops_root: Path) -> None:
    """The shapes the live directory uses must be silent, or the first run
    would report 26 perfectly ordinary artifacts as defects."""
    full = "sha256:" + "a" * 64
    _write_park(loops_root, f"sha256_{'a' * 64}.json", {"id": full, "kind": "park"})
    _write_park(loops_root, f"sha256_{'a' * 64}.parkinfo.json",
                {"id": full, "kind": "proposal", "reason": "blocked-on-human"})
    _write_park(loops_root, "sha256_d4b10e97d5e3f.json",
                {"id": SHORT_ID, "kind": "proposal"})
    _write_park(loops_root, "billing-harm-9eddb04dd5-0810.json",
                {"id": NAMESPACED_ID, "kind": "park"})

    alerts: list[str] = []
    assert _select(loops_root, alerts=alerts) == []
    assert alerts == []


def test_disagreeing_sidecar_alerts_once_not_every_iteration(
        loops_root: Path) -> None:
    _write_park(loops_root, "odd-name-0811.json",
                {"id": "sha256:" + "b" * 64, "kind": "park"})
    for _ in range(3):
        _select(loops_root, persist=True)
    lines = [ln for ln in (loops_root / "ALERTS.md").read_text().splitlines()
             if "odd-name-0811.json" in ln]
    assert len(lines) == 1, lines


# --------------------------------------------------------------------------
# The boundary: a sidecar that cannot be INTERPRETED still refuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("filename", "body"), [
    ("corrupt.json", "{not json"),
    ("truncated.json", '{"id": "sha256:aaa'),
    ("list-body.json", [{"id": SHORT_ID}]),
    ("string-body.json", '"sha256:d4b10e97d5e3f"'),
    ("null-body.json", "null"),
    ("no-id.json", {"kind": "park", "reason": "operator ruling owed"}),
    ("null-id.json", {"id": None, "kind": "park"}),
    ("int-id.json", {"id": 17, "kind": "park"}),
    ("empty-id.json", {"id": "", "kind": "park"}),
    ("blank-id.json", {"id": "   ", "kind": "park"}),
])
def test_uninterpretable_sidecar_still_refuses_selection(
        loops_root: Path, filename: str, body: object) -> None:
    _write_proposal(loops_root, _proposal("victim"))
    _write_park(loops_root, filename, body)
    with pytest.raises(sb._SelectionRefused):
        _select(loops_root)


def test_unreadable_sidecar_still_refuses_selection(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError reading a sidecar is an instrument hole, not an absent park."""
    _write_park(loops_root, "sha256_" + "c" * 64 + ".json",
                {"id": "sha256:" + "c" * 64, "kind": "park"})

    real_read = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.parent.name == "parked":
            raise PermissionError("nope")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(sb._SelectionRefused):
        _select(loops_root)


def test_parked_dir_that_is_not_a_directory_still_refuses(tmp_path: Path) -> None:
    root = tmp_path / "loopqueue"
    for name in ("claims", "state", "proposals"):
        (root / name).mkdir(parents=True)
    (root / "ledger.jsonl").touch()
    (root / "parked").write_text("not a directory")
    with pytest.raises(sb._SelectionRefused):
        _select(root)


def test_ledger_hole_still_refuses_even_with_clean_sidecars(
        loops_root: Path) -> None:
    """The park relaxation must not have loosened the ledger gate beside it."""
    _write_park(loops_root, "sha256_d4b10e97d5e3f.json", {"id": SHORT_ID})
    (loops_root / "ledger.jsonl").write_text("{torn")
    with pytest.raises(sb._SelectionRefused):
        _select(loops_root)


# --------------------------------------------------------------------------
# id-shape helpers
# --------------------------------------------------------------------------


def test_park_ident_from_name_matches_the_estate_derivation() -> None:
    """Same rule janitor._parked_ids and integration.py use: stem, first
    `_` back to `:`.

    `.parkinfo` is asserted here as the LITERAL derivation the siblings
    produce — deliberately, because this helper is the comparison point
    against them. It is NOT the exclusion key; see the `_park_idents_from_name`
    tests below, which is where the safety property lives."""
    assert sb._park_ident_from_name("sha256_d4b10e97d5e3f.json") == SHORT_ID
    assert sb._park_ident_from_name(
        "billing-harm-9eddb04dd5-0810.json") == "billing-harm-9eddb04dd5-0810"
    assert sb._park_ident_from_name(
        f"sha256_{'d' * 64}.parkinfo.json") == f"sha256:{'d' * 64}.parkinfo"


def test_parkinfo_name_yields_the_bare_id_too() -> None:
    """RED-FIRST (cross-lineage BLOCKER, 2026-08-11): the park-reason carrier
    must contribute the id BENEATH `.parkinfo`, not only the literal stem."""
    full = f"sha256:{'d' * 64}"
    assert sb._park_idents_from_name(f"sha256_{'d' * 64}.parkinfo.json") == {
        f"{full}.parkinfo", full}
    # a name with no .parkinfo infix contributes exactly itself
    assert sb._park_idents_from_name("sha256_d4b10e97d5e3f.json") == {SHORT_ID}


def test_parkinfo_only_carrier_with_stale_body_still_excludes(
        loops_root: Path) -> None:
    """THE BLOCKER, end to end: a proposal parked ONLY by a `.parkinfo.json`
    whose body id is stale must NOT be handed to a builder.

    Before the fix the name key derived `<id>.parkinfo` and the body key was
    the stale value, so neither matched and the parked proposal was selected.
    """
    item = _proposal("parked-by-parkinfo-only")
    _write_proposal(loops_root, item)
    stem = item["id"].replace(":", "_", 1)
    _write_park(loops_root, f"{stem}.parkinfo.json",
                {"id": f"sha256:{'0' * 64}", "reason": "stale body id"})

    assert item["id"] in _parked(loops_root)
    assert [i["id"] for i in _select(loops_root)] == []


@pytest.mark.parametrize("exc", [
    IsADirectoryError(21, "Is a directory"),      # unwritable alert path
    TypeError("argument of type 'NoneType' is not iterable"),  # alerted.json = null
    UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
    RuntimeError("anything at all"),
])
def test_alert_failure_never_aborts_the_fan_out(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch,
        exc: Exception) -> None:
    """MAJOR (cross-lineage, 2026-08-11, rounds 1 AND 2): persisting the
    name/body-disagreement alert must not decide whether builders run.

    Round 1 closed only `OSError`. Round 2 reproduced two ordinary non-OSError
    escapes — `state/alerted.json` holding JSON `null` raises TypeError inside
    `claim.alert_once`, and a lone-surrogate body id raises UnicodeEncodeError
    on the ALERTS.md write — either of which aborted the whole fan-out for a
    sidecar that had just been successfully INTERPRETED. Parametrised rather
    than pinned to one type on purpose: the property is that NO ordinary
    exception from the telemetry call reaches the selection path."""
    item = _proposal("selectable-despite-alert-failure")
    _write_proposal(loops_root, item)
    # a readable, INTERPRETABLE sidecar whose name disagrees with its body
    _write_park(loops_root, "some-other-park-0811.json", {"id": SHORT_ID})

    def _boom(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(sb._claim, "alert_once", _boom)

    alerts: list[str] = []
    got = sb._admitted_unclaimed(loops_root, persist_alerts=True, alerts=alerts)
    assert [i["id"] for i in got] == [item["id"]]
    assert any("could not persist park-name alert" in a for a in alerts)


def test_alert_failure_still_lets_keyboardinterrupt_through(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The blanket catch is `Exception`, never `BaseException`: an operator's
    Ctrl-C must still stop the loop rather than being swallowed as a failed
    alert."""
    _write_proposal(loops_root, _proposal("interruptible"))
    _write_park(loops_root, "some-other-park-0811.json", {"id": SHORT_ID})

    def _boom(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sb._claim, "alert_once", _boom)
    with pytest.raises(KeyboardInterrupt):
        sb._admitted_unclaimed(loops_root, persist_alerts=True, alerts=[])
