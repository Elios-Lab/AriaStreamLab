# Project: gaze on fixation targets

**Question.** Where does the on-device eye gaze land when a participant fixates a known
point, and how stable is it over three seconds? The answer is the accuracy and precision you
can expect from `eye_gaze` in your own studies, per participant, before you rely on it.

**Design.** Five targets on a sheet or a monitor at about one metre — centre, left, right,
top, bottom, roughly 15° apart — in two blocks, so ten fixations of three seconds per
participant. The experimenter names the target, waits for the participant to look at it, and
presses ENTER; the script records a `fixation` interval carrying the target name.

**Recorded.** RGB at 10 fps (so each fixation can be checked by eye), both eye cameras, the
on-device gaze at 30 Hz projected onto the RGB frame by `gaze.project_to_rgb`
(`gaze_rgb`: `x`, `y`, `x_norm`, `y_norm`, `valid`), both IMUs (head stillness). The raw
VRS is kept as always.

| File | Role |
|---|---|
| `experiment.json` | what is recorded and processed; `ariastreamlab inspect-config experiment.json` shows the plan |
| `run_session.py` | the protocol: one participant, ten fixations, the annotations |
| `protocol.md` | the session-day checklist for the experimenter |
| `analyze.py` | per-fixation position and dispersion → `results/fixations.csv`, one figure per session |
| `sessions/` | one directory per participant, created by `run_session.py` |

## Run it

```bash
cd examples/projects/gaze
python run_session.py P000 --mock --auto     # 45 s of synthetic data, no keyboard, no glasses
python analyze.py                            # results/fixations.csv + results/targets_P000_*.png
```

With the glasses (after the one-time setup in the manual):

```bash
python run_session.py P003                   # the monitor opens; follow protocol.md
python analyze.py
ariastreamlab inspect sessions/<folder> --events
```

## What `analyze.py` computes

For every `fixation` interval, after discarding its first 500 ms (the saccade and the
experimenter's reaction): the median of the valid, in-image `gaze_rgb` points in normalised
frame coordinates and in pixels, the RMS distance of the points from that median
(`dispersion_norm` — precision), the fraction of valid gaze samples (blinks and tracking
loss), the median gaze depth, and the RMS gyroscope norm of the left IMU (did the head move).
Accuracy needs the targets' true positions in the frame: mark them once on the first RGB
frame of each session (the figure shows it) and subtract — or place the targets so that their
pixel positions are known from the camera geometry.

On synthetic data the gaze wanders smoothly across the frame, so the centres scatter: that is
the mock, not a bug.
