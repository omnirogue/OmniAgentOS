"""Seeded task worlds for the Hypothesis Tester.

Every world is a pure function of (family, world_seed): the hidden structure,
the 16-episode task sequence, and all verifier feedback are deterministic, so
any arm/model sees the identical paired task stream and every verdict can be
re-derived from the logs. Verification is code, never an LLM (prompt-ab
doctrine: mechanical grading only).

Families (see DESIGN.md §2):
  gatekeeper  hidden policy rules over a 288-point config space; refusals name
              the violated field in prose but only a stable code in the ledger.
  strategy    12 feature combos -> 1 winning strategy of 5; win/loss is the
              ONLY feedback that exists, so outcome memory should suffice.
  trapcli     a fictional CLI with seeded undocumented quirks and cryptic,
              stable error codes; the fix is only visible across episodes.
"""

from __future__ import annotations

import json
import random
import re
import shlex
from dataclasses import dataclass, field
from itertools import product
from typing import Any

Action = dict[str, Any]

FAMILIES = ("gatekeeper", "strategy", "trapcli")


@dataclass
class Verdict:
    win: bool
    feedback: str  # prose feedback, visible in transcript-style memory
    code: str  # stable short code, the only detail in outcome-ledger memory
    detail: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> dict[str, Any] | None:
    """First parseable JSON object in a model response, tolerant of fences."""
    if not text:
        return None
    # Fast path: whole string.
    for candidate in (text.strip(), text.strip().strip("`")):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except (ValueError, TypeError):
                    continue
    return None


# ---------------------------------------------------------------------------
# gatekeeper
# ---------------------------------------------------------------------------

GATE_FIELDS: dict[str, list[str]] = {
    "mode": ["fast", "safe", "turbo"],
    "cache": ["on", "off"],
    "retries": ["0", "1", "2", "3"],
    "scope": ["narrow", "wide"],
    "fsync": ["on", "off"],
    "priority": ["low", "medium", "high"],
}

# (violated_field, predicate) — predicate returns True when the config is OK.
_GATE_RULE_BANK: list[tuple[str, Any]] = [
    ("retries", lambda c: c["retries"] in ("0", "1")),
    ("retries", lambda c: c["retries"] != "0"),
    ("mode", lambda c: c["mode"] != "turbo"),
    ("mode", lambda c: c["mode"] != "fast"),
    ("fsync", lambda c: not (c["cache"] == "off" and c["fsync"] != "on")),
    ("priority", lambda c: not (c["scope"] == "wide" and c["priority"] != "high")),
    ("priority", lambda c: c["priority"] != "low"),
    ("cache", lambda c: not (c["mode"] == "fast" and c["cache"] != "on")),
    ("retries", lambda c: not (c["fsync"] == "off" and c["retries"] not in ("2", "3"))),
    ("scope", lambda c: not (c["priority"] == "low" and c["scope"] != "narrow")),
]


def _all_gate_configs() -> list[dict[str, str]]:
    keys = list(GATE_FIELDS)
    return [dict(zip(keys, combo, strict=True)) for combo in product(*GATE_FIELDS.values())]


