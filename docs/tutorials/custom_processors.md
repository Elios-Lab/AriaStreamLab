# Writing your own processor

A *processor* turns samples of one or more streams into new, derived streams — a filter on the
IMU, a crop of the RGB image, a saccade detector, a feature extractor. You write one small
Python class, point the experiment JSON at it, and AriaStreamLab takes care of threads,
queues, timestamps, storage, replay, provenance and the dashboard. The same class runs on the
live stream and on a recording, so an algorithm can be developed and validated offline on
data captured once and then deployed live unchanged.

## 1. The smallest possible processor

```python
# processors/accel_norm.py  (a directory called processors/ next to your experiment.json)
from ariastreamlab.processing import Processor, Inputs, Emit
from ariastreamlab.core.payloads import ScalarPayload


class AccelNorm(Processor):
    version = "1"  # bump it when you change the algorithm

    def process(self, inputs: Inputs):
        imu = inputs.primary.data  # an ImuPayload
        a = imu.accel_msec2
        return Emit(
            "accel_norm", ScalarPayload((a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5, "m/s^2")
        )
```

```json
{
  "extends": "preset:base",
  "experiment": { "name": "norm_demo", "participant": "P001" },
  "streams": { "imu": {} },
  "processors": [
    { "id": "norm", "type": "accel_norm.py", "inputs": ["imu_left"], "outputs": ["accel_norm"] }
  ]
}
```

```bash
ariastreamlab inspect-config experiment.json     # resolves the class, checks parameters
ariastreamlab run experiment.json --mock -d 10   # no glasses needed
ariastreamlab inspect sessions/<folder>          # accel_norm/ is under derived/
```

That is all. `derived/accel_norm/part-00000.parquet` now holds one row per IMU sample with the
device timestamp of the sample it came from, and `_meta.json` records that `AccelNorm`
version `1` from `accel_norm.py` produced it, with which parameters.

## 2. What `process()` receives and may return

`inputs` is a read-only mapping `stream_id -> sample`:

| | |
|---|---|
| `inputs.primary` | the sample that triggered the call (first stream in `inputs` of the JSON) |
| `inputs["eye_gaze"]` | a secondary input, matched by the join policy (see §4) |
| `inputs.dt_ns["eye_gaze"]` | `secondary.timestamp - primary.timestamp`, nanoseconds |
| `sample.data` | the payload: a NumPy image, `ImuPayload`, `GazePayload`, `PosePayload`, … (`ariastreamlab.core.payloads`) |
| `sample.device_timestamp_ns` | the glasses' clock, integer nanoseconds — never rewritten |
| `sample.metadata` | extras such as exposure or the camera label |

Return nothing, one output or a list of outputs. An output is an `Emit(stream_id, data,
metadata=None, timestamp_ns=None)` or a plain `("stream_id", data)` tuple. `stream_id` must be
one of the processor's declared `outputs`; anything else is counted as an error and dropped.
`timestamp_ns` defaults to the primary input's device timestamp, which is what you want for
"a value computed from this sample"; set it explicitly for things like "the blink that just
*ended*".

Payloads are plain Python/NumPy objects. Images are `uint8` arrays, `HxWx3` (RGB order) or
`HxW`; tabular results work best as the payload dataclasses or a small dict of numbers, which
land in Parquet columns.

## 3. Parameters, set-up and calibration

```python
from pydantic import BaseModel, Field
from ariastreamlab.processing import Processor, Inputs, Emit, OutputSpec, ProcessorContext


class Threshold(Processor):
    version = "2"
    description = "1 while the value is above a threshold"  # shown by `ariastreamlab processors`
    outputs = {"*": OutputSpec(payload="scalar", units={"value": "bool"})}

    class Parameters(BaseModel):  # validated at start-up
        threshold: float = Field(default=0.5, gt=0)
        invert: bool = False

    def setup(self, ctx: ProcessorContext) -> None:
        super().setup(ctx)
        self.above = False  # state lives on the instance: one thread, no locks
        ctx.log.info("threshold.ready", threshold=self.params.threshold)

    def process(self, inputs: Inputs):
        above = inputs.primary.data.value > self.params.threshold
        if above != self.above:
            self.above = above
            return Emit(self.ctx.outputs[0], float(above != self.params.invert))
        return None

    def on_calibration(self, calibration: dict) -> None:  # optional
        super().on_calibration(calibration)
        cam = calibration["cameras"]["camera-rgb"]  # intrinsics, extrinsics, image size

    def shutdown(self) -> None:  # optional
        self.ctx.log.info("threshold.done")
```

* With a nested `Parameters` model, a typo in the JSON (`"treshold"`) is reported before the
  run starts, and `self.params` is a typed object. Without it, `self.params` is the raw dict.
