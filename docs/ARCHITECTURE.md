# AriaStreamLab — Architecture Proposal

*Status: design approved on 2026‑10‑10 (dashboard = NiceGUI, raw ground truth = host‑received VRS); Phase 0 and Phase 1 implemented — see §14. Prepared against `projectaria_client_sdk` 2.5.1 and `projectaria_tools` 2.3.0 (the current PyPI releases, installed and introspected — not taken from memory). Items marked **[A‑n]** are assumptions that depend on hardware behaviour or undocumented SDK behaviour and must be verified on the glasses before Phase 4 is considered done.*

---

## 0. What the current SDK actually offers (verified)

Everything below was read from the installed wheel (`aria/sdk_gen2.pyi`, the runtime module, the bundled `gen2_samples/`, `aria/stream_receiver.py`, the native `aria_gen2` CLI `--help`) and from the official Gen 2 docs. This section is the ground truth the rest of the design is built on.

### 0.1 Versions and platforms

| Item | Fact |
|---|---|
| Client SDK | `projectaria_client_sdk` 2.5.1 (Oct 2026). Wheels for **cp310, cp311, cp312** only (macOS arm64, manylinux x86_64/aarch64). No 3.13. |
| Pulled in automatically | `projectaria_tools ~=2.3.0`, `projectaria_login ~=0.2`, `rerun-sdk ~=0.33.0`, `numpy`, `termcolor` |
| Supported hosts | macOS 14+ Apple Silicon; Ubuntu 22.04/24.04 x64; ARM64 Linux experimental. Intel Mac not supported. Windows planned. |
| BLE | **Experimental; macOS only** (Ubuntu unsupported due to bluez version). CLI only. |
| Bundled tools | `aria_gen2` native CLI, `aria_doctor` (firewall/ports/python checks), `aria_streaming_viewer` (Rerun‑based) |

### 0.2 Control plane (`aria.sdk_gen2`)

| Capability | Python API | Native CLI (`aria_gen2`) |
|---|---|---|
| Pair / authenticate | `DeviceClient.authenticate()` (USB) | `auth pair`, `auth pair --ble` (installs BLE creds over an existing USB auth) |
| Connect | `DeviceClient.connect(DeviceTarget(ip="", serial=""))` → `Device` — USB (adb‑style) **or Wi‑Fi IP** | global `--device-ip`, `--serial`, `--ble` |
| Connection events | `DeviceClient.set_observer(obj)`, `ConnectionState.{CONNECTED,DISCONNECTED}` (present at runtime, **undocumented** in the stub) **[A‑6]** | — |
| Status | `Device.status()` → `DeviceStatus`: `battery_level, charger_connected, charging, wifi_enabled, wifi_configured, wifi_connected, wifi_ip_address, wifi_ssid, thermal_mitigation_triggered, skin_temp_celsius, default_recording_profile, is_recording_allowed, device_mode, …` | `device status` (also works over `--ble`) |
| Wi‑Fi STA join | **not in Python** | `device wifi scan/connect/forget/keep-on` |
| **Hotspot (wifi_sap)** | **not in Python** | `device hotspot start [--wifi-band BAND_5GHZ --channel-bandwidth BW_80MHZ]`, `hotspot status`, `hotspot stop` |
| Profiles | `Device.device_profiles()` → `{name: DataFlowProfileList}` | `device profile list/add/remove/pull`, `streaming profiles` |
| Streaming config | `HttpStreamingConfig`: `profile_name` **or** `profile_json` (path), `streaming_interface ∈ {USB_NCM, USB_RNDIS, WIFI_STA, WIFI_SAP, WIFI_ANY, WIFI_PROXY}`, `batch_period_ms`, `streaming_cert_name`, `keep_streaming_on_disconnection`, `time_domain_mapping_config` | `streaming start --interface {usb,wifi_sta,wifi_sap} --batch-period-ms N --profile/--json-profile` |
| Start / stop | `Device.start_streaming(record: bool=False)`, `stop_streaming()`, `is_streaming()`, `get_streaming_info()` | `streaming start/stop` |
| On‑device recording | `RecordingConfig` + `start_recording() -> uuid`, `stop_recording()`, `list/download/delete_recording` | `recording …` |
| Data quality | `Device.get_data_quality_stats() -> str` (only during an active session; **format undocumented**, probably JSON) **[A‑5]** | `device data-quality-stats` |
| NTP | `set_ntp_sync(bool)` | — |

### 0.3 Data plane (how samples reach the laptop)

* HTTP streaming is **device‑push**: the laptop runs `AriaGen2HttpServer(HttpServerConfig, handler_factory, time_sync_ref)` (default `0.0.0.0:6768`, mDNS name `oatmeal_server`, mutual TLS); the glasses connect to `https://oatmeal_server.local:6768`. Required host ports: TCP/UDP 6768–7432, UDP 5353–5354 (mDNS).
* The handler is a `StreamDataInterface(enable_image_decoding)` created **per device connection** by a factory. Callbacks are registered on it; the SDK invokes them **on SDK receive threads**, one bounded native queue per stream (`set_rgb_queue_size` … defaults: RGB 100, SLAM 1000, ET 100, IMU 800, IMU‑batch 10, VIO 10, VIO‑HF 800, gaze 10, hand 10). Slow callbacks ⇒ SDK‑side drops.
* `StreamDataInterface.record_to_vrs(path)` writes the **received** stream to a VRS file on the host. The file reflects transport drops.
* Certificates: `start_streaming()` regenerates streaming certs unless `streaming_cert_name` is set (run `aria_gen2 streaming install-certs` once).
* Time sync: `TimeSyncRef` (TicSync) gives `remote_timestamp_ns_from_local(local_ns)` and `compute_latency_ms(capture_ts_ns)`.
* **HTTP streaming and on‑device recording are mutually exclusive.** Only WebRTC supports `start_streaming(record=True)`; WebRTC image timestamps are host‑derived (not true capture time), and it has no batching. ⇒ v1 uses HTTP only.
* Wireless: `batch_period_ms` must be > 0 (docs: 200 ms for `wifi_sta`, 800 ms for `wifi_sap`); it is the worst‑case added latency. Hotspot: single client, ≈500–800 ms latency, **≤ 20 min sessions recommended** (thermal throttle at 42 °C).
* Replay: `create_replay_source(ReplaySourceConfig(filename, speed_multiplier, start_time_sec, end_time_sec), stream_data) -> ReplaySession{is_finished, stop}` drives the *same* callbacks from a VRS file (0.0 = as fast as possible). No pause/seek while running. Malformed VRS **aborts the interpreter** (libvrs `XR_CHECK`).
* Offline reading: `projectaria_tools.core.data_provider.create_vrs_data_provider(path)` → random access by time/index, `deliver_queued_sensor_data()` ordered iteration, calibration, nominal rates.

### 0.4 Typed callbacks and their payloads (`projectaria_tools` types)

