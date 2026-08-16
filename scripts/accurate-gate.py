#!/usr/bin/env python3
"""
accurate-gate — a gate-agnostic accuracy protocol. Wraps ANY gate command.

Ported from ~/.omniagentos/ops/AccurateGate/accurate-gate.py, with ONE substantive
change and two portability changes.

THE SUBSTANTIVE CHANGE — the refusal ledger is POOL-WIDE, not per-machine.
The original stored refusals in a local ``state.json`` next to its evidence
directory. In a pool that is a hole in the middle of the contract: Mac B would
cheerfully spend a ten-minute gate run on the exact input Mac A refused five
times, because Mac B's state file has never heard of it. Refusal state now lives
in ``wq_refusals`` in the shared queue DB, reached through the SAME store/client
pair the worker uses (``--db`` / ``--server``, env ``WQ_DB`` / ``WQ_SERVER`` /
``WQ_TOKEN``). If neither is declared and the ledger is not explicitly disabled,
this program REFUSES to run: silently degrading to no ledger would turn the one
mechanism that stops retry storms into a no-op, and unknown must never become
permission.

PORTABILITY: (1) quiesce samples ``os.getloadavg()`` instead of shelling
``sysctl -n vm.loadavg`` — two Linux boxes are in this pool and sysctl is not on
them; (2) the ThreeLoops twin-dispatch block is dropped. It imported from a path
outside this repository, and the pool's own claim protocol is now the mechanism
that moves work to a quiet machine.

Usage:
  accurate-gate.py run <gate-name> [--var k=v ...] [--db PATH | --server URL]
  accurate-gate.py explain <gate-name> [--var k=v ...]
  accurate-gate.py receipts <gate-name>

Gate configs live in configs/gates.d/<name>.yaml (or $ACCURATE_GATE_D). No YAML
dependency: configs are a strict, flat subset parsed here (see _load_cfg).

EXIT CODES — the estate convention, ratified 2026-08-08. One code, one meaning:

  0   PASS
  1   REAL FAILURE — a verdict about the SUBJECT: the candidate's code is bad.
  2   COULD NOT RUN — the instrument could not produce a verdict. NEVER a
      statement about the subject.
  64  CALLER ERROR — EX_USAGE: this program was invoked wrong.

exit 2 is NOT "do not retry". A code cannot carry retryability; retryability
rides in the class (RETRYABLE below) and in ``remedy``, where it can be right
per class — two of the five exit-2 classes carry remedies that REQUIRE
re-running the same key.

  class            exit  why
  pass               0   graded, clean
  candidate-defect   1   graded, and the subject is at fault
  environment        2   never graded — the box was unfit
  instrument-error   2   never graded — a declared invariant was violated
  contention-flake   2   graded, but the instrument declared its own reading void
  unchanged-retry    2   never graded — this exact input was already refused
  storm-parked       2   never graded — terminal; a human or a changed input unparks
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
# FRONT of sys.path, unconditionally: THE JUDGE MUST COME FROM THE TREE IT IS
# PINNED TO (2026-08-07). An ambient PYTHONPATH or an editable install pointing at
# another checkout would otherwise supply a different omniagentos — which is how a
# 19-commit-stale gate once graded a correctly-pinned workspace. "Already on the
# path somewhere" is not the same as "first".
sys.path.insert(0, str(REPO_ROOT))

from omniagentos.workqueue.fingerprint import (  # noqa: E402
    InputKeyError,
    input_key_from_specs,
)

SELF = Path(__file__).resolve()
GATES_D = Path(os.environ.get("ACCURATE_GATE_D", REPO_ROOT / "configs" / "gates.d"))
CLASSES = (
    "instrument-error",
    "environment",
    "contention-flake",
    "candidate-defect",
    "unchanged-retry",
    "storm-parked",
    "pass",
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_COULD_NOT_RUN = 2
EXIT_CALLER_ERROR = 64  # EX_USAGE (sysexits.h); see the module docstring

# Retryability is a per-CLASS fact, not an exit-code fact. `False` means the
# remedy is not "run it again": either the input itself must change, or a human
# must unpark it. The two True entries are the reason exit 2 can never be read as
# "do not retry" — their recorded remedies REQUIRE re-running the same key.
RETRYABLE = {
    "pass": None,  # nothing to retry
    "candidate-defect": False,  # change the code; the next input_key differs anyway
    "environment": True,  # fix the box, re-run the same key
    "instrument-error": True,  # repair the instrument, re-run the same key
    "contention-flake": True,  # re-run ONCE in a quiet window
    "unchanged-retry": False,  # this exact input was already refused
    "storm-parked": False,  # terminal; alert once
}


def exit_code_for(cls: str) -> int:
    """The ONE place a class becomes a code. Two copies of this expression are
    how the receipt and the process came to disagree in the first place."""
    if cls == "pass":
        return EXIT_PASS
    if cls == "candidate-defect":
        return EXIT_FAIL
    return EXIT_COULD_NOT_RUN


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sh(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    # errors="replace": ps/git argv can contain bytes that are not valid UTF-8.
    # The instrument must degrade a byte, never crash mid-preflight.
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False, **kw)


def _die(msg: str, code: int = EXIT_COULD_NOT_RUN) -> None:
    """Default is COULD NOT RUN: an unreadable config, or a git tree that will not
    resolve, means this program produced no verdict. Pass EXIT_CALLER_ERROR where
    the fault is in the INVOCATION, not the estate."""
    print(f"accurate-gate: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- config ----
def _load_cfg(name: str, variables: dict[str, str]) -> dict[str, Any]:
    """Parse gates.d/<name>.yaml — strict flat YAML subset (key: value, lists as
    '- item', one level of nesting). Refuses unknown keys: an unparseable config
    must fail loudly, not default silently (favourable-absence rule)."""
    p = GATES_D / f"{name}.yaml"
    if not p.exists():
        _die(f"no gate config at {p}")
    cfg: dict[str, Any] = {
        "name": name,
        "command": None,
        "workdir": None,
        "input_key": [],
        "invariants": [],
        "quiesce": {},
        "evidence": {},
        "retry": {},
        "env_scrub": [],
    }
    cur, curlist = None, None
    for ln, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.split(" #")[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0 and s.endswith(":"):
            cur = s[:-1]
            if cur not in cfg:
                _die(f"{p}:{ln} unknown section '{cur}'")
            curlist = cfg[cur] if isinstance(cfg[cur], list) else None
            continue
        if indent == 0 and ":" in s:
            k, v = s.split(":", 1)
            if k not in cfg:
                _die(f"{p}:{ln} unknown key '{k}'")
            cfg[k] = v.strip()
            cur = None
            continue
        if cur and s.startswith("- "):
            item = s[2:].strip()
            if curlist is None:
                _die(f"{p}:{ln} '- ' under non-list section '{cur}'")
            curlist.append(item)
            continue
        if cur and ":" in s:
            k, v = s.split(":", 1)
            cfg[cur][k.strip()] = v.strip()
            continue
        _die(f"{p}:{ln} unparseable line: {raw!r}")

    def sub(t: str) -> str:
        for k, v in variables.items():
            t = t.replace(f"{{{k}}}", v)
        m = re.search(r"\{([a-z_]+)\}", t)
        if m:
            _die(
                f"config needs --var {m.group(1)}=... (unresolved in {t!r})",
                EXIT_CALLER_ERROR,
            )
        return t

    cfg["command"] = sub(cfg["command"])
    cfg["workdir"] = sub(cfg["workdir"]) if cfg["workdir"] else None
    cfg["input_key"] = [sub(x) for x in cfg["input_key"]]
    cfg["invariants"] = [sub(x) for x in cfg["invariants"]]
    cfg["evidence"] = {k: sub(v) for k, v in cfg["evidence"].items()}
    return cfg


# ------------------------------------------------------------ the ledger ----
class Ledger:
    """The refusal ledger, shared across the pool.

    ``mode`` is one of:
      ``full``  — read and write (standalone invocations; the default)
      ``read``  — consult, never write
      ``off``   — neither. The WORKER passes this: it has already read the ledger
                  before dispatching, and only it knows the FINAL class after the
                  §4.4b post-hoc classifier has had its say. Recording the
                  pre-override class here would put "candidate-defect" in the
                  ledger for what was really an expired token.
    """

    def __init__(self, mode: str, db: str | None, server: str | None) -> None:
        self.mode = mode
        self.q: Any = None
        if mode == "off":
            return
        if not (db or server):
            _die(
                "the refusal ledger has no home: pass --db PATH or --server URL "
                "(or set WQ_DB / WQ_SERVER), or pass --ledger off if this run must "
                "not consult it. Defaulting to 'no ledger' would silently disable "
                "the unchanged-input refusal — the one mechanism that stops storms.",
                EXIT_CALLER_ERROR,
            )
        try:
            from omniagentos.workqueue.worker import open_queue

            self.q = open_queue(server, db)
        except Exception as exc:  # a queue we cannot open is an INSTRUMENT fault
            _die(f"cannot open the queue ({type(exc).__name__}: {exc})")

    def check(self, key: str, gate: str) -> dict[str, Any] | None:
        if self.q is None:
            return None
        try:
            return self.q.refusal_check(key, gate)
        except Exception as exc:
            print(f"accurate-gate: refusal_check failed: {exc}", file=sys.stderr)
            return None

    def record(self, key: str, gate: str, cls: str, remedy: str) -> None:
        if self.q is None or self.mode != "full":
            return
        try:
            if cls == "pass":
                # A pass DELETES the row. The ledger can only ever refuse harder;
                # it has no path that emits a cached pass.
                self.q.refusal_clear(key, gate)
            else:
                self.q.refusal_record(
                    key, gate, cls, 1 if RETRYABLE.get(cls) else 0, remedy or "(no remedy)"
                )
        except Exception as exc:
            print(f"accurate-gate: refusal write failed: {exc}", file=sys.stderr)


# ------------------------------------------------------------- mechanisms ----
def input_key(cfg: dict[str, Any]) -> str:
    """Mechanism 2: content hash over declared inputs.

    Delegated to ``omniagentos.workqueue.fingerprint`` so this wrapper and the
    worker compute the SAME key by construction — two implementations of a
    content hash is two ledgers that disagree. The gate NAME is prepended (SPEC
    §4.1 entry 1) so two gates over one tree never share a refusal row.
    """
    try:
        return input_key_from_specs([f"gate:{cfg['name']}", *cfg["input_key"]])
    except InputKeyError as exc:
        _die(str(exc))
        raise  # unreachable; keeps the type checker honest


def check_invariants(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Mechanism 1. Returns list of violations; each is (spec, detail)."""
    bad: list[tuple[str, str]] = []
    for spec in cfg["invariants"]:
        parts = spec.split()
        kind = parts[0]
        if kind == "git-pinned":  # git-pinned <path> <expected-ref>
            path, ref = parts[1], parts[2]
            head = _sh(["git", "-C", path, "rev-parse", "HEAD"]).stdout.strip()
            want = _sh(["git", "-C", path, "rev-parse", ref]).stdout.strip()
            if not head or head != want:
                bad.append((spec, f"HEAD={head[:12] if head else 'unreadable'} expected {ref}"))
        elif kind == "git-clean":  # git-clean <path>
            if _sh(["git", "-C", parts[1], "status", "--porcelain"]).stdout.strip():
                bad.append((spec, "dirty working tree — one writer only"))
        elif kind == "exe":  # exe <path>
            if not os.access(parts[1], os.X_OK):
                bad.append((spec, "missing or not executable"))
        elif kind == "exists":  # exists <path>
            if not Path(parts[1]).exists():
                bad.append((spec, "does not exist"))
        elif kind == "disk-free":  # disk-free <N>G [path]
            want_gb = float(parts[1].rstrip("Gg"))
            where = parts[2] if len(parts) > 2 else str(Path.home())
            try:
                free_gb = shutil.disk_usage(where).free / 1024**3
            except OSError as exc:
                bad.append((spec, f"cannot stat {where}: {exc}"))
                continue
            if free_gb < want_gb:
                bad.append((spec, f"{free_gb:.1f}G free at {where}, need {want_gb:.0f}G"))
        else:
            bad.append((spec, f"unknown invariant kind {kind!r} — refusing to skip it"))
    return bad


