# The application: dashboard, designer, sessions

`ariastreamlab` with no arguments opens the application in a desktop window: **Home**,
**Design**, **Monitor** and **Sessions**. The window is the system WebView (WebKit on macOS,
WebView2 on Windows, GTK WebKit2 or Qt on Linux) driven by NiceGUI, so the same pages are
also what `ariastreamlab run` / `replay` serve to a browser at `http://127.0.0.1:8765/monitor`
and what `ariastreamlab app --browser` serves; `"dashboard": {"native": true}` makes `run` and
`replay` open a window too. The acquisition never waits for the window: it runs on worker
threads and the interface subscribes to the bus like any other consumer.

The **Sessions** page lists the session directories under the experiment's `storage.root`
(editable), shows the selected session's manifest facts, quality summary, events and
intervals, replays it through the pipeline at the chosen speed (the monitor opens on it) and
opens its folder in the file manager.

## The monitor

`ariastreamlab run` and `ariastreamlab replay` serve the monitor while the session runs. Its
job is to let you verify that acquisition is working *while the participant is still
wearing the glasses* — that every stream flows at the expected rate, that nothing is being
dropped and where, that the gaze projection lands where it should, that the device is not
overheating — and to let you annotate the session as it happens. It needs nothing from you:
the page opens at `http://127.0.0.1:8765/` and closes with the session (the server itself
stays up for the rest of the process, so a script that runs several experiments keeps one
tab).

```bash
ariastreamlab run examples/mock_experiment.json --mock          # try it without glasses
ariastreamlab run pilot.json --no-dashboard                     # headless
ariastreamlab replay sessions/<folder> --dashboard-port 9000    # another port
ARIASTREAMLAB_DASHBOARD=0 ariastreamlab replay … --speed 0      # a whole shell without it
```

## What is on the page

| Card | Content | Source on the bus |
|---|---|---|
| header | experiment / participant / origin (`live`, `mock`, `replay`), samples seen, seconds of data, session state | `Experiment` |
| images | every image stream in `dashboard.show` (default: all enabled), downscaled to 640 px at `dashboard.image_fps`; a circle marks each derived gaze point (`gaze.project_to_rgb` output) on the RGB frame | `raw.rgb`, `raw.et_*`, `raw.slam_*`, `derived.*` |
| Signals | any scalar channel, chosen in the multi-select: IMU axes, gaze yaw/pitch/depth, barometer, magnetometer, hand confidences, audio level (dBFS), every numeric field of a processor's output; a window slider (2–60 s); channels are thinned to 100 Hz for display and keep the last 60 s | `raw.*`, `derived.*` |
| Device | battery, charging, skin temperature, thermal mitigation, Wi-Fi (live sessions) | `health.device` |
| Link | connections, data path (`usb` / `wifi`), SDK queue depths, TicSync, seconds since the last sample when the stream has stalled (live); origin and sample count otherwise; app-queue drops per bus subscriber; session size on disk, write rate, free disk | `health.link`, `health.storage`, bus stats |
| Streams | received / effective Hz (over the last 5 s; falls to 0 when a stream stops) / nominal Hz / out-of-order / transport drops per stream | `StatsTracker` |
| Pipeline | per processor: fired, outputs, exec ms, latency ms, queue drops, errors | `ProcessingGraph.stats()` |
| Device quality | the device's own per-sensor counters (live, every 10 s) | `health.quality` |
| Trajectory | x/y of every pose stream (`vio`…) over the last 120 s, odometry frame, with the current position marked | `raw.vio` |
| Pipeline → *parameters* | a dialog with every running processor's parameters (forms generated from its `Parameters` model); *apply* calls `Experiment.set_parameters()`, which validates, swaps the parameters on the worker and records a `config_change` event — the frozen `experiment.json` is untouched | `ProcessingGraph` |
| Markers | name + optional JSON payload; *mark* (a point event), *begin* / *end* (an interval, paired by id in `inspect --events` and `SessionReader.intervals()`), *note*; the last 15 events with their phase | `events.*` |
| header | links to the *designer* (`/design`, below) | |
| Replay | pause / resume, speed (0.25–4×), a seek slider over the recording (shown for the `session` and `tools` replay backends; the `sdk` backend runs Meta's engine and has no controls) | `ReplayControl` |

Everything on the page is also served as JSON at `/api/state` (a snapshot: session, images'
sizes and ages, channel summaries, gaze points, health, events, stream and processor stats,
replay position), so a notebook or a second tool can poll it:

```python
import json, urllib.request

state = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/state"))
state["streams"]["rgb"]["effective_rate_hz"], state["replay"]
```

## Configuration

```json
"dashboard": {
  "enabled": true, "host": "127.0.0.1", "port": 8765, "open_browser": true,
  "show": ["rgb", "eye_gaze", "gaze_rgb", "imu_left"],
  "image_fps": 10
}
```

`show: null` displays every enabled stream and every processor output. Set `host` to
`"0.0.0.0"` to watch from another machine on the hotspot (there is no authentication — the
page is meant for the laptop running the experiment).

## Designing an experiment in the browser

```bash
ariastreamlab design                      # a blank experiment in the current directory
ariastreamlab design pilot.json           # edit an existing file
```

`/design` is a form view of `experiment.json`. Start from a preset, name the experiment and
participant, switch streams on (their parameters — fps, resolution, rate — appear next to each
one, restricted to what Aria Gen 2 accepts), add processors from the catalogue (built-ins,
installed plugins and the `.py` files in `processors/` next to the file; *rescan* after adding
one), wire their inputs from a list that only offers streams that exist upstream, set their
parameters in forms generated from each processor's `Parameters` model, and adjust device,
storage and dashboard settings. The right-hand column re-validates after every change with
the same code `inspect-config` uses — problems are listed with their JSON path, the *live
plan* shows the profile, interface and the warnings about what profile9 really delivers — and
previews the compact JSON that *save* writes (values equal to the defaults are left out, so
the file reads like one written by hand).

*Run* starts the experiment in the same process: mock or live, optionally for a fixed
duration; the monitor page shows it and the session lands under `storage.root` next to the
file. On a live run the "join the hotspot" step that `run` asks on the terminal becomes a
dialog on the page; *stop* ends the session (and releases that dialog). The JSON is what gets
run and what is frozen into the session's manifest — nothing happens in the designer that
could not be done by editing the file.

## How it fits the architecture

The dashboard is a bus subscriber (`DashboardState`) plus a page that reads from it twice a
second. The subscriber's queues drop the oldest item when full, so a slow browser never
back-pressures acquisition or the processors; the page thread runs the NiceGUI/uvicorn server
in a daemon thread of the same process, started once per process (`get_dashboard`). Sessions
attach their `DashboardState` on `open()` and detach on `close()`; a tab left open after the
session shows the last state until the next session attaches.

The subscriber knows nothing about sources: the mock, the glasses and a replay feed it the
same `raw.*` / `derived.*` samples, which is why the same page serves all three. `DashboardState`
can be used on its own — a custom viewer, a test, a headless logger — without NiceGUI installed.

From Python:

```python
from ariastreamlab.api import Experiment

with Experiment.from_json("pilot.json", mock=True, duration_s=60) as exp:
    print(exp.dashboard_url)  # None when disabled
    exp.run()
```
