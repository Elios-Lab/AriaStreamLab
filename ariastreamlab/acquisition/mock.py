"""Synthetic acquisition source.

Generates camera frames, IMU, gaze, poses, audio and the small sensors with realistic device
timestamps (per-stream nominal periods, Gaussian jitter, optional simulated drops and
out-of-order delivery). One scheduler thread serves every stream through a priority queue,
mirroring the "no thread per stream" rule of the real adapter.

The payloads are deterministic for a given ``seed``, so a mock session recorded today can be
regenerated bit-for-bit tomorrow (used by the invariant tests).
"""

from __future__ import annotations

import heapq
import math
import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from ariastreamlab.acquisition.base import Source, SourceCapabilities, SourceStats
from ariastreamlab.config.models import MockConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import AcquisitionError
from ariastreamlab.core.payloads import (
    AudioPayload,
    BarometerPayload,
    GazePayload,
    GpsPayload,
    HandPayload,
    ImuPayload,
    MagnetometerPayload,
    PosePayload,
    ScalarPayload,
)
from ariastreamlab.core.registry import REGISTRY, StreamDescriptor
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.timing.clock import HostClock
from ariastreamlab.timing.convert import NS_PER_S, rate_to_period_ns

MOCK_DEVICE_ID = "mock-0"
MOCK_DEVICE_EPOCH_NS = 1_000_000_000_000  # device clock starts at 1000 s, like a booted device
_AUDIO_CHUNK_S = 0.02  # 20 ms frames, as Meta's audio profile
_AUDIO_CHANNELS = 8


@dataclass(frozen=True, slots=True)
class _Scheduled:
    due_device_ns: int
    stream_id: str
    seq: int

    def __lt__(self, other: _Scheduled) -> bool:
        return (self.due_device_ns, self.stream_id) < (other.due_device_ns, other.stream_id)


def _resolution(params: Mapping[str, Any], desc: StreamDescriptor, scale: float) -> tuple[int, int]:
    """(height, width) of a mock camera frame."""
    if "resolution" in params:
        w, h = (int(x) for x in str(params["resolution"]).split("x"))
    elif desc.group == "slam":
        w = h = 512
    elif desc.group == "et":
        w = h = 200
    else:
        w, h = 2016, 1512
    return max(8, int(h * scale)), max(8, int(w * scale))


def _rate(params: Mapping[str, Any], desc: StreamDescriptor) -> float:
    for key in ("fps", "rate_hz"):
        if key in params:
            return float(params[key])
    if desc.id == "audio":
        return 1.0 / _AUDIO_CHUNK_S
    return float(desc.nominal_rate_hz or 1.0)


