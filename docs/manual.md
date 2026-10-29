# Setting up a data-collection project with AriaStreamLab

A manual for a researcher who has the Aria Gen 2 glasses, a research question, and no
experience with the framework. Part I is the generic path from the question to analysed
sessions — the same for every study. Part II follows three complete projects that live in
`examples/projects/` and can be copied as starting points: a gaze-accuracy check, a visual
search on a desk, and a walking task. Read Part I once; then open the project closest to
yours and adapt it.

The reference documents are the [README](../README.md) (what the framework is and
guarantees), [live_session.md](tutorials/live_session.md) (the glasses step by step and
troubleshooting), [dashboard.md](tutorials/dashboard.md) (the application),
[custom_processors.md](tutorials/custom_processors.md), [timestamps.md](tutorials/timestamps.md)
and [ARCHITECTURE.md](ARCHITECTURE.md) (the design and every hardware finding). This manual
points to them rather than repeating them.

---

## Part I — The generic path

### 1. The model in one page

A study with AriaStreamLab is a folder with four things in it:

```
my_study/
├── experiment.json     what the glasses record and what is computed live
├── run_session.py      the protocol: one participant, the trials, the annotations
├── protocol.md         the session-day checklist for whoever runs the participant
├── analyze.py          from the recorded sessions to a table and a figure
├── sessions/           one directory per recorded session (created by the runs)
└── results/            what analyze.py writes
```

`experiment.json` is the *only* description of the recording: streams, rates, processors,
device options, where sessions go. It is validated before anything starts and frozen into
each session, so the recording can always be traced back to it. The application's *Design*
page edits it as a form; a text editor does just as well.

