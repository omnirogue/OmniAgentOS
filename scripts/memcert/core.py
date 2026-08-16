"""memcert core: shared data shapes, normalization, and per-item scoring algebra.

Design of record: devtasks/memcert/DESIGN.md (§6 scoring, §12 contracts).
This module is dependency-free (stdlib only) and deterministic: every random
draw anywhere in memcert must come from ``rng_for(seed, name)``.

Scoring algebra (DESIGN §6, OpenAI arXiv:2509.04664 t=1/3):
    correct = 1.0
    correct abstention (absence item)            = 1.0
    abstention on an answerable item             = 0.0
    wrong                                        = -0.5
    stale (a retracted/superseded value, MEM-F)  = -1.0
    params kind: F1 == 1 -> 1.0 ; 0 < F1 < 1 -> 0.5 * F1 ; F1 == 0 -> -0.5
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

SUITE_GUID = "memcert-7f3d9a2e-1b64-4c05-9e8a-52e0c4b7a911"
ABSTAIN_TOKEN = "UNKNOWN"
AXES = ("A", "B", "C", "D", "E", "F", "G", "H")
SPLITS = ("dev", "cert")

# Identical answer-generation protocol for EVERY arm and model (industry trap:
# prompt asymmetry between arms invalidates comparisons).
ANSWER_PROTOCOL = (
    "Answer using ONLY the information in your provided context/memory.\n"
    "Reply with exactly one line in the form:\n"
    "ANSWER: <your answer>\n"
    f"If the context/memory does not contain the answer, reply exactly:\n"
    f"ANSWER: {ABSTAIN_TOKEN}\n"
    "No explanation, no extra lines.\n"
    "Example: ANSWER: 2027-03-05\n"
    f"Example when the information is absent: ANSWER: {ABSTAIN_TOKEN}"
)

# Real models abstain in prose ("I don't have access to...") despite the
# protocol (measured live 2026-08-12: 7/8 no-context replies were prose
# refusals graded wrong). A refusal is an abstention, not a wrong answer —
# grading it -0.5 distorts every arm-vs-floor delta. Patterns are deliberately
# tight refusal semantics; matched against the EXTRACTED answer text.
_ABSTAIN_PHRASES = re.compile(
    r"(i (do not|don't|dont) (have|know)|not enough information"
    r"|no (such )?information (about|on|regarding|available)"
    r"|(cannot|can't|unable to) (determine|find|answer|locate)"
    r"|is not (specified|provided|mentioned|stated|available|present)"
    r"|(context|memory|conversation|history|provided information)"
    r" does not (contain|include|specify|mention|state)"
    r"|no memory of|not in (my|the) (context|memory))",
    re.IGNORECASE,
)


def is_abstention(extracted_answer: str) -> bool:
    """True when the reply is the abstain token or a prose refusal."""
    ans = normalize_answer(extracted_answer)
    if ans == normalize_answer(ABSTAIN_TOKEN):
        return True
    return bool(_ABSTAIN_PHRASES.search(extracted_answer))
ACTION_PROTOCOL = (
    "Respond with ONLY a single JSON object of the form "
    '{"tool": "<tool-name>", "args": {<key>: <value>, ...}} '
    "using the constraints recorded in your context/memory. "
    f'If the context/memory does not specify the action, respond {{"tool": "{ABSTAIN_TOKEN}", "args": {{}}}}.'
)


def stable_hash(name: str) -> int:
    """64-bit stable hash (process-seed independent, unlike ``hash``)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def rng_for(seed: int, name: str) -> random.Random:
    return random.Random(seed ^ stable_hash(name))


def canary_line(run_uuid: str) -> str:
    return f"MEMCERT-CANARY {SUITE_GUID}:{run_uuid}"


_ARTICLES = re.compile(r"\b(the|a|an)\b")
_PUNCT = re.compile(r"[^\w\s./:-]")
_WS = re.compile(r"\s+")
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july august september october november december".split()
    )
}
_DATE_WORDY = re.compile(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})\b")