class MockSource(Source):
    """Deterministic synthetic source implementing the :class:`Source` contract.

    Args:
        streams: Concrete stream id → effective parameters
            (from ``ExperimentConfig.enabled_streams``).
        mock: Mock options (seed, jitter, drops…).
        session_id: Session UUID for provenance.
        duration_s: Stop by itself after this much device time (``None`` ⇒ until ``stop``).
        clock: Host clock (a ``FakeClock`` in tests).
    """

    def __init__(
        self,
        streams: Mapping[str, Mapping[str, Any]],
        *,
        mock: MockConfig | None = None,
        session_id: str = "mock-session",
        duration_s: float | None = None,
        clock: HostClock | None = None,
    ) -> None:
        self._streams = {sid: dict(p) for sid, p in streams.items()}
        unknown = [s for s in self._streams if s not in REGISTRY]
        if unknown:
            raise AcquisitionError(f"mock source: unknown streams {unknown}")
        self._cfg = mock or MockConfig()
        self._session_id = session_id
        self._duration_ns = None if duration_s is None else int(duration_s * NS_PER_S)
        self._clock = clock or HostClock()
        self._rng = np.random.default_rng(self._cfg.seed)
        self._bus: EventBus | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._received: Counter[str] = Counter()
        self._dropped: Counter[str] = Counter()
        self._provenance = Provenance("mock", "mock", session_id)
        self._periods = {
            sid: rate_to_period_ns(_rate(p, REGISTRY.get(sid))) for sid, p in self._streams.items()
        }
        self._shapes = {
            sid: _resolution(p, REGISTRY.get(sid), self._cfg.image_scale)
            for sid, p in self._streams.items()
            if REGISTRY.get(sid).is_image
        }
        self.capabilities = SourceCapabilities(
            origin="mock",
            streams=(*tuple(self._streams), "device_calibration"),
            can_record_raw=False,
            is_paced=self._cfg.realtime,
        )

    # -- Source API --------------------------------------------------------------------
    def start(self, bus: EventBus) -> None:  # noqa: D102
        if self._thread is not None:
            raise AcquisitionError("mock source already started")
        self._bus = bus
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mock-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:  # noqa: D102
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        self.finished.set()

    def stats(self) -> SourceStats:  # noqa: D102
        return SourceStats(dict(self._received), dict(self._dropped), {"seed": self._cfg.seed})

    # -- generation ---------------------------------------------------------------------
    def _run(self) -> None:
        bus = self._bus
        assert bus is not None
        host0 = self._clock.now_ns()
        dev0 = MOCK_DEVICE_EPOCH_NS
        self._publish_calibration(bus, dev0, host0)
        heap: list[_Scheduled] = [
            _Scheduled(dev0 + self._periods[sid], sid, 0) for sid in self._streams
        ]
        heapq.heapify(heap)
        pending_swap: dict[str, Sample] = {}
        try:
            while heap and not self._stop.is_set():
                item = heapq.heappop(heap)
                elapsed = item.due_device_ns - dev0
                if self._duration_ns is not None and elapsed > self._duration_ns:
                    break
                if self._cfg.realtime:
                    self._clock.sleep_until_ns(host0 + elapsed)
                    if self._stop.is_set():
                        break
                jitter = (
                    int(self._rng.normal(0.0, self._cfg.jitter_ms * 1e6))
                    if self._cfg.jitter_ms > 0
                    else 0
                )
                device_ns = item.due_device_ns + jitter
                host_ns = (
                    self._clock.now_ns()
                    if self._cfg.realtime
                    else host0 + elapsed + abs(jitter) + 150_000
                )
                sample = self._make_sample(item.stream_id, item.seq, device_ns, host_ns, elapsed)
                heapq.heappush(
                    heap,
                    _Scheduled(
                        item.due_device_ns + self._periods[item.stream_id],
                        item.stream_id,
                        item.seq + 1,
                    ),
                )
                if (
                    self._cfg.drop_probability > 0
                    and self._rng.random() < self._cfg.drop_probability
                ):
                    self._dropped[item.stream_id] += 1
                    continue
                # simulated out-of-order: hold one sample back and emit it after the next one
                held = pending_swap.pop(item.stream_id, None)
                if (
                    self._cfg.out_of_order_probability > 0
                    and held is None
                    and self._rng.random() < self._cfg.out_of_order_probability
                ):
                    pending_swap[item.stream_id] = sample
                    continue
                self._emit(bus, sample)
                if held is not None:
                    self._emit(bus, held)
            for held in pending_swap.values():
                self._emit(bus, held)
        finally:
            self.finished.set()

    def _emit(self, bus: EventBus, sample: Sample) -> None:
        bus.publish(f"raw.{sample.stream_id}", sample)
        self._received[sample.stream_id] += 1

    def _publish_calibration(self, bus: EventBus, device_ns: int, host_ns: int) -> None:
        calib = {
            "device_id": MOCK_DEVICE_ID,
            "cameras": {
                sid: {
                    "label": REGISTRY.get(sid).native_label,
                    "image_size": list(reversed(self._shapes[sid])),
                    "focal_px": self._shapes[sid][1] * 0.8,
                }
                for sid in self._shapes
            },
            "note": "synthetic calibration from MockSource",
        }
        bus.publish(
            "raw.device_calibration",
            Sample(
                MOCK_DEVICE_ID, "device_calibration", device_ns, host_ns, 0, calib, self._provenance
            ),
        )
        self._received["device_calibration"] += 1

    # -- payload generators -----------------------------------------------------------
    def _make_sample(
        self, sid: str, seq: int, device_ns: int, host_ns: int, elapsed_ns: int
    ) -> Sample:
        desc = REGISTRY.get(sid)
        t = elapsed_ns / NS_PER_S
        data: Any
        meta: dict[str, Any] = {"label": desc.native_label}
        if desc.is_image:
            data = self._image(sid, t, seq)
            meta |= {"camera_id": desc.sdk_camera_id, "exposure_us": 4000, "gain": 1.0}
        elif desc.payload == "imu":
            data = self._imu(sid, t)
        elif desc.payload == "mag":
            data = MagnetometerPayload(
                (25e-6 * math.cos(0.1 * t), 25e-6 * math.sin(0.1 * t), -40e-6)
            )
        elif desc.payload == "baro":
            data = BarometerPayload(
                101_325.0 + 5.0 * math.sin(0.05 * t) + float(self._rng.normal(0, 0.5)), 28.0
            )
        elif desc.payload == "gaze":
            data = self._gaze(t)
        elif desc.payload == "pose":
            data = self._pose(t, high_freq=(sid == "vio_high_frequency"))
        elif desc.payload == "hand":
            data = self._hands(t)
        elif desc.payload == "audio":
            data = self._audio(t, device_ns)
        elif desc.payload == "gps":
            data = GpsPayload(
                44.4056 + 1e-5 * math.sin(0.01 * t),
                8.9463 + 1e-5 * math.cos(0.01 * t),
                30.0,
                3.0,
                0.5,
                None,
            )
        elif desc.payload == "scalar":
            data = ScalarPayload(
                0.5 + 0.5 * math.sin(2 * math.pi * 1.2 * t)
                if sid == "ppg"
                else 300.0 + 20.0 * math.sin(0.2 * t),
                desc.units.get("value", ""),
            )
        else:
            data = {"stream": sid, "t": t}
        return Sample(MOCK_DEVICE_ID, sid, device_ns, host_ns, seq, data, self._provenance, meta)

    def _image(self, sid: str, t: float, seq: int) -> np.ndarray:
        h, w = self._shapes[sid]
        yy, xx = np.mgrid[0:h, 0:w]
        # moving gradient + a bouncing bright disc; cheap, deterministic, looks alive on a dashboard
        cx = (0.5 + 0.4 * math.sin(0.7 * t)) * w
        cy = (0.5 + 0.4 * math.cos(0.5 * t)) * h
        disc = ((xx - cx) ** 2 + (yy - cy) ** 2) < (0.08 * min(h, w)) ** 2
        base = ((xx + seq) % w) * (255.0 / w)
        mono = np.where(disc, 255.0, base * 0.6).astype(np.uint8)
        if sid == "rgb":
            rgb = np.stack(
                [mono, (yy * 255.0 / h).astype(np.uint8), np.full_like(mono, 90)], axis=-1
            )
            return np.ascontiguousarray(rgb)
        return mono

    def _imu(self, sid: str, t: float) -> ImuPayload:
        # gravity on z, breathing-like sway, and a 1-second "head turn" burst every 8 seconds
        burst = 1.0 if (t % 8.0) < 1.0 else 0.0
        side = 1.0 if sid.endswith("left") else -1.0
        noise = self._rng.normal(0, 0.02, 6)
        accel = (
            0.3 * math.sin(2 * math.pi * 0.25 * t)
            + 2.0 * burst * math.sin(2 * math.pi * 1.5 * t)
            + noise[0],
            0.1 * side * math.cos(2 * math.pi * 0.25 * t) + noise[1],
            9.81 + 0.05 * math.sin(2 * math.pi * 0.25 * t) + noise[2],
        )
        gyro = (
            0.02 * math.sin(2 * math.pi * 0.3 * t) + noise[3],
            0.02 * math.cos(2 * math.pi * 0.3 * t)
            + 1.2 * burst * math.sin(2 * math.pi * 1.5 * t)
            + noise[4],
            0.01 * side + noise[5],
        )
        return ImuPayload(tuple(float(x) for x in accel), tuple(float(x) for x in gyro), 31.5)  # type: ignore[arg-type]

    def _gaze(self, t: float) -> GazePayload:
        yaw = 0.3 * math.sin(2 * math.pi * 0.15 * t) + float(self._rng.normal(0, 0.005))
        pitch = (
            -0.1 + 0.15 * math.sin(2 * math.pi * 0.11 * t + 1.0) + float(self._rng.normal(0, 0.005))
        )
        depth = 1.0 + 0.5 * (1 + math.sin(2 * math.pi * 0.05 * t))
        # a 150 ms "blink" (invalid gaze) every 3.7 s, so gaze-validity logic can be exercised
        valid = (t % 3.7) >= 0.15
        return GazePayload(
            yaw, pitch, depth, valid, yaw - 0.02, yaw + 0.02, pitch - 0.02, pitch + 0.02
        )

    def _pose(self, t: float, *, high_freq: bool) -> PosePayload:
        r = 1.5
        w = 2 * math.pi * 0.05
        x, y, z = (
            r * math.cos(w * t),
            r * math.sin(w * t),
            1.6 + 0.02 * math.sin(2 * math.pi * 0.25 * t),
        )
        yaw = w * t + math.pi / 2
        q = (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))
        vel = (-r * w * math.sin(w * t), r * w * math.cos(w * t), 0.0)
        return PosePayload((x, y, z), q, vel, (0.0, 0.0, w), 0.9 if not high_freq else None)

    def _hands(self, t: float) -> HandPayload:
        base = np.array([[0.0, -0.2, 0.4]], dtype=np.float32) + np.linspace(
            0, 0.1, 21, dtype=np.float32
        )[:, None] * np.array([[1.0, 0.5, 0.0]], dtype=np.float32)
        left = base + np.float32(0.02 * math.sin(2 * math.pi * 0.5 * t))
        return HandPayload(left, None, 0.95, None)

    def _audio(self, t: float, device_ns: int) -> AudioPayload:
        n = int(16000 * _AUDIO_CHUNK_S)
        tt = t + np.arange(n) / 16000.0
        tone = 0.2 * np.sin(2 * np.pi * 440.0 * tt) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.5 * t))
        samples = (tone[:, None] * np.ones((1, _AUDIO_CHANNELS)) * 32767).astype(np.int16)
        ts = device_ns + (np.arange(n, dtype=np.int64) * (NS_PER_S // 16000))
        return AudioPayload(samples, 16000, ts)


def iter_mock_samples(
    streams: Mapping[str, Mapping[str, Any]], *, duration_s: float, mock: MockConfig | None = None
) -> Iterator[Sample]:
    """Convenience for tests: run a non-realtime mock source and yield its samples in order."""
    from ariastreamlab.core.bus import DropPolicy

    cfg = (mock or MockConfig()).model_copy(update={"realtime": False})
    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10_000_000, policy=DropPolicy.DROP_NEWEST, name="iter")
    src = MockSource(streams, mock=cfg, duration_s=duration_s)
    src.start(bus)
    src.wait()
    src.stop()
    yield from sub.drain()
    sub.close()
