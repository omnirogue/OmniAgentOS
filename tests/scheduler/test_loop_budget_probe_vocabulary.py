"""The livesim stuck-reservation probe must speak the ledger's state vocabulary.

`tests/livesim/categories/test_orchestration.py` asserts that no loop budget
reservation is left wedged past its TTL. That assertion is only worth anything
if the state names it filters on are states `LoopBudgetLedger` actually writes.
They were not: the probe looked for ``active``/``pending``/``held``/``reserved``
while the ledger writes ``open``/``settled``/``released``/``expired_unknown``,
so the filter matched nothing, ``stuck`` was ``[]`` for every possible database,
and the assertion could not fail. A hold sitting at 75% of the daily cap read as
a healthy budget plane.

This guard is deliberately NOT a second copy of the state list — a copy has the
same failure mode as the list it is checking. It reads the probe's own source,
resolves the names it filters on against `loop_budget`, and then puts a really
wedged reservation in front of that resolved set. It runs in the normal lane
(the livesim probe itself needs the live runtime DB and skips without it).
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.scheduler import loop_budget
from omniagentos.scheduler.loop_budget import LoopBudgetLedger

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE = _REPO_ROOT / "tests" / "livesim" / "categories" / "test_orchestration.py"

#: Every state the ledger can put in ``loop_reservations.state``. Read off the
#: module so a new state cannot be added without this guard seeing it.
_LEDGER_STATES = {value for name, value in vars(loop_budget).items() if name.startswith("STATE_")}


def _module_namespace(tree: ast.Module) -> dict[str, Any]:
    """Module-level ``NAME = <tuple/str>`` bindings in the probe, resolved."""
    namespace: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            namespace[target.id] = _resolve(node.value, {})
        except AssertionError:
            continue  # not a state vocabulary; irrelevant here
    return namespace


def _resolve(node: ast.expr, namespace: dict[str, Any]) -> Any:
    """Resolve a literal, a `loop_budget.STATE_*` reference, or a tuple of those.

    A reference is resolved with ``getattr``, so renaming a ledger state without
    updating the probe raises here instead of silently emptying the filter.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return tuple(_resolve(element, namespace) for element in node.elts)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        assert node.value.id == "loop_budget", (
            f"the probe's reservation-state filter reads {ast.unparse(node)}; only "
            "`loop_budget` may define what a reservation state is"
        )
        return getattr(loop_budget, node.attr)
    if isinstance(node, ast.Name) and node.id in namespace:
        return namespace[node.id]
    raise AssertionError(f"unsupported element in the probe's state filter: {ast.unparse(node)}")


#: The probe whose assertion this guard exists to keep honest. Scoped by
#: function name so the other `r["state"] in ...` filters in that module (the
#: spawn queue's, for one) cannot be mistaken for this one.
_PROBE_TEST = "test_loop_tables_present_and_reservations_settle"


def _probe_inflight_states() -> set[str]:
    """The state names the livesim probe treats as 'still in flight'."""
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"))
    namespace = _module_namespace(tree)
    probe = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == _PROBE_TEST
        ),
        None,
    )
    assert probe is not None, (
        f"{_PROBE.name} no longer defines {_PROBE_TEST}; the stuck-reservation probe "
        "this guard exists for has been renamed or deleted"
    )
    found: set[str] = set()
    for node in ast.walk(probe):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        if ast.unparse(node.left) not in ('r["state"]', "r['state']"):
            continue
        resolved = _resolve(node.comparators[0], namespace)
        found |= set(resolved if isinstance(resolved, tuple) else (resolved,))
    assert found, (
        f'no `r["state"] in (...)` reservation filter found in {_PROBE_TEST}; the '
        "stuck-reservation assertion this guard exists for has moved or been deleted"
    )
    return found


def test_probe_inflight_states_are_states_the_ledger_writes() -> None:
    """Every state the probe calls in-flight must be one the ledger can write."""
    inflight = _probe_inflight_states()
    unknown = inflight - _LEDGER_STATES
    assert not unknown, (
        f"the livesim stuck-reservation probe filters on {sorted(unknown)}, which "
        f"LoopBudgetLedger never writes (it writes {sorted(_LEDGER_STATES)}). A filter "
        "on names outside the vocabulary matches nothing, so `assert stuck == []` "
        "cannot fail and a wedged reservation reads as a healthy budget plane."
    )


def test_probe_catches_a_reservation_wedged_past_its_ttl(tmp_path: Path) -> None:
    """The real defect: a genuinely wedged hold must be caught by the probe's filter.

    Drives the real ledger, then applies the probe's own resolved state set to
    the rows it wrote. This is the assertion that was vacuous.
    """
    clock = [1_000_000.0]
    ledger = LoopBudgetLedger(
        str(tmp_path / "live.db"),
        instance_caps={"render_probe": 10.0},
        clock=lambda: clock[0],
    )
    reservation = ledger.reserve(
        instance_id="render_probe",
        capability_id="model.complete",
        estimated_max_usd=7.5,
    )
    clock[0] += 7200.0  # two hours; the TTL is 900s and nobody settled it

    connection = sqlite3.connect(str(tmp_path / "live.db"))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id, state, expires_at FROM loop_reservations").fetchall()
    finally:
        connection.close()

    inflight = _probe_inflight_states()
    now_epoch = clock[0]
    stuck = [
        row["id"]
        for row in rows
        if row["expires_at"] < now_epoch - 3600 and row["state"] in inflight
    ]
    assert stuck == [reservation.id], (
        f"a reservation {now_epoch - reservation.expires_at:.0f}s past its TTL, holding "
        f"${reservation.max_usd:.2f} of a $10.00 daily cap, was not caught by the probe's "
        f"in-flight states {sorted(inflight)}; the row's state is "
        f"{rows[0]['state']!r} and the probe reported nothing stuck"
    )
    # And the ledger agrees it was wedged all along.
    assert ledger.reclaim_expired() == [reservation.id]


@pytest.mark.parametrize("state_name", ["STATE_OPEN"])
def test_ledger_still_defines_the_state_the_probe_depends_on(state_name: str) -> None:
    """A rename of the in-flight state must break loudly, not empty the filter."""
    assert getattr(loop_budget, state_name) in _probe_inflight_states()
