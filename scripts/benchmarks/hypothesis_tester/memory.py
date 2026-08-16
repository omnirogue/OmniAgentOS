"""Memory arms: how past-run data is exposed to the agent (DESIGN.md §1).

Every arm is a deterministic pure function of the episode records it is given,
so a run can be resumed (or re-audited) from its logs and produce byte-identical
memory blocks.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any

from .worlds import GATE_FIELDS, STRATEGY_FEATURES, squash

ARMS = ("none", "outcomes", "transcripts", "lessons", "placebo", "activation", "recall", "corrupt")

TRANSCRIPT_K = 6
ACTIVATION_K = 3
MEMORY_CHAR_BUDGET = 7000

_FIELD_RE = re.compile(r"field '(\w+)'")


@dataclass
class EpisodeRecord:
    ep: int
    task: dict[str, Any]
    task_summary: str
    task_text: str
    response_text: str
    action: dict[str, Any] | None
    action_summary: str
    parse_ok: bool
    win: bool
    code: str
    feedback: str


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def est_tokens(text: str) -> int:
    return len(text) // 4


def _outcome_lines(records: list[EpisodeRecord]) -> str:
    lines = [
        "Outcome ledger from previous episodes in this same environment (your own past attempts):"
    ]
    for r in records:
        verdict = "WIN" if r.win else "LOSS"
        lines.append(
            f"episode {r.ep}: task[{r.task_summary}] action[{r.action_summary}] "
            f"-> {verdict} (code {r.code})"
        )
    return "\n".join(lines)


def _transcript_block(r: EpisodeRecord) -> str:
    # The task line is the compact task summary, never a truncated render:
    # blind 400-char truncation dropped the trapcli TASK sentence entirely
    # (it sits after the ~550-char man page), so transcript arms silently
    # lost the one line that identified the attempted task (HT-002).
    return (
        f"--- past episode {r.ep} ---\n"
        f"TASK: {squash(r.task_summary, 200)}\n"
        f"AGENT RESPONSE: {squash(r.response_text, 400)}\n"
        f"RESULT: {r.feedback}"
    )


def _transcripts(records: list[EpisodeRecord], k: int, header: str) -> str:
    blocks = [_transcript_block(r) for r in records[-k:]]
    text = header + "\n" + "\n".join(blocks)
    while len(text) > MEMORY_CHAR_BUDGET and len(blocks) > 1:
        blocks.pop(0)
        text = header + "\n" + "\n".join(blocks)
    return text


def _gatekeeper_lessons(records: list[EpisodeRecord]) -> list[str]:
    lines: list[str] = []
    by_code: dict[str, list[EpisodeRecord]] = {}
    for r in records:
        if not r.win and r.code not in ("MALFORMED",):
            by_code.setdefault(r.code, []).append(r)
    for code, rs in sorted(by_code.items()):
        m = _FIELD_RE.search(rs[0].feedback)
        fld = f" (field '{m.group(1)}')" if m else ""
        tried = "; ".join(r.action_summary for r in rs[-3:])
        lines.append(
            f"refusal code {code}{fld}: {len(rs)}x, e.g. [{tried}] (eps {[r.ep for r in rs]})"
        )
    wins = [r for r in records if r.win]
    if wins:
        ok = "; ".join(r.action_summary for r in wins[-3:])
        lines.append(f"accepted submissions so far ({len(wins)}): [{ok}]")
    for fld in GATE_FIELDS:
        stats: dict[str, list[int]] = {}
        for r in records:
            if r.action:
                v = r.action.get(fld)
                if v is not None:
                    s = stats.setdefault(str(v), [0, 0])
                    s[0] += 1 if r.win else 0
                    s[1] += 1
        if stats:
            cells = ", ".join(f"{v}: {w}/{n} accepted" for v, (w, n) in sorted(stats.items()))
            lines.append(f"{fld} -> {cells}")
    return lines


def _strategy_lessons(records: list[EpisodeRecord]) -> list[str]:
    by_combo: dict[str, dict[str, list[str]]] = {}
    for r in records:
        if not r.action:
            continue
        f = r.task["features"]
        combo = "/".join(f[k] for k in STRATEGY_FEATURES)
        by_combo.setdefault(combo, {}).setdefault(r.action_summary, []).append(
            "WIN" if r.win else "LOSS"
        )
    lines = []
    for combo, tries in sorted(by_combo.items()):
        cells = "; ".join(f"{s} -> {','.join(res)}" for s, res in sorted(tries.items()))
        lines.append(f"incident {combo}: {cells}")
    return lines


def _trapcli_lessons(records: list[EpisodeRecord]) -> list[str]:
    lines: list[str] = []
    by_code: dict[str, list[EpisodeRecord]] = {}
    for r in records:
        if not r.win and r.code not in ("MALFORMED",):
            by_code.setdefault(r.code, []).append(r)
    for code, rs in sorted(by_code.items()):
        ex = "; ".join(f"`{squash(r.action_summary, 90)}` ({r.feedback})" for r in rs[-2:])
        lines.append(f"error {code}: {len(rs)}x, e.g. {ex}")
    wins = [r for r in records if r.win]
    for r in wins[-4:]:
        lines.append(f"worked (ep {r.ep}, {r.task_summary}): `{squash(r.action_summary, 90)}`")
    return lines


def _lessons(family: str, records: list[EpisodeRecord]) -> str:
    if family == "gatekeeper":
        lines = _gatekeeper_lessons(records)
    elif family == "strategy":
        lines = _strategy_lessons(records)
    else:
        lines = _trapcli_lessons(records)
    header = (
        "Consolidated lessons mechanically derived from your previous episodes "
        "in this same environment (with provenance):"
    )
    return header + "\n" + "\n".join(f"- {ln}" for ln in lines)


def _overlap(family: str, cur: dict[str, Any], past: dict[str, Any]) -> float:
    if family == "gatekeeper":
        score = 0.0
        for f, v in cur.get("pins", {}).items():
            if f in past.get("pins", {}):
                score += 1.0
                if past["pins"][f] == v:
                    score += 1.0
        return score
    if family == "strategy":
        cf, pf = cur["features"], past["features"]
        return float(sum(1 for k in STRATEGY_FEATURES if cf[k] == pf[k]))
    score = 2.0 if cur["sub"] == past["sub"] else 0.0
    if cur.get("json") and past.get("json"):
        score += 1.0
    if cur.get("force") and past.get("force"):
        score += 1.0
    return score


def activation_scores(
    family: str, current_task: dict[str, Any], records: list[EpisodeRecord]
) -> list[tuple[float, EpisodeRecord]]:
    n = len(records)
    scored = []
    for i, r in enumerate(records):
        s = _overlap(family, current_task, r.task)
        if r.win:
            s += 1.5 if family == "strategy" else 1.0
        elif r.code != "MALFORMED":
            s += 1.0 if family == "strategy" else 1.5
        if n - i <= 3:
            s += 0.5
        scored.append((s, r))
    return scored


def _activation(family: str, current_task: dict[str, Any], records: list[EpisodeRecord]) -> str:
    scored = activation_scores(family, current_task, records)
    top = sorted(scored, key=lambda t: (-t[0], -t[1].ep))[:ACTIVATION_K]
    chosen = sorted((r for _, r in top), key=lambda r: r.ep)
    header = (
        "The episodes below were selected from your history as most relevant to "
        "the current task (relevance x outcome-informativeness x recency):"
    )
    return header + "\n" + "\n".join(_transcript_block(r) for r in chosen)


_RECALL_HEADER = (
    "Consolidated recall mechanically derived from your previous episodes in this "
    "same environment, matched to the current task (with provenance):"
)


def _gatekeeper_recall(records: list[EpisodeRecord], task: dict[str, Any]) -> list[str]:
    import json as _json

    pins = task.get("pins", {})
    wins = [r for r in records if r.win and r.action]
    matching = [r for r in wins if all(r.action.get(f) == v for f, v in pins.items())]
    lines: list[str] = []
    if matching:
        r = matching[-1]
        lines.append(
            f"a configuration you already got ACCEPTED (episode {r.ep}) satisfies "
            f"every current requirement — submitting it EXACTLY is known to pass: "
            f"{_json.dumps(r.action, sort_keys=True)}"
        )
        return lines
    refused = {
        tuple(sorted(r.action.items()))
        for r in records
        if not r.win and r.action and r.code != "MALFORMED"
    }
    best = None
    for r in wins:
        cand = dict(r.action)
        cand.update(pins)
        changed = sum(1 for f in cand if cand[f] != r.action.get(f))
        if tuple(sorted(cand.items())) in refused:
            continue
        if best is None or changed < best[0]:
            best = (changed, r, cand)
    if best:
        changed, r, cand = best
        diff = ", ".join(f"{f}: {r.action[f]}->{cand[f]}" for f in cand if cand[f] != r.action.get(f))
        lines.append(
            f"closest ACCEPTED config (episode {r.ep}) adapted to the current "
            f"requirement by changing only [{diff}]; this exact variant has never "
            f"been refused — submit it: {_json.dumps(cand, sort_keys=True)}"
        )
    for r in wins[-3:]:
        lines.append(
            f"accepted before (episode {r.ep}, requirement [{r.task_summary}]): "
            f"{_json.dumps(r.action, sort_keys=True)} — differs from the current "
            f"requirement only in the required settings; keep its other fields."
        )
    if not wins:
        lines.append("no submission has been accepted yet.")
        # Coordinate descent: the refusal names one field — freeze the rest.
        fails = [r for r in records if not r.win and r.action and r.code != "MALFORMED"]
        if fails:
            last = fails[-1]
            fld = None
            if last.code == "REQ":
                m = re.search(r"required setting (\w+)=(\w+)", last.feedback)
                if m:
                    lines.append(
                        f"your last submission {_json.dumps(last.action, sort_keys=True)} "
                        f"was REFUSED only because it ignored the requirement "
                        f"{m.group(1)}={m.group(2)} — resubmit it with EXACTLY that "
                        f"one field changed to '{m.group(2)}'."
                    )
            else:
                m = _FIELD_RE.search(last.feedback)
                fld = m.group(1) if m else None
                if fld and fld in GATE_FIELDS:
                    base = {k: v for k, v in last.action.items() if k != fld}
                    refused = sorted({
                        f.action[fld]
                        for f in fails
                        if f.action and {k: v for k, v in f.action.items() if k != fld} == base
                        and fld in f.action
                    })
                    untried = [v for v in GATE_FIELDS[fld] if v not in refused]
                    lines.append(
                        f"your last submission {_json.dumps(last.action, sort_keys=True)} "
                        f"was REFUSED for field '{fld}'. Keep the other five fields "
                        f"EXACTLY as submitted and change ONLY '{fld}'. Values of "
                        f"'{fld}' already refused with this base: "
                        f"{', '.join(refused) if refused else '(none)'}; not yet "
                        f"tried: {', '.join(untried) if untried else '(none — change a different field)'}."
                    )
    lines.extend(_gatekeeper_lessons(records))
    return lines


def _strategy_recall(records: list[EpisodeRecord], task: dict[str, Any]) -> list[str]:
    from .worlds import STRATEGIES

    cur = task["features"]
    combo = "/".join(cur[k] for k in STRATEGY_FEATURES)
    same = [r for r in records if r.action and r.task["features"] == cur]
    lines: list[str] = []
    same_wins = [r for r in same if r.win]
    if same_wins:
        r = same_wins[-1]
        lines.append(
            f"KNOWN CORRECT for this exact incident profile ({combo}): "
            f"'{r.action_summary}' (won episode {r.ep}). Play it."
        )
        return lines
    # Forced-choice elimination ledger: failed choices appear only as a
    # do-not-choose constraint, never in an imitable answer slot.
    failed_here = sorted({r.action_summary for r in same if not r.win})
    if failed_here:
        remaining = [s for s in STRATEGIES if s not in failed_here]
        lines.append(f"THIS profile ({combo}) — PROVEN WRONG (do not choose): {', '.join(failed_here)}")
        lines.append(f"THIS profile ({combo}) — NOT YET TRIED: {', '.join(remaining)}")
        lines.append("RULE: pick from the NOT YET TRIED list only.")
        counts: dict[str, int] = {}
        for r in same:
            if not r.win:
                counts[r.action_summary] = counts.get(r.action_summary, 0) + 1
        flagged = [s for s, n in counts.items() if n >= 2]
        if flagged:
            lines.append(
                f"NOTE: {', '.join(sorted(flagged))} lost repeatedly here — this "
                f"profile is an EXCEPTION; do not reason from what usually works, "
                f"use only the ledger above."
            )
        return lines
    ft = cur["failure_type"]
    stats: dict[str, list[int]] = {}
    for r in records:
        if r.action and r.task["features"]["failure_type"] == ft:
            s = stats.setdefault(r.action_summary, [0, 0])
            s[0] += 1 if r.win else 0
            s[1] += 1
    if stats:
        cells = ", ".join(f"{s}: {w}/{n}" for s, (w, n) in sorted(stats.items()))
        lines.append(f"win/tries per strategy across other failure_type={ft} profiles: {cells}")
    if not lines:
        lines.append("no informative history for this profile yet.")
    return lines


_TRAPCLI_NAME_FIELDS = ("project", "store", "item", "label", "file")


def _trapcli_shape(t: dict[str, Any]) -> tuple:
    return (t["sub"], bool(t.get("json")), bool(t.get("force")))


def _trapcli_adapt(win_task: dict[str, Any], win_cmd: str, cur: dict[str, Any]) -> str:
    """Mechanical case adaptation: swap the past task's name values for the
    current task's, preserving any surrounding decoration (prefixes, suffixes,
    upper-casing) the winning command carried. Pure string substitution — no
    knowledge of the world's quirks is used."""
    adapted = win_cmd
    for fld in _TRAPCLI_NAME_FIELDS:
        old, new = win_task.get(fld), cur.get(fld)
        if not old or not new or old == new:
            continue
        adapted = adapted.replace(str(old).upper(), str(new).upper())
        adapted = adapted.replace(str(old), str(new))
    return adapted


