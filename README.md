# AriaStreamLab

An experiment-oriented Python framework for **Meta Project Aria Gen 2** glasses, built for
university students and researchers: one JSON file describes the experiment, one command runs
it, and the raw VRS recording stays the ground truth while processors produce derived data.

> **Status: Phase 5 (pre-alpha).** Configuration, core data model, timing utilities, event
> bus, synthetic (mock) source, crash-safe session storage, session/VRS replay, the
> processing graph with plugin processors, the live Aria Gen 2 adapter, the browser dashboard
> and the CLI exist; everything but the live path is tested without hardware, the live path
> against a fake SDK and with a hardware smoke test that passes on a MacBook with the glasses
> (live session over the hotspot, control hand-over to Wi-Fi, VRS, replay of the session).
> Phase 6 (annotations, quality summary, polish) follows — see
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

## With the glasses

```bash
pip install -e ".[dev,aria]"            # adds Meta's client SDK + projectaria_tools
aria_gen2 auth pair                     # once, over USB; accept in the Companion App
ariastreamlab doctor --device           # SDK, CLI, pairing, streaming port, disk
ariastreamlab devices                   # what the laptop can reach
ariastreamlab run examples/rgb_imu.json --duration 120
```

`run` connects over the USB link, starts the glasses' hotspot (asks you to join it), streams
`profile9` to the laptop, records the host-received VRS under `raw/`, runs your processors live,
and moves control to Wi-Fi as soon as data flows so the cable can be unplugged. Everything it
does is a `device_state` event in the session. See
[docs/tutorials/live_session.md](docs/tutorials/live_session.md) for the sequence, the health
signals and the troubleshooting table.

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

## Layout

| Path | Purpose |
|---|---|
| `ariastreamlab/config` | Pydantic models, presets (`extends: "preset:rgb_imu"`), JSON Schema |
| `ariastreamlab/core` | `Sample` / `DerivedSample`, stream registry, event bus, errors |
| `ariastreamlab/timing` | host clock + UTC anchor, exact ns conversions, temporal lookup/join |
| `ariastreamlab/acquisition` | `Source` interface, mock source, replay engine (`session`, `tools`, `sdk` backends), and `aria_gen2/` — the only package that imports Meta's SDK: live adapter, device manager, CLI wrapper, quality parser, conversions |
| `ariastreamlab/processing` | `Processor` contract, plugin discovery, per-processor workers (queues, joins, stats), the graph, built-ins |
| `ariastreamlab/storage` | session layout, manifest, chunked Parquet/image/video writers, reader, timeline index, `inspect`/repair |
| `ariastreamlab/monitoring` | per-stream rates and drop accounting |
| `ariastreamlab/dashboard` | `DashboardState` (the bus collector, usable without NiceGUI) and the NiceGUI page |
| `ariastreamlab/cli` | `ariastreamlab doctor \| devices \| streams \| presets \| processors \| schema \| inspect-config \| run \| replay \| inspect` |
| `scripts/probe_sdk.py` | raw-SDK hardware probe that answers the assumptions in the architecture doc |
| `docs/` | architecture, `tutorials/live_session.md`, `tutorials/custom_processors.md`, `tutorials/dashboard.md` |
| `examples/` | experiment files, `custom_processor/` (a processor loaded from a file next to the JSON) |

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
