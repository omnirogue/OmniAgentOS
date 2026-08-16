"""Tier B: eight non-coding tests, every one machine-graded.

Each task exposes ``prompt`` (plus optional ``system``) and a ``grade`` callable
returning ``(score_0_to_1, note)``. No LLM judging happens here — a judge pass
runs later over the saved outputs, so the primary quality number is objective
and reproducible.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

JSON_ONLY = (
    "Reply with the requested content only. No preamble, no explanation, no markdown fences."
)


@dataclass
class TaskB:
    task_id: str
    kind: str
    prompt: str
    grade: Callable[[str], tuple[float, str]]
    system: str | None = None
    max_tokens: int = 4096


_THINK_BLOCK = re.compile(r"<(think|thinking|reason|reasoning)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(?:think|thinking|reason|reasoning)>.*$", re.S | re.I)


def _visible(text: str) -> str:
    """Drop inline reasoning so only the model's actual answer is graded.

    Several open models (LFM2.5, Qwen3.5) emit `<think>...</think>` in the content
    channel rather than a separate reasoning field. That broke the JSON fallback
    badly: it took the first `{` (a *draft* object inside the thinking) and the last
    `}`, producing unparseable junk, and scored 0 for a model that had in fact
    answered correctly after `</think>`. Numbers and keywords in the scratchpad were
    polluting the maths and keyword graders the same way.

    An unclosed opener means the model never stopped thinking, so there is no visible
    answer and the result is empty — which is the correct score, not a parse bug.
    """
    return _THINK_OPEN.sub("", _THINK_BLOCK.sub("", text)).strip()


def _strip_fences(text: str) -> str:
    """Pull the payload out of a ```-fenced block when the model adds one."""
    body = _visible(text)
    fence = re.search(r"```(?:json|sql|python|regex|text)?\s*\n(.*?)```", body, re.S)
    return (fence.group(1) if fence else body).strip()


def _first_json_object(text: str) -> dict | list | None:
    body = _strip_fences(text)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost brace/bracket span.
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = body.find(opener), body.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(body[i : j + 1])
            except json.JSONDecodeError:
                continue
    return None


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in re.findall(r"-?\d[\d,]*\.?\d*", text)]


# --------------------------------------------------------------------------
# B1 — long-context needle retrieval (also stresses prefill throughput)
# --------------------------------------------------------------------------

_NEEDLE_ID = "SHP-7741-QX93"
_NEEDLE_CITY = "Trondheim"


def _build_haystack(lines: int = 2600) -> str:
    """Deterministic synthetic shipping log with one fact buried mid-document."""
    cities = ["Lisbon", "Osaka", "Denver", "Cairo", "Perth", "Lyon", "Bogota", "Tallinn"]
    out = []
    needle_at = int(lines * 0.63)
    for i in range(lines):
        if i == needle_at:
            out.append(
                f"[{i:05d}] AUDIT anomaly: consignment {_NEEDLE_ID} was re-routed "
                f"through the {_NEEDLE_CITY} depot after a customs hold."
            )
            continue
        city = cities[i % len(cities)]
        out.append(
            f"[{i:05d}] INFO consignment SHP-{1000 + (i * 7) % 8999}-"
            f"{chr(65 + i % 26)}{chr(65 + (i * 3) % 26)}{10 + i % 89} "
            f"cleared the {city} depot; weight {40 + i % 900}kg; status nominal."
        )
    return "\n".join(out)


_HAYSTACK = _build_haystack()


def _grade_needle(text: str) -> tuple[float, str]:
    got = _visible(text)
    if _NEEDLE_CITY.lower() in got.lower():
        return 1.0, f"found {_NEEDLE_CITY}"
    return 0.0, f"missed; said {got[:90]!r}"


B1 = TaskB(
    task_id="needle_long_context",
    kind="retrieval",
    max_tokens=256,
    prompt=(
        "Below is a shipping audit log. Exactly one line records a consignment that was "
        f"re-routed after a customs hold. Name the depot city it was re-routed through, and "
        "give the consignment id. Answer in one short sentence.\n\n"
        f"<log>\n{_HAYSTACK}\n</log>"
    ),
    grade=_grade_needle,
)


# --------------------------------------------------------------------------
# B2 — strict JSON schema compliance
# --------------------------------------------------------------------------

_B2_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "replicas", "healthy", "regions", "owner"],
    "properties": {
        "service": {"type": "string"},
        "replicas": {"type": "integer", "minimum": 1, "maximum": 99},
        "healthy": {"type": "boolean"},
        "regions": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {"enum": ["us-east", "us-west", "eu-central", "ap-south"]},
        },
        "owner": {
            "type": "object",
            "additionalProperties": False,
            "required": ["team", "pager"],
            "properties": {
                "team": {"type": "string"},
                "pager": {"type": "string", "pattern": r"^\+1-\d{3}-\d{3}-\d{4}$"},
            },
        },
    },
}


def _grade_schema(text: str) -> tuple[float, str]:
    import jsonschema  # type: ignore[import-untyped]

    obj = _first_json_object(text)
    if obj is None:
        return 0.0, "no parseable JSON"
    score = 0.5  # parsed at all
    try:
        jsonschema.validate(obj, _B2_SCHEMA)
        score = 1.0
        note = "schema valid"
    except jsonschema.ValidationError as exc:
        note = f"invalid: {exc.message[:110]}"
    # Penalise prose wrapped around the JSON — the instruction said JSON only. Judge
    # the visible answer, not the raw stream: a `<think>` scratchpad is not prose the
    # caller sees, and penalising it would mark down every inline-reasoning model.
    if not _visible(text).startswith(("{", "```")):
        score -= 0.15
        note += "; leading prose"
    return max(0.0, score), note