| `register_*_callback` | Payload | Device timestamp field | Notes |
|---|---|---|---|
| `rgb`, `slam`, `et` | `(ImageData, ImageDataRecord)` | `record.capture_timestamp_ns` (+ `arrival_timestamp_ns`, `frame_number`, `camera_id`, `exposure_duration`, `gain`) | H.265 decoded in Python (`aria.image_decoder`, HW‑accel on macOS) when `enable_image_decoding=True`. `camera_id` bitmask: 1/2 slam front L/R, 4/8 slam side L/R, 16/32 et L/R, 64 rgb |
| `imu`, `imu_batch`, `magnetometer` | `(MotionData, label)` / `(List[MotionData], label)` | `capture_timestamp_ns` | labels `imu-left`, `imu-right`, `mag0` |
| `audio` | `(AudioData, AudioDataRecord, num_channels)` | `record.capture_timestamps_ns` (per sample) | 16 kHz, 7+1 mics, chunked |
| `barometer` | `BarometerData` | `capture_timestamp_ns` | |
| `gps`, `phone_location` | `GpsData` | `capture_timestamp_ns`, `utc_time_ms` | |
| `ppg` | `PpgData` | `capture_timestamp_ns` | 128 Hz |
| `eye_gaze` | `EyeGaze` | `tracking_timestamp` (**timedelta**, device clock) | yaw/pitch/depth, CPF frame |
| `hand_pose` | `HandTrackingResult` | `tracking_timestamp` (timedelta) | left/right hand landmarks |
| `vio` | `FrontendOutput` | `capture_timestamp_ns`, `unix_timestamp_ns` | pose, velocities, online calib |
| `vio_high_frequency` | `OpenLoopTrajectoryPose` | `tracking_timestamp` (timedelta) | 800 Hz |
| `device_calib` | `DeviceCalibration` | — | once per connection |
| `wifi_beacon`, `bluetooth_beacon`, `neural_band_batch` | respective types | | |
| `raw_message` | `(SharedMessage, offset)` | `message.timestamp()` | every flatbuffer, incl. `ALS_EVENT`, `TEMP_EVENT`, `BATTERY_STATUS_DATA` which have **no typed callback** |
| `time_domain_mapping` | `(capture_ns, broadcaster_ns, broadcaster_id)` | | multi‑device |

### 0.5 Streams exposed by Aria Gen 2 (from VRS label map + profiles)

| AriaStreamLab id | VRS label | Nominal rate | Live typed callback | Profile key |
|---|---|---|---|---|
| `rgb` | camera-rgb (214‑1) | 5/10/30 Hz, 2016×1512 or 2560×1920, H.265 | rgb | `rgb_camera` |
| `slam_front_left/right`, `slam_side_left/right` | slam-front-left … (1201‑1..4) | 10/30 Hz, 512×512 | slam | `slam_cameras` |
| `et_left/right` | camera-et-left/right (211‑1/2) | 5 Hz, 200×200, IR | et | `et_cameras` |
| `imu_left/right` | imu-left/right (1202‑1/2) | 800 Hz | imu / imu_batch | `imus` |
| `magnetometer` | mag0 (1203‑1) | 100 Hz | magnetometer | `magnetometer` |
| `barometer` | baro0 (247‑1) | 50 Hz | barometer | `barometer` |
| `audio` | mic (231‑1) | 16 kHz, Opus | audio | `audio` |
| `gps`, `gps_app` | gps (281‑2), gps-app (281‑1) | 1 Hz | gps / phone_location | `gps` |
| `wifi_beacons`, `ble_beacons` | wps (282‑1), bluetooth (283‑1) | periodic scans | wifi_beacon / bluetooth_beacon | `wifi`, `ble` |
| `ppg` | ppg (248‑1) | 128 Hz | ppg | `ppg` |
| `als` | als (500‑1) | ≈9.4 Hz | **raw only** | `als` |
| `temperature` | temperature (246‑1) | 1 Hz | **raw only** | `temperature` |
| `eye_gaze` | eyegaze (373‑1) | 30 Hz | eye_gaze | `et` |
| `hand_tracking` | handtracking (371‑x) | 10/30 Hz | hand_pose | `ht` |
| `vio` | vio (371‑x) | 10 Hz | vio | `vio` |
| `vio_high_frequency` | vio_high_frequency (371‑x) | 800 Hz | vio_high_frequency | `vio_high_frequency_pose` |
| `device_calibration` | (file tag) | once | device_calib | — |

Pre‑defined profiles: `profile8` (recording, general), `profile9` (streaming, low bandwidth, RGB 5 Hz, no ET cams), `profile10` (recording, RGB 30 Hz 3 MP), `mp_streaming_demo` (streaming, RGB 10 Hz, VIO/gaze/hand). Custom profile JSON keys: `name, description, imus{rate_hz}, magnetometer, barometer, audio{sample_rate, frame_period, sample_format, encoding_format, opus_config}, gps, ble{period_ms,duration_ms}, wifi{period_ms,scan_mode}, slam_cameras{rate_hz, encoding, video_config.cqp.qp|cbr}, et_cameras{…, resolution, ir_led}, rgb_camera{rate_hz, resolution, width, height, encoding, video_config}, et{rate_hz}, ht{rate_hz}, vio{rate_hz}, device_info{period_ms}, ppg, temperature, als{rate_hz, exposure_time_us}, vio_high_frequency_pose{rate_hz}, tags`. Meta explicitly labels custom profiles "not recommended for general use".

### 0.6 Assumptions to verify on hardware

| Id | Assumption | Why it matters | How we verify |
|---|---|---|---|
| **A‑1** | `DeviceClient.connect(DeviceTarget(ip=<device‑ip>))` accepts `set_streaming_config` + `start_streaming()/stop_streaming()` over Wi‑Fi (the streaming doc says "all start/stop controls require USB", the CLI reference says `--device-ip` is a global option). | Decides whether the untethered flow can start/stop without the cable. | `scripts/probe_sdk.py --ip` on the Mac with the glasses. Fallback: tethered start → unplug (`keep_streaming_on_disconnection=True`) → re‑plug to stop; documented by Meta. |
| **A‑2** | `aria_gen2 device hotspot start/status` is executable over USB and prints SSID/password/IP (or the hotspot credentials come from the Companion App). Whether it works over `--ble` is unknown. | Needed for `prepare_hotspot()`. | Same probe; parse CLI output. If BLE hotspot control works, the laptop needs no cable at all after pairing. |
| **A‑3** | After the laptop joins the hotspot, the glasses' IP = default gateway, and the glasses can resolve `oatmeal_server.local` (mDNS) on that link. | Device‑push streaming needs the device to find the laptop. | Probe. Fallback: `HttpStreamerConfig.endpoint.url` / `--url https://<laptop-ip>:6768`. |
| **A‑4** | `record_to_vrs()` writes a VRS that `projectaria_tools` can open with decoded images (H.265 → via XPRS) and that `create_replay_source` accepts. | Raw ground truth + replay from raw. | Record 30 s, open with `create_vrs_data_provider`, replay with both backends. |
| **A‑5** | `get_data_quality_stats()` returns JSON with per‑sensor drop/sequential‑drop/timestamp‑error/quality‑score fields. | Device‑side drop accounting in dashboard and manifest. | Probe prints it; the parser is written defensively (unknown → stored verbatim). |
| **A‑6** | `DeviceClient.set_observer()` accepts a Python object with a connection‑state callback. | Reconnect handling. | Probe; fallback is polling `is_connected()` and `AriaGen2HttpServer.connections()` (documented). |
| **A‑7** | The custom profile derived from the experiment config is accepted via `HttpStreamingConfig.profile_json` and does not need to be `device profile add`‑ed first. | Config → profile translation. | Probe with a profile8‑derived JSON. Default behaviour (see §5) does not depend on this. |
| **A‑8** | `ImageData.to_numpy_array()` after Python decoding yields `uint8` HxWx3 (RGB) / HxW (mono) without an extra copy per frame being prohibitive at 30 Hz 3 MP. | Processing throughput. | Measure in probe; if slow, keep RGB at ≤10 Hz for live processors and use `latest` policy. |

