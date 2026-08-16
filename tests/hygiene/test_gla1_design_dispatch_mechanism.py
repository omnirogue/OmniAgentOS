"""GLA-1 decisive test: the CLI `design` dispatch mechanism actually invokes
`_design_existing_request` — distinct from the GLA-3 counterfeit.

GLA-3 (tests/api/test_reliability_routes.py) proves the spawn *argv string*
names the right module. It never executes ``main()``'s command dispatch, so
it cannot fail if the ``if args.command == "design":`` branch in
``omniagentos/orgdims/company_requests.py::main`` is deleted or its call to
``_design_existing_request`` is dropped — the spawned-process string would
still be correct while the mechanism it names silently did nothing.

This test calls ``main()`` in-process for the ``design`` subcommand with
``_make_store`` and ``_design_existing_request`` monkeypatched to a sentinel,
and asserts the sentinel was actually invoked with the parsed request id and
that its return payload was the one printed and used for the exit code.

Case label: Case B (mechanism removal) per devtasks/LANE-ACCEPTANCE-DOCTRINE.md
section 1. Red-first proof (recorded, not merely claimed): the dispatch branch

    if args.command == "design":
        try:
            payload = _design_existing_request(store, args.request_id)
        ...

was temporarily replaced with ``pass`` (mechanism removed) in a scratch copy
of ``omniagentos/orgdims/company_requests.py`` and this test was run against
it — it FAILED with ``AssertionError: sentinel was not called`` (the far-side
property: the dispatch never reaches ``_design_existing_request``). The
branch was restored and the test passes again (GREEN), which is the state
committed here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omniagentos.orgdims import company_requests


def test_design_command_dispatches_to_design_existing_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel_payload: dict[str, Any] = {
        "id": "req-gla1",
        "status": "awaiting_approval",
        "design_json": {"name": "gla1-probe"},
        "improvement_id": None,
    }
    sentinel = MagicMock(return_value=sentinel_payload)

    # Real dispatch calls _design_existing_request(store, args.request_id);
    # a fake store is enough since the sentinel never touches it.
    monkeypatch.setattr(company_requests, "_make_store", MagicMock(return_value=object()))
    monkeypatch.setattr(company_requests, "_design_existing_request", sentinel)

    rc = company_requests.main(["design", "--request", "req-gla1"])

    # Far-side witnesses, not a call-count alone:
    # 1) the mechanism actually reached the helper with the parsed request id
    assert sentinel.called, "sentinel was not called — design dispatch branch is missing"
    (_store_arg, request_id_arg), _kwargs = sentinel.call_args
    assert request_id_arg == "req-gla1"
    # 2) the helper's return payload is what determines exit code and stdout —
    #    not a fixed/ignored success value.
    assert rc == 0
    printed = capsys.readouterr().out
    assert '"awaiting_approval"' in printed
    assert '"gla1-probe"' in printed


def test_design_command_dispatch_exists_in_source() -> None:
    # Structural guard: the specific conditional dispatch line must be present,
    # so a refactor that renames the command string can't silently orphan it
    # while the behavioral test above happens to still pass via a stale binding.
    import inspect

    src = inspect.getsource(company_requests.main)
    assert 'args.command == "design"' in src
    assert "_design_existing_request(store, args.request_id)" in src
