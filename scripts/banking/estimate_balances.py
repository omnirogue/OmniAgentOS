#!/usr/bin/env python3
"""Anchored bank-balance estimator.

current_estimate = real_anchor + (qbo_book_now - qbo_book_at_anchor)

Reads configs/bank_anchors.json (the anchors) and the live QuickBooks book
balances (qbo_book_now). The delta cancels QuickBooks' unreconciled historical
offset — only movement since the anchor is applied to the real balance.
Re-anchoring resets drift to zero.

qbo_book_now source, in order:
  1. a JSON arg on the command line: `estimate_balances.py '{"136": 528255.58}'`
  2. otherwise the Google Sheet named in configs/bank_anchors.json
     (google_sheets.read via the broker — headless-capable Google OAuth; the
     sheet is refreshed daily by a native Zapier Zap). Falls back to
     qbo_book_at_anchor (delta 0) if the sheet can't be read.

Runs unattended: the QuickBooks pull happens on Zapier's servers into the sheet;
this reads the sheet over Google OAuth, which works in cron/headless jobs.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANCHORS = os.path.join(ROOT, "configs", "bank_anchors.json")


def qbo_from_sheet(cfg: dict) -> dict:
    src = cfg.get("qbo_book_source") or {}
    sid = src.get("spreadsheet_id")
    if not sid:
        return {}
    try:
        from omniagentos.connectors import broker

        rng = src.get("range", "Sheet1!A2:D50")
        r = broker.call(
            "google_sheets.read",
            granted=["google_sheets.read"],
            method="GET",
            path=f"/v4/spreadsheets/{sid}/values/{rng}",
        )
        out = {}
        for row in (r.get("body") or {}).get("values", []):
            if len(row) >= 3 and row[0]:
                try:
                    out[str(row[0]).strip()] = float(str(row[2]).replace(",", ""))
                except ValueError:
                    pass
        return out
    except Exception as exc:  # noqa: BLE001 -- degrade to delta 0, never crash
        print(
            f"# (qbo sheet unavailable: {type(exc).__name__}; using anchor values)", file=sys.stderr
        )
        return {}


def main() -> None:
    cfg = json.load(open(ANCHORS))
    if len(sys.argv) > 1:
        qbo_now = json.loads(sys.argv[1])
    else:
        qbo_now = qbo_from_sheet(cfg)
    print(f"{'Account':34} {'anchor':>12} {'QBO Δ':>12} {'estimate':>13}")
    print("-" * 74)
    total = 0.0
    for a in cfg["accounts"]:
        if a.get("real_anchor") is None:
            print(f"{a['label'][:34]:34} {'(no anchor yet — pending real balance)':>50}")
            continue
        aid = a.get("qbo_account_id")
        if aid is None or a.get("qbo_book_at_anchor") is None:
            est = a["real_anchor"]
            total += est
            print(
                f"{a['label'][:34]:34} {a['real_anchor']:>12,.2f} {'(manual)':>12} "
                f"{est:>13,.2f}   (anchored {a['anchor_date']}, no QBO feed)"
            )
            continue
        book_now = qbo_now.get(str(aid), a["qbo_book_at_anchor"])
        delta = round(book_now - a["qbo_book_at_anchor"], 2)
        est = round(a["real_anchor"] + delta, 2)
        total += est
        print(
            f"{a['label'][:34]:34} {a['real_anchor']:>12,.2f} {delta:>+12,.2f} "
            f"{est:>13,.2f}   (anchored {a['anchor_date']})"
        )
    print("-" * 74)
    print(f"{'TOTAL (anchored bank accounts)':34} {'':12} {'':12} {total:>13,.2f}")


if __name__ == "__main__":
    main()