def quiesce(cfg: dict[str, Any], receipt: dict[str, Any]) -> str:
    """Mechanism 3. Real load signal + anchored external-process patterns.

    Returns 'quiet' | 'deadline' | 'not-configured'. Never raises. ``os.getloadavg``
    is the portable form of the original's ``sysctl -n vm.loadavg`` — same signal,
    and it also exists on the Linux members of the pool.
    """
    q = cfg["quiesce"]
    if not q:
        return "not-configured"
    load_max = float(q.get("load1_max", "16"))
    wait_max = int(q.get("wait_max_s", "1800"))
    patt = q.get("external_pattern", "")

    deadline = time.time() + wait_max
    while True:
        try:
            l1 = os.getloadavg()[0]
        except OSError:
            return "not-configured"
        ext = 0
        if patt:
            out = _sh(["ps", "-axo", "command"]).stdout
            # anchored: the pattern must appear in the command's first two tokens,
            # never in argument text (the phantom-429 / prompt-text lesson)
            ext = sum(
                1
                for c in out.splitlines()
                if re.match(r"^\S*(bash|sh|python[\d.]*)\s+\S*" + re.escape(patt), c)
            )
        receipt["load_samples"].append({"t": _now(), "load1": round(l1, 2), "external": ext})
        if ext == 0 and l1 < load_max:
            return "quiet"
        if time.time() >= deadline:
            return "deadline"
        time.sleep(30)


