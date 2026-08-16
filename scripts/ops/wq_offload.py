#!/usr/bin/env python3
"""wq_offload.py — offload a test run to the fleet compute pool (wq).

The doctrine (~/.omniagentos/ops/Offload-Before-Overload-Doctrine-2026-08-13.md) rules
that anything self-contained given a repo URL + SHA belongs on the fleet. This
helper turns "run these tests" into a correct, FAIL-CLOSED wq unit and enqueues
it on the pool server (127.0.0.1:8487, bearer ``WQ_TOKEN``):

    python scripts/ops/wq_offload.py test --ref <sha|branch> \
        --tests tests/taskcontract [--label pytest] [--wait]

Fail-closed by construction:

* ``acceptance_cmd`` IS the pytest command (default runner ``uv run --frozen
  pytest -q``) — there is no wrapper that could swallow a red exit code, and
  the unit passes only when the process exits 0 (``worker._verdict``).
* ``base_sha`` must resolve to a 40-hex commit. A branch is resolved via
  ``origin/<ref>`` FIRST, because the fleet fetches from GitHub: a locally
  resolved but unpushed commit would refuse on every box
  (``worker._ensure_mirror``: "base_sha is not in the mirror after one fetch").
* ``owned_paths`` stays minimal (``var/wq-offload/**`` — a path the run never
  writes): a test run that writes tracked files FAILS the scope check, which is
  the honest verdict.
* ``agent_profile: script`` — no agent turn; the acceptance command is the
  whole unit (configs/workqueue.yaml profiles).
* a missing ``WQ_TOKEN`` aborts before the wire: the server fails closed with
  401 anyway, so asking without a token is buying a guaranteed refusal.

The idempotency key is deterministic over (repo, sha, command, labels, owned
paths): re-running the same offload dedupes to the existing unit — the queue's
own answer to "buying the same answer twice". ``--fresh`` salts the key when a
genuinely new run of an unchanged input is wanted (e.g. after an environment
repair; the refusal ledger still applies its own unchanged-input rules).

On a pass the worker pushes ``branch`` (pinned at ``base_sha``, so no new
commits) to the repo's origin — name it something disposable; the coordinator
deletes it. ``wait`` polls the unit through queued→claimed/running→done and
prints where it ran; this in-process poll is the same sanctioned shape as
``wq status --watch``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_REPO_URL = "https://github.com/Globex/OmniAgentOS.git"
DEFAULT_REPO_SLUG = "OmniAgentOS"
DEFAULT_SERVER = "http://127.0.0.1:8487"
#: ``uv run --frozen`` builds the workdir's own venv from uv.lock (warm cache on
#: enrolled boxes — enroll.sh preflights uv), so the command works in the fresh
#: clone the worker grades, not against some other checkout's venv.
DEFAULT_RUNNER = "uv run --frozen pytest -q"
#: ``pytest`` is declared by the darwin studios (configs/workqueue.yaml fleet map).
DEFAULT_LABELS = ["pytest"]
#: A path the test run never writes: any tracked write becomes a scope FAIL.
DEFAULT_OWNED_PATHS = ["var/wq-offload/**"]
DEFAULT_TIMEOUT_S = 900
DEFAULT_MAX_ATTEMPTS = 2
CONNECTIONS_ENV = Path.home() / ".config" / "omni" / "connections.env"

POLL_INTERVAL_S = 10.0
TERMINAL_STATES = {"done", "review", "parked", "cancelled"}

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_token() -> str:
    """WQ_TOKEN from the environment, else ~/.config/omni/connections.env."""
    token = os.environ.get("WQ_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in CONNECTIONS_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :]
            if line.startswith("WQ_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def default_submitter() -> str:
    """WQ_USER first (the pool's attribution var), then USER. Never guessed."""
    return os.environ.get("WQ_USER", "").strip() or os.environ.get("USER", "").strip()


def resolve_ref(ref: str, repo_root: Path = REPO_ROOT) -> str:
    """Resolve ``ref`` to a 40-hex sha the FLEET can fetch.

    ``origin/<ref>`` is tried first on purpose: workers clone from the GitHub
    URL, so only pushed commits exist for them. A ref that resolves only
    locally is refused with the remedy named — enqueueing it would burn a fleet
    attempt on a guaranteed instrument-error.
    """
    ref = ref.strip()
    if _SHA40.fullmatch(ref):
        return ref

    def _rev_parse(candidate: str) -> str | None:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
             f"{candidate}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        out = res.stdout.strip()
        return out if res.returncode == 0 and _SHA40.fullmatch(out) else None

    sha = _rev_parse(f"origin/{ref}")
    if sha:
        return sha
    local_sha = _rev_parse(ref)
    if local_sha:
        raise SystemExit(
            f"wq_offload: {ref!r} resolves only LOCALLY ({local_sha[:12]}). The fleet "
            "fetches from the repo URL, so an unpushed commit refuses on every box. "
            "Push the ref first, or pass a sha that exists on origin."
        )
    raise SystemExit(f"wq_offload: cannot resolve {ref!r} to a commit (tried origin/{ref}, {ref})")


def build_unit(
    *,
    base_sha: str,
    tests: str,
    runner: str = DEFAULT_RUNNER,
    labels: list[str] | None = None,
    owned_paths: list[str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    priority: int = 2,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    repo_url: str = DEFAULT_REPO_URL,
    repo_slug: str = DEFAULT_REPO_SLUG,
    base_ref: str = "main",
    submitted_by: str | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """A complete, fail-closed unit_submit payload (contract.schema.json)."""
    if not _SHA40.fullmatch(base_sha):
        raise ValueError(f"base_sha must be 40-hex (got {base_sha!r}) — resolve the ref first")
    tests = tests.strip()
    if not tests:
        raise ValueError("--tests must name a pytest selection (e.g. tests/taskcontract)")
    acceptance_cmd = f"{runner.strip()} {tests}"
    labels = list(labels) if labels else list(DEFAULT_LABELS)
    owned_paths = list(owned_paths) if owned_paths else list(DEFAULT_OWNED_PATHS)
    core = json.dumps(
        [repo_url, base_sha, acceptance_cmd, sorted(labels), sorted(owned_paths)],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(core.encode("utf-8")).hexdigest()[:12]
    salt = f"-{int(time.time())}" if fresh else ""
    return {
        "idempotency_key": f"wq-offload-{base_sha[:12]}-{digest}{salt}",
        "repo_url": repo_url,
        "repo_slug": repo_slug,
        "base_sha": base_sha,
        "base_ref": base_ref,
        "branch": f"wq/offload-{base_sha[:8]}-{digest[:6]}{salt}",
        "owned_paths": owned_paths,
        "submitted_by": submitted_by if submitted_by is not None else default_submitter(),
        "agent_profile": "script",
        "timeout_s": int(timeout_s),
        "labels": labels,
        "risk_class": "mechanical",
        "acceptance_cmd": acceptance_cmd,
        "acceptance_gate": None,
        "priority": int(priority),
        "max_attempts": int(max_attempts),
    }


def make_client(server: str, token: str) -> Any:
    from omniagentos.workqueue.client import HttpQueueClient

    return HttpQueueClient(server, token=token)


def enqueue_unit(unit: dict[str, Any], server: str, token: str) -> tuple[str, bool]:
    """POST the unit via the server API. Returns (unit_id, deduped)."""
    return make_client(server, token).enqueue(unit)  # type: ignore[no-any-return]


def wait_for_unit(
    unit_id: str,
    server: str,
    token: str,
    *,
    timeout_s: float = 900.0,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> int:
    """Poll the unit to a terminal state, printing every transition.

    Exit 0 on done/review (accepted), 1 on parked/cancelled, 3 on wait timeout
    (the unit keeps running — this is only the WATCHER giving up).
    """
    client = make_client(server, token)
    deadline = time.time() + timeout_s
    last_state = ""
    while True:
        unit = client.get_unit(unit_id)
        if unit is None:
            print(f"wq_offload: no such unit {unit_id}", file=sys.stderr)
            return 1
        state = str(unit.get("state"))
        if state != last_state:
            owner = unit.get("lease_owner") or ""
            where = f"  on {str(owner).split(':', 1)[0]}" if owner else ""
            print(f"[{time.strftime('%H:%M:%S')}] {unit_id} {last_state or '(new)'} -> {state}{where}")
            last_state = state
        if state in TERMINAL_STATES:
            attempts = client.list_attempts(unit_id)
            for att in attempts:
                print(
                    f"  attempt {att.get('attempt')}: {att.get('machine_id')} "
                    f"outcome={att.get('outcome')} exit={att.get('exit_code')} "
                    f"log={att.get('log_path')}"
                )
            if unit.get("result_branch"):
                print(f"  result_branch: {unit['result_branch']} @ {unit.get('result_sha')}")
            if unit.get("terminal_reason"):
                print(f"  terminal_reason: {unit['terminal_reason']}")
            return 0 if state in ("done", "review") else 1
        if time.time() >= deadline:
            print(
                f"wq_offload: wait timed out after {int(timeout_s)}s in state {state!r} — "
                f"the unit is still in flight; check later with: "
                f"python scripts/ops/wq_offload.py wait --unit {unit_id}",
                file=sys.stderr,
            )
            return 3
        time.sleep(poll_interval_s)


def cmd_test(args: argparse.Namespace) -> int:
    base_sha = resolve_ref(args.ref)
    unit = build_unit(
        base_sha=base_sha,
        tests=args.tests,
        runner=args.runner,
        labels=args.label or None,
        owned_paths=args.owned_path or None,
        timeout_s=args.timeout_s,
        priority=args.priority,
        max_attempts=args.max_attempts,
        repo_url=args.repo_url,
        repo_slug=args.repo_slug,
        base_ref=args.base_ref,
        fresh=args.fresh,
    )
    if args.dry_run:
        print(json.dumps(unit, indent=1))
        return 0
    token = load_token()
    if not token:
        raise SystemExit(
            "wq_offload: no WQ_TOKEN (env or ~/.config/omni/connections.env). The pool "
            "fails closed without it — mint one with scripts/workqueue/mint-token.sh."
        )
    unit_id, deduped = enqueue_unit(unit, args.server, token)
    print(f"{'dedup' if deduped else 'queued'} {unit_id} {unit['idempotency_key']}")
    print(f"  base_sha={base_sha[:12]} labels={unit['labels']} cmd={unit['acceptance_cmd']!r}")
    if args.wait:
        return wait_for_unit(unit_id, args.server, token, timeout_s=args.wait_timeout_s)
    print(f"  poll with: python scripts/ops/wq_offload.py wait --unit {unit_id}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    token = load_token()
    if not token:
        raise SystemExit("wq_offload: no WQ_TOKEN (env or ~/.config/omni/connections.env)")
    return wait_for_unit(args.unit, args.server, token, timeout_s=args.wait_timeout_s)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wq_offload.py", description="offload a test run to the fleet compute pool"
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("WQ_SERVER") or DEFAULT_SERVER,
        help=f"wq-server base URL (default: env WQ_SERVER, else {DEFAULT_SERVER})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    test = sub.add_parser("test", help="enqueue a pytest run as a fail-closed script unit")
    test.add_argument("--ref", required=True, help="40-hex sha, or a ref resolved via origin/<ref>")
    test.add_argument(
        "--tests", required=True, help="pytest selection, e.g. 'tests/taskcontract'"
    )
    test.add_argument(
        "--runner",
        default=DEFAULT_RUNNER,
        help=f"command prefix the tests are appended to (default: {DEFAULT_RUNNER!r})",
    )
    test.add_argument(
        "--label",
        action="append",
        default=[],
        help=f"capability label (repeatable; default: {DEFAULT_LABELS})",
    )
    test.add_argument(
        "--owned-path",
        action="append",
        default=[],
        help=f"owned_paths glob (repeatable; default: {DEFAULT_OWNED_PATHS})",
    )
    test.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    test.add_argument("--priority", type=int, default=2)
    test.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    test.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    test.add_argument("--repo-slug", default=DEFAULT_REPO_SLUG)
    test.add_argument("--base-ref", default="main")
    test.add_argument(
        "--fresh",
        action="store_true",
        help="salt the idempotency key: force a NEW unit instead of deduping to an old one",
    )
    test.add_argument("--dry-run", action="store_true", help="print the unit JSON, enqueue nothing")
    test.add_argument("--wait", action="store_true", help="poll the unit to a terminal state")
    test.add_argument("--wait-timeout-s", type=float, default=900.0)
    test.set_defaults(func=cmd_test)

    wait = sub.add_parser("wait", help="poll an already-enqueued unit to a terminal state")
    wait.add_argument("--unit", required=True)
    wait.add_argument("--wait-timeout-s", type=float, default=900.0)
    wait.set_defaults(func=cmd_wait)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