def _canon_dates(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        month = _MONTHS.get(m.group(1))
        if month is None:
            return m.group(0)
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"

    return _DATE_WORDY.sub(repl, text)


_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")


def normalize_answer(text: str) -> str:
    """Lowercase, canonicalize wordy dates to ISO, strip punctuation/articles.

    Thousands separators are removed ("52,400" == "52400") and trailing
    sentence punctuation is stripped ("blue vault." == "blue vault") — both
    misgraded real model output as wrong before the 2026-08-12 build review.
    """
    t = text.strip().lower()
    t = _DIGIT_COMMA.sub("", t)
    t = _canon_dates(t)
    t = _PUNCT.sub(" ", t)
    t = _ARTICLES.sub(" ", t)
    return _WS.sub(" ", t).strip().rstrip(" .:,;")


@dataclass(frozen=True)
class AnswerSpec:
    """What counts as correct. NEVER serialized into fixture output for arms.

    kind: exact | set | ordered | params | abstain
    value: str for exact; list[str] for set/ordered; dict for params
    aliases: alternate normalized-equal forms accepted for ``exact``
    stale_values: superseded values that score -1.0 (MEM-D/MEM-F)

    Fixture-author constraints (build review 2026-08-12): params value MUST be
    ``{"tool": ..., "args": {...}}`` — a value without an ``args`` key grades
    against the remaining top-level keys, which is rarely what you meant.
    set/ordered member values must not contain "," ";" or " and " — those are
    the reply separators.
    """

    kind: str
    value: Any
    aliases: tuple[str, ...] = ()
    stale_values: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "aliases": list(self.aliases),
            "stale_values": list(self.stale_values),
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> AnswerSpec:
        return AnswerSpec(
            kind=d["kind"],
            value=d["value"],
            aliases=tuple(d.get("aliases", ())),
            stale_values=tuple(d.get("stale_values", ())),
        )


@dataclass(frozen=True)
class Item:
    item_id: str
    axis: str  # one of AXES
    level: int  # 1..3
    split: str  # dev | cert
    question: str
    answer_spec: AnswerSpec
    session_scope: tuple[str, ...] = ()
    cluster_id: str = ""  # fixture-world id: the clustering unit for error bars
    arm_overrides: dict[str, Any] = field(default_factory=dict)  # MEM-G lesson routing

    def public_json(self) -> dict[str, Any]:
        """Serialization WITHOUT the answer spec — the only form written to fixtures."""
        return {
            "item_id": self.item_id,
            "axis": self.axis,
            "level": self.level,
            "split": self.split,
            "question": self.question,
            "session_scope": list(self.session_scope),
            "cluster_id": self.cluster_id,
            "arm_overrides": self.arm_overrides,
        }


@dataclass
class ArmContext:
    """What an arm injects. Same answer protocol is appended by the runner."""

    arm: str
    context_block: str
    meta: dict[str, Any] = field(default_factory=dict)


_ANSWER_RE = re.compile(r"^\s*ANSWER\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _contains_word(haystack_norm: str, needle_norm: str) -> bool:
    """Word-boundary containment over already-normalized strings."""
    return f" {needle_norm} " in f" {haystack_norm} "


def extract_answer(raw: str) -> str:
    """Pull the answer line out of a model reply; falls back to trimmed text."""
    matches = _ANSWER_RE.findall(raw or "")
    if matches:
        return matches[-1].strip()
    return (raw or "").strip()


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """First balanced top-level JSON object in the text, or None."""
    s = raw or ""
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = s.find("{", start + 1)
    return None


def _params_f1(expected: dict[str, Any], got: dict[str, Any]) -> float:
    exp = {(k, normalize_answer(json.dumps(v) if not isinstance(v, str) else v)) for k, v in expected.items()}
    act = {(k, normalize_answer(json.dumps(v) if not isinstance(v, str) else v)) for k, v in got.items()}
    if not exp:
        return 1.0 if not act else 0.0
    tp = len(exp & act)
    if tp == 0:
        return 0.0
    precision = tp / len(act) if act else 0.0
    recall = tp / len(exp)
    return 2 * precision * recall / (precision + recall)


def grade_item(spec: AnswerSpec, raw_answer: str) -> tuple[str, float]:
    """Return (verdict, score). Verdicts: correct | abstain_correct | abstain_miss |
    wrong | stale | partial."""
    if spec.kind == "params":
        got = extract_json_object(raw_answer)
        # Dialect tolerance (measured live 2026-08-12): models trained on the
        # OpenAI function-calling shape emit {"name": ..., "arguments": {...}}
        # with the REMEMBERED VALUES correct — grade the memory, not the JSON
        # dialect.
        if got is not None and "tool" not in got and "name" in got:
            remapped_args = got.get("arguments") or got.get("parameters") or got.get("args")
            got = {"tool": got["name"], "args": remapped_args if isinstance(remapped_args, dict) else {}}
        if got is None or "tool" not in got:
            if is_abstention(raw_answer or ""):
                return ("abstain_miss", 0.0)
            return ("wrong", -0.5)
        if got.get("tool") == ABSTAIN_TOKEN:
            return ("abstain_miss", 0.0)
        expected = dict(spec.value)
        exp_tool = expected.pop("tool", None)
        got_args = got.get("args") if isinstance(got.get("args"), dict) else {}
        if not got_args and isinstance(got.get("arguments"), dict):
            got_args = got["arguments"]
        exp_args = expected.get("args", expected)
        tool_ok = exp_tool is None or normalize_answer(str(got.get("tool"))) == normalize_answer(str(exp_tool))
        f1 = _params_f1(dict(exp_args), dict(got_args)) if tool_ok else 0.0
        if f1 == 1.0:
            return ("correct", 1.0)
        if f1 > 0.0:
            return ("partial", 0.5 * f1)
        return ("wrong", -0.5)

    extracted = extract_answer(raw_answer)
    ans = normalize_answer(extracted)
    # An explicit ANSWER: line with a non-abstain value is an ASSERTION — it
    # overrides any hedge phrasing around it (Grok review BLOCKER-1: hedged
    # answers must not launder through abstention detection).
    answer_line = _ANSWER_RE.findall(raw_answer or "")
    asserted_value = bool(answer_line) and normalize_answer(answer_line[-1]) != normalize_answer(
        ABSTAIN_TOKEN
    )
    abstained = (not asserted_value) and is_abstention(extracted)

    if spec.kind == "abstain":
        return ("abstain_correct", 1.0) if abstained else ("wrong", -0.5)

    # Verdict priority (Grok review BLOCKER-1): correct > stale > abstain >
    # wrong. A hedged reply that still CONTAINS the expected value graded the
    # memory correctly; a hedged reply containing a superseded value acted on
    # stale memory. Only a reply asserting neither can count as abstention.
    if spec.kind == "exact":
        accepted = {normalize_answer(str(spec.value))} | {normalize_answer(a) for a in spec.aliases}
        # Containment fallback (measured live 2026-08-12): on long contexts,
        # models answer correctly in prose ("deploys now run on **Fridays**")
        # while dropping the ANSWER: line — standard normalized-containment EM
        # with a stale guard. A reply naming BOTH the current and a superseded
        # value is an ambiguous claim and stays wrong; superseded-only is stale.
        # Known gaming vectors accepted for v1 and documented in DESIGN §6 +
        # LANE.md residual risks: enumerate-every-candidate (stale guard kills
        # it on update/retraction axes) and hedged-wrong grading 0.0 instead of
        # -0.5 (hedging can lift -0.5 to 0.0, never to 1.0).
        exp_hit = ans in accepted or any(a and _contains_word(ans, a) for a in accepted)
        stale_hit = any(
            _contains_word(ans, normalize_answer(str(s))) for s in spec.stale_values
        )
        if exp_hit and stale_hit:
            return ("wrong", -0.5)
        if exp_hit:
            return ("correct", 1.0)
        if stale_hit:
            return ("stale", -1.0)
        if abstained:
            return ("abstain_miss", 0.0)
        return ("wrong", -0.5)

    if abstained:
        return ("abstain_miss", 0.0)

    for stale in spec.stale_values:
        if ans == normalize_answer(str(stale)):
            return ("stale", -1.0)

    if spec.kind in ("set", "ordered"):
        expected_list = [normalize_answer(str(v)) for v in spec.value]
        got_parts = [normalize_answer(p) for p in re.split(r"[,;\n]| and ", extract_answer(raw_answer)) if p.strip()]
        if spec.kind == "set":
            ok = set(got_parts) == set(expected_list)
        else:
            ok = got_parts == expected_list
        return ("correct", 1.0) if ok else ("wrong", -0.5)

    raise ValueError(f"unknown answer kind: {spec.kind}")
