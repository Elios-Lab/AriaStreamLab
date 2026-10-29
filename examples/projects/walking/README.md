# Project: walking laps

**Question.** How does a participant walk a known indoor loop at two paces — distance,
speed, step cadence, head movement — measured from the glasses alone, with no external
tracking? The same recording is the basis for anything that needs where the participant
was: what they looked at along the route, how long they spent at each place.

**Design.** Six laps of a fixed loop (a corridor circuit of 30–60 m with a marked start
line): three at the participant's normal pace, three slow. The experimenter presses ENTER
when the participant crosses the start line and again at the end of the lap; each lap is a
`lap` interval with its number and pace. The participant stands still for five seconds
before the first lap so that VIO initialises.

**Recorded.** VIO poses at 10 Hz (`vio`: position and orientation in the odometry frame,
velocities), both IMUs at 800 Hz, the barometer at 50 Hz, RGB at 5 fps (what the
participant saw, for annotation later — low to keep the session cool and small), and a
`head_motion` indicator emitted on changes. The session is wireless; the experiment file sets
`stall_timeout_s: 30` and `on_stall: stop` so that a thermal stop ends the session cleanly.

| File | Role |
|---|---|
| `experiment.json` | streams and processors; `ariastreamlab inspect-config experiment.json` |
| `run_session.py` | the protocol: VIO settle, six laps, their annotations, the skin temperature after each lap |
| `protocol.md` | the session-day checklist |
| `analyze.py` | trajectory, distance, speed, cadence, altitude per lap → `results/laps.csv` + figures |

## Run it

```bash
cd examples/projects/walking
python run_session.py P000 --mock --auto     # 70 s of synthetic data (a circle)
python analyze.py                            # results/laps.csv, trajectory_P000.png, speed_P000.png
```

With the glasses: `python run_session.py P003`, then follow `protocol.md`.

## What `analyze.py` computes

Per `lap` interval: `duration_s`, `distance_m` (the 2-D VIO path length after 0.5 s of
smoothing, so head bobbing does not inflate it), `mean_speed_msec`, `closure_m` (how far the
end of the lap is from its start — a loop should close; a large value means VIO drifted or
the start line was crossed differently), `cadence_steps_per_min` (the dominant frequency of
the left IMU's acceleration norm between 1.2 and 2.6 Hz, when a clear peak exists),
`altitude_change_m` (barometer, −8.3 m per hPa, end of lap relative to its start) and
`head_moving_fraction`. `trajectory_*.png` is the top view of every lap; `speed_*.png` the
walking speed over the whole session with the laps shaded.

On synthetic data the participant walks a circle of 1.5 m radius at a constant speed and
the IMU has no gait rhythm, so `cadence` is empty — with the glasses it is where the
interesting numbers are.

Things to add: a loop-closure correction (the VIO frame drifts slowly; the start line is a
fixed point), time spent in zones of the route (polygons in the odometry frame once the
route has been walked once), the RGB frames at points of interest (`SessionReader.iter_stream
("rgb", start_ns=…, end_ns=…)`).
