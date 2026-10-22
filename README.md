# AriaStreamLab

A Python framework for running experiments with **Meta Project Aria Gen 2** glasses. One
JSON file describes the experiment, one command runs it; the raw VRS recording the glasses
produce is kept as the ground truth, processors derive data from the live streams, every
session carries its provenance, and any session can be replayed through the same pipeline
without the glasses.

It is written for researchers who need recordings they can trust and analyses they can
reproduce: every design decision favours temporal correctness, provenance and observability
over convenience, and the places where the hardware behaves differently from its
documentation are measured and written down rather than papered over.

> **Status: 0.1 (alpha).** The complete pipeline — configuration, acquisition adapter, event
> bus, processing graph, storage, replay, dashboard, designer, annotations and quality
> monitoring — exists and is tested without hardware (173 tests); the live path is tested
> against a fake SDK and verified on a MacBook with the glasses (14 hardware runs, including
> the untethered hotspot flow and a replay reproducing the live counts). A 20-minute
> validation session is the remaining step before calling it a release. The design, the plan
> and all hardware findings are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
Meta Aria Gen 2 ──wireless──▶ Acquisition ──▶ Event bus ──▶ Processing DAG ──▶ Derived data
                                   │                            │
                                   └──▶ Raw VRS (ground truth)  └──▶ Live dashboard
Recorded session ──▶ Replay source ──▶ (same event bus, processors and dashboard)
```

## What a session gives you

Every run writes one self-describing directory, `sessions/<utc>_<experiment>_<participant>/`:

| Path | Content |
|---|---|
| `manifest.json` | session id, UTC start/end, software and SDK versions, git commit, device serial and firmware, streaming profile and interface, the enabled streams with their measured rates and drop counts, the processors with class, version and validated parameters, the calibration, the quality summary, warnings |
| `experiment.json`, `experiment.source.json` | the resolved configuration that ran (frozen when the session starts) and the file you actually wrote |
| `raw/<file>.vrs` | the recording as received from the glasses — the authoritative record; never modified |
| `derived/<stream>/part-*.parquet` · `.mp4` · `.png` | processor outputs, chunked, each with a `_meta.json` naming the producer, its version and parameters |
| `events/events.parquet` | markers, intervals, device state changes, parameter changes, clock anchors — all on the device time axis, each with how its timestamp was obtained |
| `quality/` | `summary.{json,txt}` (per-stream losses by cause, rates, duration, disk, battery, temperature, warnings) and the raw device-health and quality-statistics logs |
| `calibration/` | the device calibration as JSON (cameras, IMUs, CPF transform) |
| `indexes/timeline.parquet`, `logs/session.log` | a time index over every stored stream; the structured log |

Three guarantees hold across the framework and are enforced by tests, not by convention:

* **Timestamps are the device's.** `device_timestamp_ns` is read from the SDK or the VRS
  and never rewritten; host receive times are kept separately for latency. Events created on
  the laptop are mapped onto the device axis with an explicit estimate and labelled as such.
* **Raw data is never touched.** Processors read from the bus and write to `derived/`; the
  VRS is written by the SDK and only ever read.
* **Live and replay are the same pipeline.** A processor cannot tell whether a sample came
  from the glasses, a replay or the synthetic source; a session replayed through its frozen
  configuration reproduces every derived value and timestamp (invariant I-1 in the design
  document, covered by a test).

Everything is Parquet, JSON, MP4 or VRS: readable from pandas, DuckDB, R or MATLAB without
AriaStreamLab installed.

## Install and try it without the glasses

Python 3.11 is recommended (3.10–3.12 work; Meta's SDK has no 3.13 wheels yet).

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                   # add ",aria" on the laptop that talks to the glasses
ariastreamlab doctor                      # environment check
ariastreamlab inspect-config examples/mock_experiment.json
ariastreamlab run examples/mock_experiment.json --mock --duration 10    # opens the dashboard
ariastreamlab inspect sessions/<the new folder>            # what was written, quality summary
ariastreamlab replay sessions/<the new folder> --speed 0   # same pipeline, from disk
```

The synthetic source produces camera frames, IMU, gaze, poses, audio and the small sensors
with realistic timestamps and rates (and, on request, transport drops and out-of-order
delivery), so processors, storage, annotations and the dashboard can be developed and tested
anywhere.