A **session** is one run of that file with one participant: a self-describing directory
with the raw VRS recording from the glasses, the processors' outputs as Parquet, the
events, the device's health history, a quality summary and a manifest with everything that
identifies the run (software versions, device serial, calibration, git commit). Nothing in a
session depends on files outside it. The [README](../README.md#what-a-session-gives-you)
lists the layout.

**Annotations** are how the protocol gets into the data. A *marker* is an instant
("stimulus shown"); an *interval* is a `begin`/`end` pair ("trial 3, condition A, found =
true"). They are stamped on the device's clock, like every sensor sample, so a trial's
samples are those between its `begin` and its `end`. **The interval is your unit of
analysis**: design the protocol so that every quantity you will report can be computed from
the samples inside some interval plus the payload you attached to it.

**Live versus offline.** A *processor* runs on the live streams and its output is saved
next to the raw data; the on-device gaze projected onto the RGB frame (`gaze.project_to_rgb`)
and a head-motion indicator (`imu.motion_detector`) are the ones the projects use. Run
live only what you need to *see* during the session (the monitor shows it) or must react
to; compute everything else afterwards from the session. A recorded session can be
**replayed** through the same pipeline, with a different `experiment.json`, so a new
processor can be tried on old data and produces the same values it would have live.

**Quality.** Every session ends with a summary: per stream, how many samples arrived, at
what rate against what was expected, what was lost and where (the device, the link, the
application's queues, a processor), coverage and outages, the skin temperature, warnings. It
is printed at the end of the run and stored in the session; look at it before the
participant leaves.

### 2. Install once per laptop

```bash
git clone https://github.com/Elios-Lab/AriaStreamLab.git && cd AriaStreamLab
bash scripts/install.sh --sdk        # macOS / Linux; Windows: powershell scripts/install.ps1 -Sdk
```

The script creates `.venv` with a Python version Meta's SDK supports (3.10–3.12), installs
AriaStreamLab and the SDK, runs `ariastreamlab doctor` and opens the application window. The
window's **Setup** page has a button for each one-time step with the glasses on the USB cable:
*check pairing* / *pair over USB* (accept in the Companion App on the phone), *install
streaming certificates* (only if `doctor` says there is none), *hotspot status*. When *run
checks with the glasses* is all green, the laptop is ready. The command-line equivalents are
in [live_session.md §0](tutorials/live_session.md).

From now on, every terminal that uses the framework activates the environment first
(`source .venv/bin/activate`, or the `aria` conda environment if that is where you installed
it); `ariastreamlab --version` confirms.

### 3. Start from a project folder

Copy the project closest to your study (Part II describes the three) and rename it:

```bash
cp -r examples/projects/gaze ~/studies/my_study && cd ~/studies/my_study
rm -rf sessions results            # the copies of the example's recordings, if any
```

The four files are yours to edit; they are short. Keep the folder under version control
(the manifest records the git commit of the *framework*; your study folder has its own
history, and `experiment.json` and `run_session.py` are what a reviewer will ask for).

### 4. Decide what to record

Open `experiment.json`. The `extends` line starts from a preset (`ariastreamlab presets`
lists them, `inspect-config` shows the result); `streams` switches sensors on and sets their
parameters; `ariastreamlab streams --live-only` lists what the glasses can stream and the
parameters each accepts.

Three facts decide most choices:

*What profile9 really delivers.* The default streaming profile sends RGB at about 5 Hz
whatever `fps` says, SLAM at 10, IMU at 800, eye gaze and hand tracking at 30, VIO at 10
(`docs/ARCHITECTURE.md` §0.8–0.13 has the measurements). `inspect-config` prints the *live
plan* with these expectations; the quality summary judges the session against them, not
against the nominal numbers. If you need faster RGB, record the VRS at full rate on the
device and read it back with the `tools` replay backend — the live path is for seeing and
annotating, the VRS is the record.

*Heat.* In the 20-minute validation run the skin temperature rose from 29 °C to 42 °C in
9½ minutes while the glasses streamed over the hotspot *and charged over USB*; at 42 °C the
device stops streaming by itself.
Uncharged, wireless sessions of 15–20 minutes are the realistic unit; plan the protocol in
blocks of that length with cooling pauses, never charge while streaming, and let the stall
detector (`device.connection.stall_timeout_s`, default 30 s, `on_stall: "stop"`) end a
session cleanly rather than recording silence. The validation checklist's step 7 is the
measurement of the uncharged case.

*Size.* profile9 with RGB, SLAM and IMUs writes about 1.4 MB/s — 5 GB per hour — most of it
the VRS. Set `storage.root` to a disk with room; `doctor` reports the free space. Streams
you do not need cost heat and disk: a walking study does not need the eye cameras.

Write the file, then:

```bash
ariastreamlab inspect-config experiment.json     # validates; prints streams, processors, the live plan
```

or do the same on the application's *Design* page, which validates after every change and
shows the plan's warnings. Both use the same code.

### 5. Decide what to compute live

Add a processor to `processors` when (a) you need its output on the monitor while the
participant is wearing the glasses — the gaze point on the RGB frame tells you in two seconds
whether the eye tracking is working for this participant; a head-motion flag tells you the
IMU threshold is right — or (b) the protocol reacts to it. Everything else is better done in
`analyze.py`, where you can change your mind.

The built-ins (`ariastreamlab processors`) cover gaze projection, image resize/crop/
grayscale, IMU low-pass, magnitude and motion detection, pass-through and downsampling. A
processor of your own is a class with a `process()` method in a `.py` file in `processors/`
next to `experiment.json`, referenced as `"type": "my_file.py"`; its parameters are a
Pydantic model and appear in the designer and the monitor's *parameters* dialog
([custom_processors.md](tutorials/custom_processors.md), and `examples/custom_processor/`).
`save_output: true` writes the output into `derived/<stream>/` with a `_meta.json` naming
the processor, its version and the parameters that produced it.

A processor that is too slow drops its input (the summary says so: *processor-dropped*); one
that raises is counted and the session continues. Neither can touch the raw data or the
other streams.

### 6. The protocol script

`run_session.py` is where the study's logic lives. The pattern, from the gaze project:

```python
from ariastreamlab.api import Experiment

exp = Experiment.from_json("experiment.json", participant="P003")
with exp:  # opens the session directory, builds everything
    exp.start()  # acquisition runs in the background from here
    time.sleep(3)  # let data flow before the first annotation
    for trial, target in enumerate(TARGETS, start=1):
        input(f"target {target} — ENTER when fixating ")
        tid = exp.begin("fixation", {"trial": trial, "target": target})
        time.sleep(3)
        exp.end(tid)  # payloads of begin and end are merged
    exp.stop()  # or let a duration end it: exp.start(duration_s=600)
    exp.wait()
print(exp.quality_summary.text())  # the same summary `ariastreamlab run` prints
```

Rules that make the data analysable later:

* **One interval name per trial type, payload for the factors.** `search` with
  `{"trial": 7, "layout": "cluttered", "target": "mug"}`, ended with `{"found": true}` —
  not `search_cluttered_7`. `analyze.py` then filters by payload, and
  `ariastreamlab inspect <session> --events` prints a readable table.
* **Stamp instants you know exactly with `at_ns`.** `exp.begin(..., at_ns=sample.device_timestamp_ns)`
  from a subscriber callback puts the interval at the sample's own time
  (`timestamp_source = "device"`); an annotation made from the keyboard is mapped through
  the host↔device clock estimate and labelled `estimated` — within the link latency, a few
  milliseconds over Wi-Fi, which is fine for a keypress but not for a stimulus onset. The
  stimulus computer should drive the annotation, or flash something the RGB camera sees.
  [timestamps.md](tutorials/timestamps.md) explains the precision of each kind.
* **Leave room for the unexpected.** The experimenter can add a *note* or a marker from the
  monitor at any time; `Ctrl-C` in the script ends the session cleanly (the example scripts
  record an `aborted` marker and close). Never lose a session to a typo: catch
  `KeyboardInterrupt` around the trial loop, as the examples do.
* **Changing a parameter mid-session** (`exp.set_parameters("head_motion", threshold=0.4)`,
  or the monitor's *parameters* dialog) is recorded as a `config_change` event with the
  device time it took effect; the frozen `experiment.json` keeps the start-up values.

`--mock` and `--auto` switches, as in the examples, let the same script run with synthetic
data and without a keyboard — the basis of the next step.

### 7. Pilot without the glasses

```bash
python run_session.py P000 --mock --auto
python analyze.py
ariastreamlab inspect sessions/<folder> --events
```

Every project's protocol runs against the synthetic source, which produces every stream at
the configured rate with plausible values (a wandering gaze, a circular walk, a
blink-shaped gaze invalidity every 3.7 s). Use it to finish `run_session.py` and `analyze.py`
before touching the glasses: the session layout, the interval names and payloads, the
columns of the results table are all identical to a live session's. When the analysis
produces the table you want from mock data, the live sessions will only change the numbers.

The *Sessions* page of the application lists the mock sessions too, shows their summaries
and events, and replays them to the monitor.

### 8. Pilot with the glasses

First a one-minute session with yourself, with the cable connected, from the terminal:

```bash
ariastreamlab run experiment.json --mock      # one more time, to check the dashboard on this laptop
ariastreamlab run experiment.json --duration 60 --participant pilot
```

The hotspot is started and you are asked to join it (once per laptop boot; the run detects
when you are already on it). The monitor opens; check in this order: every stream in the
*Streams* card at its expected rate; the images (eye cameras showing the pupils, RGB showing
the scene); the derived outputs (the gaze circle on the RGB frame follows your eyes); the
*Device* card (temperature, charging); the *Link* card (data path `wifi`, no queue growth).
Then the quality summary at the end: no warnings you cannot explain.

Then the real protocol with a colleague as participant, uncharged, with `run_session.py`
and `protocol.md` in hand, timing everything. This is where the protocol gets fixed: the
instructions the participant did not understand, the step that takes too long, the layout
that is outside the camera's field of view, the temperature after fifteen minutes. Run
`analyze.py` on that session: the table should already be the one you will publish.

[live_session.md](tutorials/live_session.md) has the troubleshooting table; the most
frequent problems are the laptop not actually on the hotspot (no data after 20 s), a
firewall on port 6768, and charging while streaming.

### 9. Session day

`protocol.md` is the page the experimenter follows; the three examples share the same
skeleton — before the participant (charge, cable off, `doctor --device`, hotspot, the
physical setup), with the participant (fit, start the script, the monitor checks, the
instructions, the trials, notes), **before the participant leaves** (the summary without
warnings, the right number of intervals, a first `analyze.py`), afterwards (notebook,
backup). The ten minutes it takes to check a session while the participant is still there
are the cheapest insurance in the whole study.

Two habits worth keeping: name participants with a fixed scheme (`P001`…) and pass the id
on the command line rather than editing the file; and keep one laptop, one environment and
one git commit of the study folder for the whole data collection — the manifest records the
framework's version and the device's firmware, so a change in the middle is at least
visible.

### 10. After each session

```bash
ariastreamlab inspect sessions/<folder>              # manifest facts, streams, summary
ariastreamlab inspect sessions/<folder> --events     # markers, intervals (paired), parameter changes
```

A session directory is complete in itself: copy it to the archive as it is (the VRS makes it
large; `derived/`, `events/`, `quality/` and the manifest are small and are what the
analysis reads). Do not rename or edit anything inside it; the manifest's `status` says
whether it ended normally (`completed`) or not (`crashed` — the data up to that point is
still readable; `ariastreamlab inspect` lists what was recovered).

### 11. Analysis

`analyze.py` in each project is one pattern: open every session with `SessionReader`, take
the intervals of the trial type, select the samples inside each interval from the streams
you need, compute, write a CSV with one row per trial and a figure. The reader returns
samples as the live pipeline saw them (payload objects with named fields, images as arrays):

```python
from ariastreamlab.storage.reader import SessionReader

r = SessionReader("sessions/20261029T091855Z_gaze_targets_P001")
r.manifest.participant, r.streams.keys()
for iv in r.intervals("fixation"):  # begin/end pairs, payloads merged
    gaze = [s.data for s in r.iter_stream("gaze_rgb", start_ns=iv.start_ns, end_ns=iv.end_ns)]
```

Everything is also plain Parquet, so pandas works without the framework — convenient for
notebooks and for colleagues who use R or MATLAB:

```python
import pandas as pd, json

ev = pd.read_parquet("sessions/<folder>/events/events.parquet")  # all events
imu = pd.read_parquet(
    "sessions/<folder>/raw_mock/imu_left/part-00000.parquet"
)  # mock sessions only
gz = pd.read_parquet("sessions/<folder>/derived/gaze_rgb/part-00000.parquet")
gz = pd.concat([gz.drop(columns="json"), pd.json_normalize(gz["json"].map(json.loads))], axis=1)
```

(Dict-payload outputs such as `gaze_rgb` store their fields in a `json` column; typed
payloads — IMU, gaze, poses — have one column per field. Live sessions keep the raw sensors
in the VRS; `projectaria_tools` reads it, or `ariastreamlab replay <session>` streams it
through the pipeline and any processor you add — the way to extract a raw stream to Parquet
is a `generic.passthrough` processor with `save_output: true`.)

Reproducibility comes from the session: `manifest.json` names the framework version, the
SDK version, the device, the calibration, the processors with their versions and parameters;
`experiment.json` in the session is the exact configuration. A replay of the session through
its own `experiment.json` reproduces every derived value (invariant I-1), so an analysis can
always be rerun from the raw recording.

### 12. Mistakes that cost sessions

All seen on hardware; the details are in `docs/ARCHITECTURE.md` §0.

| Mistake | What happens | Avoid it |
|---|---|---|
| charging while streaming wirelessly | 42 °C after about ten minutes, the glasses stop streaming | cable off; charge between participants |
| restarting a running hotspot | the laptop drops to its home network, no data | `hotspot status` first; the run does not restart a hotspot it is already on |
| `fps: 30` on RGB with profile9 | 5 Hz arrive anyway; the summary warns | accept 5 Hz live, or record the VRS at the device's rate |
| annotating from the keyboard what the stimulus computer knows exactly | ±5 ms instead of exact | let the stimulus side call `begin(at_ns=…)` or mark through `/api/state`'s session |
| checking the data the next day | a lost participant | the summary and `inspect --events` before they leave |
| a 40-minute protocol in one session | a thermal stop in the middle | blocks of ≤ 15 min, cooling pauses; several sessions per participant are fine |
| editing files inside a session | the manifest no longer describes the data | never; copy out what you need |

---

## Part II — Three worked projects

Each lives in `examples/projects/<name>/`, runs on synthetic data with
`python run_session.py P000 --mock --auto && python analyze.py`, and is described in its
own `README.md`. Here is what each one teaches and what to change for your study.

### A. Gaze on fixation targets (`examples/projects/gaze`)

The first thing to do with the glasses in any eye-movement study: find out how accurate and
how stable the on-device gaze is for your participants, in your lighting, at your working
distance. Five targets, two blocks, ten three-second fixations; the experimenter names the
target and presses ENTER when the participant is on it.

*Recording.* `preset:gaze_experiment` (RGB 10 fps, both eye cameras, eye gaze at 30 Hz,
IMUs) with one processor, `gaze.project_to_rgb` on `eye_gaze` alone, so that every gaze
sample becomes a point in the RGB frame (`x_norm`, `y_norm`, `valid`, `in_image`). Projecting
on the gaze stream rather than on the frames gives 30 points per second instead of 10 — the
dispersion needs them.

*Protocol.* `run_session.py` is the simplest of the three: `start()`, ten
`begin("fixation", {...})` / `end()` pairs separated by `time.sleep(3)`. `protocol.md` adds
what the script cannot know: the nose pads, the target sheet in the frame, the temperature.

*Analysis.* For each fixation, after discarding its first 500 ms: the median gaze position,
the RMS scatter around it (precision), the fraction of valid samples, the head's gyroscope
RMS. `results/fixations.csv` has a row per fixation; the figure draws the centres over the
first RGB frame. Accuracy — the distance from the true target — needs the targets' pixel
positions, which you mark once per session on that frame.

*Adapt it.* More targets or distances: edit `TARGETS` and the sheet. A calibration routine
of your own: the same script with a different stimulus. A reading study: replace the
fixations by `reading` intervals per line of text and compute the horizontal gaze velocity.

### B. Visual search on a desk (`examples/projects/visual_search`)

A reaction-time paradigm without a screen. Eight trials alternating a sparse (6 objects) and
a cluttered (12 objects) layout; the experimenter names the target, presses ENTER at *go* and
again at *found* (or `n` for given up); search time is the interval's duration.

*Recording.* As A, plus `imu.motion_detector` on the left IMU, emitted on every sample
(`emit: "always"`) so that the fraction of a trial spent turning the head is a mean. The
threshold (0.3 rad/s) is the kind of parameter you tune on the monitor's *parameters*
dialog during the pilot and then write into the file.

*Protocol.* Two inputs per trial, the second one timed; the end payload carries `found` and
`timed_out`. The target list is a tuple at the top of the script — the natural place for a
`csv` of trials if yours are randomised per participant.

*Analysis.* Per trial: search time, found, the gaze path length in frame units, the number
of head movements and the moving fraction; a strip plot of search time by layout and the
gaze path of the longest found trial drawn over the first RGB frame. Fixation detection and
the position of the target in the frame are the two additions a real study will want.

*Adapt it.* Any trial-based task with an experimenter-timed response: change `TRIALS`, the
payload keys and the layout factor. If the response comes from a button box or a stimulus
computer, that side should call `exp.begin/end` (or send the device timestamp) instead of
the keyboard.

### C. Walking laps (`examples/projects/walking`)

Movement in space, measured from the glasses' own visual-inertial odometry. Six laps of a
marked indoor loop, three at a normal pace and three slow; the experimenter presses ENTER
at the start line.

*Recording.* `preset:base` with VIO (10 Hz poses in the odometry frame), both IMUs, the
barometer, RGB at 5 fps (what the participant saw, kept light), and `imu.motion_detector`
emitted on changes only (`emit: "changes"`) — a sparse event stream integrated in the
analysis. The experiment file sets the stall timeout explicitly because the participant
walks away from the laptop: a lost link ends the session cleanly after 30 s instead of
recording nothing. This is the project where heat matters most; the script prints the skin
temperature after every lap and the protocol says what to do at 40 °C.

*Protocol.* Five seconds of standing still before the first lap so that VIO initialises
(the script marks `vio_ready`), then one interval per lap with its pace.

*Analysis.* Per lap: duration, path length from the smoothed VIO translation, mean speed,
loop closure (the distance between the lap's end and start — the check that VIO tracked),
step cadence from the IMU's acceleration spectrum, altitude change from the barometer, the
head-moving fraction; a top view of all laps and the speed over the session. On the mock the
participant walks a perfect circle and has no gait, so the cadence is empty.

*Adapt it.* Routes, zones and dwell times: polygons in the odometry frame once you have
walked the route. Stairs: the barometer and the VIO z. Outdoor: GPS is a stream too
(`gps`), where the device provides it. Long sessions: several sessions per participant with
cooling in between — the analysis takes a list of session directories.

---

## Appendix — Adapting a project: the checklist

1. Copy the folder; rename `experiment.name`; write the `description` as you would in a
   methods section (it is frozen into every session).
2. `streams`: only what the analysis needs; `inspect-config` to see the live plan.
3. `processors`: only what you must see live; `save_output` on what the analysis reads.
4. `run_session.py`: the trial structure as intervals with factor payloads; `--mock --auto`
   until `analyze.py` produces the table.
5. `protocol.md`: the physical setup, the monitor checks, the checks before the participant
   leaves — tested on a colleague.
6. One uncharged pilot of the full length; look at the temperature curve
   (`quality/device_health.jsonl`).
7. Freeze: commit the folder; note the framework version (`ariastreamlab --version`) in the
   study's log.