B2 = TaskB(
    task_id="json_schema_strict",
    kind="instruction-following",
    max_tokens=1024,
    system=JSON_ONLY,
    prompt=(
        "Emit a single JSON object describing a deployment, obeying every rule:\n"
        "- keys exactly: service, replicas, healthy, regions, owner (no others, at any level)\n"
        '- service: string, the value "checkout-api"\n'
        "- replicas: integer between 1 and 99 inclusive, use 12\n"
        "- healthy: boolean true\n"
        "- regions: array of 2 to 4 values drawn ONLY from "
        '["us-east", "us-west", "eu-central", "ap-south"]; include exactly us-east and eu-central\n'
        '- owner: object with exactly the keys team and pager; team is "payments-core"; '
        "pager matches the pattern +1-NNN-NNN-NNNN\n"
        "Output the JSON object and nothing else."
    ),
    grade=_grade_schema,
)


# --------------------------------------------------------------------------
# B3 — structured extraction from a messy source (partial credit per field)
# --------------------------------------------------------------------------

_B3_SOURCE = """\
fwd: RE: re: URGENT?? invoice thing

hey - chasing the Q3 thing again. So the PO we raised back in may was PO-00001 but
finance rejected that one (wrong cost centre) and reissued it as PO-00002, use that.

Vendor is Vandelay Example AS (they changed name from Vandelay Example ANS last year,
old name still on some paperwork). Their VAT is NO 999 999 999 MVA.

Amount: they first quoted 45,000 NOK but after the scope cut it's 40,000 NOK ex-VAT.
VAT 25% on top. Do NOT pay the 45,000.

Due date got pushed - was 14 Aug, agreed new terms net-45 from invoice date which was
1 Aug, so you can work it out.

Contact there is now Alice (alice@vandelay.example) - Frank left in June,
his address bounces.
"""

_B3_EXPECTED = {
    "purchase_order": (["po-00002"], ["po-00001"]),
    "vendor_name": (["vandelay example as"], ["ans"]),
    "vat_number": (["999999999", "999 999 999"], []),
    "amount_ex_vat_nok": (["40000", "40,000"], ["45000", "45,000"]),
    "amount_inc_vat_nok": (["50000", "50,000"], []),
    "due_date": (["2026-09-15", "15 sep", "sep 15", "15/09", "september 15"], ["14 aug"]),
    "contact_email": (["alice@vandelay.example"], ["frank"]),
}


def _grade_extract(text: str) -> tuple[float, str]:
    obj = _first_json_object(text)
    if not isinstance(obj, dict):
        return 0.0, "no JSON object"
    flat = json.dumps(obj, ensure_ascii=False).lower()
    hits, misses = 0, []
    for field, (accept, reject) in _B3_EXPECTED.items():
        blob = str(obj.get(field, "")).lower() or flat
        if any(a in blob.replace(" ", "") or a in blob for a in accept) and not any(
            r in str(obj.get(field, "")).lower() for r in reject
        ):
            hits += 1
        else:
            misses.append(field)
    return hits / len(_B3_EXPECTED), f"{hits}/{len(_B3_EXPECTED)} fields; missed {misses}"


