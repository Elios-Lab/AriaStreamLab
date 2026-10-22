# AriaStreamLab

An experiment-oriented Python framework for **Meta Project Aria Gen 2** glasses, built for
university students and researchers: one JSON file describes the experiment, one command runs
it, and the raw VRS recording stays the ground truth while processors produce derived data.

> **Status: Phase 6 (alpha).** Configuration, core data model, timing utilities, event
> bus, synthetic (mock) source, crash-safe session storage, session/VRS replay, the
> processing graph with plugin processors, the live Aria Gen 2 adapter, the browser dashboard
> and experiment designer, annotations, quality monitoring and the CLI exist; everything but
> the live path is tested without hardware, the live path against a fake SDK and with a
> hardware smoke test that passes on a MacBook with the glasses (live session over the
> hotspot, control hand-over to Wi-Fi, VRS, replay of the session). See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the plan and the hardware
> findings.

```
Meta Aria Gen 2 ──wireless──▶ Acquisition ──▶ Event bus ──▶ Processing DAG ──▶ Derived data
                                   │                            │
                                   └──▶ Raw VRS (ground truth)  └──▶ Live dashboard
Recorded session ──▶ Replay source ──▶ (same event bus, processors and dashboard)
```

## Quick start (no glasses needed)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                   # add ",aria" on a machine that talks to the glasses
ariastreamlab inspect-config examples/mock_experiment.json
ariastreamlab run examples/mock_experiment.json --mock --duration 10    # opens the dashboard
ariastreamlab inspect sessions/<the new folder>              # what was written, any problems
ariastreamlab replay sessions/<the new folder> --speed 0     # same pipeline, from disk
```

A run writes `sessions/<utc>_<experiment>_<participant>/` with `manifest.json`, the frozen
`experiment.json`, the data in Parquet / PNG / MP4 chunks, `events/`, `indexes/timeline.parquet`
and `logs/`. `inspect --repair` marks a session that crashed and rebuilds its index.

With `projectaria_tools` installed, a Gen 2 recording replays through the same pipeline:

```bash
ariastreamlab replay recording.vrs --start 5 --end 8 --streams imu_left,eye_gaze,rgb
ariastreamlab replay recording.vrs --config my_experiment.json   # only the configured streams
ariastreamlab replay recording.vrs --backend sdk                 # Meta's replay engine, "as live"
```

Processors never learn whether a sample came from the glasses, a mock or a replay — that
invariant is tested, not promised.

## From zero to a first capture

1. **Install.** Python 3.11 (3.10–3.12 work; Meta's SDK has no 3.13 wheels yet), then
   `pip install -e ".[dev,aria]"` — `aria` adds `projectaria_client_sdk` and
   `projectaria_tools`. `ariastreamlab doctor` checks the environment.
2. **Pair the glasses.** Plug them in over USB, run `aria_gen2 auth pair` and accept the
   prompt in the Aria Companion App on your phone; `aria_gen2 auth check` confirms.
   Optionally pair over Bluetooth too (`aria_gen2 auth pair --ble --serial <serial>`) so the
   hotspot can be started without the cable. `ariastreamlab doctor --device` must be green.
3. **Network.** Streaming goes over the glasses' own Wi-Fi hotspot: `run` starts it, prints
   the network name and password and waits for you to join it from the laptop (macOS can do
   it for you with `"hotspot": {"auto_join": true}`). Keep the cable in for the first run; it
   can be unplugged once data flows. Hotspot sessions warm the glasses, so keep them under
   ~20 min or tether. Details, including every failure mode we met, are in
   [docs/tutorials/live_session.md](docs/tutorials/live_session.md).
4. **Describe the experiment** — the smallest useful file, or use the designer
   (`ariastreamlab design`) and let it write the file for you:

   ```json
   { "extends": "preset:rgb_imu",
     "experiment": { "name": "pilot", "participant": "P001" } }
   ```

   `ariastreamlab inspect-config pilot.json` says what it will do (streams, processors, the
   live plan with its warnings). The examples are complete experiments:
   `examples/rgb_imu.json` (RGB + IMU capture), `examples/eye_gaze.json` (eye-gaze study with
   the gaze projected on the RGB frames) and `examples/rgb_processing.json` (RGB + a realtime
   processing pipeline); `examples/mock_experiment.json` runs without glasses.
5. **Start.** `ariastreamlab run pilot.json --duration 300` (or no duration: it runs until
   you stop it). The dashboard opens in the browser; the terminal prints rates every 2 s.
6. **Stop safely.** Press **Ctrl-C once** (or the duration elapses, or *stop* in the
   dashboard): the adapter stops streaming, releases the receiver so the VRS is finalised,
   flushes every writer and writes the manifest and the quality summary — wait for the
   summary to print. A second Ctrl-C forces the exit; the session is still readable
   (`inspect --repair` marks it crashed and rebuilds its index).
7. **Look at the data.** `ariastreamlab inspect sessions/<folder>` lists what was written and
   the quality summary; `--events` lists markers and intervals; the folder has
   `manifest.json`, the frozen `experiment.json`, `raw/<file>.vrs` (the ground truth),
   `derived/<stream>/…parquet|mp4`, `events/events.parquet`, `quality/` and `logs/`.
   Everything is Parquet/JSON/MP4, readable from pandas, DuckDB, MATLAB or R.
8. **Replay.** `ariastreamlab replay sessions/<folder> --speed 0` re-runs the same pipeline
   from the recording at full speed; `--config other.json` applies a different one;
   `--speed 1`, `2`, `0.5`, pause and seek are there for watching it in the dashboard.
9. **Add your own processor.** A ten-line class in `processors/` next to the JSON — see
   [docs/tutorials/custom_processors.md](docs/tutorials/custom_processors.md) and
   `examples/custom_processor/`. It runs identically live and in replay; that invariant is
   tested.

Also read [docs/tutorials/timestamps.md](docs/tutorials/timestamps.md) before analysing
anything: the streams have different rates, arrive in batches and out of order, and the
tutorial explains how to line them up without fooling yourself.

## Processing

Processors are declared in the experiment JSON and run on their own threads with bounded
queues, join policies for multi-input processors and per-processor statistics:

```json
"processors": [
  { "id": "motion", "type": "imu.motion_detector", "inputs": ["imu_left"], "outputs": ["head_motion"],
    "parameters": { "threshold": 0.15 } },
  { "id": "gaze_on_rgb", "type": "gaze.project_to_rgb", "inputs": ["rgb", "eye_gaze"], "outputs": ["gaze_rgb"],
    "execution": { "join": "nearest", "tolerance_ms": 40 } },
  { "id": "blinks", "type": "blink_counter.py", "inputs": ["eye_gaze"], "outputs": ["blinks"] }
]
```

`ariastreamlab processors` lists what is available (built-ins such as `image.resize`,
`imu.lowpass`, `gaze.project_to_rgb`, `generic.downsample`; packages that register an
`ariastreamlab.processors` entry point; files in `processors/` next to your JSON). Writing
one is a ten-line class — see [docs/tutorials/custom_processors.md](docs/tutorials/custom_processors.md)
and `examples/custom_processor/`. Replaying a session at `--speed 0` re-processes every sample
with back-pressure instead of drop policies, so a recording can be re-analysed with new
parameters at any time.

## Annotations

Markers and intervals share the sensor streams' time base (device time, estimated from the
samples when created on the laptop, exact when you pass a sample's timestamp):

```python
with Experiment.from_json("pilot.json") as exp:
    trial = exp.begin("trial", {"trial": 3, "condition": "A"})  # an interval
    exp.mark("stimulus", {"id": "img_17"})  # a point event
    exp.mark("blink", at_ns=sample.device_timestamp_ns)  # exact device time
    exp.end(trial, {"correct": True})
    exp.set_parameters("motion", threshold=0.3)  # a config_change event
