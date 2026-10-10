# AriaStreamLab

An experiment-oriented Python framework for **Meta Project Aria Gen 2** glasses, built for
university students and researchers: one JSON file describes the experiment, one command runs
it, and the raw VRS recording stays the ground truth while processors produce derived data.

> **Status: Phase 1 (pre-alpha).** Configuration, core data model, timing utilities, event
> bus, synthetic (mock) source and CLI exist and are tested without hardware. Storage, replay,
> the processing graph, the Aria Gen 2 adapter and the dashboard follow in the next phases
> — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the plan.

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
```

## Layout

| Path | Purpose |
|---|---|
| `ariastreamlab/config` | Pydantic models, presets (`extends: "preset:rgb_imu"`), JSON Schema |
| `ariastreamlab/core` | `Sample` / `DerivedSample`, stream registry, event bus, errors |
| `ariastreamlab/timing` | host clock + UTC anchor, exact ns conversions, temporal lookup/join |
| `ariastreamlab/acquisition` | `Source` interface, mock source (Aria Gen 2 adapter and replay: next phases) |
| `ariastreamlab/monitoring` | per-stream rates and drop accounting |
| `ariastreamlab/cli` | `ariastreamlab doctor | devices | streams | inspect-config | run | replay | inspect` |
| `scripts/probe_sdk.py` | raw-SDK hardware probe that answers the assumptions in the architecture doc |
| `docs/` | architecture, tutorials |

## Development

```bash
ruff check . && ruff format --check .
mypy
lint-imports            # only acquisition/aria_gen2 may import Meta's SDK
pytest                  # hardware tests are marked and skipped by default
```

## Licence

To be decided (MIT or Apache-2.0). Meta's Project Aria Client SDK is a separate, proprietary
dependency that AriaStreamLab calls but does not redistribute.