B3 = TaskB(
    task_id="messy_extraction",
    kind="extraction",
    max_tokens=1500,
    system=JSON_ONLY,
    prompt=(
        "Extract the currently-valid facts from this email thread into one JSON object with "
        "exactly these keys: purchase_order, vendor_name, vat_number, amount_ex_vat_nok, "
        "amount_inc_vat_nok, due_date, contact_email.\n"
        "Superseded values must not appear. Compute amount_inc_vat_nok yourself. "
        "Give due_date as YYYY-MM-DD (the invoice date is 1 August 2026).\n\n"
        f"<thread>\n{_B3_SOURCE}\n</thread>"
    ),
    grade=_grade_extract,
)


# --------------------------------------------------------------------------
# B4 — multi-step quantitative reasoning (unique integer answer: 1137)
# --------------------------------------------------------------------------


def _grade_math(text: str) -> tuple[float, str]:
    nums = _numbers(_visible(text))
    if not nums:
        return 0.0, "no number found"
    if 1137 in nums[-4:]:  # answer should land near the end
        return 1.0, "correct (1137)"
    if 1137 in nums:
        return 0.8, "correct value present but not stated as the final answer"
    if 1128 in nums:
        return 0.3, "1128 — forgot the re-melt replacements"
    return 0.0, f"wrong; final numbers {nums[-3:]}"


B4 = TaskB(
    task_id="math_multistep",
    kind="reasoning",
    max_tokens=3000,
    prompt=(
        "A workshop runs an 8-hour shift with three machines.\n"
        "- Machine A makes 50 widgets per hour for the whole shift.\n"
        "- Machine C makes 52 widgets per hour for the whole shift.\n"
        "- Machine B makes 38 widgets per hour, but it fails exactly 4 hours in and is "
        "immediately swapped for a loaner making 58 widgets per hour for the rest of the shift.\n"
        "At the end of the shift, 6% of all widgets produced fail QC and are scrapped. "
        "The scrapped widgets are re-melted, and every 8 scrapped widgets yield exactly 1 "
        "extra widget that passes QC.\n"
        "How many sellable widgets does the workshop have at the end of the shift? "
        "Show your working, then state the final number on its own last line."
    ),
    grade=_grade_math,
)


# --------------------------------------------------------------------------
# B5 — SQL synthesis, graded by executing it against a seeded fixture
# --------------------------------------------------------------------------

_B5_DDL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, country TEXT);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, customer_id INTEGER, amount_cents INTEGER,
  status TEXT, created_at TEXT
);
"""

_B5_ROWS = """
INSERT INTO customers VALUES
 (1,'Ada','NO'),(2,'Bo','NO'),(3,'Cy','SE'),(4,'Di','SE'),(5,'Ed','DK'),(6,'Fi','FI');
INSERT INTO orders VALUES
 (1,1,10000,'paid','2026-01-05'),(2,1,25000,'paid','2026-02-11'),
 (3,2,5000,'refunded','2026-01-19'),(4,2,7000,'paid','2026-03-02'),
 (5,3,90000,'paid','2026-01-21'),(6,4,1000,'pending','2026-02-01'),
 (7,4,60000,'paid','2026-02-14'),(8,5,45000,'paid','2026-03-09'),
 (9,6,30000,'paid','2026-01-30'),(10,3,15000,'paid','2026-04-04'),
 (11,5,2000,'refunded','2026-04-06'),(12,1,3000,'pending','2026-04-08');