### 0.7 Hardware probe findings (Ricky's Mac, SDK 2.5.0, glasses 1M0YDD5H980277, 2026‑10‑10)

| Id | Result | Evidence |
|---|---|---|
| USB control | **works without adb**: `DeviceClient.usb_devices()` raises "adb executable unavailable" on macOS, but `usb_network_devices()` returns the USB‑NCM link (`192.168.68.165`) and `connect()` succeeds in 0.4 s. The CLI logs "Using known device IP … for recording manager on port **23456**" ⇒ *all* control is IP‑based, USB just provides a network link. | PRE, A‑1a, CERTS |
| BLE control | **works on this Mac**: `aria_gen2 auth check` fell back to BLE, connected by scan and reported "authenticated" (19 s). | PRE |
| `Device.status()` | all fields as in the stub; device was on `HomeWifi` as STA with `wifi_ip_address=192.168.1.41`, mode `DeviceMode.Research`. | STATUS |
| Profiles | native: `profile8`, `profile9`, `profile10`, `mp_streaming_demo`; cloud: `low_latency_streaming`, `profile8_ml_et`, `low_latency_streaming_capture`. | PROFILES |
| **A‑5** | **answered**: `get_data_quality_stats()` returns JSON `{"data_type":"GetSensorHealthStats","data":{"sensor_health_stats_list":[{sensor_type, expected_frame_count, actual_frame_count, dropped_frame_count, early_time_error_count, late_time_error_count, total_time_error_count, score, sequential_drops}]}}` with sensor types `AUDIO, CAMERA_POV, CAMERA_SLAM_1..4, IMU_1, IMU_2, MAG, BARO, PPG, ET_RESULT, HT_RESULT, VIO_RESULT, ALS, VIO_HIGH_FREQUENCY_POSE, UTC_TIME_SYNC, TEMPERATURE_ACROPOLIS, TEMPERATURE_SOC, PROXIMITY`. It answers even when not streaming (stats of the last/ongoing session). | A‑5 |
| **A‑6** | `set_observer(obj)` accepts a plain Python object; no callback fired during the run (no disconnect happened), so the method name it expects is still unknown → polling `is_connected()` / `connections()` remains the plan. | A‑6, OBSERVER |
| A‑1b (control over Wi‑Fi STA) | inconclusive: `DeviceTarget(ip=192.168.1.41)` failed with "Operation canceled"; the Mac was probably not on `HomeWifi`. The probe now checks TCP reachability of `ip:23456` first. | A‑1b |
| A‑1c/A‑2/A‑4/A‑8 | blocked by two probe bugs (ANSI codes in the parsed cert name ⇒ "(9) Failed to find specified certificates"; kwarg clash in the hotspot step), both fixed. **Re‑run needed.** | |

---

## 1. Architecture

### 1.1 Layers and the core invariant

```
            ┌──────────────────────────── control plane ────────────────────────────┐
            │  DeviceManager (state machine) ── AriaGen2Adapter ── aria.sdk_gen2 /  │
            │                                                      aria_gen2 CLI    │
            └────────────────────────────────────────────────────────────────────────┘
 LIVE                                              REPLAY
 Aria Gen 2 ──wifi/usb──▶ AriaGen2Source           raw/aria_stream.vrs ──▶ VrsReplaySource
 (device push, SDK        (SDK callbacks:                                   (projectaria_tools
  receive threads)         wrap+timestamp+enqueue)                           data provider, paced)
                                 │                                                │
                                 │  Sample(device_id, stream_id, device_ts_ns, …)  │
                                 ▼                                                ▼
                        ┌──────────────────────── EventBus (topics, bounded queues) ───┐
                        │ raw.<stream>   derived.<stream>   events.*   health.*        │
                        └──────────────────────────────────────────────────────────────┘
       ┌──────────────────┬───────────────────┼────────────────────┬──────────────────┐
       ▼                  ▼                   ▼                    ▼                  ▼
 RawRecorder        ProcessingGraph      DerivedRecorder        Dashboard        Monitoring
 (VRS via SDK,      (processors, one     (Parquet / MP4 /      (NiceGUI, own     (rates, drops,
  live only;        worker thread each,   npy chunks + index)   thread, `latest`  queues, latency,
  replay: none)     drop policies)                               policy)          quality stats)
```

**Invariant I‑1 (source transparency).** Every consumer downstream of the EventBus receives `Sample` objects whose content is a function of the sensor data only. `Sample.provenance.origin ∈ {"live", "replay", "mock"}` is the *only* field that differs, and processors receive it as opaque metadata. Test: run the same processor graph on (a) a mock session and (b) a replay of the session recorded from (a); the derived Parquet files must be identical modulo `host_receive_timestamp_ns`, `processing_*_ns` and `origin`.

**Invariant I‑2 (raw is immutable).** Nothing in the process writes into `raw/` except the SDK's VRS writer during live acquisition. Derived recorders refuse paths under `raw/`.

**Invariant I‑3 (timestamps are never replaced).** `device_timestamp_ns` is copied from the SDK record. Host time is a *second* field. Derived samples carry `source_timestamps_ns`.

### 1.2 Why this shape (trade‑offs)