class GatekeeperWorld:
    family = "gatekeeper"

    def __init__(self, world_seed: int, n_episodes: int = 16) -> None:
        self.world_seed = world_seed
        rng = random.Random(f"gatekeeper:{world_seed}")
        configs = _all_gate_configs()
        chosen: list[int] = []
        feasible: list[dict[str, str]] = []
        for _ in range(200):
            chosen = sorted(rng.sample(range(len(_GATE_RULE_BANK)), 4))
            feasible = [c for c in configs if all(_GATE_RULE_BANK[i][1](c) for i in chosen)]
            frac = len(feasible) / len(configs)
            if 0.05 <= frac <= 0.60:
                break
        self.rule_indices = chosen
        self.feasible = feasible
        self.feasible_fraction = len(feasible) / len(configs)
        task_rng = random.Random(f"gatekeeper:{world_seed}:tasks")
        self.tasks: list[dict[str, Any]] = []
        for ep in range(1, n_episodes + 1):
            target = task_rng.choice(feasible)
            n_pins = task_rng.choice([1, 1, 2])
            pin_fields = task_rng.sample(list(GATE_FIELDS), n_pins)
            pins = {f: target[f] for f in pin_fields}
            pin_space = [c for c in configs if all(c[f] == v for f, v in pins.items())]
            pin_feasible = [c for c in feasible if all(c[f] == v for f, v in pins.items())]
            self.tasks.append(
                {
                    "ep": ep,
                    "pins": pins,
                    "chance": len(pin_feasible) / len(pin_space),
                }
            )

    def hidden_signature(self) -> tuple:
        return ("gatekeeper", tuple(self.rule_indices))

    def task(self, ep: int) -> dict[str, Any]:
        return self.tasks[ep - 1]

    def task_summary(self, task: dict[str, Any]) -> str:
        pins = " ".join(f"{k}={v}" for k, v in sorted(task["pins"].items()))
        return f"deploy requiring {pins}"

    def render_task(self, task: dict[str, Any]) -> str:
        pins = ", ".join(f"{k}={v}" for k, v in sorted(task["pins"].items()))
        allowed = "; ".join(f"{k}: {'|'.join(v)}" for k, v in GATE_FIELDS.items())
        example = {k: v[0] for k, v in GATE_FIELDS.items()}
        return (
            "You operate a deployment gate simulator. Submit a full configuration "
            "for this deployment.\n"
            f"Fields and allowed values: {allowed}\n"
            f"This deployment REQUIRES: {pins}\n"
            "The gate also enforces undocumented operator policies. Submissions "
            "violating them are refused.\n"
            "Respond with ONLY a JSON object with exactly the six field names as keys, "
            f"for example: {json.dumps(example)}"
        )

    def parse_action(self, text: str) -> Action | None:
        obj = extract_json(text)
        if obj is None:
            return None
        cfg: dict[str, str] = {}
        for k, values in GATE_FIELDS.items():
            v = obj.get(k)
            if isinstance(v, bool):
                v = "on" if v else "off"
            v = str(v).strip().lower() if v is not None else None
            if v not in values:
                return None
            cfg[k] = v
        return cfg

    def action_summary(self, action: Action | None) -> str:
        if not action:
            return "invalid-submission"
        return " ".join(f"{k}={action[k]}" for k in GATE_FIELDS)

    def verify(self, task: dict[str, Any], action: Action | None) -> Verdict:
        if not action:
            return Verdict(False, "REFUSED: not a valid submission (code MALFORMED)", "MALFORMED")
        # Sorted so feedback is identical when the task is replayed from a
        # sort_keys-serialized log line (HT-009).
        for f, v in sorted(task["pins"].items()):
            if action[f] != v:
                return Verdict(
                    False,
                    f"REFUSED: submission ignores the required setting {f}={v} (code REQ)",
                    "REQ",
                )
        for i in self.rule_indices:
            fieldname, pred = _GATE_RULE_BANK[i]
            if not pred(action):
                return Verdict(
                    False,
                    f"REFUSED: operator policy violation involving field '{fieldname}' (code P{i})",
                    f"P{i}",
                    {"field": fieldname},
                )
        return Verdict(True, "ACCEPTED (code OK)", "OK")

    def scripted_action(self, task: dict[str, Any], rng: random.Random) -> Action:
        cfg = {k: rng.choice(v) for k, v in GATE_FIELDS.items()}
        cfg.update(task["pins"])
        return cfg


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

STRATEGIES = ["bisect", "replay", "rollback", "patch-forward", "quarantine"]

STRATEGY_FEATURES: dict[str, list[str]] = {
    "failure_type": ["flaky", "regression", "corruption"],
    "blast_radius": ["low", "high"],
    "repro": ["deterministic", "nondeterministic"],
}

_BASE_RULE = {"regression": "bisect", "corruption": "rollback", "flaky": "quarantine"}


def _all_strategy_combos() -> list[tuple[str, str, str]]:
    return list(
        product(
            STRATEGY_FEATURES["failure_type"],
            STRATEGY_FEATURES["blast_radius"],
            STRATEGY_FEATURES["repro"],
        )
    )