## Running an experiment with the glasses

1. **Install the SDK side.** `pip install -e ".[dev,aria]"` adds `projectaria_client_sdk`
   and `projectaria_tools`.
2. **Pair.** Connect the glasses over USB, run `aria_gen2 auth pair` and accept in the Aria
   Companion App; `aria_gen2 auth check` confirms. Pair over Bluetooth as well
   (`aria_gen2 auth pair --ble --serial <serial>`) if you want to start the hotspot without
   the cable. `ariastreamlab doctor --device` must report everything green.
3. **Network.** Data streams over the glasses' own Wi-Fi hotspot. `run` starts it, prints the
   network name and password and waits for you to join it from the laptop (`"hotspot":
   {"auto_join": true}` lets macOS do it). Keep the cable in for the first run; it can be
   unplugged once data flows — control moves to Wi-Fi by itself. Wireless sessions warm the
   glasses (≈ +7 °C in two minutes; throttling starts at 42 °C skin temperature), so keep them
   short or tether for long recordings.
4. **Describe the experiment.** The smallest useful file:

   ```json
   { "extends": "preset:rgb_imu",
     "experiment": { "name": "pilot", "participant": "P001" } }
   ```

   `ariastreamlab inspect-config pilot.json` shows what it will do: streams with their
   parameters, processors, and the *live plan* — which streaming profile, which interface, and
   warnings about what the profile actually delivers (for example, `profile9` streams RGB at
   5 Hz and eye gaze at 25 Hz whatever the file asks). `ariastreamlab design` opens a form view
   of the same file in the browser, validated as you edit, and can start the run from there.
5. **Run.** `ariastreamlab run pilot.json --duration 300`, or without a duration until you
   stop it. The dashboard opens; the terminal reports rates every two seconds.
6. **Stop safely.** Ctrl-C once (or the duration elapses, or *stop* on the dashboard): the
   adapter stops streaming, releases the receiver so the SDK finalises the VRS, flushes every
   writer, and prints the quality summary — wait for it. A second Ctrl-C forces the exit; the
   session remains readable (`inspect --repair` marks it crashed and rebuilds its index).
7. **Check the session.** `ariastreamlab inspect sessions/<folder>` lists what was stored and
   the quality summary; `--events` lists markers, intervals and state changes.

The complete sequence, the health signals and a troubleshooting table built from the
failure modes met on real hardware are in
[docs/tutorials/live_session.md](docs/tutorials/live_session.md).

### Complete examples

| File | Experiment |
|---|---|
| `examples/rgb_imu.json` | RGB + both IMUs, the simplest capture |
| `examples/eye_gaze.json` | RGB + eye cameras + on-device gaze, projected onto the RGB frames; trials as intervals |
| `examples/rgb_processing.json` | RGB + a realtime pipeline: downscale → grayscale, IMU low-pass, head-motion gate |
| `examples/mock_experiment.json` | the synthetic source with two processors — runs anywhere |
| `examples/custom_processor/` | a processor written for one experiment, loaded from `processors/` next to the JSON |

## Describing an experiment

The JSON file has sections for `experiment` (name, participant, description, tags),
`streams` (which sensors, with the parameters Aria Gen 2 accepts — `fps`, `resolution`,
`rate_hz`), `processors`, `device` (connection, hotspot, profile), `storage`, `dashboard`,
`replay`, `logging` and `mock`. A preset (`"extends": "preset:rgb_imu"`, `gaze_experiment`,
`full_capture`, `mock_experiment`, `base`) supplies defaults; your file overrides. Unknown keys
and unsupported values are errors with the JSON path and the allowed values, found before
anything is started; `ariastreamlab schema` exports the JSON Schema for editor completion.

## Processing

Processors are declared in the file and run on their own threads with bounded queues, join
policies for multi-input processors and per-processor statistics:

```json
"processors": [
  { "id": "motion", "type": "imu.motion_detector", "inputs": ["imu_left"], "outputs": ["head_motion"],
    "parameters": { "threshold": 0.15 } },
  { "id": "gaze_on_rgb", "type": "gaze.project_to_rgb", "inputs": ["rgb", "eye_gaze"], "outputs": ["gaze_rgb"],
    "execution": { "join": "nearest", "tolerance_ms": 40 } },
  { "id": "blinks", "type": "blink_counter.py", "inputs": ["eye_gaze"], "outputs": ["blinks"] }
]
```