| Decision | Alternative considered | Why chosen |
|---|---|---|
| In‑process EventBus with bounded per‑subscriber queues and threads | asyncio everywhere; multiprocessing with shared memory; ZeroMQ | SDK callbacks are native threads, not coroutines; numpy/OpenCV/Torch release the GIL, so threads give real parallelism for the heavy parts; multiprocessing is kept possible (§7) but not default because image samples would need shared‑memory transport and students would lose simple debugging. |
| Host‑side VRS (`record_to_vrs`) as raw ground truth | On‑device recording in parallel | Not possible over HTTP (mutually exclusive); WebRTC `record=True` exists but its image timestamps are host‑derived. Host VRS records exactly what the pipeline saw, which is the correct ground truth for *reproducing derived data*. The manifest states `raw.kind = "host_received_vrs"` and stores device‑side drop stats so the gap vs. on‑device truth is quantified. A later `capture.mode = "record_on_device"` (record, then download, then replay) is reserved for fidelity‑first studies. |
| Replay via `projectaria_tools` data provider (primary) + SDK `create_replay_source` (secondary) | SDK replay only | Data‑provider replay is pure Python pacing → pause/seek/speed/max‑speed are trivial and errors are exceptions, not process aborts. SDK replay reproduces the exact live decode path and is kept as `--backend sdk` for validation. Both produce identical `Sample`s (invariant I‑1 test runs on both). |
| Python API for everything except hotspot/Wi‑Fi/BLE, which go through the `aria_gen2` CLI subprocess | Reimplement over adb/BLE | Those operations simply do not exist in the Python module today. Wrapping the bundled CLI (same wheel, same version) keeps us on official code; the wrapper lives entirely inside the adapter, so when Meta adds them to Python only `acquisition/aria_gen2/cli.py` changes. |
| Config → **pre‑defined profile by default**, custom profile JSON on explicit opt‑in | Always generate a custom profile | Meta marks custom profiles unsupported/unstable; students should get stable defaults. The translator picks the closest pre‑defined profile, warns about every stream parameter it cannot honour, and only emits a custom JSON when `device.profile.mode = "custom"`. |
| NiceGUI browser dashboard | Rerun (already installed), Dash, PyQtGraph, Streamlit | See §10. Rerun has no input widgets (markers) and no health panel; Dash is polling‑oriented; Qt packaging on student Macs is painful; Streamlit's rerun model is wrong for 10 Hz updates. NiceGUI = Python only, images+plots+buttons, FastAPI/uvicorn underneath, runs in its own thread, closing the tab affects nothing. Rerun remains an optional sink (`dashboard.rerun = true`) for 3‑D trajectories. |
| Parquet (pyarrow) for tabular derived data, MP4 (+ frame index Parquet) for image streams, `.npy` chunks for arrays | CSV; HDF5; Zarr | Parquet is columnar, typed, chunk‑appendable, pandas/polars native; MP4 keeps image streams small; npy chunks are zero‑dependency. Zarr is an optional extra for large tensors. |
| Pydantic v2 + exported JSON Schema; preset inheritance via `extends` | Hydra/OmegaConf; plain dict | One JSON file + one command for students; schema gives editor autocompletion and student‑readable errors; `extends` is 20 lines, Hydra is a framework. |

---

## 2. Package / module tree

```
ariastreamlab/
├── __init__.py                 # Experiment, __version__
├── api.py                      # Experiment facade (§4.8)
├── config/
│   ├── models.py               # Pydantic models (ExperimentConfig, StreamConfig, ProcessorConfig, …)
│   ├── loader.py               # load_json(), presets, `extends` resolution, defaults
│   ├── schema.py               # export_json_schema()
│   └── presets/                # rgb_imu.json, gaze_experiment.json, full_capture.json, mock_*.json
├── core/
│   ├── sample.py               # Sample, DerivedSample, Provenance (frozen dataclasses, __slots__)
│   ├── bus.py                  # EventBus, Topic, Subscription, DropPolicy
│   ├── registry.py             # StreamDescriptor registry (§3)
│   ├── session.py              # Session object: ids, paths, lifecycle, manifest
│   ├── state.py                # DeviceState enum + transitions
│   └── errors.py               # student‑facing exception hierarchy
├── timing/
│   ├── clock.py                # HostClock (monotonic_ns + UTC anchor), ClockAnchor
│   ├── convert.py              # timedelta→ns, ms→ns, TimeSyncRef wrapper
│   └── sync.py                 # TimeSeriesBuffer, nearest/prev/next/window/interp, temporal_join
├── acquisition/
│   ├── base.py                 # Source (abstract), SourceCapabilities, DeviceManager (abstract)
│   ├── mock.py                 # MockSource: synthetic rgb/imu/gaze/pose with realistic timing
│   ├── replay.py               # VrsReplaySource (data provider), SdkReplaySource, ReplayControl
│   └── aria_gen2/              # the ONLY package importing aria.sdk_gen2 / aria.* 
│       ├── adapter.py          # AriaGen2Source: StreamDataInterface callbacks → Sample
│       ├── device.py           # AriaGen2DeviceManager: DeviceClient/Device + state machine
│       ├── cli.py              # subprocess wrapper for aria_gen2 (hotspot, wifi, ble status, certs)
│       ├── profiles.py         # experiment streams → predefined/custom profile JSON
│       ├── quality.py          # get_data_quality_stats() parser
│       └── convert.py          # projectaria_tools payloads → plain numpy/dict payloads
├── processing/
│   ├── base.py                 # Processor, ProcessorContext, ProcessorStats
│   ├── graph.py                # ProcessingGraph: builds DAG from config, validates streams
│   ├── runner.py               # ProcessorWorker thread, input join policies, stats
│   ├── plugins.py              # discovery: built‑ins + entry points + `processors/` dir in config
│   └── builtin/
│       ├── image.py            # image.resize, image.grayscale, image.crop
│       ├── imu.py              # imu.lowpass, imu.motion_detector
│       └── gaze.py             # gaze.project_to_rgb (needs calibration)
├── storage/
│   ├── layout.py               # SessionLayout: paths, naming, atomic writes
│   ├── manifest.py             # Manifest model + writer (start/finalize)
│   ├── writers.py              # ParquetChunkWriter, VideoWriter(+index), NpyChunkWriter
│   ├── derived.py              # DerivedRecorder: bus subscriber → writer per stream
│   ├── raw.py                  # RawRecorder interface (live: delegates to adapter's record_to_vrs)
│   ├── timeline.py             # TimelineIndex builder/reader
│   └── inspect.py              # session summary, crash recovery (rebuild index from chunks)
├── events/
│   ├── markers.py              # Marker/Event model, EventLog (parquet)
│   └── config_changes.py       # runtime config change events
├── monitoring/
│   ├── stats.py                # per‑stream rate/drops, per‑processor latency percentiles
│   ├── health.py               # device/host health snapshot (battery, temp, wifi, disk, queues)
│   └── quality.py              # end‑of‑session quality summary
├── dashboard/
│   ├── app.py                  # NiceGUI app factory; runs in its own thread
│   ├── panels/                 # images, timeseries, gaze overlay, trajectory, health, markers
│   └── rerun_sink.py           # optional Rerun logging sink
├── cli/
│   ├── main.py                 # typer app: doctor, devices, inspect-config, run, replay, inspect
│   └── doctor.py               # extends aria_doctor checks with ASL‑specific ones
└── py.typed
examples/      mock_experiment.json, rgb_imu.json, gaze_experiment.json, rgb_processing.json, custom_processor/
docs/          ARCHITECTURE.md (this), tutorials/timestamps_and_sync.md, custom_processors.md
tests/         unit (no hardware), integration/ (marked `hardware`), invariants/ (I‑1..I‑3)
scripts/       probe_sdk.py (raw‑SDK hardware probe for A‑1…A‑8)
```

