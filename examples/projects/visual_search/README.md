# Project: visual search on a desk

**Question.** How long does it take to find a named object among others on a desk, and how
does the search behave — how far the eyes travel, how often the head turns — when the layout
is sparse (6 objects) or cluttered (12)? A classic paradigm in a natural setting, with the
glasses in place of a screen.

**Design.** Eight trials per participant, alternating sparse and cluttered layouts. The
experimenter arranges the desk, names the target, presses ENTER when the participant may
start (the `search` interval begins) and again when the participant says *found* (ENTER) or
gives up (`n`). The interval carries the target, the layout and the object count; its end
carries `found`; its duration is the search time. A trial longer than 20 s counts as not
found.

**Recorded.** RGB at 10 fps, both eye cameras, on-device gaze at 30 Hz projected onto the
frame (`gaze_rgb`), both IMUs, and a `head_motion` indicator (`imu.motion_detector`, 1 while
the head turns, emitted on every IMU sample so it can be averaged over a trial). The raw VRS
is kept.

| File | Role |
|---|---|
| `experiment.json` | streams and processors; `ariastreamlab inspect-config experiment.json` |
| `run_session.py` | the protocol: eight searches, their annotations; edit `TRIALS` for your objects |
| `protocol.md` | the session-day checklist |
| `analyze.py` | search time, gaze path and head movements per trial → `results/trials.csv` + figures |

## Run it

```bash
cd examples/projects/visual_search
python run_session.py P000 --mock --auto     # 45 s of synthetic data
python analyze.py                            # results/trials.csv, search_times.png, gaze_path_*.png
```

With the glasses: `python run_session.py P003`, then follow `protocol.md`.

## What `analyze.py` computes

Per `search` interval: `search_time_s`, `found`, the number of gaze samples and the fraction
that were valid and inside the frame, `gaze_path_norm` (the summed distance between
consecutive valid gaze points, in frame units — 1.0 is one frame width), `head_moves`
(0→1 transitions of `head_motion`) and `head_moving_fraction`. The console prints a table by
layout; `search_times.png` shows the distribution; one `gaze_path_*.png` per session draws
the longest found search over the first RGB frame.

Things you will want to add: the target's position in the frame (mark it on the RGB frame of
the trial's end, which is when the participant was looking at it) to compute the final
saccade; fixations rather than raw gaze points (a dispersion-threshold algorithm on
`gaze_rgb` is twenty lines); the hand reaching the object, if you enable `hand_tracking`.