"""

# paid totals by country: NO = 10000+25000+7000 = 42000 (3 orders)
#                         SE = 90000+60000+15000 = 165000 (3 orders)
#                         DK = 45000 (1 order)  -> excluded, <2 paid orders
#                         FI = 30000 (1 order)  -> excluded
_B5_EXPECTED = [("SE", 165000), ("NO", 42000)]


def _grade_sql(text: str) -> tuple[float, str]:
    sql = _strip_fences(text)
    sql = re.sub(r"^\s*(sql|SQL)\s*:\s*", "", sql).strip().rstrip(";")
    if not sql:
        return 0.0, "empty"
    if re.search(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|ATTACH)\b", sql, re.I):
        return 0.0, "non-SELECT statement"
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_B5_DDL + _B5_ROWS)
        rows = conn.execute(sql).fetchall()
    except Exception as exc:  # noqa: BLE001
        return 0.0, f"sql error: {type(exc).__name__}: {str(exc)[:90]}"
    finally:
        conn.close()
    norm = [(str(r[0]).strip().upper(), int(r[1])) for r in rows if len(r) >= 2]
    if norm == _B5_EXPECTED:
        return 1.0, "exact result set and order"
    if sorted(norm) == sorted(_B5_EXPECTED):
        return 0.75, "right rows, wrong ordering"
    return 0.0, f"got {norm[:4]}"


B5 = TaskB(
    task_id="sql_synthesis",
    kind="code-adjacent",
    max_tokens=1200,
    system="Reply with one SQLite SELECT statement only. No commentary.",
    prompt=(
        "SQLite schema:\n"
        "  customers(id INTEGER PRIMARY KEY, name TEXT, country TEXT)\n"
        "  orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount_cents INTEGER, "
        "status TEXT, created_at TEXT)\n\n"
        "Write one SELECT returning exactly two columns — country, then the summed "
        "amount_cents of that country's orders whose status is 'paid'. Include only "
        "countries having at least 2 such paid orders. Sort by the summed total, "
        "highest first. Return the statement only."
    ),
    grade=_grade_sql,
)


# --------------------------------------------------------------------------
# B6 — regex synthesis, graded against positive/negative cases
# --------------------------------------------------------------------------

_B6_POS = ["AB-1234-RED", "ZZ-9999-BLU", "QX-1000-GRN", "MM-5678-RED"]

# Negatives grouped by the rule they violate. A rule counts as enforced only if
# *every* one of its cases is rejected, and the final score multiplies positive
# recall by rule coverage — so a pattern that nails the shape but ignores three
# rules cannot coast to a passing number on the structural cases alone.
_B6_RULES: dict[str, list[str]] = {
    "letter_case": ["ab-1234-RED", "aB-1234-RED"],
    "no_leading_zero": ["AB-0234-RED", "AB-0000-RED"],
    "exactly_four_digits": ["AB-123-RED", "AB-12345-RED"],
    "colour_whitelist": ["AB-1234-PNK", "AB-1234-BLUE"],
    "colour_case": ["AB-1234-red", "AB-1234-Red"],
    "exactly_two_letters": ["A-1234-RED", "ABC-1234-RED"],
    "hyphen_separators": ["AB1234RED", "AB_1234_RED"],
    "no_surrounding_text": [" AB-1234-RED", "AB-1234-RED ", "XAB-1234-REDX"],
}


def _grade_regex(text: str) -> tuple[float, str]:
    body = _strip_fences(text).strip().strip("`")
    # Accept r"..." / "..." / /.../ wrappers as well as a bare pattern.
    m = re.match(r'^(?:r?["\']|/)(.*)(?:["\']|/)$', body, re.S)
    pattern = (m.group(1) if m else body).strip()
    if not pattern or "\n" in pattern:
        pattern = body.splitlines()[0].strip() if body else ""
    if not pattern:
        return 0.0, "no pattern"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return 0.0, f"uncompilable: {exc}"
    matched = sum(1 for s in _B6_POS if rx.fullmatch(s))
    pos_rate = matched / len(_B6_POS)
    enforced = [
        rule for rule, cases in _B6_RULES.items() if not any(rx.fullmatch(c) for c in cases)
    ]
    rule_rate = len(enforced) / len(_B6_RULES)
    broken = [r for r in _B6_RULES if r not in enforced]
    return (
        pos_rate * rule_rate,
        f"valid {matched}/{len(_B6_POS)}, rules {len(enforced)}/{len(_B6_RULES)}"
        + (f", broke {broken}" if broken else "")
        + f"; pattern={pattern[:60]!r}",
    )


B6 = TaskB(
    task_id="regex_synthesis",
    kind="code-adjacent",
    max_tokens=900,
    system="Reply with the regex pattern only, on one line, no delimiters and no explanation.",
    prompt=(
        "Write a Python regular expression that fullmatch-es an asset tag and nothing else.\n"
        "Valid tag: exactly two UPPERCASE A-Z letters, a hyphen, a 4-digit number whose first "
        "digit is not 0, a hyphen, then exactly one of RED, BLU, GRN in uppercase.\n"
        "Nothing may precede or follow the tag. Output the pattern only."
    ),
    grade=_grade_regex,
)


# --------------------------------------------------------------------------
# B7 — planted-bug code review, scored by recall of the 5 real defects
# --------------------------------------------------------------------------

_B7_CODE = """\
 1  import threading
 2
 3  class RateLimiter:
 4      \"\"\"Token bucket. refill_rate tokens per second, capacity max tokens.\"\"\"
 5
 6      def __init__(self, capacity, refill_rate):
 7          self.capacity = capacity
 8          self.refill_rate = refill_rate
 9          self.tokens = 0