The dependency rule, enforced by an import‑linter contract in CI: `ariastreamlab.acquisition.aria_gen2` is the only package allowed to import `aria` or `projectaria_tools`. (`storage`, `replay` may import `projectaria_tools` through a thin `acquisition.aria_gen2.convert` facade for calibration/VRS reading.)

---

## 3. Stream registry

```python
@dataclass(frozen=True)
class StreamDescriptor:
    id: str  # "rgb", "imu_left", "eye_gaze", …
    category: Literal[
        "camera", "imu", "mag", "baro", "audio", "gps", "beacon", "bio", "env", "mp", "calib"
    ]
    native_label: str  # VRS label, e.g. "camera-rgb"
    native_stream_ids: tuple[str, ...]  # ("214-1",)
    sdk_callback: str | None  # "rgb" → register_rgb_callback; None ⇒ raw‑only
    profile_key: str | None  # custom‑profile section, e.g. "rgb_camera"
    payload: type  # ImagePayload | ImuPayload | GazePayload | …
    timestamp_domain: Literal["device"]  # all Aria streams; host domain added by us
    nominal_rate_hz: float | None
    parameters: Mapping[
        str, ParameterSpec
    ]  # e.g. {"fps": Choice(5,10,30), "resolution": Choice("2016x1512","2560x1920")}
    units: Mapping[str, str]  # {"accel": "m/s^2", "gyro": "rad/s"}
    default_save_raw: bool
```

The registry is a single module (`core/registry.py`) populated from §0.5. Validation of `streams.*` in the config is driven by `parameters`, so an unknown parameter or an unsupported value fails with: `streams.rgb.fps: 25 is not supported by Aria Gen 2 (allowed: 5, 10, 30)`.

Payloads delivered on the bus are **plain** (`numpy.ndarray`, floats, small dataclasses), never `projectaria_tools` objects, so processors and tests do not need the SDK installed. Converting at the adapter boundary costs one small copy per sample for IMU‑class data and zero copies for images (we keep the array the decoder produced).

---

## 4. Central interfaces

### 4.1 Samples (`core/sample.py`)

```python
@dataclass(frozen=True, slots=True)
class Provenance:
    origin: Literal["live", "replay", "mock"]
    source_id: str  # "aria_gen2", "vrs:raw/aria_stream.vrs", "mock"
    session_id: str


@dataclass(frozen=True, slots=True)
class Sample:
    device_id: str  # device serial (or "mock-0")
    stream_id: str  # registry id
    device_timestamp_ns: int  # from the SDK record, never rewritten
    host_receive_timestamp_ns: int  # time.monotonic_ns() at callback entry
    sequence_number: int | None  # frame_number where the SDK gives one, else our counter
    data: Any  # plain payload (ndarray / dataclass)
    metadata: Mapping[str, Any]  # exposure, gain, camera label, batch size, …
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class DerivedSample:
    device_id: str
    stream_id: str
    timestamp_ns: int  # chosen by the processor (default: primary input's device ts)
    source_streams: tuple[str, ...]
    source_timestamps_ns: tuple[int, ...]
    processor_id: str
    processing_start_ns: int  # host monotonic
    processing_end_ns: int
    data: Any
    metadata: Mapping[str, Any]
    provenance: Provenance
```

### 4.2 Event bus (`core/bus.py`)

```python
class DropPolicy(StrEnum): BLOCK = "block"; DROP_OLDEST = "drop_oldest"; DROP_NEWEST = "drop_newest"; LATEST = "latest"

class EventBus:
    def publish(self, topic: str, item: Sample | DerivedSample | Event) -> None   # non‑blocking, O(#subscribers)
    def subscribe(self, topic_pattern: str, *, maxsize: int, policy: DropPolicy, name: str) -> Subscription
    def topics(self) -> list[str]
    def stats(self) -> BusStats     # per subscription: enqueued, dropped, depth

class Subscription:
    def get(self, timeout: float | None) -> Sample | None
    def drain(self) -> list[Sample]
    def close(self) -> None
```

Topics: `raw.<stream_id>`, `derived.<stream_id>`, `events.marker`, `events.config`, `health.device`, `health.pipeline`. Each subscription owns a bounded `collections.deque` + condition; `LATEST` is a size‑1 deque with overwrite. Publishing never blocks the SDK thread (`BLOCK` is implemented as "publisher waits", so it is allowed only for recorders, never for the live source — the config validator enforces this).

### 4.3 Acquisition (`acquisition/base.py`)

```python
class Source(ABC):
    capabilities: SourceCapabilities          # streams it can provide, can_record_raw, is_paced
    def start(self, bus: EventBus, session: Session) -> None
    def stop(self) -> None
    def stats(self) -> SourceStats            # received per stream, sdk‑side drops if known

class ReplayControl(Protocol):                # implemented by replay sources only
    def pause(self) -> None; def resume(self) -> None
    def seek(self, device_timestamp_ns: int) -> None
    def set_speed(self, multiplier: float) -> None   # 0.0 = max speed

class DeviceManager(ABC):
    state: DeviceState
    def discover(self) -> list[DeviceInfo]
    def connect(self, target: DeviceInfo | None = None) -> None
    def disconnect(self) -> None
    def get_status(self) -> DeviceStatus
    def prepare_hotspot(self) -> HotspotInfo
    def configure_streaming(self, plan: StreamingPlan) -> None
    def start_streaming(self) -> None
    def stop_streaming(self) -> None
    def quality_stats(self) -> QualityStats | None
```

`DeviceState`: `DISCONNECTED → DISCOVERED → CONNECTED → NETWORK_CONFIGURED → STREAM_READY → STREAMING → STOPPING → (CONNECTED | DISCONNECTED)`, plus `ERROR` reachable from any state with `last_error` and `recoverable: bool`. Transitions are logged as `events.device_state` on the bus (so they land in `events.parquet` and the dashboard).

Untethered flow as implemented by `AriaGen2DeviceManager` (transport per step, given §0):

| Step | State after | Transport actually used |
|---|---|---|
| `discover()` → `DeviceClient.usb_devices()`, plus `aria_gen2 --ble-scan device list` if `preferred_control=ble` and macOS | DISCOVERED | USB / BLE |
| `connect()` → `DeviceClient.connect(DeviceTarget(serial))` | CONNECTED | USB (first time) |
| `prepare_hotspot()` → `aria_gen2 device hotspot start --wifi-band …` → parse SSID/pass/IP; helper tells the student (or, on macOS with `auto_join=true`, runs `networksetup -setairportnetwork`) → reconnect `connect(DeviceTarget(ip))` | NETWORK_CONFIGURED | USB or BLE [A‑2]; then Wi‑Fi |
| `configure_streaming()` → `install_streaming_certs` once, `set_streaming_config(HttpStreamingConfig(WIFI_SAP, batch_period_ms, cert name))`; start our `AriaGen2HttpServer` | STREAM_READY | Wi‑Fi [A‑1] |
| `start_streaming()` | STREAMING | Wi‑Fi [A‑1] — fallback: USB then unplug |
| `stop_streaming()` → device stop, wait for `connections()` to empty, stop server | STOPPING → CONNECTED | Wi‑Fi / USB |

