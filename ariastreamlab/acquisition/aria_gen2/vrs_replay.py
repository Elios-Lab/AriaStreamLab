"""Replay an Aria Gen 2 VRS recording.

Two backends, both producing the same :class:`~ariastreamlab.core.sample.Sample` objects:

* :class:`VrsReplaySource` — ``projectaria_tools`` data provider, iterated in device-time
  order by :class:`~ariastreamlab.acquisition.replay.PacedReplay` ⇒ pause / seek / speed.
  This is the primary backend (``replay.backend = "tools"``).
* :class:`SdkReplaySource` — the client SDK's ``create_replay_source``: the SDK paces the
  file and drives the very same callbacks as a live device. No pause/seek; speed is fixed at
  start. Kept as the "exactly like live" validation backend (``replay.backend = "sdk"``).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.aria_gen2 import convert as cv
from ariastreamlab.acquisition.aria_gen2.callbacks import CallbackBinder
from ariastreamlab.acquisition.base import Source, SourceCapabilities, SourceStats
from ariastreamlab.acquisition.replay import PacedReplay
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ReplayError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.timing.clock import HostClock

log = get_logger("ariastreamlab.acquisition.aria_gen2.vrs_replay")


def open_provider(path: Path | str) -> Any:
    """``create_vrs_data_provider`` with a readable error."""
    from projectaria_tools.core import data_provider

    p = Path(path)
    if not p.is_file():
        raise ReplayError(f"VRS file not found: {p}")
    provider = data_provider.create_vrs_data_provider(str(p))
    if provider is None:
        raise ReplayError(f"projectaria_tools could not open {p} (not a VRS file, or truncated)")
    return provider


def vrs_streams(provider: Any) -> dict[str, Any]:
    """Stream id → VRS ``StreamId`` for every stream AriaStreamLab knows about."""
    out: dict[str, Any] = {}
    for sid in provider.get_all_streams():
        label = provider.get_label_from_stream_id(sid)
        ours = cv.LABEL_TO_STREAM.get(label)
        if ours is not None and provider.get_num_data(sid) > 0:
            out[ours] = sid
    return out


def vrs_device_serial(provider: Any) -> str:
    """Device serial from the file metadata (``"unknown"`` if absent)."""
    try:
        return str(provider.get_metadata().device_serial) or "unknown"
    except Exception:
        return "unknown"


class VrsReplaySource(PacedReplay):
    """Paced replay through the ``projectaria_tools`` data provider."""

    def __init__(
        self, vrs_path: Path | str, *, session_id: str, streams: list[str] | None = None, **kw: Any
    ) -> None:
        self.path = Path(vrs_path)
        self._provider = open_provider(self.path)
        available = vrs_streams(self._provider)
        wanted = list(available) if streams is None else [s for s in streams if s in available]
        missing = [] if streams is None else [s for s in streams if s not in available]
        if missing:
            log.warning("replay.streams_missing", missing=missing, available=list(available))
        self._ids = {s: available[s] for s in wanted}
        super().__init__(
            streams=tuple(wanted),
            source_id=f"vrs:{self.path}",
            session_id=session_id,
            device_id=vrs_device_serial(self._provider),
            **kw,
        )
        self.calibration = self._read_calibration()

    def _read_calibration(self) -> dict[str, Any] | None:
        try:
            c = self._provider.get_device_calibration()
            return None if c is None else cv.calibration_to_dict(c)
        except Exception:
            log.exception("replay.calibration_failed")
            return None

    def time_range(self) -> tuple[int, int] | None:  # noqa: D102
        from projectaria_tools.core.sensor_data import TimeDomain

        lo: int | None = None
        hi: int | None = None
        for sid in self._ids.values():
            a = self._provider.get_first_time_ns(sid, TimeDomain.DEVICE_TIME)
            b = self._provider.get_last_time_ns(sid, TimeDomain.DEVICE_TIME)
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        return None if lo is None or hi is None else (int(lo), int(hi))

    def _iter_from(self, start_ns: int | None) -> Iterator[Sample]:
        from projectaria_tools.core.sensor_data import SensorDataType, TimeDomain

        opts = self._provider.get_default_deliver_queued_options()
        opts.deactivate_stream_all()
        for vrs_id in self._ids.values():
            opts.activate_stream(vrs_id)
        if start_ns is not None:
            opts.set_truncate_first_device_time_ns(
                int(max(0, start_ns - (self.time_range() or (0, 0))[0]))
            )
        id_to_stream = {str(v): k for k, v in self._ids.items()}
        host = self._clock.now_ns  # replaced per sample by PacedReplay._restamp
        if self.calibration is not None and start_ns is None:
            yield Sample(
                self.device_id,
                "device_calibration",
                (self.time_range() or (0, 0))[0],
                host(),
                0,
                self.calibration,
                self._provenance,
                {"note": "from VRS file"},
            )
        seq: Counter[str] = Counter()
        for sd in self._provider.deliver_queued_sensor_data(opts):
            stream_id = id_to_stream.get(str(sd.stream_id()))
            if stream_id is None:
                continue
            t = sd.sensor_data_type()
            ts = int(sd.get_time_ns(TimeDomain.DEVICE_TIME))
            meta: dict[str, Any] = {}
            data: Any
            if t == SensorDataType.IMAGE:
                img, rec = sd.image_data_and_record()
                data = cv.image_to_payload(img)
                meta = cv.image_metadata(rec)
                n = int(rec.frame_number)
            elif t == SensorDataType.IMU:
                data = cv.imu_to_payload(sd.imu_data())
                n = seq[stream_id]
            elif t == SensorDataType.MAGNETOMETER:
                data = cv.mag_to_payload(sd.magnetometer_data())
                n = seq[stream_id]
            elif t == SensorDataType.BAROMETER:
                data = cv.baro_to_payload(sd.barometer_data())
                n = seq[stream_id]
            elif t == SensorDataType.AUDIO:
                a, r = sd.audio_data_and_record()
                data = cv.audio_to_payload(a, r)
                n = seq[stream_id]
            elif t == SensorDataType.EYE_GAZE:
                data = cv.gaze_to_payload(sd.eye_gaze_data())
                n = seq[stream_id]
            elif t == SensorDataType.HAND_POSE:
                data = cv.hand_to_payload(sd.hand_pose_data())
                n = seq[stream_id]
            elif t == SensorDataType.VIO:
                v = sd.vio_data()
                data, meta = cv.vio_to_payload(v), cv.vio_metadata(v)
                n = seq[stream_id]
            elif t == SensorDataType.VIO_HIGH_FREQ:
                data = cv.vio_hf_to_payload(sd.vio_high_freq_data())
                n = seq[stream_id]
            elif t == SensorDataType.PPG:
                data = cv.ppg_to_payload(sd.ppg_data())
                n = seq[stream_id]
            elif t == SensorDataType.ALS:
                a = sd.als_data()
                data, meta = cv.als_to_payload(a), cv.als_metadata(a)
                n = seq[stream_id]
            elif t == SensorDataType.GPS:
                data = cv.gps_to_payload(sd.gps_data())
                n = seq[stream_id]
            elif stream_id == "temperature":
                tp = sd.temperature_data()
                data, meta = (
                    cv.temperature_to_payload(tp),
                    {"sensor": str(getattr(tp, "sensor_name", ""))},
                )
                n = seq[stream_id]
            else:
                continue
            seq[stream_id] += 1
            if start_ns is not None and ts < start_ns:
                continue
            yield Sample(
                self.device_id,
                stream_id,
                ts,
                host(),
                n,
                data,
                self._provenance,
                {"label": REGISTRY.get(stream_id).native_label, **meta},
            )


class SdkReplaySource(Source):
    """Replay through the client SDK's own replay engine (same callbacks as a live device)."""

    def __init__(
        self,
        vrs_path: Path | str,
        *,
        session_id: str,
        streams: list[str] | None = None,
        speed: float = 1.0,
        start_s: float | None = None,
        end_s: float | None = None,
        clock: HostClock | None = None,
    ) -> None:
        import aria.sdk_gen2 as sdk

        self.path = Path(vrs_path)
        if not self.path.is_file():
            raise ReplayError(f"VRS file not found: {self.path}")
        self._sdk = sdk
        self._speed = speed
        self._start_s, self._end_s = start_s, end_s
        provider = open_provider(self.path)
        available = vrs_streams(provider)
        wanted = list(available) if streams is None else [s for s in streams if s in available]
        self.capabilities = SourceCapabilities(
            origin="replay",
            streams=tuple(wanted),
            can_record_raw=False,
            is_paced=speed > 0,
            supports_replay_control=False,
        )
        self._binder = CallbackBinder(
            EventBus(),
            Provenance("replay", f"vrs:{self.path}", session_id),
            vrs_device_serial(provider),
            set(wanted),
            clock,
        )
        self._handler: Any = None
        self._session: Any = None
        self._watch: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, bus: EventBus) -> None:  # noqa: D102
        self._binder._bus = bus
        self._handler = self._sdk.StreamDataInterface(True)
        self._binder.bind(self._handler)
        cfg = self._sdk.ReplaySourceConfig()
        cfg.filename = str(self.path)
        cfg.speed_multiplier = float(self._speed)
        if self._start_s is not None:
            cfg.start_time_sec = float(self._start_s)
        if self._end_s is not None:
            cfg.end_time_sec = float(self._end_s)
        self._session = self._sdk.create_replay_source(cfg, self._handler)
        if self._session is None:
            raise ReplayError(f"the SDK could not open {self.path} for replay")
        self._stop.clear()
        self._watch = threading.Thread(
            target=self._wait_finished, name="sdk-replay-watch", daemon=True
        )
        self._watch.start()

    # The SDK reports ``is_finished()`` as soon as the *reader* reaches the end of the file,
    # while its per-stream dispatch queues may still hold hundreds of samples ("Enqueued:
    # 16588, Processed: 16282, Remaining queue size: 305" in its own log). Stopping at that
    # moment would silently truncate the replay, so wait until the callbacks go quiet.
    _DRAIN_QUIET_S = 0.5
    _DRAIN_MAX_S = 30.0

    def _wait_finished(self) -> None:
        while not self._stop.is_set():
            if self._session is not None and self._session.is_finished():
                self._drain_queues()
                break
            self._stop.wait(0.1)
        self.finished.set()

    def _drain_queues(self) -> None:
        deadline = time.monotonic() + self._DRAIN_MAX_S
        last = sum(self._binder.received.values())
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(self._DRAIN_QUIET_S)
            now = sum(self._binder.received.values())
            if now == last:
                return
            last = now

    def stop(self) -> None:  # noqa: D102
        self._stop.set()
        if self._session is not None:
            with contextlib.suppress(Exception):
                self._session.stop()
            self._session = None
        if self._handler is not None:
            with contextlib.suppress(Exception):
                self._handler.clear_callbacks()
            self._handler = None
        self.finished.set()

    def stats(self) -> SourceStats:  # noqa: D102
        return SourceStats(
            dict(self._binder.received),
            {},
            {"callback_errors": dict(self._binder.errors), "speed": self._speed},
        )