10          self.last = time.monotonic()
11          self.lock = threading.Lock()
12
13      def allow(self, cost=1):
14          now = time.monotonic()
15          elapsed = now - self.last
16          self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
17          self.last = now
18          if self.tokens >= cost:
19              self.tokens -= cost
20              return True
21          return False
22
23      def drain(self, keys):
24          for k in keys:
25              if k in self.pending:
26                  keys.remove(k)
27          return keys
"""

_B7_BUGS = {
    "missing_time_import": [r"import\s+time", r"\btime\b.*(not|never)\s+import", r"NameError"],
    "unused_lock": [r"lock", r"race", r"thread[- ]?safe", r"not.*acquir"],
    "starts_empty": [r"tokens\s*=\s*0", r"start.*empty", r"initiali[sz]e.*capacity", r"burst"],
    "mutate_while_iterating": [
        r"mutat",
        r"modif.*iterat",
        r"remove.*(while|during).*iterat",
        r"skip",
    ],
    "missing_pending_attr": [
        r"self\.pending",
        r"pending.*(not|never).*(defined|initiali)",
        r"AttributeError",
    ],
}


def _grade_bug_hunt(text: str) -> tuple[float, str]:
    # Same markdown-emphasis hazard as the hallucination grader: these answers are
    # heavily formatted, and `**self.pending**` must still match `self\.pending`.
    low = _normalise(text)
    found = [name for name, pats in _B7_BUGS.items() if any(re.search(p, low, re.I) for p in pats)]
    missed = [b for b in _B7_BUGS if b not in found]
    return len(found) / len(_B7_BUGS), f"{len(found)}/{len(_B7_BUGS)}; missed {missed}"


B7 = TaskB(
    task_id="bug_hunt_review",
    kind="code-review",
    max_tokens=3000,
    prompt=(
        "Review this Python class and list every real defect. For each, give the line number "
        "and say concretely what goes wrong. Do not suggest stylistic changes.\n\n"
        f"```python\n{_B7_CODE}```"
    ),
    grade=_grade_bug_hunt,
)


# --------------------------------------------------------------------------
# B8 — hallucination resistance: the thing asked about does not exist
# --------------------------------------------------------------------------

_ADMITS = [
    r"do(es)?n'?t\s+exist",
    r"not\s+a\s+(real|valid|standard)",
    r"no\s+such\s+(function|method|attribute|api)",
    r"unable to (find|verify|confirm)",
    r"couldn'?t find",
    r"not\s+part\s+of",
    r"i'?m not aware",
    r"appears?\s+to\s+be\s+(fictional|made[- ]up)",
    r"no\s+.{0,20}in\s+the\s+standard\s+library",
    r"not documented",
    r"you may be (thinking|confusing)",
]
# Only *positive* documentation counts as fabrication. Patterns that merely mention
# a feature match denials too — "no `stride` parameter" is a correct refusal, and an
# earlier draft scored grok-4.5 0.50 for it. Every pattern here must be a claim that
# the method exists and behaves a certain way.
_FABRICATES = [
    r"def\s+chunked_readinto",
    r"chunked_readinto\s*\([^)]*\)\s*(->|:)",
    r"(added|introduced|available|new)\s+in\s+python\s+3",
    r"(version|python)\s+added\s*:?\s*python\s*3",
    r"returns?\s+(a|an)\s+(\w+\s+)?(object|integer|int|count)\b",
]


def _normalise(text: str) -> str:
    """Lowercase and strip markdown emphasis before pattern matching.

    Models format these answers heavily, and `added in **Python 3.13**` does not
    match `in\\s+python` — the bold markers sit between the words. Left unhandled,
    a fully fabricated answer scored 0.25 ("neither denied nor fabricated") instead
    of 0.0, which flattered the model that invented an API.
    """
    cleaned = re.sub(r"[*_`~]+", "", _visible(text).lower())
    return re.sub(r"[ \t]+", " ", cleaned)


def _grade_hallucination(text: str) -> tuple[float, str]:
    low = _normalise(text)
    admits = [p for p in _ADMITS if re.search(p, low)]
    fabricates = [p for p in _FABRICATES if re.search(p, low)]
    if admits and not fabricates:
        return 1.0, "correctly denied"
    if admits and fabricates:
        return 0.5, "hedged but also fabricated detail"
    if fabricates:
        return 0.0, "fabricated an API"
    return 0.25, "neither clearly denied nor fabricated"


B8 = TaskB(
    task_id="hallucination_guard",
    kind="calibration",
    max_tokens=1500,
    prompt=(
        "Document Python's `io.BufferedReader.chunked_readinto(buf, *, stride=None)` method: "
        "give its exact signature, what it returns, which Python version added it, and a "
        "short usage example."
    ),
    grade=_grade_hallucination,
)


TIER_B: list[TaskB] = [B1, B2, B3, B4, B5, B6, B7, B8]