The student never chooses a transport; `device.connection.preferred_control` is a *hint* (`"auto" | "usb" | "wifi" | "ble"`), and the manager records which transport each command used in `manifest.device.transport_log`.

### 4.4 Processor (`processing/base.py`)

```python
class Processor(ABC):
    version: str = "1"
    def setup(self, ctx: ProcessorContext) -> None: ...       # ctx: config params, calibration, logger, session paths, registry
    @abstractmethod
    def process(self, inputs: Inputs) -> Sequence[Output] | Output | None
    def shutdown(self) -> None: ...

# Inputs is a mapping stream_id -> Sample (single‑input processors get exactly one entry).
# Output is (stream_id, data, metadata) or a DerivedSample the runner completes.
```

Processors are declared in config by `type` (e.g. `"image.resize"`, `"mypkg.my_module:MyProcessor"`, or a file under `processors/` next to the JSON). Discovery order: built‑ins → `ariastreamlab.processors` entry points → `processors_path` in config. Each processor runs in its own `ProcessorWorker` thread with an input `Subscription` per input stream and the configured `drop_policy`. Multi‑input processors declare a `join` policy: `"latest"` (fire on primary input, attach most recent sample of the others) or `"nearest"` (fire when all inputs have a sample within `tolerance_ms`, using `timing.sync`). Stats per processor (`inputs, outputs, dropped, exec_time avg/p50/p95/p99, queue_depth, latency = end − primary device_ts (via clock mapping)`) are published on `health.pipeline` every second.

### 4.5 Recorders (`storage/derived.py`, `storage/raw.py`)

`DerivedRecorder` subscribes `derived.*` (and `raw.*` for streams with `save_raw` that are not VRS‑covered, i.e. mock mode) with `policy=BLOCK, maxsize=256`; a writer per stream is chosen by payload type: tabular → `ParquetChunkWriter` (row groups of N rows or T seconds, fsync on close, file rotated every `chunk_seconds`), images → `VideoWriter` (MP4/H.264 via OpenCV, plus `frames.parquet` index: `frame_idx, device_timestamp_ns, host_ts, seq`), arrays → `NpyChunkWriter`. `RawRecorder` in live mode is the SDK's `record_to_vrs` (configured before the server starts); in replay/mock mode it is `NullRawRecorder` (replay never rewrites raw; mock writes its raw streams via the derived writers under `raw_mock/`).

### 4.6 Session (`core/session.py`)

Owns `session_id` (UUID4), `layout`, `clock_anchor`, `manifest`, lifecycle `open() → running → finalize()`. `manifest.json` is written at `open()` with `status: "running"` and rewritten atomically at `finalize()` (`status: "completed" | "crashed"`); `inspect` treats a `running` manifest with no process alive as crashed and rebuilds `indexes/timeline.parquet` from chunk footers.

### 4.7 Dashboard contract

The dashboard is a *subscriber* like any other (`policy=LATEST` for images, `drop_oldest` ring of 2 000 points for scalars). It reads health snapshots from `monitoring` and publishes `events.marker` and `events.config` when the user clicks. It has no reference to the source or recorders.

### 4.8 Python API (`api.py`)

```python
from ariastreamlab import Experiment

exp = Experiment.from_json("experiment.json", mock=False)  # or Experiment.replay("sessions/2026…")
with exp:  # open session, connect, start source + recorders + graph + dashboard
    exp.subscribe("rgb", on_rgb)  # callback on a dedicated thread, policy "latest"
    exp.mark("trial_start", {"trial": 1})
    exp.run(duration_s=60)  # or exp.run() until Ctrl‑C / dashboard "Stop"
# exit: stop streaming, flush, finalize manifest, print quality summary
```

Everything else (`exp.device`, `exp.bus`, `exp.graph`, `exp.session`) is available but not needed by students.

---

## 5. JSON configuration

### 5.1 Minimal student file

```json
{ "extends": "preset:rgb_imu", "experiment": { "name": "first_capture", "participant": "P001" } }
```

### 5.2 Full model (defaults shown; Pydantic `extra="forbid"` everywhere)

```jsonc
{
  "$schema": "https://…/ariastreamlab.schema.json",
  "schema_version": 1,
  "extends": "preset:full_capture",            // or a relative path; single inheritance, deep‑merge, lists replaced
  "experiment": { "name": "…", "participant": "…", "description": "", "tags": [] },
  "device": {
    "connection": { "preferred_control": "auto", "streaming_interface": "wifi_sap",
                    "batch_period_ms": null,     // null ⇒ 0 usb, 200 wifi_sta, 800 wifi_sap
                    "hotspot": { "band": "5GHz", "bandwidth": "80MHz", "auto_join": false },
                    "reconnect_attempts": 2 },
    "profile": { "mode": "predefined", "name": null, "custom_json": null },   // predefined|custom|named
    "serial": null
  },
  "streams": {                                   // keys = registry ids; unknown key ⇒ error with suggestion
    "rgb":        { "enabled": true, "fps": 10, "resolution": "2016x1512" },
    "slam":       { "enabled": false, "fps": 10 },                      // group alias for the 4 CV cams
    "imu":        { "enabled": true, "rate_hz": 800 },                  // alias for imu_left+imu_right
    "eye_gaze":   { "enabled": true, "rate_hz": 30 },
    "hand_tracking": { "enabled": false },
    "vio":        { "enabled": false }, "audio": { "enabled": false }, "…": {}
  },
  "processors": [
    { "id": "rgb_small", "type": "image.resize", "inputs": ["rgb"], "outputs": ["rgb_small"],
      "parameters": { "width": 640, "height": 480 },
      "execution": { "queue_size": 2, "drop_policy": "latest", "join": "latest", "tolerance_ms": 50 },
      "save_output": false, "enabled": true }
  ],
  "processors_path": "./processors",             // optional directory of student processors
  "dashboard": { "enabled": true, "host": "127.0.0.1", "port": 8765, "open_browser": true,
                 "show": ["rgb", "imu", "eye_gaze", "head_motion"], "image_fps": 10, "rerun": false },
  "storage": { "root": "./sessions", "save_raw_vrs": true, "chunk_seconds": 60,
               "image_format": "mp4", "video_codec": "h264", "jpeg_quality": 90 },
  "replay": { "speed": 1.0, "start_s": null, "end_s": null, "backend": "tools" },
  "logging": { "level": "INFO", "json": false }
}
```

`save_raw` per stream is gone from the user model on purpose: on Aria, "raw" means "in the VRS", which is all‑or‑nothing per session (`storage.save_raw_vrs`). In mock mode the same flag writes mock raw streams as derived‑style files. Translation to the device profile (`acquisition/aria_gen2/profiles.py`): `mode=predefined` picks the pre‑defined profile that enables every requested stream and warns for each parameter it cannot match (e.g. "rgb.fps=30 requested, profile10 selected; ET cameras not available in that profile"); `mode=custom` starts from profile8/9 and overrides the requested sections; `mode=named` uses `name` verbatim. The chosen profile JSON is stored in `manifest.device.streaming_profile`.

