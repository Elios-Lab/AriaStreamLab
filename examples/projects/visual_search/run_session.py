#!/usr/bin/env python
"""Record one participant of the visual-search study.

    python run_session.py P003               # with the glasses
    python run_session.py P003 --mock        # synthetic data, same protocol
    python run_session.py P003 --mock --auto # unattended: every search lasts a few seconds

Eight trials, alternating a *sparse* layout (6 objects) and a *cluttered* one (12 objects).
For each trial the experimenter arranges the desk, names the target object, presses ENTER
when the participant may start looking (the `search` interval begins), and presses ENTER
again when the participant says "found" (ENTER) or gives up (`n` + ENTER); the interval ends
with `found` and the search time is its duration. The target list is just an example —
change `TRIALS` to your objects, or load them from a file.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ariastreamlab.api import Experiment

HERE = Path(__file__).resolve().parent
TRIALS = (
    ("stapler", "sparse"),
    ("blue pen", "cluttered"),
    ("coffee mug", "sparse"),
    ("scissors", "cluttered"),
    ("usb stick", "sparse"),
    ("red notebook", "cluttered"),
    ("tape", "sparse"),
    ("calculator", "cluttered"),
)
OBJECTS = {"sparse": 6, "cluttered": 12}
TIMEOUT_S = 20.0
SETTLE_S = 3.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("participant")
    ap.add_argument("--mock", action="store_true", help="synthetic source (no glasses)")
    ap.add_argument("--auto", action="store_true", help="no keyboard (unattended test)")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args(argv)

    exp = Experiment.from_json(
        HERE / "experiment.json",
        mock=args.mock,
        participant=args.participant,
        dashboard=False if args.no_dashboard else None,
    )
    with exp:
        print(f"session {exp.session_dir}")
        if exp.dashboard_url:
            print(f"monitor  {exp.dashboard_url}")
        exp.start()
        time.sleep(SETTLE_S)
        try:
            for i, (target, layout) in enumerate(TRIALS, start=1):
                payload = {
                    "trial": i,
                    "target": target,
                    "layout": layout,
                    "n_objects": OBJECTS[layout],
                }
                if args.auto:
                    time.sleep(1.0)
                    tid = exp.begin("search", payload)
                    time.sleep(2.0 + 0.5 * i)
                    exp.end(tid, {"found": i % 4 != 0})
                    continue
                input(
                    f"trial {i} ({layout}, {OBJECTS[layout]} objects): arrange the desk, then ENTER to start — target: {target.upper()} "
                )
                tid = exp.begin("search", payload)
                t0 = time.monotonic()
                answer = input("  ENTER when found, n + ENTER if given up: ").strip().lower()
                elapsed = time.monotonic() - t0
                found = answer != "n" and elapsed <= TIMEOUT_S
                exp.end(tid, {"found": found, "timed_out": elapsed > TIMEOUT_S})
                print(f"  {elapsed:.1f} s — {'found' if found else 'not found'}")
        except KeyboardInterrupt:
            exp.mark("aborted")
            print("\ninterrupted — closing the session")
        exp.stop()
        exp.wait()
    assert exp.quality_summary is not None
    print(exp.quality_summary.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
