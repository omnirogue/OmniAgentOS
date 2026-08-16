"""H3(b) — a gate's subprocess must not inherit the operator's credentials.

``run_gates`` executes PLANNER-AUTHORED argv (the swarm's ``verify_command``, or
the auto-detected mechanical suite). Before this fix it called ``subprocess.run``
with no ``env=``, so the child inherited the full parent environment — every
payment/bank/infra credential, provider API key, admin DSN and OPERATOR_TOKEN —
while the two other spawn sites for planner-influenced processes
(``adapters/common.py``, ``harnesses/fusion.py``) had already been scrubbed.

Every assertion here is made on the FAR SIDE: the child process prints what it can
actually see and the test reads that, rather than inspecting the call arguments.
A test that asserted ``env=`` was passed would still pass if the helper returned
``os.environ``.
"""

from __future__ import annotations

import json
import sys

import pytest

from omniagentos.gates.engine import GateSpec, run_gates

# Names shaped like credentials (force-denied by substring) plus a registry-known
# one. None of these may reach a gate subprocess.
_SECRET_ENV = {
    "AWS_SECRET_ACCESS_KEY": "h3b-aws-secret",
    "STRIPE_API_KEY": "h3b-stripe-key",
    "OPERATOR_TOKEN": "h3b-operator-token",
    "ADMIN_DATABASE_DSN": "h3b-dsn",
    "GITHUB_PERSONAL_ACCESS_TOKEN": "h3b-pat",
    "MY_SERVICE_PASSWORD": "h3b-password",
}

_DUMP_ENV = "import json,os; print(json.dumps(dict(os.environ)))"


def _child_env(tmp_path: object) -> dict[str, str]:
    spec = GateSpec(argv=[sys.executable, "-c", _DUMP_ENV], name="env_dump")
    results = run_gates([spec], str(tmp_path))
    assert len(results) == 1
    assert results[0].infra_error is None, results[0].infra_error
    assert results[0].ok, results[0].output
    return dict(json.loads(results[0].output.strip().splitlines()[-1]))


def test_gate_subprocess_cannot_see_operator_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    for name, value in _SECRET_ENV.items():
        monkeypatch.setenv(name, value)

    child = _child_env(tmp_path)

    for name, value in _SECRET_ENV.items():
        assert name not in child, f"gate subprocess inherited {name}"
        assert value not in child.values(), f"gate subprocess inherited the VALUE of {name}"


def test_gate_subprocess_still_receives_the_hygiene_allowlist(tmp_path: object) -> None:
    """The scrub must not break the gate: a verifier still needs PATH/HOME.

    Without this row, "strip everything" would look identical to "strip the
    credentials" right up until every gate started failing to find its binary.
    """
    child = _child_env(tmp_path)
    assert child.get("PATH"), "gate subprocess lost PATH"
    assert "HOME" in child


def test_unknown_non_credential_vars_are_also_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """``_scrubbed_env`` is an ALLOWLIST, so an unknown secret is dropped too.

    This is the property that makes the boundary hold against a credential whose
    name nobody added to a denylist -- the same argument as H1's durable floor.
    """
    monkeypatch.setenv("SOME_VENDOR_UNGUESSABLE_VALUE", "h3b-unknown")
    child = _child_env(tmp_path)
    assert "SOME_VENDOR_UNGUESSABLE_VALUE" not in child
