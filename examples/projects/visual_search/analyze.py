#!/usr/bin/env python
"""Analyse the visual-search sessions: search time, gaze path and head movements per trial.

    python analyze.py                      # every session under ./sessions
    python analyze.py sessions/2026*_P003  # some of them

Writes `results/trials.csv` (one row per `search` interval) and, with matplotlib,
`results/search_times.png` (search time by layout, found trials only) and one
`results/gaze_path_<participant>_<trial>.png` for the longest found trial of each session.

Per trial: `search_time_s` (the interval's duration), `found`, the gaze path length in
normalised frame units (sum of distances between consecutive valid gaze points — a proxy for
how much the eyes travelled), the number of head movements (0→1 transitions of the
`head_motion` indicator) and the fraction of the trial with the head moving.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

import numpy as np

from ariastreamlab.storage.reader import SessionReader

HERE = Path(__file__).resolve().parent


def sessions_from(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    root = HERE / "sessions"
    return (
        sorted(p for p in root.iterdir() if (p / "manifest.json").exists()) if root.exists() else []
    )


def analyse(session: Path) -> list[dict[str, object]]:
    r = SessionReader(session)
    gaze = list(r.iter_stream("gaze_rgb"))
    g_ts = np.array([s.device_timestamp_ns for s in gaze], dtype=np.int64)
    motion = list(r.iter_stream("head_motion")) if "head_motion" in r.streams else []
    m_ts = np.array([s.device_timestamp_ns for s in motion], dtype=np.int64)
    m_val = np.array([s.data.value for s in motion]) if motion else np.array([])

    rows: list[dict[str, object]] = []
    for iv in r.intervals("search"):
        if iv.end_ns is None:
            continue
        sel = (g_ts >= iv.start_ns) & (g_ts <= iv.end_ns)
        pts = [gaze[i].data for i in np.flatnonzero(sel)]
        valid = [(p["x_norm"], p["y_norm"]) for p in pts if p.get("valid") and p.get("in_image")]
        path = (
            float(np.sum(np.linalg.norm(np.diff(np.array(valid), axis=0), axis=1)))
            if len(valid) > 1
            else 0.0
        )
        row: dict[str, object] = {
            "session": session.name,
            "participant": r.manifest.participant,
            "trial": iv.payload.get("trial"),
            "target": iv.payload.get("target"),
            "layout": iv.payload.get("layout"),
            "n_objects": iv.payload.get("n_objects"),
            "found": iv.payload.get("found"),
            "search_time_s": round(iv.duration_s or 0.0, 3),
            "gaze_samples": len(pts),
            "gaze_valid_fraction": round(len(valid) / len(pts), 3) if pts else None,
            "gaze_path_norm": round(path, 3),
            "gaze_path": valid,  # kept for the figure, dropped from the CSV
        }
        if motion:
            msel = (m_ts >= iv.start_ns) & (m_ts <= iv.end_ns)
            v = m_val[msel]
            row["head_moves"] = int(np.sum((v[1:] == 1.0) & (v[:-1] == 0.0))) if v.size > 1 else 0
            row["head_moving_fraction"] = round(float(np.mean(v)), 3) if v.size else None
        rows.append(row)
    return rows


def plots(all_rows: list[dict[str, object]], results: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    found = [r_ for r_ in all_rows if r_["found"]]
    layouts = sorted({str(r_["layout"]) for r_ in found})
    fig, ax = plt.subplots(figsize=(6, 4))
    for i, layout in enumerate(layouts):
        ys = [float(r_["search_time_s"]) for r_ in found if r_["layout"] == layout]
        ax.scatter(
            np.full(len(ys), i) + np.random.default_rng(0).uniform(-0.08, 0.08, len(ys)),
            ys,
            alpha=0.7,
        )
        if ys:
            ax.hlines(statistics.median(ys), i - 0.2, i + 0.2, color="black")
    ax.set_xticks(range(len(layouts)), layouts)
    ax.set_ylabel("search time (s)")
    ax.set_title(f"search time by layout — {len(found)} found trials, bar = median")
    fig.tight_layout()
    fig.savefig(results / "search_times.png", dpi=120)
    plt.close(fig)

    # the longest found trial of every session, as a gaze path over the first RGB frame
    by_session: dict[str, dict[str, object]] = {}
    for r_ in found:
        s = str(r_["session"])
        if s not in by_session or r_["search_time_s"] > by_session[s]["search_time_s"]:  # type: ignore[operator]
            by_session[s] = r_
    for s, r_ in by_session.items():
        reader = SessionReader(HERE / "sessions" / s) if (HERE / "sessions" / s).exists() else None
        frame = None
        if reader is not None and "rgb" in reader.streams:
            first = next(iter(reader.iter_stream("rgb")), None)
            frame = None if first is None else first.data
        pts = np.array(r_["gaze_path"])
        if pts.size == 0:
            continue
        fig, ax = plt.subplots(figsize=(8, 6))
        if frame is not None:
            ax.imshow(frame)
            h, w = frame.shape[:2]
        else:
            w = h = 1.0
            ax.set_xlim(0, 1)
            ax.set_ylim(1, 0)
        ax.plot(pts[:, 0] * w, pts[:, 1] * h, "-", lw=1, alpha=0.8)
        ax.scatter(pts[:1, 0] * w, pts[:1, 1] * h, c="lime", s=60, label="start", zorder=3)
        ax.scatter(pts[-1:, 0] * w, pts[-1:, 1] * h, c="red", s=60, label="found", zorder=3)
        ax.legend(loc="upper right")
        ax.set_title(
            f"gaze path — {r_['participant']} trial {r_['trial']} ({r_['target']}, {r_['search_time_s']} s)"
        )
        ax.set_axis_off()
        fig.tight_layout()
        fig.savefig(results / f"gaze_path_{r_['participant']}_trial{r_['trial']}.png", dpi=120)
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
        rows = analyse(s)
        all_rows.extend(rows)
        n_found = sum(1 for r_ in rows if r_["found"])
        print(f"{s.name}: {len(rows)} trials, {n_found} found")
    if not all_rows:
        print("no `search` intervals in these sessions")
        return 1
    plotted = plots(all_rows, results)
    keys = [k for k in all_rows[0] if k != "gaze_path"]
    with (results / "trials.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    print(
        f"wrote {results / 'trials.csv'} ({len(all_rows)} rows)"
        + ("" if plotted else " — install matplotlib for the figures")
    )
    print(
        f"\n{'layout':10} {'n':>3} {'found':>6} {'median s':>9} {'gaze path':>10} {'head moves':>11}"
    )
    for layout in sorted({str(r_["layout"]) for r_ in all_rows}):
        rows = [r_ for r_ in all_rows if str(r_["layout"]) == layout]
        found = [r_ for r_ in rows if r_["found"]]
        med = (
            statistics.median(float(r_["search_time_s"]) for r_ in found) if found else float("nan")
        )
        path = (
            statistics.fmean(float(r_["gaze_path_norm"]) for r_ in found) if found else float("nan")
        )
        moves = (
            statistics.fmean(float(r_.get("head_moves", 0)) for r_ in found)
            if found
            else float("nan")
        )
        print(f"{layout:10} {len(rows):3d} {len(found):6d} {med:9.2f} {path:10.2f} {moves:11.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
