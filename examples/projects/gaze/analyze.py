#!/usr/bin/env python
"""Analyse the gaze-targets sessions: where did the gaze land on each target, and how stable was it?

    python analyze.py                      # every session under ./sessions
    python analyze.py sessions/2026*_P003  # some of them

Writes `results/fixations.csv` (one row per fixation: participant, trial, target, median gaze
position in normalised RGB coordinates and in pixels, dispersion, validity, head motion) and
`results/targets_<participant>.png` (the fixation centres drawn over the first RGB frame of
that session, when matplotlib is installed).

The method is deliberately simple: a fixation's position is the median of the on-device gaze
projected onto the RGB camera (`gaze_rgb`, from `gaze.project_to_rgb`) over the interval,
dispersion is the RMS distance from that median, and the first 500 ms of every interval are
discarded (the saccade to the target and the experimenter's reaction time). Replace it with
your own — everything needed is in the session directory.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

import numpy as np

from ariastreamlab.storage.reader import SessionReader

HERE = Path(__file__).resolve().parent
SKIP_S = 0.5  # discard the start of every fixation


def sessions_from(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    root = HERE / "sessions"
    return (
        sorted(p for p in root.iterdir() if (p / "manifest.json").exists()) if root.exists() else []
    )


def analyse(session: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    r = SessionReader(session)
    participant = r.manifest.participant
    rows: list[dict[str, object]] = []
    centres: dict[str, list[tuple[float, float]]] = {}
    gaze = list(r.iter_stream("gaze_rgb"))  # one row per gaze sample, 30 Hz
    motion = list(r.iter_stream("imu_left")) if "imu_left" in r.streams else []
    g_ts = np.array([s.device_timestamp_ns for s in gaze], dtype=np.int64)
    m_ts = np.array([s.device_timestamp_ns for s in motion], dtype=np.int64)
    gyro = (
        np.array([np.linalg.norm(s.data.gyro_radsec) for s in motion]) if motion else np.array([])
    )

    for iv in r.intervals("fixation"):
        if iv.end_ns is None:
            continue
        lo = iv.start_ns + int(SKIP_S * 1e9)
        sel = (g_ts >= lo) & (g_ts <= iv.end_ns)
        pts = [gaze[i].data for i in np.flatnonzero(sel)]
        valid = [p for p in pts if p.get("valid") and p.get("in_image")]
        row: dict[str, object] = {
            "session": session.name,
            "participant": participant,
            "trial": iv.payload.get("trial"),
            "block": iv.payload.get("block"),
            "target": iv.payload.get("target"),
            "duration_s": round(iv.duration_s or 0.0, 3),
            "samples": len(pts),
            "valid_fraction": round(len(valid) / len(pts), 3) if pts else None,
        }
        if valid:
            xs = np.array([p["x_norm"] for p in valid])
            ys = np.array([p["y_norm"] for p in valid])
            mx, my = float(np.median(xs)), float(np.median(ys))
            disp = float(np.sqrt(np.mean((xs - mx) ** 2 + (ys - my) ** 2)))
            row.update(
                {
                    "x_norm": round(mx, 4),
                    "y_norm": round(my, 4),
                    "x_px": round(float(np.median([p["x"] for p in valid])), 1),
                    "y_px": round(float(np.median([p["y"] for p in valid])), 1),
                    "dispersion_norm": round(disp, 4),
                    "depth_m": round(float(np.median([p["depth_m"] for p in valid])), 3),
                }
            )
            centres.setdefault(str(row["target"]), []).append((mx, my))
        if motion:
            msel = (m_ts >= lo) & (m_ts <= iv.end_ns)
            row["head_gyro_rms_radsec"] = (
                round(float(np.sqrt(np.mean(gyro[msel] ** 2))), 4) if msel.any() else None
            )
        rows.append(row)

    summary = {
        "participant": participant,
        "fixations": len(rows),
        "valid_fraction": round(
            statistics.fmean(
                float(r_["valid_fraction"]) for r_ in rows if r_["valid_fraction"] is not None
            ),
            3,
        )
        if any(r_["valid_fraction"] is not None for r_ in rows)
        else None,
        "dispersion_norm": round(
            statistics.fmean(
                float(r_["dispersion_norm"]) for r_ in rows if "dispersion_norm" in r_
            ),
            4,
        )
        if any("dispersion_norm" in r_ for r_ in rows)
        else None,
        "centres": centres,
        "session": session,
    }
    return rows, summary


def plot(summary: dict[str, object], out: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    r = SessionReader(summary["session"])  # type: ignore[arg-type]
    frame = None
    if "rgb" in r.streams:
        first = next(iter(r.iter_stream("rgb")), None)
        frame = None if first is None else first.data
    fig, ax = plt.subplots(figsize=(8, 6))
    if frame is not None:
        ax.imshow(frame)
        h, w = frame.shape[:2]
    else:
        w, h = 1.0, 1.0
        ax.set_xlim(0, 1)
        ax.set_ylim(1, 0)
    centres: dict[str, list[tuple[float, float]]] = summary["centres"]  # type: ignore[assignment]
    for target, pts in centres.items():
        ax.scatter([x * w for x, _ in pts], [y * h for _, y in pts], s=60, label=target)
    ax.legend(loc="upper right")
    ax.set_title(f"fixation centres — {summary['participant']} (median gaze per trial)")
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return True


def main(argv: list[str]) -> int:
    sessions = sessions_from(argv)
    if not sessions:
        print("no sessions found — run run_session.py first (or pass session directories)")
        return 1
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    all_rows: list[dict[str, object]] = []
    for s in sessions:
        rows, summary = analyse(s)
        all_rows.extend(rows)
        plotted = plot(summary, results / f"targets_{summary['participant']}_{s.name[:15]}.png")
        print(
            f"{s.name}: {summary['fixations']} fixations, valid {summary['valid_fraction']}, "
            f"dispersion {summary['dispersion_norm']} (normalised)"
            + ("" if plotted else " — install matplotlib for the figure")
        )
    if not all_rows:
        print("no `fixation` intervals in these sessions")
        return 1
    keys = list(all_rows[0]) + sorted({k for row in all_rows for k in row} - set(all_rows[0]))
    with (results / "fixations.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(all_rows)
    print(f"wrote {results / 'fixations.csv'} ({len(all_rows)} rows)")
    # per-target table on stdout
    by_target: dict[str, list[dict[str, object]]] = {}
    for row in all_rows:
        by_target.setdefault(str(row["target"]), []).append(row)
    print(f"\n{'target':8} {'n':>3} {'x_norm':>7} {'y_norm':>7} {'disp':>7} {'valid':>6}")
    for target, rows in by_target.items():
        xs = [float(r_["x_norm"]) for r_ in rows if "x_norm" in r_]
        ys = [float(r_["y_norm"]) for r_ in rows if "y_norm" in r_]
        ds = [float(r_["dispersion_norm"]) for r_ in rows if "dispersion_norm" in r_]
        vs = [float(r_["valid_fraction"]) for r_ in rows if r_["valid_fraction"] is not None]
        fm = lambda v: f"{statistics.fmean(v):7.3f}" if v else "      –"  # noqa: E731
        print(f"{target:8} {len(rows):3d} {fm(xs)} {fm(ys)} {fm(ds)} {fm(vs)[1:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
