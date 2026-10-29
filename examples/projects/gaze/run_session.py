#!/usr/bin/env python
"""Record one participant of the gaze-targets study.

    python run_session.py P003            # with the glasses
    python run_session.py P003 --mock     # synthetic data, same protocol
    python run_session.py P003 --mock --auto   # no keyboard: trials run on a timer

The protocol: five targets (centre, left, right, top, bottom) in two blocks. For each trial
the experimenter names the target, waits until the participant fixates it, presses ENTER, and
the script records a 3-second `fixation` interval with the target name as payload. Everything
else — the streams, the processors, where the session goes — is `experiment.json` next to
this file; this script only adds the annotations.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ariastreamlab.api import Experiment

HERE = Path(__file__).resolve().parent
TARGETS = ("centre", "left", "right", "top", "bottom")
BLOCKS = 2
FIXATION_S = 3.0
SETTLE_S = 3.0  # data must be flowing before the first annotation


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("participant", help="participant id, e.g. P003")
    ap.add_argument("--mock", action="store_true", help="synthetic source (no glasses)")
    ap.add_argument("--auto", action="store_true", help="do not wait for ENTER (unattended test)")
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
        exp.start()  # acquisition runs in the background from here on
        time.sleep(SETTLE_S)

        trial = 0
        try:
            for block in range(1, BLOCKS + 1):
                for target in TARGETS:
                    trial += 1
                    if args.auto:
                        time.sleep(1.0)
                    else:
                        input(
                            f"[block {block}] trial {trial}: target {target.upper()} — ENTER when fixating "
                        )
                    tid = exp.begin("fixation", {"trial": trial, "block": block, "target": target})
                    time.sleep(FIXATION_S)
                    exp.end(tid)
                    print(f"  recorded {FIXATION_S:.0f} s on {target}")
        except KeyboardInterrupt:
            exp.mark("aborted", {"trial": trial})
            print("\ninterrupted — closing the session")
        exp.stop()
        exp.wait()
    assert exp.quality_summary is not None
    print(exp.quality_summary.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