`ariastreamlab processors` lists what is available: built-ins (`image.resize | grayscale |
crop`, `imu.lowpass | magnitude | motion_detector`, `gaze.project_to_rgb`, `generic.downsample
| passthrough`), packages registering an `ariastreamlab.processors` entry point, and `.py`
files in `processors/` next to your JSON. A processor is a small class with a `process()`
method and an optional Pydantic `Parameters` model; its class, version and validated
parameters go into the manifest and into every output's `_meta.json`. Multi-input processors
choose how secondary streams are matched to the primary one (`nearest`, `previous`, `latest`)
with an explicit tolerance. See
[docs/tutorials/custom_processors.md](docs/tutorials/custom_processors.md).

## Annotations

Markers and intervals live on the same time axis as the sensor data:

```python
from ariastreamlab.api import Experiment

with Experiment.from_json("pilot.json") as exp:
    trial = exp.begin("trial", {"trial": 3, "condition": "A"})  # an interval
    exp.mark("stimulus", {"id": "img_17"})  # a point event, stamped now
    exp.mark("blink", at_ns=sample.device_timestamp_ns)  # an exact device time
    exp.end(trial, {"correct": True})
    exp.set_parameters("motion", threshold=0.3)  # recorded as a config_change event
    exp.run()
```

Each event records `timestamp_source`: `device` (exact, you supplied the timestamp),
`estimated` (created on the laptop and mapped through the current host↔device offset
estimate, accurate to the link latency) or `host_only` (created before any data arrived,
re-estimated at the end). Parameter changes made while running are events too: the frozen
`experiment.json` and the manifest keep the start-up values, and the event carries the change
and the device time it took effect. The dashboard offers the same actions (name, optional
JSON payload, *mark* / *begin* / *end*). Afterwards `SessionReader(folder).events()` and
`.intervals()`, or `ariastreamlab inspect <folder> --events`, return them paired.

## Quality

