# AriaStreamLab

An experiment-oriented Python framework for **Meta Project Aria Gen 2** glasses, built for
university students and researchers: one JSON file describes the experiment, one command runs
it, and the raw VRS recording stays the ground truth while processors produce derived data.

> **Status: Phase 4 (pre-alpha).** Configuration, core data model, timing utilities, event
> bus, synthetic (mock) source, crash-safe session storage, session/VRS replay, the
> processing graph with plugin processors, the live Aria Gen 2 adapter and the CLI exist;
> everything but the live path is tested without hardware, the live path against a fake SDK
> and with a hardware smoke test. The dashboard follows in Phase 5 — see
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
ariastreamlab run examples/mock_experiment.json --mock --duration 10
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
| `ariastreamlab/cli` | `ariastreamlab doctor \| devices \| streams \| presets \| processors \| schema \| inspect-config \| run \| replay \| inspect` |
| `scripts/probe_sdk.py` | raw-SDK hardware probe that answers the assumptions in the architecture doc |
| `docs/` | architecture, `tutorials/live_session.md`, `tutorials/custom_processors.md` |
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
