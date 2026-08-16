"""Static and empirical validation gates for the reflection loop (Lane R3)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from omniagentos.reflection.guard import (
    GOVERNANCE_PATH,
    POLICY_DIR,
    PROTECTED_NAMES,
    PROTECTED_PREFIXES,
    REJECTED_WORDS,
    RESTRICTED_KEY_WORDS,
    ContentKind,
    WriteRefused,
    authorise_write,
    coerce_payload,
    is_hard_stop,
    normalise_target_path,
    reflection_repo_root,
)
from omniagentos.reflection.kinds import (
    ALLOWED_KINDS,
    CONFIG_KINDS,
    resolve_target_path,
    target_key,
)

# NOTHING IN THIS MODULE IS A COPY. `resolve_target_path` is DEFINED in
# omniagentos.reflection.kinds and `is_hard_stop` / `authorise_write` in
# omniagentos.reflection.guard, which apply.py imports too: one resolver object
# and one authoriser object, so validation and the writers cannot disagree about
# which file is at stake or whether it may be written. Do not shadow either with
# a local copy — the whole class of bug this module has already shipped three
# times is two judgment sites with private copies of one policy.
#
# The hard-stop rule set and the path-normalisation helper are re-exported here
# rather than defined here, because importers (watchdog.py, the test suite) name
# them through this module. Re-export, never re-declare.
__all__ = [
    "GOVERNANCE_PATH",
    "POLICY_DIR",
    "PROTECTED_NAMES",
    "PROTECTED_PREFIXES",
    "REJECTED_WORDS",
    "RESTRICTED_KEY_WORDS",
    "is_hard_stop",
    "normalise_target_path",
    "resolve_target_path",
    "run_bench_canary_hook",
    "run_blind_judge_hook",
    "traverse_and_set",
    "validate_batch",
    "validate_proposal",
    "validate_router_weight_shadow",
    "round_trip_content",
    "validate_yaml_round_trip",
]


def traverse_and_set(data: dict | list, key_path: str, value: Any) -> None:
    """Traverse a nested dict or list and set a value at key_path (e.g. 'router.weights.0')."""
    parts = key_path.split(".")
    curr = data
    for _i, part in enumerate(parts[:-1]):
        if isinstance(curr, dict):
            if part not in curr:
                curr[part] = {}
            curr = curr[part]
        elif isinstance(curr, list):
            try:
                idx = int(part)
                curr = curr[idx]
            except (ValueError, IndexError):
                raise ValueError(f"Invalid list index: {part}") from None
        else:
            raise ValueError(f"Cannot traverse through non-container: {part}")

    last_part = parts[-1]
    if isinstance(curr, dict):
        curr[last_part] = value
    elif isinstance(curr, list):
        try:
            idx = int(last_part)
            curr[idx] = value
        except (ValueError, IndexError):
            raise ValueError(f"Invalid list index for assignment: {last_part}") from None
    else:
        raise ValueError("Target is not a container")


def round_trip_content(content: str, key_path: str, proposed_value: Any) -> tuple[bool, str]:
    """Verify a proposed config edit preserves YAML parsing, given the CONTENT.

    Split out from :func:`validate_yaml_round_trip` so ``validate_proposal`` can
    round-trip the bytes it read through the authorised handle instead of
    re-opening a path of its own. Two ways to reach a file is two chances to
    reach a different one; there is one implementation and it takes the text.
    """
    try:
        data = yaml.safe_load(content) or {}

        # Apply the proposed change in-memory
        traverse_and_set(data, key_path, proposed_value)

        # Serialize back to YAML
        new_yaml = yaml.safe_dump(data, default_flow_style=False)

        # Re-verify that it parses correctly
        yaml.safe_load(new_yaml)

        return True, ""
    except Exception as exc:
        return False, f"YAML round-trip validation failed: {exc}"


def validate_yaml_round_trip(
    file_path: str, key_path: str, proposed_value: Any
) -> tuple[bool, str]:
    """Verify that a proposed config edit preserves YAML parsing and syntax."""
    if not os.path.exists(file_path):
        return False, f"YAML file does not exist: {file_path}"

    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        return False, f"YAML round-trip validation failed: {exc}"

    return round_trip_content(content, key_path, proposed_value)


def validate_router_weight_shadow(
    log_path: str, threshold: float = 0.55, min_decisions: int = 10
) -> tuple[bool, str, float, int]:
    """Analyze the router shadow log to verify the challenger win-rate is >= threshold over >= N decisions."""
    if not os.path.exists(log_path):
        return False, f"Router shadow log file does not exist: {log_path}", 0.0, 0

    total_decisions = 0
    challenger_wins = 0

    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                incumbent = data.get("incumbent_decision")
                flash = data.get("flash_decision") or data.get("challenger_decision")
                if incumbent and flash:
                    total_decisions += 1

                    is_win = False
                    if "challenger_win" in data:
                        is_win = bool(data["challenger_win"])
                    elif "winner" in data:
                        winner = str(data["winner"]).lower()
                        if winner in ["challenger", "flash", "gemini-flash", "gemini"]:
                            is_win = True
                    elif "outcome" in data:
                        outcome = str(data["outcome"]).lower()
                        if outcome in ["challenger_win", "flash_win", "win"]:
                            is_win = True

                    if is_win:
                        challenger_wins += 1

        if total_decisions < min_decisions:
            return (
                False,
                f"Not enough shadow decisions (found {total_decisions}, need {min_decisions})",
                0.0,
                total_decisions,
            )

        win_rate = challenger_wins / total_decisions if total_decisions > 0 else 0.0
        if win_rate < threshold:
            return (
                False,
                f"Challenger win rate {win_rate:.2f} is below threshold {threshold} over {total_decisions} decisions",
                win_rate,
                total_decisions,
            )

        return True, "", win_rate, total_decisions
    except Exception as exc:
        return False, f"Error processing shadow log: {exc}", 0.0, total_decisions


def run_blind_judge_hook(proposal: dict[str, Any]) -> tuple[bool, str]:
    """Wrap the blind pairwise judge script as a validation hook (mockable)."""
    if os.environ.get("OMNIAGENTOS_BLIND_JUDGE") != "1":
        return True, "Blind judge execution skipped (env flag OMNIAGENTOS_BLIND_JUDGE not '1')"

    try:
        res = subprocess.run(
            ["python3", "tests/policy/blind_judge.py", "--proposal", json.dumps(proposal)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True, f"Blind judge approved proposal: {res.stdout.strip()}"
        else:
            return (
                False,
                f"Blind judge rejected proposal (exit {res.returncode}): {res.stderr or res.stdout}",
            )
    except Exception as exc:
        return False, f"Blind judge hook failed to execute: {exc}"


def run_bench_canary_hook(proposal: dict[str, Any]) -> tuple[bool, str]:
    """Execute 'make bench' with an env overlay to canary-test the proposed config."""
    if os.environ.get("OMNIAGENTOS_RUN_BENCH_CANARY") != "1":
        return (
            True,
            "Bench canary run skipped (env flag OMNIAGENTOS_RUN_BENCH_CANARY not '1'). TODO: Wire full benchmark on CI.",
        )

    try:
        env = dict(os.environ)
        env["OMNIAGENTOS_REFLECTION_PROPOSAL"] = json.dumps(proposal)

        res = subprocess.run(
            ["make", "bench"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True, f"Bench canary succeeded: {res.stdout.strip()}"
        else:
            return False, f"Bench canary failed (exit {res.returncode}): {res.stderr or res.stdout}"
    except Exception as exc:
        return False, f"Bench canary failed to execute: {exc}"


def validate_proposal(proposal: dict[str, Any], limits_config: dict[str, Any]) -> tuple[bool, str]:
    """Perform static, YAML round-trip, hard-stop, and empirical validation on a single proposal."""
    # 1. Schema / Shape validation
    required_keys = ["id", "kind", "target", "current", "proposed"]
    for k in required_keys:
        if k not in proposal:
            return False, f"Missing required proposal schema key: {k}"

    kind = proposal.get("kind")
    # ALLOWED_KINDS lives in omniagentos.reflection.kinds alongside the routing
    # table the writers use. A local list here is how "skill" came to be
    # accepted by the validator and routed by the writer while the resolver had
    # never heard of it.
    if kind not in ALLOWED_KINDS:
        return False, f"Invalid proposal kind: {kind}"

    target = proposal.get("target") or {}
    key_path = target_key(target)

    # The path the WRITER will use — same resolver object the writers call,
    # including its per-kind fallbacks. Reading target["file"] here while
    # apply.write_document_change wrote target["doc"] or target["file"] meant a
    # doc-only proposal skipped this gate entirely and was then written anyway.
    file_path = resolve_target_path(kind, target)

    # 2. Hard-stop Refusal Gate — runs on every proposal that writes ANYTHING.
    # `if file_path:` used to skip the gate when the key was merely absent;
    # absence of a target is not absence of a write. Fail closed for EVERY
    # kind, not just the doc kinds that happened to be enumerated here: an
    # unroutable proposal is refused, never routed to a default document.
    if not file_path:
        return False, (
            f"terminal refusal: {kind} proposal resolves to no writable target "
            "(no target given, and this kind has no default document)"
        )

    # ...and the gate itself is `guard.authorise_write` — the SAME call the
    # writers make to obtain the file they open, against the SAME repo root and
    # with the SAME per-lane confinement. This is not "the validator also
    # checks": there is one authoriser, and a verdict here is a prediction of
    # the writer's verdict only because it is literally the writer's verdict.
    #
    # The predecessor of this line ran a textual `is_hard_stop(file_path)` while
    # the writers ran `Path.resolve()`, so `docs/configs_link/governance.yaml`
    # validated clean here (ok=True) and then overwrote configs/governance.yaml
    # there. Textual normalisation cannot see a symlink; the authoriser resolves
    # by inode and re-runs the rules on what it finds.
    # The content kind and the payload are part of the authorisation, so the
    # validator rules on the same (path, content, key, mode) tuple the writer
    # does. Passing only the path here is what let a payload carrying `api_key`
    # and `budget` be "validated" and then written.
    content_kind = ContentKind.CONFIG if kind in CONFIG_KINDS else ContentKind.DOCUMENT
    payload = coerce_payload(content_kind, proposal["proposed"])

    repo_root = reflection_repo_root()
    try:
        allowed = authorise_write(
            repo_root,
            file_path,
            content_kind=content_kind,
            payload=payload,
            key=key_path,
            confine_to="configs" if kind in CONFIG_KINDS else None,
        )
    except WriteRefused as exc:
        return False, f"terminal refusal: {exc}"

    # 3. YAML Round-trip checks
    if content_kind is ContentKind.CONFIG:
        # Round-trip the file the WRITER will actually open — read through the
        # authorised handle, not a CWD-relative re-reading of the same string.
        # Reading a different file than the one under test is the identical
        # mistake at one remove.
        #
        # `key_path` is non-empty by now: authorise_write refuses a keyless
        # config write outright, so the old "missing target.key" branch here is
        # unreachable and has been removed rather than left as a second copy of
        # a rule the guard already owns.
        if allowed.exists():
            ok, err = round_trip_content(allowed.read_text(), str(key_path), payload)
            if not ok:
                return False, err

    # 4. Empirical validation hooks
    if kind == "router_weight":
        # Resolve path to shadow log
        shadow_log_path = os.environ.get("OMNIAGENTOS_ROUTER_SHADOW_LOG") or str(
            Path(__file__).resolve().parent.parent.parent
            / "var"
            / "modelintel"
            / "router_shadow.jsonl"
        )
        val_cfg = limits_config.get("validation") or {}
        threshold = val_cfg.get("router_win_rate_threshold", 0.55)
        min_decisions = val_cfg.get("router_min_decisions", 10)

        ok, err, win_rate, total_decisions = validate_router_weight_shadow(
            shadow_log_path, threshold, min_decisions
        )
        if not ok:
            return False, f"Empirical router shadow check failed: {err}"

    elif kind == "formation":
        # Run blind judge hook
        ok, err = run_blind_judge_hook(proposal)
        if not ok:
            return False, f"Empirical blind judge hook failed: {err}"

        # Run bench canary hook
        ok, err = run_bench_canary_hook(proposal)
        if not ok:
            return False, f"Empirical bench canary hook failed: {err}"

    return True, ""


def validate_batch(
    proposals: list[dict[str, Any]], limits_config: dict[str, Any]
) -> list[tuple[dict[str, Any], bool, str]]:
    """Validate a batch of proposals under static and budget change limits."""
    results = []

    # Count proposed changes for budget check
    limits = limits_config.get("limits") or {}
    max_formation = limits.get("max_formation_changes", 1)
    max_router_weight = limits.get("max_router_weight_changes", 2)

    formation_count = 0
    router_weight_count = 0

    for p in proposals:
        ok, reason = validate_proposal(p, limits_config)
        if ok:
            kind = p.get("kind")
            if kind == "formation":
                formation_count += 1
                if formation_count > max_formation:
                    ok = False
                    reason = f"Formation change exceeds bounded-change budget limit of {max_formation} per night"
            elif kind == "router_weight":
                router_weight_count += 1
                if router_weight_count > max_router_weight:
                    ok = False
                    reason = f"Router weight change exceeds bounded-change budget limit of {max_router_weight} per night"

        results.append((p, ok, reason))

    return results