```

They land in `events/events.parquet` with `timestamp_source` (`device` / `estimated` /
`host_only`), next to the device state changes, the periodic clock anchors and any
parameter change made while running (the frozen `experiment.json` and the manifest keep the
start-up values; the change is an event with the time it took effect). The dashboard has a
name + JSON payload box with *mark*, *begin* and *end* buttons. Afterwards,
`SessionReader(folder).events()` / `.intervals()` or `ariastreamlab inspect <folder> --events`
give them back, paired.

## Quality

Four kinds of loss are kept apart and shown side by side — **device** drops (the glasses'
own counters, polled every 10 s), **transport** drops (sequence gaps seen by the laptop),
**application queue** drops (a bus subscriber that fell behind: dashboard, recorder…) and
**processor** drops (a processor's input queue). The dashboard shows them live with battery,
temperature, Wi-Fi, disk usage and write rate; every session ends with a concise summary,
printed by `run`, shown by `inspect`, stored in `manifest.json` (`quality`) and in
`quality/summary.{json,txt}` next to the raw device-health and quality-stats logs:

```
rgb        received 5988   device-dropped 3   transport-dropped 9   processor-dropped 0   effective 9.98 Hz (nominal 10)
imu_left   received 479400 device-dropped 0   transport-dropped 0   processor-dropped 0   effective 799.0 Hz (nominal 800)
app queues   no drops
processors   gaze_on_rgb 5900→5900 p95 0.5 ms
session      duration 600.2 s   disk 1.84 GB (free 120 GB)   battery 90→60 %   skin temp max 38.1 °C   warnings: 1
  - rgb: 9 samples lost in transport