class StrategyWorld:
    family = "strategy"

    def __init__(self, world_seed: int, n_episodes: int = 16) -> None:
        self.world_seed = world_seed
        rng = random.Random(f"strategy:{world_seed}")
        combos = _all_strategy_combos()
        self.mapping: dict[tuple[str, str, str], str] = {c: _BASE_RULE[c[0]] for c in combos}
        self.exceptions = sorted(rng.sample(combos, 3))
        for c in self.exceptions:
            others = [s for s in STRATEGIES if s != self.mapping[c]]
            self.mapping[c] = rng.choice(others)
        seq_rng = random.Random(f"strategy:{world_seed}:tasks")
        seq = combos[:]
        seq_rng.shuffle(seq)
        extras = list(self.exceptions) + [seq_rng.choice(combos)]
        seq_rng.shuffle(extras)
        seq = seq + extras
        # Deterministic extension past 16: repeat the same block structure with
        # the SAME rng stream, so the first 16 tasks are byte-identical to a
        # 16-episode world (round-9 prefix-preservation requirement).
        while len(seq) < n_episodes:
            block = combos[:]
            seq_rng.shuffle(block)
            more = list(self.exceptions) + [seq_rng.choice(combos)]
            seq_rng.shuffle(more)
            seq += block + more
        seq = seq[:n_episodes]
        self.tasks = [
            {"ep": i + 1, "features": dict(zip(STRATEGY_FEATURES, c, strict=True))}
            for i, c in enumerate(seq)
        ]

    def hidden_signature(self) -> tuple:
        return ("strategy", tuple(sorted(self.mapping.items())))

    def _combo(self, task: dict[str, Any]) -> tuple[str, str, str]:
        f = task["features"]
        return (f["failure_type"], f["blast_radius"], f["repro"])

    def task(self, ep: int) -> dict[str, Any]:
        return self.tasks[ep - 1]

    def task_summary(self, task: dict[str, Any]) -> str:
        f = task["features"]
        return f"{f['failure_type']}/{f['blast_radius']}-radius/{f['repro']}"

    def render_task(self, task: dict[str, Any]) -> str:
        f = task["features"]
        return (
            "You are the incident commander in a simulated operations environment. "
            "Exactly one remediation strategy succeeds for each incident profile; "
            "the mapping is specific to this environment and only partially matches "
            "intuition.\n"
            f"Incident: failure_type={f['failure_type']}, "
            f"blast_radius={f['blast_radius']}, repro={f['repro']}\n"
            f"Available strategies: {', '.join(STRATEGIES)}\n"
            'Respond with ONLY a JSON object: {"strategy": "<one of the strategies>"}'
        )

    def parse_action(self, text: str) -> Action | None:
        obj = extract_json(text)
        if obj is None:
            return None
        s = str(obj.get("strategy", "")).strip().lower().replace("_", "-").replace(" ", "-")
        if s not in STRATEGIES:
            return None
        return {"strategy": s}

    def action_summary(self, action: Action | None) -> str:
        return action["strategy"] if action else "invalid-submission"

    def verify(self, task: dict[str, Any], action: Action | None) -> Verdict:
        if not action:
            return Verdict(False, "FAILED: not a valid submission (code MALFORMED)", "MALFORMED")
        winner = self.mapping[self._combo(task)]
        if action["strategy"] == winner:
            return Verdict(True, "STRATEGY SUCCEEDED (code WIN)", "WIN")
        return Verdict(False, "STRATEGY FAILED (code LOSS)", "LOSS")

    def scripted_action(self, task: dict[str, Any], rng: random.Random) -> Action:
        return {"strategy": rng.choice(STRATEGIES)}


# ---------------------------------------------------------------------------
# trapcli
# ---------------------------------------------------------------------------

_QUIRKS = ("json_first", "ack_after_force", "out_suffix", "at_prefix", "upper_label", "purge_yes")
_ERROR_CATEGORIES = {
    "E11": "misplaced global flag",
    "E17": "missing companion flag",
    "E23": "invalid output path",
    "E31": "unknown name",
    "E41": "invalid label",
    "E52": "missing confirmation",
    "E02": "unexpected token",
}

_QUIRK_CODES = {
    "json_first": "E11",
    "ack_after_force": "E17",
    "out_suffix": "E23",
    "at_prefix": "E31",
    "upper_label": "E41",
    "purge_yes": "E52",
}

_PROJECTS = ["acme", "zephyr", "orbit", "lumen"]
_STORES = ["depot", "vault9", "mirror", "cache2"]
_ITEMS = ["item17", "node4", "batch88", "card3"]
_LABELS = ["urgent", "stale", "golden", "risky"]
_FILES = ["report", "dump1", "extract", "summary"]