Four kinds of loss are kept apart because they call for different remedies: **device** drops
(the glasses' own per-sensor counters, polled every 10 s), **transport** drops (sequence gaps
observed on the laptop), **application-queue** drops (a bus subscriber that fell behind —
dashboard, recorder) and **processor** drops (a processor's input queue). The dashboard shows
them live together with battery, skin temperature, thermal mitigation, Wi-Fi, disk usage and
write rate. Every session ends with a summary, printed by `run`, shown by `inspect`, stored
as `manifest.quality` and as `quality/summary.{json,txt}`:

```
rgb        received 5988   device-dropped 3   transport-dropped 9   processor-dropped 0   effective 9.98 Hz (nominal 10)
imu_left   received 479400 device-dropped 0   transport-dropped 0   processor-dropped 0   effective 799.0 Hz (nominal 800)
app queues   no drops
processors   gaze_on_rgb 5900→5900 p95 0.5 ms
session      duration 600.2 s   disk 1.84 GB (free 120 GB)   battery 90→60 %   skin temp max 38.1 °C   warnings: 1
  - rgb: 9 samples lost in transport
```

## Dashboard and designer

`run` and `replay` serve a dashboard at `http://127.0.0.1:8765/`: image streams with the
projected gaze, any scalar channel on a scrolling chart, the VIO trajectory, per-stream rates
and drops, per-processor counters and latencies, the device's health and quality counters,
marker and interval buttons, a parameter editor for running processors, and pause / speed /
seek on a replay. It is a bus subscriber like any other — it cannot slow acquisition down and
closing the tab changes nothing. `--no-dashboard`, `--dashboard-port`, `"dashboard":
{"enabled": false}` or `ARIASTREAMLAB_DASHBOARD=0` turn it off or move it; the same data is
served as JSON at `/api/state`.

`ariastreamlab design [experiment.json]` opens the **experiment designer**: presets, streams
with their allowed parameters, processors from the catalogue with forms generated from their
`Parameters`, device / storage / dashboard options, live validation with the same checks as
`inspect-config`, a preview of the compact file it saves, and a *run* button (mock or live,
with the hotspot step as a dialog). Nothing happens there that could not be done by editing
the file — it only writes `experiment.json`. See
[docs/tutorials/dashboard.md](docs/tutorials/dashboard.md).

Optionally, `pip install 'ariastreamlab[rerun]'` and `"dashboard": {"rerun": true}` also
stream images, signals and the trajectory to a [Rerun](https://rerun.io) viewer.

## Replay and reproducibility

```bash
ariastreamlab replay sessions/<folder> --speed 0            # same configuration, every sample
ariastreamlab replay sessions/<folder> --config other.json  # a different pipeline on the same recording
ariastreamlab replay recording.vrs --start 5 --end 8 --streams imu_left,eye_gaze,rgb
ariastreamlab replay recording.vrs --backend sdk            # Meta's replay engine, "as live"
```

A replay reads the raw VRS (or, for synthetic sessions, the stored raw streams) and injects it
into the same bus, processors and dashboard. `--speed 0` processes every sample with
back-pressure instead of drop policies — the mode for results you will publish; `--speed 1`,
`2`, `0.5`, pause and seek are for watching. Each replay is itself a session, with its own
manifest pointing at the recording it came from, so re-analyses are as traceable as the
original capture.

## Analysing the data

```python
import pandas as pd
from ariastreamlab.storage.reader import SessionReader

s = SessionReader("sessions/20261022T121420Z_pilot_P001")
s.manifest.streams["rgb"].effective_rate_hz  # what was measured
events = s.events(kind="marker")  # Event objects, device time
trials = s.intervals("trial")  # paired begin/end, with payloads
motion = pd.read_parquet(s.layout.root / "derived/head_motion")  # or any Parquet reader
for sample in s.iter_stream("gaze_rgb", start_ns=trials[0].start_ns, end_ns=trials[0].end_ns):
    ...
```

Raw sensor data lives in the VRS and is read with `projectaria_tools`
(`create_vrs_data_provider`), or replayed through AriaStreamLab with a processor that writes
what you need. Before joining streams, read
[docs/tutorials/timestamps.md](docs/tutorials/timestamps.md): streams have different rates,
arrive in batches and out of order, and the tutorial explains the two clocks, the join modes
and the precision of each kind of event.

## Documentation

| Document | Content |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | the design: interfaces, configuration model, storage format, concurrency, timestamp strategy, the hardware findings (§0), implementation status (§14) |
| [docs/tutorials/live_session.md](docs/tutorials/live_session.md) | a live session step by step, health signals, troubleshooting |
| [docs/tutorials/custom_processors.md](docs/tutorials/custom_processors.md) | writing, parameterising, joining and packaging processors |
| [docs/tutorials/dashboard.md](docs/tutorials/dashboard.md) | the dashboard cards, the designer, `/api/state` |
| [docs/tutorials/timestamps.md](docs/tutorials/timestamps.md) | clocks, synchronisation, event precision, replay |

## Layout

| Path | Purpose |
|---|---|
| `ariastreamlab/config` | Pydantic models, presets (`extends: "preset:rgb_imu"`), JSON Schema |
| `ariastreamlab/core` | `Sample` / `DerivedSample`, stream registry, event bus, errors |
| `ariastreamlab/timing` | host clock + UTC anchor, exact ns conversions, temporal lookup/join |
| `ariastreamlab/acquisition` | `Source` interface, synthetic source, replay engine (`session`, `tools`, `sdk` backends), and `aria_gen2/` — the only package that imports Meta's SDK: live adapter, device manager, CLI wrapper, quality parser, conversions |
| `ariastreamlab/processing` | `Processor` contract, plugin discovery, per-processor workers (queues, joins, stats), the graph, built-ins |
| `ariastreamlab/storage` | session layout, manifest, chunked Parquet/image/video writers, reader, timeline index, `inspect`/repair |
| `ariastreamlab/monitoring` | per-stream rates and drop accounting, the health log, the quality summary |
| `ariastreamlab/dashboard` | `DashboardState` (the bus collector), the monitor page, `Designer` and the designer page, the Rerun sink |
| `ariastreamlab/cli` | `ariastreamlab doctor \| devices \| streams \| presets \| processors \| schema \| inspect-config \| design \| run \| replay \| inspect` |
| `scripts/probe_sdk.py` | raw-SDK hardware probe that answered the design's assumptions |

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