def _trapcli_documented_tokens(task: dict[str, Any]) -> list[str]:
    """The DOCUMENTED command for a task, straight from the man page shown in
    every episode prompt (public information; encodes zero undocumented
    behavior — same standing as GATE_FIELDS/STRATEGIES imports above)."""
    sub = task["sub"]
    toks = ["omnex", sub]
    if sub == "export":
        toks += [task["project"], task["file"]]
    elif sub == "sync":
        toks.append(task["store"])
        if task.get("force"):
            toks.append("--force")
    elif sub == "purge":
        toks.append(task["store"])
    else:
        toks += [task["item"], task["label"]]
    if task.get("json"):
        toks.append("--json")
    return toks


def _trapcli_rules(wins: list[EpisodeRecord]) -> list[tuple[str, str, dict[str, Any]]]:
    """Generic transformation rules mechanically diffed from ACCEPTED commands
    vs their documented form. Each rule = (kind, provenance, params). Only
    generic string transforms are detected (prefix / suffix / case / insertion
    / flag move) — nothing quirk-specific is encoded."""
    rules: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for w in wins:
        doc = _trapcli_documented_tokens(w.task)
        got = list(w.action.get("tokens") or [])
        prov = f"ep {w.ep}: `{w.action['command']}`"
        doc_set = set(doc)
        for tok in got:
            if tok.startswith("@") and tok[1:] in doc_set:
                rules.setdefault("at_prefix", ("at_prefix", prov, {}))
            for d in doc:
                if tok != d and tok == d + ".out":
                    rules.setdefault("out_suffix", ("out_suffix", prov, {}))
                if tok != d and tok == d.upper():
                    rules.setdefault("upper", ("upper", prov, {"slot": "label"}))
        if "--json" in got and "--json" in doc and got.index("--json") == 1 != doc.index("--json"):
            rules.setdefault("json_first", ("json_first", prov, {}))
        extras = [t for t in got if t not in doc_set and not (t.startswith("@") and t[1:] in doc_set)
                  and not any(t in (d + ".out", d.upper()) for d in doc)]
        for t in extras:
            i = got.index(t)
            prev = got[i - 1] if i else ""
            if prev.startswith("--"):
                rules.setdefault(f"after:{prev}:{t}", (f"after:{prev}:{t}", prov, {"anchor": prev, "flag": t}))
            else:
                rules.setdefault(f"end:{w.task['sub']}:{t}", (f"end:{w.task['sub']}:{t}", prov,
                                                             {"sub": w.task["sub"], "flag": t}))
    return list(rules.values())


