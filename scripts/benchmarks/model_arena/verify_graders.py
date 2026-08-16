"""Self-check for the Tier B graders: a good answer must score 1.0 and a
plausible-but-wrong answer must not. Run this before spending contestant tokens —
a grader without teeth produces a benchmark that means nothing.

    python -m scripts.benchmarks.model_arena.verify_graders
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.benchmarks.model_arena.tasks_b import TIER_B  # noqa: E402

# (task_id, reference answer that must score 1.0, wrong answer that must score < 0.6)
CASES: dict[str, tuple[str, str]] = {
    "needle_long_context": (
        "Consignment SHP-7741-QX93 was re-routed through the Trondheim depot.",
        "Consignment SHP-1234-AB12 was re-routed through the Osaka depot.",
    ),
    "json_schema_strict": (
        '{"service":"checkout-api","replicas":12,"healthy":true,'
        '"regions":["us-east","eu-central"],'
        '"owner":{"team":"payments-core","pager":"+1-555-867-5309"}}',
        '{"service":"checkout-api","replicas":12,"healthy":true,'
        '"regions":["us-east","eu-central"],"tier":"gold",'
        '"owner":{"team":"payments-core","pager":"555-867-5309"}}',
    ),
    "messy_extraction": (
        '{"purchase_order":"PO-00002","vendor_name":"Vandelay Example AS",'
        '"vat_number":"NO 999 999 999 MVA","amount_ex_vat_nok":40000,'
        '"amount_inc_vat_nok":50000,"due_date":"2026-09-15",'
        '"contact_email":"alice@vandelay.example"}',
        '{"purchase_order":"PO-00001","vendor_name":"Vandelay Example ANS",'
        '"vat_number":"unknown","amount_ex_vat_nok":45000,'
        '"amount_inc_vat_nok":56250,"due_date":"2026-08-14",'
        '"contact_email":"frank@vandelay.example"}',
    ),
    "math_multistep": (
        "Total produced 1200. QC scrap 72, passing 1128. Re-melt gives 9 more.\n1137",
        "Total produced 1200, 6% scrapped leaves 1128 sellable widgets.\n1128",
    ),
    "sql_synthesis": (
        "SELECT c.country, SUM(o.amount_cents) AS total FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.status = 'paid' "
        "GROUP BY c.country HAVING COUNT(*) >= 2 ORDER BY total DESC",
        "SELECT c.country, SUM(o.amount_cents) AS total FROM orders o "
        "JOIN customers c ON c.id = o.customer_id GROUP BY c.country ORDER BY total DESC",
    ),
    "regex_synthesis": (
        r"^[A-Z]{2}-[1-9][0-9]{3}-(?:RED|BLU|GRN)$",
        r"[A-Za-z]{2}-\d{4}-\w+",
    ),
    "bug_hunt_review": (
        "Line 10: `time` is never imported, so __init__ raises NameError. "
        "Line 11: the lock is created but never acquired in allow(), so concurrent "
        "callers race on self.tokens — not thread-safe. "
        "Line 9: tokens initialise to 0 rather than capacity, so no initial burst is allowed. "
        "Line 26: keys.remove(k) mutates the list while iterating it, which skips elements. "
        "Line 25: self.pending is never defined in __init__, so drain() raises AttributeError.",
        "The code looks fine overall; consider adding type hints and a docstring for drain, "
        "and renaming `last` to `last_refill` for readability.",
    ),
    "hallucination_guard": (
        "There is no such method — `io.BufferedReader` does not provide "
        "chunked_readinto in any Python version. You may be thinking of readinto().",
        "chunked_readinto(buf, *, stride=None) was added in Python 3.11. It returns "
        "an int with the number of bytes read into buf.",
    ),
}


# A correct answer preceded by an inline <think> scratchpad must still score 1.0.
# The scratchpad deliberately contains a *wrong* draft (a bad pager, the wrong city,
# the wrong number) so a grader that reads the thinking instead of the answer fails.
THINK_WRAPPED: dict[str, str] = {
    "needle_long_context": (
        "<think>Maybe Osaka? No, scanning again for the customs hold line.</think>\n"
        "Consignment SHP-7741-QX93 was re-routed through the Trondheim depot."
    ),
    "json_schema_strict": (
        '<think>Draft: {"service":"checkout-api","pager":"555"} — wrong, pager needs '
        "the +1-NNN-NNN-NNNN shape. Let me redo it.</think>\n"
        '{"service":"checkout-api","replicas":12,"healthy":true,'
        '"regions":["us-east","eu-central"],'
        '"owner":{"team":"payments-core","pager":"+1-555-867-5309"}}'
    ),
    "math_multistep": (
        "<think>1200 total, 6% is 72, so 1128. But the re-melt adds more.</think>\n"
        "Sellable widgets: 1137"
    ),
    "sql_synthesis": (
        "<think>First idea: no HAVING clause. That would wrongly include DK.</think>\n"
        "SELECT c.country, SUM(o.amount_cents) AS total FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.status = 'paid' "
        "GROUP BY c.country HAVING COUNT(*) >= 2 ORDER BY total DESC"
    ),
}


def main() -> int:
    failures = 0
    for task in TIER_B:
        good, bad = CASES[task.task_id]
        g_score, g_note = task.grade(good)
        b_score, b_note = task.grade(bad)
        ok_good, ok_bad = g_score >= 0.999, b_score < 0.6
        status = "ok  " if (ok_good and ok_bad) else "FAIL"
        if not (ok_good and ok_bad):
            failures += 1
        print(f"[{status}] {task.task_id:22} good={g_score:.2f} ({g_note})")
        print(f"{'':29} bad={b_score:.2f} ({b_note})")

        wrapped = THINK_WRAPPED.get(task.task_id)
        if wrapped is not None:
            t_score, t_note = task.grade(wrapped)
            ok = t_score >= 0.999
            if not ok:
                failures += 1
            print(f"[{'ok  ' if ok else 'FAIL'}] {'':22} <think>-wrapped={t_score:.2f} ({t_note})")
    print()
    if failures:
        print(f"{failures} grader(s) lack teeth — fix before running the arena")
        return 1
    print("all graders verified: reference answers score 1.0, wrong answers score < 0.6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
