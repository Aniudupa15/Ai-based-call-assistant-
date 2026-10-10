#!/usr/bin/env python3
"""How fast the bot replies, from the timings logged on every bot turn (N1).

Reply delay = end-of-speech wait + time from then to the first audio out.
The target is under 1.5 s on most turns.

    cd server && python ../tools/timing_report.py --db data/calls.db [--last 50]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/calls.db")
    ap.add_argument("--last", type=int, default=50, help="most recent bot turns to include")
    ap.add_argument("--target-ms", type=int, default=1500)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute("SELECT timing FROM turns WHERE speaker = 'bot' AND timing IS NOT NULL "
                       "ORDER BY id DESC LIMIT ?", [args.last]).fetchall()
    timings = [json.loads(r[0]) for r in rows]
    timings = [t for t in timings if "first_audio_ms" in t]
    if not timings:
        print("No timed replies yet. Make a few test calls first.")
        return 1

    delay = [t.get("eos_wait_ms", 0) + t["first_audio_ms"] for t in timings]
    under = sum(d < args.target_ms for d in delay)
    print(f"{len(delay)} replies")
    print(f"reply delay   median {statistics.median(delay):6.0f} ms   p90 {pct(delay, 90):6.0f} ms   "
          f"max {max(delay):6.0f} ms")
    print(f"under {args.target_ms} ms: {under}/{len(delay)} ({100 * under / len(delay):.0f}%)")
    for key, label in [("eos_wait_ms", "end-of-speech wait"), ("stt_ms", "transcription"),
                       ("first_token_ms", "first model token"), ("first_audio_ms", "to first audio")]:
        vals = [t[key] for t in timings if key in t]
        if vals:
            print(f"  {label:20} median {statistics.median(vals):6.0f} ms   p90 {pct(vals, 90):6.0f} ms")
    slow = sum(1 for t in timings if t.get("slow"))
    if slow:
        print(f"  {slow} replies needed the 'one moment' filler")
    verdict = under >= 0.8 * len(delay)
    print("\nPASS: most turns meet the target" if verdict else
          "\nNot yet: try a smaller Whisper or language model, or a lower end_of_turn_ms")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