_RULE_PROSE = {
    "at_prefix": "names are written with a leading '@'",
    "out_suffix": "output filenames carry a '.out' suffix",
    "upper": "labels are written in UPPERCASE",
    "json_first": "'--json' goes immediately after 'omnex', before the subcommand",
}


def _trapcli_apply_rules(task: dict[str, Any], rules) -> str:
    toks = _trapcli_documented_tokens(task)
    kinds = {r[0].split(":")[0] if ":" in r[0] else r[0] for r in rules}
    names = {v for k, v in task.items() if k in ("project", "store") and isinstance(v, str)}
    out = []
    for t in toks:
        if "at_prefix" in kinds and t in names:
            t = "@" + t
        if "out_suffix" in kinds and t == task.get("file"):
            t = t + ".out"
        if "upper" in kinds and t == task.get("label"):
            t = t.upper()
        out.append(t)
    for r in rules:
        p = r[2]
        if r[0].startswith("after:") and p["anchor"] in out:
            i = out.index(p["anchor"])
            if p["flag"] not in out:
                out.insert(i + 1, p["flag"])
        if r[0].startswith("end:") and p["sub"] == task["sub"] and p["flag"] not in out:
            out.append(p["flag"])
    if "json_first" in kinds and "--json" in out and out.index("--json") != 1:
        out.remove("--json")
        out.insert(1, "--json")
    return " ".join(out)


