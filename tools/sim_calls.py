#!/usr/bin/env python3
"""Runs simulated calls against the real conversation code and prints each transcript.

No microphone, GPU or models needed: stand-in speech models with realistic delays
play the caller and the bot (see server/app/sim.py and app/sim_scenarios.py).

    cd server && python ../tools/sim_calls.py              # every scenario
    cd server && python ../tools/sim_calls.py barge_in echo_line
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from app.sim import run_all  # noqa: E402
from app.sim_scenarios import SCENARIOS  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    names = sys.argv[1:]
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    if not chosen:
        print("No such scenario. Choose from:", ", ".join(s.name for s in SCENARIOS))
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for r in asyncio.run(run_all(chosen)):
        print(r.render())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