---

## 6. Session storage format

```
sessions/20261010T143012Z_first_capture_P001/
├── manifest.json            # §8 contents; status running|completed|crashed
├── experiment.json          # fully resolved config (presets merged); read‑only after start (chmod 0444 + sha256 in manifest)
├── experiment.source.json   # the file the student actually wrote
├── calibration/
│   ├── device_calibration.json    # from register_device_calib_callback (SDK serialisation)
│   └── online_calibration.parquet # from VIO FrontendOutput.online_calib, if VIO enabled
├── raw/
│   └── aria_stream.vrs      # host‑received VRS (live only). Replay sessions contain raw/SOURCE (text: path+sha256 of the source VRS) instead
├── derived/
│   ├── head_motion/ part-00000.parquet … part-000NN.parquet
│   ├── rgb_small/    video-00000.mp4, frames-00000.parquet, …
│   └── <stream>/     _meta.json  (payload type, columns, units, processor id + version + params)
├── events/
│   └── events.parquet       # markers, device state changes, config changes, warnings: ts_ns(device), host_ts_ns, kind, name, payload(json)
├── indexes/
│   └── timeline.parquet     # stream_id, device_timestamp_ns, host_ts_ns, seq, file, row/frame  — built incrementally, rebuilt by `inspect` if missing
├── quality/
│   ├── device_quality_stats.jsonl   # periodic get_data_quality_stats() dumps (verbatim + parsed)
│   └── summary.json                 # end‑of‑session quality summary (§11)
└── logs/
    └── session.log          # structured (structlog → JSON lines when logging.json=true)
```

Every Parquet file carries the `device_timestamp_ns` (int64) and `host_timestamp_ns` columns first. Writers flush row groups every `chunk_seconds` or N rows and rotate files, so a crash loses at most one row group; `inspect` repairs truncated last files by re‑reading footers and rewriting `timeline.parquet`.

---

## 7. Concurrency model

| Component | Mechanism | Rationale |
|---|---|---|
| SDK receive threads | native, owned by SDK | our callbacks do `monotonic_ns()`, `to_numpy_array()` (already decoded), build `Sample`, `bus.publish` — microseconds. |
| EventBus | lock + deque per subscription | publish is O(subs); no fan‑out thread. |
| Processors | **one thread per processor** (`ProcessorWorker`) | bounded, configurable; numpy/cv2/torch release the GIL. Pure‑Python heavy processors are the GIL risk → `execution.isolate: "process"` (Phase 6+) runs the worker in a `multiprocessing` process fed through a `SharedMemory` ring; the `Processor` interface is already process‑safe because instances are built from config, not closures. |
| Recorders | one writer thread per recorder, `BLOCK` policy | back‑pressure protects data; a stalled disk shows up as queue depth in the dashboard instead of silent loss. |
| Dashboard | NiceGUI/uvicorn in its own thread with its own asyncio loop; timers poll `LATEST` subscriptions | closing the browser only stops the timers; acquisition is unaffected. |
| Replay source | one pacing thread (`sleep` until next device ts × speed), `Event` for pause, seek = reposition data‑provider iterator | mirrors SDK threading so processors see the same contract. |
| Thread budget | ≈ 1 (bus) + #processors + 2 (recorders) + 1 (dashboard) + 1 (monitor) | no per‑stream or per‑sample threads. |
| Shutdown | ordered: source.stop → graph.drain(timeout) → recorders.flush/close → manifest.finalize; `signal.SIGINT` handled once, second Ctrl‑C forces exit | sessions remain readable. |

---

## 8. Timestamp strategy

* **Device domain.** `device_timestamp_ns` is the SDK's `capture_timestamp_ns` (int64 ns, device monotonic clock). Streams delivering `timedelta` (`EyeGaze`, `HandTrackingResult`, `OpenLoopTrajectoryPose.tracking_timestamp`) are converted with `int(td.total_seconds() * 1e9)` computed from `td.days/seconds/microseconds` exactly (no float rounding). Audio chunks carry per‑sample timestamps; the sample's `device_timestamp_ns` is the first, `metadata["timestamps_ns"]` keeps the array.
* **Host domain.** `host_receive_timestamp_ns = time.monotonic_ns()` at callback entry. At `Session.open()` a `ClockAnchor{monotonic_ns, utc_ns = time.time_ns(), perf_counter_ns}` is stored and re‑sampled every 60 s into `events.parquet` (kind `clock_anchor`) so drift can be analysed.
* **Device ↔ host mapping.** When `TimeSyncRef.is_valid()`, the adapter publishes `health.clock` samples with `(host_ns, remote_ns_from_local(host_ns))`; `monitoring` fits a linear map (slope, offset) used for the latency metric and stored in the manifest. Without TicSync, latency is estimated from `arrival_timestamp_ns − capture_timestamp_ns` when the SDK provides both, otherwise reported as unavailable (never faked).
* **Ordering.** Nothing assumes order. `timing.sync.TimeSeriesBuffer` keeps a bounded sorted array (bisect insert); late arrivals are inserted in place and counted (`out_of_order` stat per stream, visible in the dashboard). Query API: `nearest(ts, max_dt)`, `previous(ts)`, `next(ts)`, `window(t0, t1)`, `interpolate(ts)` (linear for IMU/pose‑translation, slerp for rotations, none for images), `temporal_join(primary, others, tolerance)`.
* **Replay.** Device timestamps are read verbatim from VRS; host timestamps are the replay machine's clock (documented in the tutorial as the one thing that legitimately differs).

---

## 9. Dependencies

