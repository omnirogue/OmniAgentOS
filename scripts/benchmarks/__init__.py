"""A/B benchmark harness (T7.0) — the frozen reference point for the upgrade program.

Layered deliberately thin on top of apparatus that already exists:

* ``omniagentos.contracts.Arm`` names the arms (b0 / b1 / champion);
* ``omniagentos.harnesses.bench.runner.build_manifest`` builds the RunManifest
  with the arm + harness identity + ``env_hash`` the ``runs`` table already
  carries (arm, harness, harness_version, env_hash, harness_params);
* ``omniagentos.harnesses.bench.b0.run_b0_arm`` IS the b0 arm — this package
  calls it, it does not reimplement it;
* ``omniagentos.harnesses.envhash.env_hash`` pins the environment.

What is genuinely new here, and why it could not come from the existing bench:
the existing bench scores nothing. It runs a prompt and records usage. A
baseline needs an *acceptance verdict* per task and *governance observations*
(undeclared modifications, out-of-scope access), so this package adds a
hermetic seed workspace, a frozen acceptance check run after the arm finishes,
and a workspace/transcript observer.

Nothing in ``omniagentos/harnesses/bench`` or ``omniagentos/lab`` is modified.
"""

from __future__ import annotations

__all__ = ["fixtures", "observe", "runner", "store"]