MANPAGE = (
    "The `omnex` CLI (v3) documented usage:\n"
    "  omnex export <project> <outfile>   copy a project's items to a file\n"
    "  omnex sync <store> [--force]       synchronize a store (--force overrides locks)\n"
    "  omnex purge <store>                clear a store's cache\n"
    "  omnex tag <item> <label>           attach a label to an item\n"
    "  global flags: --json (JSON output; documented position: after the subcommand args)\n"
    "NOTE: this production deployment of omnex is known to enforce undocumented\n"
    "strictness beyond this manual; error codes are not documented anywhere."
)


class TrapCliWorld:
    family = "trapcli"

    def __init__(self, world_seed: int, n_episodes: int = 16) -> None:
        self.world_seed = world_seed
        rng = random.Random(f"trapcli:{world_seed}")
        self.quirks = set(rng.sample(_QUIRKS, 4))
        seq_rng = random.Random(f"trapcli:{world_seed}:tasks")
        self.tasks = []
        # Tasks are generated in 16-episode blocks, each block fully consuming
        # its shuffle THEN its field draws — so the first 16 tasks (shuffle +
        # draws in the original order) are byte-identical to a 16-episode
        # world, and extension past 16 continues the same rng stream (round 9).
        while len(self.tasks) < n_episodes:
            subs = ["export", "sync", "purge", "tag"] * 4
            seq_rng.shuffle(subs)
            for sub in subs:
                if len(self.tasks) >= n_episodes:
                    break
                t: dict[str, Any] = {"ep": len(self.tasks) + 1, "sub": sub}
                if sub == "export":
                    t["project"] = seq_rng.choice(_PROJECTS)
                    t["file"] = seq_rng.choice(_FILES)
                    t["json"] = seq_rng.random() < 0.5
                elif sub == "sync":
                    t["store"] = seq_rng.choice(_STORES)
                    t["force"] = seq_rng.random() < 0.6
                elif sub == "purge":
                    t["store"] = seq_rng.choice(_STORES)
                else:
                    t["item"] = seq_rng.choice(_ITEMS)
                    t["label"] = seq_rng.choice(_LABELS)
                self.tasks.append(t)

    def hidden_signature(self) -> tuple:
        return ("trapcli", tuple(sorted(self.quirks)))

    # -- canonical command construction ------------------------------------

    def _name(self, raw: str) -> str:
        return f"@{raw}" if "at_prefix" in self.quirks else raw

    def canonical_tokens(self, task: dict[str, Any]) -> list[str]:
        sub = task["sub"]
        tokens = ["omnex"]
        if task.get("json") and "json_first" in self.quirks:
            tokens.append("--json")
        tokens.append(sub)
        if sub == "export":
            out = task["file"] + (".out" if "out_suffix" in self.quirks else "")
            tokens += [self._name(task["project"]), out]
        elif sub == "sync":
            tokens.append(self._name(task["store"]))
            if task.get("force"):
                tokens.append("--force")
                if "ack_after_force" in self.quirks:
                    tokens.append("--ack")
        elif sub == "purge":
            tokens.append(self._name(task["store"]))
            if "purge_yes" in self.quirks:
                tokens.append("--yes")
        else:
            label = task["label"].upper() if "upper_label" in self.quirks else task["label"]
            tokens += [task["item"], label]
        if task.get("json") and "json_first" not in self.quirks:
            tokens.append("--json")
        return tokens

    def task(self, ep: int) -> dict[str, Any]:
        return self.tasks[ep - 1]

    def task_summary(self, task: dict[str, Any]) -> str:
        sub = task["sub"]
        if sub == "export":
            j = " as json" if task.get("json") else ""
            return f"export {task['project']} -> {task['file']}{j}"
        if sub == "sync":
            return f"sync {task['store']}{' (force)' if task.get('force') else ''}"
        if sub == "purge":
            return f"purge {task['store']}"
        return f"tag {task['item']} {task['label']}"

    def render_task(self, task: dict[str, Any]) -> str:
        sub = task["sub"]
        if sub == "export":
            prose = (
                f"Export the items of project '{task['project']}' to a file named '{task['file']}'."
            )
            if task.get("json"):
                prose += " The output must be requested in JSON format."
        elif sub == "sync":
            prose = f"Synchronize the store '{task['store']}'."
            if task.get("force"):
                prose += " Locks must be overridden (forced sync)."
        elif sub == "purge":
            prose = f"Clear the cache of store '{task['store']}'."
        else:
            prose = f"Attach the label '{task['label']}' to item '{task['item']}'."
        return (
            f"{MANPAGE}\n\nTASK: {prose}\n"
            'Respond with ONLY a JSON object: {"command": "omnex ..."}'
        )

    def parse_action(self, text: str) -> Action | None:
        obj = extract_json(text)
        if obj is None:
            return None
        cmd = obj.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        try:
            tokens = shlex.split(cmd.strip())
        except ValueError:
            return None
        if not tokens:
            return None
        return {"command": cmd.strip(), "tokens": tokens}

    def action_summary(self, action: Action | None) -> str:
        return action["command"] if action else "invalid-submission"

    def _fail(self, code: str, near: str) -> Verdict:
        # Category words locate the problem class (pilot amendment, DESIGN.md
        # §10) but never disclose the fix.
        category = _ERROR_CATEGORIES.get(code, "syntax error")
        return Verdict(False, f"error: {code} ({category}) near '{near}'", code)

    def verify(self, task: dict[str, Any], action: Action | None) -> Verdict:
        if not action:
            return Verdict(False, "error: E00 unreadable input (code MALFORMED)", "MALFORMED")
        tokens = list(action["tokens"])
        canonical = self.canonical_tokens(task)
        if tokens == canonical:
            return Verdict(True, "command executed (code OK)", "OK")
        # Stable, quirk-specific error codes for the most diagnostic divergence.
        norm = [t.lower() for t in tokens]
        sub = task["sub"]
        if task.get("json") and "json_first" in self.quirks and "--json" in norm:
            if len(norm) < 2 or norm[1] != "--json" or norm[0] != "omnex":
                return self._fail(_QUIRK_CODES["json_first"], "--json")
        if "--force" in norm and "ack_after_force" in self.quirks:
            i = norm.index("--force")
            if i + 1 >= len(norm) or norm[i + 1] != "--ack":
                return self._fail(_QUIRK_CODES["ack_after_force"], "--force")
        if sub == "export" and "out_suffix" in self.quirks:
            args = [t for t in tokens[1:] if not t.startswith("--") and t != sub]
            if len(args) >= 2 and not args[1].endswith(".out"):
                return self._fail(_QUIRK_CODES["out_suffix"], args[1])
        if "at_prefix" in self.quirks:
            raw = task.get("project") or task.get("store")
            if raw and raw in tokens:
                return self._fail(_QUIRK_CODES["at_prefix"], raw)
        if sub == "tag" and "upper_label" in self.quirks:
            if task["label"] in tokens:  # lowercase form used
                return self._fail(_QUIRK_CODES["upper_label"], task["label"])
        if (
            sub == "purge"
            and "purge_yes" in self.quirks
            and ("--yes" not in norm or norm[-1] != "--yes")
        ):
            near = tokens[-1] if tokens else "omnex"
            return self._fail(_QUIRK_CODES["purge_yes"], near)
        # Generic divergence: first token that differs from canonical.
        for got, want in zip(tokens, canonical, strict=False):
            if got != want:
                return self._fail("E02", got)
        near = tokens[len(canonical)] if len(tokens) > len(canonical) else "end-of-line"
        return self._fail("E02", near)

    def scripted_action(self, task: dict[str, Any], rng: random.Random) -> Action:
        if rng.random() < 0.4:
            tokens = self.canonical_tokens(task)
        else:
            # The plain documented form, ignoring quirks.
            plain = TrapCliWorld.__new__(TrapCliWorld)
            plain.quirks = set()
            tokens = TrapCliWorld.canonical_tokens(plain, task)
        cmd = " ".join(tokens)
        return {"command": cmd, "tokens": tokens}


# ---------------------------------------------------------------------------


def make_world(family: str, world_seed: int, n_episodes: int = 16):
    if family == "gatekeeper":
        return GatekeeperWorld(world_seed, n_episodes)
    if family == "strategy":
        return StrategyWorld(world_seed, n_episodes)
    if family == "trapcli":
        return TrapCliWorld(world_seed, n_episodes)
    raise ValueError(f"unknown family: {family}")


_WS = re.compile(r"\s+")


def squash(text: str, limit: int = 500) -> str:
    text = _WS.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