```

## Dashboard

`run` and `replay` serve a browser dashboard at `http://127.0.0.1:8765/` (opened
automatically; `--no-dashboard`, `--dashboard-port`, or `"dashboard": {"enabled": false}` in the
JSON turn it off or move it; `ARIASTREAMLAB_DASHBOARD=0` disables it for a whole shell, e.g. a
batch of replays). It shows the image streams with gaze overlays, any scalar channel on a
scrolling chart (IMU axes, gaze angles, processor outputs — pick them in the *Signals* card),
per-stream rates and drops, per-processor counters, the device's battery / temperature / Wi-Fi
and per-sensor quality on a live session, marker buttons (`trial_start`, `trial_end`, your own
names) that land in `events/events.parquet` with device time, and pause / speed / seek controls
on a replay. It is a bus subscriber like any other: it cannot slow down acquisition (its queues
drop the oldest frame first) and the same page serves live, mock and replay sessions. Anything
it shows is also available as JSON at `/api/state`, and `Experiment.dashboard_url` tells a
script where it is.

`ariastreamlab design [experiment.json]` opens the **experiment designer**: a form view of the
JSON file — presets, streams with their allowed parameters, processors from the catalogue with
generated parameter forms and input wiring, device/storage/dashboard options — validated live
with the same checks as `inspect-config`, previewed as the compact JSON it saves, and runnable
from the page (mock or live, with the hotspot prompt as a dialog). See
[docs/tutorials/dashboard.md](docs/tutorials/dashboard.md).

Optional: `pip install 'ariastreamlab[rerun]'` and `"dashboard": {"rerun": true}` also
stream images, signals and the VIO trajectory to a [Rerun](https://rerun.io) viewer (3-D,
time scrubbing) next to the dashboard.

## Layout

| Path | Purpose |
|---|---|
| `ariastreamlab/config` | Pydantic models, presets (`extends: "preset:rgb_imu"`), JSON Schema |
| `ariastreamlab/core` | `Sample` / `DerivedSample`, stream registry, event bus, errors |
| `ariastreamlab/timing` | host clock + UTC anchor, exact ns conversions, temporal lookup/join |
| `ariastreamlab/acquisition` | `Source` interface, mock source, replay engine (`session`, `tools`, `sdk` backends), and `aria_gen2/` — the only package that imports Meta's SDK: live adapter, device manager, CLI wrapper, quality parser, conversions |
| `ariastreamlab/processing` | `Processor` contract, plugin discovery, per-processor workers (queues, joins, stats), the graph, built-ins |
| `ariastreamlab/storage` | session layout, manifest, chunked Parquet/image/video writers, reader, timeline index, `inspect`/repair |
| `ariastreamlab/monitoring` | per-stream rates and drop accounting, the health log (device/quality snapshots, disk), the end-of-session quality summary |
| `ariastreamlab/dashboard` | `DashboardState` (the bus collector, usable without NiceGUI), the NiceGUI monitor page, `Designer` (the experiment-file model) and the designer page |
| `ariastreamlab/cli` | `ariastreamlab doctor \| devices \| streams \| presets \| processors \| schema \| inspect-config \| design \| run \| replay \| inspect` |
| `scripts/probe_sdk.py` | raw-SDK hardware probe that answers the assumptions in the architecture doc |
| `docs/` | architecture; tutorials: `live_session.md`, `custom_processors.md`, `dashboard.md` (and the designer), `timestamps.md` |
| `examples/` | `rgb_imu.json`, `eye_gaze.json`, `rgb_processing.json`, `mock_experiment.json`, `custom_processor/` (a processor loaded from a file next to the JSON) |

## Development

```bash
ruff check . && ruff format --check .
mypy
lint-imports            # only acquisition/aria_gen2 may import Meta's SDK
pytest                  # hardware tests are marked and skipped by default
ARIA_HARDWARE=1 pytest -m hardware -s   # on the paired laptop: 15 s live session + replay
# tests/integration/test_vrs_replay.py runs only with projectaria_tools and a recording at
# tests/data/aria_gen2_sample.vrs (see tests/data/README.md)
```

## Licence

To be decided (MIT or Apache-2.0). Meta's Project Aria Client SDK is a separate, proprietary
dependency that AriaStreamLab calls but does not redistribute.
