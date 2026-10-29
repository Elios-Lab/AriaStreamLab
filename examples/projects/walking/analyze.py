#!/usr/bin/env python
"""Analyse the walking-laps sessions: trajectory, distance, speed, cadence and altitude per lap.

    python analyze.py                      # every session under ./sessions
    python analyze.py sessions/2026*_P003  # some of them

Writes `results/laps.csv` (one row per `lap` interval) and, with matplotlib,
`results/trajectory_<participant>.png` (the VIO path of every lap, top view, coloured by
pace) and `results/speed_<participant>.png` (walking speed over the session with the laps
shaded).

Per lap: duration, path length and mean speed from the VIO translation (2-D, in the odometry
frame; the 10 Hz poses are smoothed over 0.5 s before differencing so that head bobbing does
not inflate the distance), the step cadence from the dominant frequency of the left IMU's
acceleration norm between 1.2 and 2.6 Hz (`cadence_steps_per_min`; the spectral peak must
stand out, otherwise `None`), the altitude change from the barometer (−8.3 m per hPa,
relative to the lap's start) and the fraction of the lap with the head moving.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

import numpy as np

from ariastreamlab.storage.reader import SessionReader

HERE = Path(__file__).resolve().parent
SMOOTH_S = 0.5
CADENCE_BAND = (1.2, 2.6)  # Hz, walking


def sessions_from(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    root = HERE / "sessions"
    return (
        sorted(p for p in root.iterdir() if (p / "manifest.json").exists()) if root.exists() else []
    )


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or x.size < n:
        return x
    padded = np.pad(x, (n // 2, n - 1 - n // 2), mode="edge")
    return np.convolve(padded, np.ones(n) / n, mode="valid")


def cadence(acc_norm: np.ndarray, rate_hz: float) -> float | None:
    if acc_norm.size < int(4 * rate_hz):
        return None
    x = acc_norm - acc_norm.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    f = np.fft.rfftfreq(x.size, 1 / rate_hz)
    band = (f >= CADENCE_BAND[0]) & (f <= CADENCE_BAND[1])
    if not band.any():
        return None
    peak = f[band][np.argmax(spec[band])]
    # the peak must dominate the band (a flat spectrum is noise, not walking)
    if spec[band].max() < 3 * np.median(spec[band]):
        return None
    return float(peak * 60)


def analyse(session: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    r = SessionReader(session)
    poses = list(r.iter_stream("vio"))
    p_ts = np.array([s.device_timestamp_ns for s in poses], dtype=np.int64)
    xy = np.array([s.data.translation_m[:2] for s in poses]) if poses else np.zeros((0, 2))
    rate = len(poses) / ((p_ts[-1] - p_ts[0]) / 1e9) if len(poses) > 1 else 10.0
    n_smooth = max(1, round(SMOOTH_S * rate))
    xy_s = (
        np.column_stack([_smooth(xy[:, 0], n_smooth), _smooth(xy[:, 1], n_smooth)])
        if len(poses)
        else xy
    )

    imu = list(r.iter_stream("imu_left")) if "imu_left" in r.streams else []
    i_ts = np.array([s.device_timestamp_ns for s in imu], dtype=np.int64)
    acc = np.array([np.linalg.norm(s.data.accel_msec2) for s in imu]) if imu else np.array([])
    imu_rate = len(imu) / ((i_ts[-1] - i_ts[0]) / 1e9) if len(imu) > 1 else 800.0

    baro = list(r.iter_stream("barometer")) if "barometer" in r.streams else []
    b_ts = np.array([s.device_timestamp_ns for s in baro], dtype=np.int64)
    b_pa = np.array([s.data.pressure_pa for s in baro]) if baro else np.array([])

    motion = list(r.iter_stream("head_motion")) if "head_motion" in r.streams else []

    rows: list[dict[str, object]] = []
    paths: list[tuple[str, int, np.ndarray]] = []
    for iv in r.intervals("lap"):
        if iv.end_ns is None:
            continue
        sel = (p_ts >= iv.start_ns) & (p_ts <= iv.end_ns)
        pts = xy_s[sel]
        dist = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) if len(pts) > 1 else 0.0
        dur = iv.duration_s or 0.0
        row: dict[str, object] = {
            "session": session.name,
            "participant": r.manifest.participant,
            "lap": iv.payload.get("lap"),
            "pace": iv.payload.get("pace"),
            "duration_s": round(dur, 2),
            "poses": int(sel.sum()),
            "distance_m": round(dist, 2),
            "mean_speed_msec": round(dist / dur, 3) if dur else None,
            "closure_m": round(float(np.linalg.norm(pts[-1] - pts[0])), 2)
            if len(pts) > 1
            else None,
        }
        if imu:
            isel = (i_ts >= iv.start_ns) & (i_ts <= iv.end_ns)
            c = cadence(acc[isel], imu_rate)
            row["cadence_steps_per_min"] = None if c is None else round(c, 1)
        if baro:
            bsel = (b_ts >= iv.start_ns) & (b_ts <= iv.end_ns)
            if bsel.sum() > 2:
                pa = b_pa[bsel]
                row["altitude_change_m"] = round(
                    float(-(pa[-10:].mean() - pa[:10].mean()) / 100 * 8.3), 2
                )
        if motion:
            # `emit: changes` — integrate the 0/1 state over the lap
            state = 0.0
            moving_ns = 0
            last = iv.start_ns
            for s in motion:
                if s.device_timestamp_ns < iv.start_ns:
                    state = s.data.value
                    continue
                if s.device_timestamp_ns > iv.end_ns:
                    break
                moving_ns += (s.device_timestamp_ns - last) * int(state)
                last, state = s.device_timestamp_ns, s.data.value
            moving_ns += (iv.end_ns - last) * int(state)
            row["head_moving_fraction"] = round(moving_ns / (iv.end_ns - iv.start_ns), 3)
        rows.append(row)
        paths.append((str(row["pace"]), int(row["lap"] or 0), pts))
    speed_t = (p_ts[1:] - p_ts[0]) / 1e9 if len(poses) > 1 else np.array([])
    speed = np.linalg.norm(np.diff(xy_s, axis=0), axis=1) * rate if len(poses) > 1 else np.array([])
    return rows, {
        "participant": r.manifest.participant,
        "paths": paths,
        "speed": (speed_t, speed),
        "laps": r.intervals("lap"),
        "t0": p_ts[0] if len(p_ts) else 0,
    }


def plot(summary: dict[str, object], results: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    pid = summary["participant"]
    paths: list[tuple[str, int, np.ndarray]] = summary["paths"]  # type: ignore[assignment]
    fig, ax = plt.subplots(figsize=(6, 6))
    colours = {"normal": "tab:blue", "slow": "tab:orange"}
    seen: set[str] = set()
    for pace, lap, pts in paths:
        if len(pts) < 2:
            continue
        ax.plot(
            pts[:, 0],
            pts[:, 1],
            color=colours.get(pace, "gray"),
            alpha=0.8,
            label=pace if pace not in seen else None,
        )
        ax.annotate(str(lap), pts[0], fontsize=8)
        seen.add(pace)
    ax.set_aspect("equal")
    ax.set_xlabel("x (m, odometry frame)")
    ax.set_ylabel("y (m)")
    ax.set_title(f"laps from VIO — {pid}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(results / f"trajectory_{pid}.png", dpi=120)
    plt.close(fig)

    t, v = summary["speed"]  # type: ignore[misc]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(t, v, lw=0.8)
    t0 = int(summary["t0"])  # type: ignore[arg-type]
    for iv in summary["laps"]:  # type: ignore[attr-defined]
        if iv.end_ns is not None:
            ax.axvspan(
                (iv.start_ns - t0) / 1e9,
                (iv.end_ns - t0) / 1e9,
                alpha=0.15,
                color=colours.get(str(iv.payload.get("pace")), "gray"),
            )
    ax.set_xlabel("time (s)")
    ax.set_ylabel("speed (m/s)")
    ax.set_title(f"walking speed, laps shaded — {pid}")
    fig.tight_layout()
    fig.savefig(results / f"speed_{pid}.png", dpi=120)
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
    plotted = True
    for s in sessions:
        rows, summary = analyse(s)
        all_rows.extend(rows)
        plotted = plot(summary, results) and plotted
        print(f"{s.name}: {len(rows)} laps")
    if not all_rows:
        print("no `lap` intervals in these sessions")
        return 1
    keys = list(all_rows[0]) + sorted({k for row in all_rows for k in row} - set(all_rows[0]))
    with (results / "laps.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(all_rows)
    print(
        f"wrote {results / 'laps.csv'} ({len(all_rows)} rows)"
        + ("" if plotted else " — install matplotlib for the figures")
    )
    print(
        f"\n{'pace':8} {'n':>3} {'duration s':>11} {'distance m':>11} {'speed m/s':>10} {'cadence':>8}"
    )
    for pace in sorted({str(r_["pace"]) for r_ in all_rows}):
        rows = [r_ for r_ in all_rows if str(r_["pace"]) == pace]
        cad = [
            float(r_["cadence_steps_per_min"])
            for r_ in rows
            if r_.get("cadence_steps_per_min") is not None
        ]
        print(
            f"{pace:8} {len(rows):3d} {statistics.fmean(float(r_['duration_s']) for r_ in rows):11.1f} "
            f"{statistics.fmean(float(r_['distance_m']) for r_ in rows):11.1f} "
            f"{statistics.fmean(float(r_['mean_speed_msec'] or 0) for r_ in rows):10.2f} "
            f"{statistics.fmean(cad) if cad else float('nan'):8.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
