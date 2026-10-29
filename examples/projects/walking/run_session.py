#!/usr/bin/env python
"""Record one participant of the walking-laps study.

    python run_session.py P003               # with the glasses, wireless
    python run_session.py P003 --mock        # synthetic data, same protocol
    python run_session.py P003 --mock --auto # unattended: six short laps on a timer

Six laps of a fixed indoor loop: three at the participant's normal pace, three slow. The
experimenter presses ENTER when the participant crosses the start line (the `lap` interval
begins, with the lap number and the pace) and again when they cross it at the end of the
lap. The session is wireless and the participant moves, so the script also prints the skin
temperature every lap: above 40 °C, stop after the current lap and let the glasses cool.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ariastreamlab.api import Experiment

HERE = Path(__file__).resolve().parent
LAPS = (("normal", 1), ("normal", 2), ("normal", 3), ("slow", 4), ("slow", 5), ("slow", 6))
SETTLE_S = 5.0  # VIO needs a few seconds of standing still to initialise


def temperature(exp: Experiment) -> str:
    dev = (exp.health.latest.get("device") if exp.health is not None else None) or {}
    t = dev.get("skin_temperature_c")
    return "n/a" if t is None else f"{t:.1f} °C" + (" (charging!)" if dev.get("charging") else "")


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
        print(f"stand still for {SETTLE_S:.0f} s (VIO initialisation)…")
        time.sleep(SETTLE_S)
        exp.mark("vio_ready")
        try:
            for pace, lap in LAPS:
                payload = {"lap": lap, "pace": pace}
                if args.auto:
                    time.sleep(1.0)
                    tid = exp.begin("lap", payload)
                    time.sleep(8.0 if pace == "normal" else 11.0)
                    exp.end(tid)
                    print(f"  lap {lap} ({pace}) — skin {temperature(exp)}")
                    continue
                input(
                    f"lap {lap} ({pace.upper()} pace): ENTER when the participant crosses the start line "
                )
                tid = exp.begin("lap", payload)
                t0 = time.monotonic()
                input("  ENTER when they cross it again ")
                exp.end(tid)
                print(f"  lap {lap}: {time.monotonic() - t0:.1f} s — skin {temperature(exp)}")
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
