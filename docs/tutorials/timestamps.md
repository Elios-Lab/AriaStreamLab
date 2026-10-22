# Timestamps and synchronisation

Aria Gen 2 glasses carry a dozen sensors that run at different rates — the RGB camera at
10 frames per second, the IMUs at 800 Hz, eye gaze at 30 Hz, audio in 20 ms chunks — and
their samples reach the laptop over Wi-Fi in batches, not one by one and not in the order
they were captured. Nothing in an experiment can be analysed correctly without being clear
about *which* clock a number refers to and *what* "at the same time" means. This tutorial
explains the two time bases AriaStreamLab keeps, why it never rewrites a device timestamp,
how to line up streams that do not tick together, and where the dangers are.

## 1. Two clocks, two fields

Every sample has two integer nanosecond timestamps:

| Field | Clock | Meaning | Who sets it |
|---|---|---|---|
| `device_timestamp_ns` | the glasses' monotonic clock | when the sensor captured the sample | the device, read verbatim from the SDK / the VRS |
| `host_receive_timestamp_ns` | the laptop's `time.monotonic_ns()` | when the sample entered AriaStreamLab | the acquisition adapter |

The device clock is the scientific one: it is the same for every sensor on the glasses, it
is what the VRS recording stores, and it is what `events.parquet`, every derived sample and
every Parquet chunk are sorted by. It is *not* wall-clock time and it does not start at
zero: on real glasses it is nanoseconds since the device booted (a number like
`1.6 × 10¹⁴`), on the mock it starts at `10¹²`. Treat it as an opaque monotonic counter and
only ever subtract two of them.

The host clock exists to measure the transport: `host − device` is the latency of the link
(plus a constant offset between the two clocks that never needs to be known precisely). It
is also the only clock the laptop has before any data arrives, which matters for events
(§4).

Timestamps are integers on purpose. A float64 holds 53 bits of mantissa; at `10¹⁴` ns the
spacing between representable floats is 16 ns, and a few arithmetic steps turn that into
microseconds. Keep them as `int`, convert to seconds only for plotting, and when you must
build a timestamp from a `timedelta` do it exactly (`days`, `seconds`, `microseconds`), as the
SDK adapter does.

## 2. What "the same time" cannot mean

The streams are not aligned at the source. With RGB at 10 Hz and IMU at 800 Hz there are
eighty IMU samples between two frames, and the frame's exposure midpoint falls *between* two
of them; eye gaze at 30 Hz is computed on the device from the eye cameras with its own
latency; audio timestamps mark the first sample of each 20 ms chunk. So:

* two samples from different streams **never** share a timestamp exactly;
* arrival order on the laptop says nothing about capture order (Wi-Fi batches 800 ms of data
  at a time and streams are batched independently);
* the device's own counters say when a sample was captured; the laptop can only say when it
  heard about it.

AriaStreamLab therefore never assumes order (late samples are inserted in place and counted
as `out_of_order`), never fills gaps, and never replaces a device timestamp with a host one —
what you read in a session is what the sensor said.

## 3. Lining streams up

Joining is a *query*: pick the primary stream (the one that decides when something happens,
typically the slower one — the RGB frame) and ask, for each frame, what the other streams
were doing then. `ariastreamlab.timing.sync` has the tools, and the processing graph uses the
same ones for multi-input processors.

```python
from ariastreamlab.timing.sync import TimeSeriesBuffer, temporal_join

gaze = TimeSeriesBuffer(capacity=4096)  # sorted by device_timestamp_ns, late arrivals ok
for s in gaze_samples:
    gaze.push(s)

m = gaze.nearest(frame.device_timestamp_ns, max_dt_ns=40_000_000)  # within ±40 ms
if m is not None:
    print(m.item.data.yaw_rad, m.dt_ns)  # dt_ns = gaze.ts − frame.ts (signed)

m = gaze.previous(frame.device_timestamp_ns)  # latest gaze not after the frame (causal)
window = gaze.window(t0_ns, t1_ns)  # every gaze sample in [t0, t1]
```

The three join modes, which are also the `execution.join` choices of a processor:

| mode | returns | use it when |
|---|---|---|
| `nearest` | the secondary sample closest in time, within `tolerance_ms` | offline-quality alignment (gaze on a frame) |
| `previous` | the latest secondary sample *not after* the primary | causal processing, "what was known at that moment" |
| `latest` | whatever arrived last, no time check | dashboards and anything where a few tens of ms do not matter |