| Group | Packages | Notes |
|---|---|---|
| core (required) | `pydantic>=2`, `numpy`, `pyarrow`, `structlog`, `typer`, `rich` | ~ what students already have |
| aria (extra `[aria]`) | `projectaria-client-sdk~=2.5`, `projectaria-tools~=2.3` | pulls `rerun-sdk 0.33`, `projectaria-login`, `pandas`, `scipy`, `pillow` transitively (Meta's pins) |
| vision (required) | `opencv-python-headless` | video writer, resize, undistort; not a dependency of the SDK |
| dashboard (extra `[dashboard]`, on by default in docs) | `nicegui` | FastAPI/uvicorn included |
| optional | `zarr`, `scipy` (interp/filters), `rerun-sdk` | |
| dev | `pytest`, `pytest-timeout`, `ruff`, `mypy`, `import-linter`, `pre-commit` | |

Python **3.11** is the development version; `requires-python = ">=3.10,<3.13"` because of the SDK wheels. Packaging with `pyproject.toml` (hatchling), `uv` lock file committed, console script `ariastreamlab`.

---

## 10. Dashboard technology evaluation

| Option | Images @10 Hz | Plots | 3‑D trajectory | Buttons / markers | macOS reliability | Student friendliness | Decoupling |
|---|---|---|---|---|---|---|---|
| **NiceGUI** (chosen) | base64 JPEG via `ui.interactive_image` — fine to ~15 Hz at 640 px | ECharts (`ui.echart`) streaming | `ui.scene` (three.js) basic | native | web server; no native deps | Python only, one file per panel | own thread; tab close harmless |
| Rerun 0.33 | excellent | good | excellent | **none** | good (SDK already ships it) | Python logging API easy, layout harder | separate process ✔ |
| Dash/Plotly | polling, laggy for images | good | ok | yes | ok | callbacks model confusing | own thread |
| PyQtGraph/Qt | excellent | excellent | ok | yes | Qt install issues on student Macs | hard | must own main thread ✘ |
| Streamlit | poor (rerun loop) | ok | ok | yes | ok | easy | poor for realtime |
| FastAPI + custom JS | excellent | needs JS libs | needs JS | yes | ok | requires JS | own thread |

NiceGUI covers every required panel with Python only; Rerun stays an optional sink for people who want a rich 3‑D view (`dashboard.rerun: true` logs images/IMU/VIO to a spawned viewer).

---

## 11. Monitoring and quality summary

`monitoring.stats` keeps per stream: received, effective rate (sliding 5 s), out‑of‑order, bus drops (per subscriber), SDK‑queue size (from `get_*_queue_size`, polled), device‑side drops (parsed from `get_data_quality_stats()` every 10 s **[A‑5]**), transport drops (gaps in `frame_number` / expected‑vs‑received at nominal rate). The dashboard shows four separate counters per stream: **device**, **transport**, **app queue**, **processor**. `quality/summary.json` at the end:

```
RGB      received 5 988   device‑dropped 3   transport‑dropped 9   app‑dropped 0   effective 9.98 Hz (nominal 10)
IMU‑L    received 479 400 …                                         effective 799.0 Hz
Session  duration 600.2 s   disk 1.84 GB   warnings: 2 (thermal_mitigation at 412 s; wifi rssi low)
```

---

## 12. Phased implementation plan (adjusted to the SDK constraints)

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0** — repo + hardware probe | `pyproject`, CI (ruff/mypy/pytest), `scripts/probe_sdk.py` that runs the raw SDK flow on the Mac (USB connect → status → hotspot start → Wi‑Fi connect → stream 30 s with `record_to_vrs` → quality stats → replay both backends) and prints a report answering A‑1…A‑8 | Ricky runs it with the glasses; answers recorded in this document |
| **1** — config, core, timing, mock, bus | §3–§5, `MockSource`, `EventBus`, `timing.sync`, CLI `inspect-config`, `run --mock` printing rates | unit tests; invariant I‑3; `ariastreamlab run examples/mock_experiment.json --mock` runs headless |
| **2** — storage + replay | session layout, manifest, writers, timeline, `inspect`, `VrsReplaySource` (needs a Gen 2 VRS — a short host‑recorded one from Phase 0, or Meta's sample data), mock‑session replay | invariant I‑1 test (mock → record → replay → identical derived output); crash test (kill ‑9 mid‑session → `inspect` repairs) |
| **3** — processing graph | `Processor`, worker, policies, stats, built‑ins (`image.resize/grayscale/crop`, `imu.lowpass/motion_detector`, `gaze.project_to_rgb`), plugin discovery, custom‑processor example | tests for every drop policy with a deliberately slow processor; p50/p95/p99 reported |
| **4** — Aria Gen 2 adapter | `AriaGen2DeviceManager` state machine, CLI wrapper, profile translation, adapter callbacks → `Sample`, `record_to_vrs`, TicSync mapping, quality parser, `doctor`, `devices` | hardware integration tests (marked `hardware`); 20‑min hotspot session with no app‑side drops at profile9 |
| **5** — dashboard | NiceGUI panels, markers, health, replay controls (pause/seek/speed) | works live and in replay with the same panels; closing the tab leaves a session intact |
| **6** — polish | annotations API, config‑change events, quality summary, README, three examples, timestamps tutorial, Rerun sink, `isolate: process` | docs reviewed by a student; `doctor` green on a fresh Mac |

Phase 0 is new; it exists because A‑1/A‑2/A‑3 decide the shape of the untethered flow, and verifying them costs an hour with hardware versus days of guessing. Phases 1–3 do not need hardware at all; Phase 4 starts as soon as the probe report exists.

---

## 13. Decisions needed from you

1. **Hardware probe first?** Phase 0 requires the glasses and your Mac for ~30 minutes. If the glasses are not available soon, I start Phase 1 immediately (no hardware needed) and run the probe when they are.
2. **Dashboard: NiceGUI** (proposed) vs. Rerun‑only (simpler dependencies, no markers from the UI; markers would come only from the Python API / CLI).
3. **Raw ground truth:** accept host‑received VRS as the v1 definition (with device‑side drop stats in the manifest), reserving on‑device recording mode for later?
4. **Project name on PyPI / repo:** `ariastreamlab`, licence (MIT or Apache‑2.0 — Meta's SDK itself is proprietary, our code only *calls* it).

---

## 14. Implementation status

| Phase | State | Notes |
|---|---|---|
| 0 — repo + hardware probe | **done** | `pyproject.toml` (hatchling, ruff, mypy strict, import‑linter, pytest), CI matrix (Ubuntu/macOS × 3.10–3.12), `scripts/probe_sdk.py` (raw SDK, answers A‑1…A‑8; smoke‑tested against SDK 2.5.1 without hardware). **Waiting for a run on the Mac with the glasses.** |
| 1 — config, core, timing, mock, bus | **done** | `config/` (Pydantic v2, presets with `extends`, JSON Schema, student‑readable errors), `core/` (Sample/DerivedSample, payloads, registry, EventBus with 4 drop policies, errors, logging), `timing/` (ClockAnchor, exact ns conversions, TimeSeriesBuffer + interpolation + temporal_join), `acquisition/base.py` (Source, DeviceManager, state machine) + `acquisition/mock.py`, `monitoring/stats.py` (rates, out‑of‑order, seq‑gap transport drops with late‑fill), `events/markers.py` (markers with estimated device time), `api.Experiment`, CLI `streams / presets / schema / inspect-config / run --mock`. 71 unit/invariant tests, ruff + mypy strict + import contracts clean. |
| 2 — storage + replay | next | needs a Gen 2 VRS for the replay backend tests (probe output or Meta sample data) |
| 3 — processing graph | pending | |
| 4 — Aria Gen 2 adapter | pending | blocked on probe report for A‑1/A‑2/A‑3 |
| 5 — dashboard (NiceGUI) | pending | |
| 6 — polish | pending | |

Deviations from the proposal so far: `logging.json` became `logging.format: "text" | "json"` (``json`` shadows a Pydantic method); `SessionInfo` lives in `storage/session.py` rather than `core/` because it depends on `timing` (layer rule); `InvariantViolation` is named `InvariantError`.