def preserve_evidence(cfg: dict[str, Any], receipt_dir: Path, stdout_text: str) -> list[str]:
    """Mechanism 4: copy declared globs BEFORE anyone's cleanup trap can run."""
    ev = cfg["evidence"]
    (receipt_dir / "stdout.log").write_text(stdout_text)
    saved: list[str] = []
    for g in [v for k, v in ev.items() if k.startswith("preserve")]:
        sources = Path("/").glob(g.lstrip("/")) if g.startswith("/") else Path.cwd().glob(g)
        for src in sources:
            try:
                dst = receipt_dir / "preserved" / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                saved.append(str(src))
            except OSError:
                saved.append(f"FAILED:{src}")
    return saved


def classify(
    cfg: dict[str, Any], rc: int, stdout_text: str, inv_bad: list[Any], quiesce_state: str
) -> str:
    """Mechanism 5. Deterministic, ordered."""
    if inv_bad:
        return "instrument-error"
    if rc == 0:
        return "pass"
    # Negative returncode = the gate was KILLED BY A SIGNAL (subprocess semantics).
    # A killed gate graded nothing: environment, never candidate-defect (a SIGKILL
    # at minute 55 with empty stdout minted a false defect on 2026-08-08).
    if rc < 0:
        return "environment"
    # An instrument that self-reports a blown timing bound is declaring its own
    # reading void. Believe it — classifying it candidate-defect poisons the input
    # key and blames code that was never graded.
    if re.search(r"instrument bound exhausted|control \(unpatched\) timed out", stdout_text):
        return "contention-flake"
    single = re.search(r"\b1 failed, (\d{3,}) passed", stdout_text)
    if single and quiesce_state == "deadline":
        return "contention-flake"
    if re.search(
        r"No such file|command not found|Permission denied|disk full|No space", stdout_text
    ):
        return "environment"
    return "candidate-defect"


