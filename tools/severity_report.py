#!/usr/bin/env python3
"""Compares the bot's severity with your corrections from the app.

Target: correct or one level off on at least 9 of 10 calls. The listed
disagreements show where to tune the scale in server/prompts/postcall.txt.

    cd server && python ../tools/severity_report.py --db data/calls.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

LEVELS = ["low", "normal", "high", "urgent"]


def main() -> int:
    # Windows consoles default to a code page that cannot print Hindi or Kannada.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/calls.db")
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    reviewed = con.execute("SELECT id, severity, severity_corrected, reason FROM calls "
                           "WHERE severity IS NOT NULL AND handled_by IN ('bot', 'voicemail') "
                           "AND seen_at IS NOT NULL").fetchall()
    if not reviewed:
        print("No reviewed calls yet. Open calls in the app; correct severity where it is wrong.")
        return 1
    exact = near = 0
    misses = []
    for r in reviewed:
        truth = r["severity_corrected"] or r["severity"]
        gap = abs(LEVELS.index(r["severity"]) - LEVELS.index(truth))
        exact += gap == 0
        near += gap <= 1
        if gap:
            misses.append((r["id"], r["severity"], truth, r["reason"] or ""))
    n = len(reviewed)
    print(f"{n} reviewed calls: exact {exact}/{n}, within one level {near}/{n} ({100 * near / n:.0f}%)")
    for cid, said, truth, reason in misses:
        print(f"  {cid}: bot said {said}, you said {truth} — {reason[:70]}")
    ok = near >= 0.9 * n
    print("\nPASS" if ok else "\nBelow target: adjust the severity examples in prompts/postcall.txt")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