* `self.ctx` also gives you `inputs`/`outputs` (the declared stream ids), `session_dir`
  (where you may write extra files), `clock.now_ns()`, `registry` and `log`.
* `on_calibration()` is called once the device calibration is known (on a live run, a few
  seconds after start; on a VRS replay, before the first sample). `ProjectToRgb`
  (`gaze.project_to_rgb`) shows how to use it.
* Parameters can be changed while the experiment runs — `experiment.set_parameters("thr",
  threshold=0.7)` from Python or the *parameters* dialog of the dashboard. The new values are
  validated against `Parameters`, `self.params` is swapped, and a `config_change` event with
  the device time of the change is written next to the markers; the frozen `experiment.json`
  keeps the start-up values. If `setup()` derived state from the parameters (a filter
  coefficient, a release threshold), override `on_parameters_changed(old, new)` to refresh
  it; it runs on the caller's thread, so keep it to simple assignments.

## 4. Several inputs

```json
{ "id": "gaze_on_rgb", "type": "gaze.project_to_rgb",
  "inputs": ["rgb", "eye_gaze"], "outputs": ["gaze_rgb"],
  "execution": { "join": "nearest", "tolerance_ms": 40 } }
```

The first input is the *primary*: every sample of it triggers one `process()` call. The other
inputs are buffered and matched to the primary by `execution.join`:

| join | picks | when it fires |
|---|---|---|
| `latest` (default) | the most recent secondary sample, whatever its timestamp | immediately |
| `previous` | the last secondary sample at or before the primary's timestamp, within `tolerance_ms` | once the secondary stream has reached the primary's timestamp (bounded wait) |
| `nearest` | the secondary sample closest in time, within `tolerance_ms` | same |

If a secondary has no acceptable sample the call is skipped and counted as a *join miss*
(visible in `pipeline` stats). The bounded wait is `tolerance_ms + 20 ms` of host time, so
a 30 Hz gaze stream joined to a 10 fps frame costs at most that much latency.

## 5. Queues and drop policies

Each processor runs on its own thread with a bounded queue in front of it. When it cannot keep
up, `execution.drop_policy` decides what happens — never the acquisition, which is not
allowed to block on a slow processor:

| policy | behaviour | typical use |
|---|---|---|
| `drop_oldest` (default) | newest samples win | signals (IMU, gaze) |
| `latest` | queue of 1, always the freshest sample | images, anything for display |
| `drop_newest` | keep what is queued, ignore new arrivals | batch-like work |

`queue_size` defaults to half a second of the primary input (400 for an 800 Hz IMU, 64 for a
camera; at least 64, at most 2000) — enough to ride out the interpreter pauses a laptop
produces under load; set it explicitly for images (`1`–`2` with `latest`). The value used is
recorded as `execution.effective_queue_size` in the manifest. Drops are counted per processor
and per input (`ariastreamlab run` prints them, the dashboard's Pipeline card shows them, the
manifest keeps `pipeline.<id>.queue_dropped` and the quality summary lists them per stream). **At maximum-speed replay (`--speed 0`) and
with the non-realtime mock, the policies are replaced by back-pressure**: nobody is waiting in
real time, so the source simply waits for the processors and every sample is processed. This
is what makes `replay` the tool for re-processing a recording with new parameters.

## 6. Chaining, saving, displaying

Outputs are streams like any other: another processor may list `"accel_norm"` in its `inputs`
(the configuration is validated as a DAG — no cycles, no duplicate outputs, no unknown
inputs). `save_output: false` keeps a stream in memory only (for the dashboard or a
downstream processor). Every saved stream gets `derived/<stream>/` with Parquet chunks, or
PNG/JPEG/MP4 for images, plus `_meta.json` describing the producer.

## 7. Packaging a processor for others

Publish a package with an entry point and the type name becomes available everywhere:

```toml
[project.entry-points."ariastreamlab.processors"]
"mylab.saccade_detector" = "mylab.processors:SaccadeDetector"
```

Any importable class also works directly: `"type": "mylab.processors:SaccadeDetector"`.
`ariastreamlab processors` lists built-ins, entry points and (with `--config`) the files next
to your experiment.

## 8. The rule that keeps replay honest

A processor must not read `sample.provenance.origin`. Live, synthetic and replayed samples
are identical in every other field, and `tests/invariants/test_live_vs_replay_processing.py`
records a session, replays it and checks that every derived value and timestamp is the same.
If your processor needs randomness, seed it from `ctx.parameters`, not from the clock; if it
needs "now", use the primary sample's device timestamp, not the host clock — the host clock
is the one thing that differs between a live run and a replay.