# ------------------------------------------------------------------- run ----
def run(
    name: str,
    variables: dict[str, str],
    *,
    explain: bool = False,
    ledger: Ledger | None = None,
    key_override: str | None = None,
) -> None:
    cfg = _load_cfg(name, variables)
    ev_root = Path(cfg["evidence"].get("dir", _default_evidence_root())) / name
    ev_root.mkdir(parents=True, exist_ok=True)
    key = key_override or input_key(cfg)
    led = ledger or Ledger("off", None, None)

    receipt: dict[str, Any] = {
        "schema": "accurate-gate.v2",
        "gate": name,
        "input_key": key,
        "input_key_source": "caller" if key_override else "computed",
        "started_at": _now(),
        "load_samples": [],
        "class": None,
        "exit_code": None,
        "remedy": None,
    }
    rdir = ev_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"

    def finish(cls: str, rc: int | None, remedy: str, extra: dict[str, Any] | None = None) -> None:
        # ONE exit-code story, ONE expression: `exit_code` in the receipt and the
        # process's own status both come from exit_code_for(). A consumer keying on
        # either gets the same answer by construction.
        #
        # `gate_exit_code` is the WRAPPED gate's own status and may only be a number
        # when the gate actually ran. Every path that did not run it passes None —
        # a fabricated measurement is worse than an absent one (a receipt once
        # asserted "the gate exited 3" for a gate that was never started).
        process_exit = exit_code_for(cls)
        receipt.update(
            {
                "class": cls,
                "exit_code": process_exit,
                "gate_exit_code": rc,
                "retryable": RETRYABLE.get(cls),
                "remedy": remedy,
                "finished_at": _now(),
                **(extra or {}),
            }
        )
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "receipt.json").write_text(json.dumps(receipt, indent=1))
        led.record(key, name, cls, remedy)
        print(
            json.dumps(
                {k: receipt[k] for k in ("gate", "class", "exit_code", "gate_exit_code", "remedy")},
                indent=1,
            )
        )
        sys.exit(process_exit)

    # 2 — unchanged-input refusal + storm parking
    prior = led.check(key, name)
    if prior:
        r = cfg["retry"]
        max_att = int(r.get("max_attempts", "5"))
        count = int(prior.get("count") or 0)
        prior_class = prior.get("refusal_class")
        if count >= max_att:
            finish(
                "storm-parked",
                None,
                f"{count} refusals for this exact input — PARKED. A human or a changed "
                "input un-parks it; retrying is prohibited (terminal, alert once).",
            )
        # Two refusal classes carry remedies that REQUIRE re-running the same key,
        # so they must fall through instead of refusing as unchanged-retry:
        #   instrument-error — the instrument (workspace state, missing exe) is not
        #     part of input_key, so a repaired instrument never changes the key;
        #   contention-flake — its recorded remedy is literally "re-run ONCE in a
        #     quiet window"; blocking that re-run makes the remedy unfollowable.
        # Storm parking above still caps total attempts for both.
        if r.get("unchanged_input", "refuse") == "refuse" and prior_class not in (
            "instrument-error",
            "contention-flake",
        ):
            finish(
                "unchanged-retry",
                None,
                f"identical inputs were refused {count}x (class {prior_class}, last "
                f"{prior.get('last_seen_at')}). Change the tree, the gate config, or the "
                "gate itself; re-running unchanged is not a strategy.",
            )

    # 1 — instrument preflight
    inv_bad = check_invariants(cfg)
    if inv_bad:
        finish(
            "instrument-error",
            None,
            "fix the INSTRUMENT, not the candidate: "
            + "; ".join(f"{s} -> {d}" for s, d in inv_bad),
        )
    if explain:
        print(
            json.dumps(
                {
                    "gate": name,
                    "input_key": key,
                    "invariants": "ok",
                    "would_run": cfg["command"],
                    "prior_refusals": prior,
                },
                indent=1,
            )
        )
        return

    # 3 — quiesce
    qstate = quiesce(cfg, receipt)

    # run the actual gate — the wrapped command and its exit code are the ONLY
    # things that can mint a verdict; nothing here can produce a pass without that
    # subprocess actually running and returning rc == 0.
    env = {k: v for k, v in os.environ.items() if k not in set(cfg["env_scrub"])}
    t0 = time.time()
    proc = subprocess.run(
        shlex.split(cfg["command"]),
        cwd=cfg["workdir"] or None,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    wall = round(time.time() - t0, 1)
    out = proc.stdout + "\n--- stderr ---\n" + proc.stderr
    rc = proc.returncode

    # 4 — evidence always
    rdir.mkdir(parents=True, exist_ok=True)
    preserved = preserve_evidence(cfg, rdir, out)

    # 5 — classify + remedy
    cls = classify(cfg, rc, out, [], qstate)
    remedy = {
        "pass": "merge/proceed",
        "contention-flake": "verdict is suspect: exactly-one-test failure while the box never "
        "went quiet. Re-run ONCE in a quiet window; if it fails quiet, it "
        "is a candidate-defect.",
        "environment": "fix the environment error quoted in evidence; not a code defect.",
        "candidate-defect": "read preserved evidence; fix the named cause; the next run's "
        "input_key will differ and be admitted.",
    }.get(cls, "see evidence")
    print(out[-4000:])
    finish(cls, rc, remedy, {"wall_s": wall, "quiesce": qstate, "preserved": preserved})


def _default_evidence_root() -> Path:
    return Path(os.environ.get("WQ_HOME") or (Path.home() / "wq")) / "gate-evidence"


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in ("run", "explain", "receipts"):
        # CALLER error, not "could not run": nothing about this estate is wrong.
        print(__doc__)
        sys.exit(EXIT_CALLER_ERROR)
    cmd, name = args[0], args[1]
    variables: dict[str, str] = {}
    ledger_mode = os.environ.get("WQ_LEDGER", "full")
    db = os.environ.get("WQ_DB")
    server = os.environ.get("WQ_SERVER")
    key_override = None
    rest = args[2:]
    while rest:
        head = rest[0]
        if head == "--var" and len(rest) > 1 and "=" in rest[1]:
            k, v = rest[1].split("=", 1)
            variables[k] = v
            rest = rest[2:]
        elif head == "--ledger" and len(rest) > 1:
            ledger_mode, rest = rest[1], rest[2:]
            if ledger_mode not in ("full", "read", "off"):
                _die(f"--ledger must be full|read|off, got {ledger_mode!r}", EXIT_CALLER_ERROR)
        elif head == "--db" and len(rest) > 1:
            db, rest = rest[1], rest[2:]
        elif head == "--server" and len(rest) > 1:
            server, rest = rest[1], rest[2:]
        elif head == "--input-key" and len(rest) > 1:
            key_override, rest = rest[1], rest[2:]
        else:
            _die(f"unknown arg {head!r}", EXIT_CALLER_ERROR)
    if cmd == "receipts":
        root = _default_evidence_root() / name
        try:
            root = Path(_load_cfg(name, variables)["evidence"].get("dir", root)) / name
        except SystemExit:
            pass  # config needs vars we weren't given — fall back to the default root
        for r in sorted(root.glob("*/receipt.json")):
            d = json.loads(r.read_text())
            print(f"{d['started_at']}  {d['class']:18} exit={d['exit_code']}  {r.parent.name}")
        return
    # `explain` must never WRITE a refusal — it is a dry run — but it should still
    # show what the ledger already holds for this key.
    ledger = Ledger(
        "read" if (cmd == "explain" and ledger_mode == "full") else ledger_mode, db, server
    )
    run(name, variables, explain=(cmd == "explain"), ledger=ledger, key_override=key_override)


if __name__ == "__main__":
    main()
