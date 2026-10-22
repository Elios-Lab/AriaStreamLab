# The dashboard

`ariastreamlab run` and `ariastreamlab replay` serve a single-page browser dashboard while the
session runs (NiceGUI; `pip install 'ariastreamlab[dashboard]'`, already part of `[dev]`). It
needs nothing from you: the page opens at `http://127.0.0.1:8765/` and closes with the session
(the server itself stays up for the rest of the process, so a script that runs several
experiments keeps one tab).

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
| Signals | any scalar channel, chosen in the multi-select: IMU axes, gaze yaw/pitch/depth, barometer, magnetometer, hand confidences, every numeric field of a processor's output; a window slider (2–60 s); channels are thinned to 100 Hz for display and keep the last 60 s | `raw.*`, `derived.*` |
| Device | battery, charging, skin temperature, thermal mitigation, Wi-Fi (live sessions) | `health.device` |
| Link | connections, data path (`usb` / `wifi`), SDK queue depths, TicSync (live); origin and sample count otherwise | `health.link` |
| Streams | received / effective Hz / nominal Hz / out-of-order / transport drops per stream | `StatsTracker` |
| Pipeline | per processor: fired, outputs, exec ms, latency ms, queue drops, errors | `ProcessingGraph.stats()` |
| Device quality | the device's own per-sensor counters (live, every 10 s) | `health.quality` |
| Markers | a name field, a *mark* button and shortcuts; the last 15 events (markers, `device_state`, session events); a marker made from the page is `Experiment.mark(name)` — same row in `events/events.parquet`, same device-time estimate | `events.*` |
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