`nearest` and `previous` depend only on the data, never on which thread delivered first:
the processor worker holds a frame until every secondary stream has reached the frame's
timestamp (or a bounded wait expires), which is why a live run and a replay of the same
recording give the same joins. `latest` is deliberately not reproducible across live and
replay — use it only for display.

For values between two samples, `interpolate()` takes a function of the bracketing pair:
linear for IMU and pose translations, spherical for rotations, never for images or gaze
validity flags. Interpolation is appropriate for signals that are continuous *and* sampled
much faster than they change; an 800 Hz IMU qualifies, a 10 Hz pose during a head turn does
not.

Inside a processor the join is already done: `inputs.primary` is the frame, `inputs["eye_gaze"]`
the matched gaze sample, and `inputs.dt_ns["eye_gaze"]` the signed offset so the code can
decide whether 35 ms is acceptable for its purpose.

## 4. Events and annotations

Markers (`experiment.mark("trial_start", {...})`, the dashboard's buttons, `begin()`/`end()`
intervals, device state changes) are created on the laptop, so they are born with a host
timestamp. To put them on the device time axis AriaStreamLab keeps a running estimate of
`host − device` from the samples it has seen — the smallest difference observed, i.e. the
sample that travelled fastest — and stores every event with:

| `timestamp_source` | meaning |
|---|---|
| `device` | exact: you passed `at_ns=` (a sample's own device timestamp) |
| `estimated` | the host time was mapped through the current offset estimate; accurate to the minimum transport latency (a few ms over USB, tens of ms over Wi-Fi) |
| `host_only` | created before any sample arrived (e.g. `connect`); re-estimated when the session closes if an offset became known |

So a marker pressed by hand is as precise as the human finger plus the link, no better; an
annotation produced *from* the data (a processor detecting a blink, a replay script finding
a frame) should pass `at_ns=sample.device_timestamp_ns` and is then exact. For stimulus
timing, the honest practice is to mark the stimulus on the device axis from something the
glasses saw (a flash in the RGB frames, a sound in the audio) rather than from the laptop's
clock.

UTC is anchored once per session (`manifest.clock_anchor`: the host monotonic and UTC
readings at `Session.open()`) and re-sampled every 60 s into `events.parquet` as
`clock_anchor` events that also carry the current device-offset estimate;
`manifest.started_utc` is the only wall-clock time you should quote. To convert a device
timestamp to UTC, map it to host time through the offset (the estimator's job) and then
through the anchor; the result is good to the link latency, which is why sessions are
analysed in device time and UTC is kept for the lab notebook.

## 5. Replay

A replay reads device timestamps verbatim from the VRS (or the session's Parquet) and feeds
them through the same bus, processors and dashboard. The one thing that legitimately differs
is `host_receive_timestamp_ns`: it is the replay machine's clock, so latency numbers in a
replay are meaningless (the manifest's `origin: replay` is the reminder). Pacing (`--speed 1`, `2`, `0.5`)
reproduces the device-time spacing against the host clock; `--speed 0` ignores the host
clock entirely and processes every sample with back-pressure instead of drop policies, which
is the mode to use for anything you want to be exactly reproducible.

## 6. Rates you will actually see

Nominal rates come from the registry and the profile; measured ones go into
`manifest.streams[*].effective_rate_hz` and the quality summary. On the hardware runs behind
this framework, `profile9` over the hotspot delivered RGB at **5 Hz** (not the 10 the profile
advertises), eye gaze and hand tracking at **25 Hz** (not 30) and everything else at nominal
— `inspect-config` warns about this at configuration time, and the summary flags any stream
whose effective rate falls below 80 % of nominal. An effective rate lower than expected
with zero device drops and zero transport drops is the link (move closer, 5 GHz, or tether);
with transport drops it is Wi-Fi loss; with device drops it is the glasses throttling
(thermal, see the health cards).

## 7. Checklist for analysis code

1. Subtract device timestamps; never compare a device timestamp with a host one.
2. Keep nanoseconds as integers until the last moment.
3. Choose a primary stream and join the others to it with an explicit tolerance; record the
   `dt` you accepted.
4. Do not interpolate images, flags or anything slower than what it measures.
5. Check `timestamp_source` of the events you rely on; derive stimulus times from the data
   where you can.
6. Replay with `--speed 0` when you need bit-for-bit reproducibility of derived outputs.