def _trapcli_recall(records: list[EpisodeRecord], task: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    wins = [r for r in records if r.win and r.action]
    shape = _trapcli_shape(task)
    same_shape = [r for r in wins if _trapcli_shape(r.task) == shape]
    if same_shape:
        r = same_shape[-1]
        adapted = _trapcli_adapt(r.task, r.action["command"], task)
        lines.append(
            f"a command of EXACTLY this kind already worked (episode {r.ep}, "
            f"{r.task_summary}): `{r.action['command']}`. With the current task's "
            f"names substituted in, submit EXACTLY: `{adapted}`"
        )
        return lines
    same_sub = [r for r in wins if r.task["sub"] == task["sub"]]
    if same_sub:
        r = same_sub[-1]
        lines.append(
            f"a '{task['sub']}' command already worked (episode {r.ep}, "
            f"{r.task_summary}): `{r.action['command']}` — copy its exact shape "
            f"(flag order, name spelling, casing), changing only the names and "
            f"flags your task differs in."
        )
    for r in wins[-3:]:
        if r.task["sub"] != task["sub"]:
            lines.append(
                f"worked for '{r.task['sub']}' (episode {r.ep}): "
                f"`{r.action['command']}` — note how names/labels are spelled: "
                f"the same undocumented strictness applies to every subcommand."
            )
    # Rule extraction: generic transforms diffed from ACCEPTED commands vs
    # their documented form, then applied to the CURRENT task's documented
    # form — one win on any subcommand bootstraps every other.
    rules = _trapcli_rules(wins)
    if rules:
        prose = []
        for kind, prov, params in rules:
            base = kind.split(":")[0]
            if base in _RULE_PROSE:
                prose.append(f"{_RULE_PROSE[base]} (from {prov})")
            elif base == "after":
                prose.append(f"'{params['anchor']}' must be followed by '{params['flag']}' (from {prov})")
            else:
                prose.append(f"'{task['sub']}'-style commands end with '{params['flag']}' (from {prov})"
                             if params.get("sub") == task["sub"]
                             else f"'{params['sub']}' commands end with '{params['flag']}' (from {prov})")
        lines.append(
            "undocumented rules inferred mechanically from your ACCEPTED commands "
            "(each backed by a win): " + "; ".join(prose)
        )
        built = _trapcli_apply_rules(task, rules)
        lines.append(
            f"applying every inferred rule to the documented form of the current "
            f"task gives: `{built}` — submit that unless it contradicts a "
            f"stronger line above."
        )
    # Verified-fix table: an error code that later turned into a win for the
    # same shape is a solved case — render the failing->working pair.
    fails = [r for r in records if not r.win and r.action and r.code not in ("MALFORMED",)]
    solved: dict[str, str] = {}
    for f in fails:
        for w in wins:
            if _trapcli_shape(w.task) == _trapcli_shape(f.task) and w.ep > f.ep:
                solved[f.code] = (
                    f"error {f.code}: refused `{f.action['command']}` (ep {f.ep}) but "
                    f"`{w.action['command']}` was ACCEPTED (ep {w.ep}) — the difference "
                    f"between them is the undocumented rule."
                )
                break
    lines.extend(solved[c] for c in sorted(solved))
    # No verified fix yet: freeze everything except the token the error names.
    same_shape_fails = [f for f in fails if _trapcli_shape(f.task) == shape and f.code not in solved]
    if same_shape_fails and not same_shape:
        r = same_shape_fails[-1]
        m = re.search(r"near '([^']*)'", r.feedback)
        near = m.group(1) if m else "the reported token"
        variants = sorted({f.action["command"] for f in same_shape_fails})
        lines.append(
            "REFUSED variants for this kind of task (never resubmit any verbatim): "
            + "; ".join(f"`{v}`" for v in variants)
        )
        repeat_count = sum(
            1 for f in same_shape_fails if f.action["command"] == r.action["command"]
        )
        if repeat_count >= 2:
            lines.append(
                f"WARNING: you have submitted `{r.action['command']}` {repeat_count} "
                f"times and it was refused EVERY time — a repeated command never "
                f"succeeds. You MUST alter it this episode."
            )
        # Keep the actionable directive LAST (recency position). The edit TYPE
        # is a mechanical paraphrase of the error's own category words — text
        # the model already saw — never of anything undocumented.
        category_edit = ""
        fb = r.feedback
        if "missing companion flag" in fb:
            category_edit = (f" The category says a COMPANION FLAG is MISSING: keep the whole "
                             f"command and ADD one new flag token immediately after '{near}'.")
        elif "missing confirmation" in fb:
            category_edit = (" The category says a CONFIRMATION is MISSING: keep the whole "
                             "command and ADD one new flag token at the end that expresses "
                             "confirmation.")
        elif "invalid output path" in fb:
            category_edit = (f" The category says the OUTPUT PATH '{near}' is INVALID in form: "
                             f"keep its name but write the filename token in a different FORM "
                             f"(for example a different extension or suffix).")
        elif "misplaced global flag" in fb:
            category_edit = (f" The category says '{near}' is MISPLACED: keep every token but "
                             f"MOVE '{near}' to a different position in the command.")
        elif "unknown name" in fb:
            category_edit = (f" The category says the name '{near}' is UNKNOWN as written, yet "
                             f"the task guarantees it exists: keep the name but DECORATE it "
                             f"(write it with an extra prefix or marker character).")
        elif "invalid label" in fb:
            category_edit = (f" The category says the label '{near}' is INVALID as written: "
                             f"keep the word but write it in a different form (for example a "
                             f"different case).")
        lines.append(
            f"your most recent attempt at EXACTLY this kind of task (episode {r.ep}, "
            f"{r.task_summary}) was REFUSED: `{r.action['command']}` -> {r.feedback}. "
            f"Change ONLY the part involving '{near}', into something you have never "
            f"submitted.{category_edit}"
        )
    lines.extend(_trapcli_lessons([r for r in records if not r.win]))
    if not lines:
        lines.append("no informative history yet.")
    return lines


def _recall(family: str, records: list[EpisodeRecord], task: dict[str, Any]) -> str:
    if family == "gatekeeper":
        lines = _gatekeeper_recall(records, task)
    elif family == "strategy":
        lines = _strategy_recall(records, task)
    else:
        lines = _trapcli_recall(records, task)
    text = _RECALL_HEADER + "\n" + "\n".join(f"- {ln}" for ln in lines)
    if len(text) > MEMORY_CHAR_BUDGET:
        kept = list(lines)
        while len(text) > MEMORY_CHAR_BUDGET and len(kept) > 1:
            kept.pop()
            text = _RECALL_HEADER + "\n" + "\n".join(f"- {ln}" for ln in kept)
    return text


_SUCCESS_FEEDBACK = {
    "gatekeeper": "ACCEPTED (code OK)",
    "strategy": "STRATEGY SUCCEEDED (code WIN)",
    "trapcli": "command executed (code OK)",
}


def _corrupt(family: str, records: list[EpisodeRecord]) -> str:
    """Counterfactual D (DESIGN.md §10 round 11): the agent's OWN history with
    every verdict FLIPPED — wins rendered as failures (using a real refusal
    text from this run when one exists) and failures rendered as successes.
    Deterministic pure function of records; measures poisoning susceptibility,
    not a competitor memory representation."""
    fail_texts = [r.feedback for r in records if not r.win and r.code != "MALFORMED"]
    blocks = []
    for r in records[-TRANSCRIPT_K:]:
        if r.win:
            fake = fail_texts[r.ep % len(fail_texts)] if fail_texts else "REFUSED (code P0)"
        else:
            fake = _SUCCESS_FEEDBACK[family]
        blocks.append(
            f"--- past episode {r.ep} ---\n"
            f"TASK: {squash(r.task_summary, 200)}\n"
            f"AGENT RESPONSE: {squash(r.response_text, 400)}\n"
            f"RESULT: {fake}"
        )
    header = (
        "Records from previous episodes in this same environment (your own past attempts):"
    )
    text = header + "\n" + "\n".join(blocks)
    while len(text) > MEMORY_CHAR_BUDGET and len(blocks) > 1:
        blocks.pop(0)
        text = header + "\n" + "\n".join(blocks)
    return text


def memory_block(
    arm: str,
    family: str,
    records: list[EpisodeRecord],
    current_task: dict[str, Any],
    shadow: list[EpisodeRecord] | None = None,
) -> str:
    """Render the memory text an agent sees at this episode. '' means no section."""
    if arm == "none":
        return ""
    if arm == "placebo":
        visible = (shadow or [])[: len(records)]
        if not visible:
            return ""
        return _transcripts(
            visible,
            TRANSCRIPT_K,
            "Records from previous episodes in this same environment (your own past attempts):",
        )
    if not records:
        return ""
    if arm == "outcomes":
        return _outcome_lines(records)
    if arm == "transcripts":
        return _transcripts(
            records,
            TRANSCRIPT_K,
            "Records from previous episodes in this same environment (your own past attempts):",
        )
    if arm == "lessons":
        return _lessons(family, records)
    if arm == "activation":
        return _activation(family, current_task, records)
    if arm == "recall":
        return _recall(family, records, current_task)
    if arm == "corrupt":
        return _corrupt(family, records)
    raise ValueError(f"unknown arm: {arm}")


def build_scripted_history(world, n: int, seed_tag: str) -> list[EpisodeRecord]:
    """Deterministic scripted-agent history against `world` (placebo shadows)."""
    import json as _json

    rng = random.Random(seed_tag)
    out: list[EpisodeRecord] = []
    for ep in range(1, n + 1):
        task = world.task(ep)
        action = world.scripted_action(task, rng)
        display = {k: v for k, v in action.items() if k != "tokens"}
        verdict = world.verify(task, action)
        out.append(
            EpisodeRecord(
                ep=ep,
                task=task,
                task_summary=world.task_summary(task),
                task_text=world.render_task(task),
                response_text=_json.dumps(display),
                action=action,
                action_summary=world.action_summary(action),
                parse_ok=True,
                win=verdict.win,
                code=verdict.code,
                feedback=verdict.feedback,
            )
        )
    return out
