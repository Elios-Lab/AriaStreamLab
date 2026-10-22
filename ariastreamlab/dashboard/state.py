"""What the dashboard shows, collected from the bus — no UI code here.

:class:`DashboardState` is a bus subscriber like any processor (docs/ARCHITECTURE.md §4.7):
``LATEST`` queues for images, a ring of the last N points per scalar channel, the newest
``health.*`` snapshots and the marker/device-state events. It knows nothing about sources or
recorders; the UI polls it, and tests can drive it without NiceGUI.
"""

from __future__ import annotations

import base64
import dataclasses
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.payloads import (
    AudioPayload,
    BarometerPayload,
    GazePayload,
    HandPayload,
    ImuPayload,
    MagnetometerPayload,
    PosePayload,
    ScalarPayload,
)
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import AnySample
from ariastreamlab.events.markers import Event

__all__ = ["DashboardState", "ImageFrame", "channels_of", "encode_jpeg"]


@dataclass
class ImageFrame:
    """The newest frame of an image stream, already JPEG-encoded for the browser."""

    stream_id: str
    device_timestamp_ns: int
    width: int
    height: int
    data_url: str
    received_at: float
    overlay: dict[str, float] | None = None


@dataclass
class Channel:
    """One scalar trace (``imu_left/accel_x``…)."""

    stream_id: str
    name: str
    unit: str
    t_ns: deque[int]
    values: deque[float]

    def points(self, t0_ns: int) -> list[list[float]]:
        """``[[seconds since t0, value], …]`` for a chart."""
        return [[(t - t0_ns) / 1e9, v] for t, v in zip(self.t_ns, self.values, strict=True)]


def channels_of(data: Any) -> dict[str, tuple[float, str]]:
    """Numeric channels of a payload: ``name -> (value, unit)``."""
    if isinstance(data, ImuPayload):
        a, g = data.accel_msec2, data.gyro_radsec
        return {
            "accel_x": (a[0], "m/s^2"),
            "accel_y": (a[1], "m/s^2"),
            "accel_z": (a[2], "m/s^2"),
            "gyro_x": (g[0], "rad/s"),
            "gyro_y": (g[1], "rad/s"),
            "gyro_z": (g[2], "rad/s"),
        }
    if isinstance(data, GazePayload):
        return {
            "yaw": (data.yaw_rad, "rad"),
            "pitch": (data.pitch_rad, "rad"),
            "depth": (data.depth_m, "m"),
        }
    if isinstance(data, ScalarPayload):
        return {"value": (data.value, data.unit)}
    if isinstance(data, PosePayload):
        t = data.translation_m
        return {"x": (t[0], "m"), "y": (t[1], "m"), "z": (t[2], "m")}
    if isinstance(data, BarometerPayload):
        return {"pressure": (data.pressure_pa, "Pa")}
    if isinstance(data, MagnetometerPayload):
        m = data.mag_tesla
        return {"mag_x": (m[0], "T"), "mag_y": (m[1], "T"), "mag_z": (m[2], "T")}
    if isinstance(data, HandPayload):
        return {
            "left_conf": (data.left_confidence or 0.0, ""),
            "right_conf": (data.right_confidence or 0.0, ""),
        }
    if isinstance(data, AudioPayload):
        samples = np.asarray(data.samples, dtype=np.float64)
        if samples.size == 0:
            return {}
        if np.issubdtype(np.asarray(data.samples).dtype, np.integer):
            samples = samples / 32768.0
        rms = float(np.sqrt(np.mean(samples**2)))
        level_db = 20 * math.log10(rms) if rms > 0 else -120.0
        return {"level_db": (max(-120.0, level_db), "dBFS")}
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return {"value": (float(data), "")}
    if isinstance(data, Mapping):
        out: dict[str, tuple[float, str]] = {}
        for k, v in data.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                out[str(k)] = (float(v), "")
        return out
    return {}


def encode_jpeg(img: np.ndarray, max_width: int = 640, quality: int = 80) -> tuple[str, int, int]:
    """Encode an RGB/mono uint8 array as a JPEG data URL.

    Returns ``(data_url, width, height)`` where width/height are those of the *encoded*
    image (downscaled to ``max_width``); overlays are drawn in these coordinates.
    """
    import cv2

    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, (max_width, max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    if img.ndim == 3 and img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii"), w, h


class DashboardState:
    """Collects the newest data for the UI from the bus.

    Args:
        bus: The experiment's bus.
        streams: Stream ids to display (raw and derived); ``None`` ⇒ everything seen.
        ring: Points kept per scalar channel.
        image_fps: Upper bound on image updates per stream.
        stats_provider: Returns the per-stream stats dict (the experiment's ``stream_stats``).
        processor_stats_provider: Returns per-processor stats (``processor_stats``).
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        streams: Iterable[str] | None = None,
        ring: int = 6000,
        image_fps: float = 10.0,
        max_channel_hz: float = 100.0,
        stats_provider: Callable[[], Mapping[str, Any]] | None = None,
        processor_stats_provider: Callable[[], Mapping[str, Any]] | None = None,
        bus_stats_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._bus = bus
        self._wanted = None if streams is None else set(streams)
        self._ring = ring
        self._min_image_interval = 1.0 / image_fps
        self._min_channel_dt_ns = int(1e9 / max_channel_hz) if max_channel_hz > 0 else 0
        self._stats_provider = stats_provider
        self._proc_provider = processor_stats_provider
        self._bus_provider = bus_stats_provider
        self._lock = threading.Lock()
        self.images: dict[str, ImageFrame] = {}
        self.channels: dict[str, Channel] = {}
        self.gaze_points: dict[str, dict[str, float]] = {}
        self._pose_streams: set[str] = set()
        self.health: dict[str, Any] = {}
        self.events: deque[Event] = deque(maxlen=200)
        self.t0_ns: int | None = None
        self.last_device_ns: int | None = None
        self.samples_seen = 0
        self._last_image_at: dict[str, float] = {}
        self._subs: list[Subscription[Any]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.session: dict[str, Any] = {}
        self.started_at = time.monotonic()

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        """Subscribe and start the collector thread."""
        if self._thread is not None:
            return
        self._subs = [
            self._bus.subscribe(
                "raw.*", maxsize=4096, policy=DropPolicy.DROP_OLDEST, name="dash:raw"
            ),
            self._bus.subscribe(
                "derived.*", maxsize=4096, policy=DropPolicy.DROP_OLDEST, name="dash:derived"
            ),
            self._bus.subscribe(
                "health.*", maxsize=64, policy=DropPolicy.DROP_OLDEST, name="dash:health"
            ),
            self._bus.subscribe(
                "events.*", maxsize=256, policy=DropPolicy.DROP_OLDEST, name="dash:events"
            ),
        ]
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dashboard-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop collecting (the UI keeps showing the last state)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None
        for s in self._subs:
            s.close()
        self._subs = []

    def _run(self) -> None:
        raw, derived, health, events = self._subs
        while not self._stop.is_set():
            got = False
            for item in raw.drain():
                got = True
                self._observe(item)
            for item in derived.drain():
                got = True
                self._observe(item)
            for item in health.drain():
                got = True
                self._observe_health(item)
            for item in events.drain():
                got = True
                if isinstance(item, Event):
                    with self._lock:
                        self.events.append(item)
            if not got:
                item = raw.get(timeout=0.05)
                if item is not None:
                    self._observe(item)

    # -- ingestion ---------------------------------------------------------------------
    def _observe(self, s: AnySample) -> None:
        sid = s.stream_id
        if self._wanted is not None and sid not in self._wanted:
            return
        ts = s.device_timestamp_ns
        with self._lock:
            self.samples_seen += 1
            if self.t0_ns is None or ts < self.t0_ns:
                self.t0_ns = ts
            if self.last_device_ns is None or ts > self.last_device_ns:
                self.last_device_ns = ts
        data = s.data
        if isinstance(data, np.ndarray) and data.ndim in (2, 3) and data.dtype == np.uint8:
            now = time.monotonic()
            if now - self._last_image_at.get(sid, 0.0) < self._min_image_interval:
                return
            try:
                url, w, h = encode_jpeg(data)
            except Exception:
                return
            self._last_image_at[sid] = now
            with self._lock:
                self.images[sid] = ImageFrame(sid, ts, w, h, url, now)
            return
        if isinstance(data, Mapping) and {"x_norm", "y_norm"} <= set(data):
            with self._lock:
                self.gaze_points[sid] = {
                    "x_norm": float(data["x_norm"]),
                    "y_norm": float(data["y_norm"]),
                    "valid": float(bool(data.get("valid", True))),
                    "in_image": float(bool(data.get("in_image", True))),
                }
        if isinstance(data, PosePayload):
            with self._lock:
                self._pose_streams.add(sid)
        for name, (value, unit) in channels_of(data).items():
            if not math.isfinite(value):
                continue
            key = f"{sid}/{name}"
            with self._lock:
                ch = self.channels.get(key)
                if ch is None:
                    unit = unit or self._unit_from_registry(sid, name)
                    ch = Channel(
                        sid, name, unit, deque(maxlen=self._ring), deque(maxlen=self._ring)
                    )
                    self.channels[key] = ch
                elif ch.t_ns and ts - ch.t_ns[-1] < self._min_channel_dt_ns:
                    continue  # thin high-rate streams: the chart never needs > max_channel_hz
                ch.t_ns.append(ts)
                ch.values.append(float(value))

    @staticmethod
    def _unit_from_registry(sid: str, name: str) -> str:
        if sid in REGISTRY:
            units = REGISTRY.get(sid).units
            for k, u in units.items():
                if name.startswith(k):
                    return str(u)
        return ""

    def _observe_health(self, item: Any) -> None:
        kind = type(item).__name__
        with self._lock:
            if hasattr(item, "as_dict"):
                payload = item.as_dict()
            elif hasattr(item, "__dict__") and not isinstance(item, dict):
                payload = {k: v for k, v in vars(item).items() if not k.startswith("_")}
            else:
                payload = dict(item) if isinstance(item, Mapping) else {"value": item}
            key = {
                "DeviceHealth": "device",
                "QualityStats": "quality",
                "dict": "link"
                if "connections" in payload
                else ("storage" if "write_rate_bps" in payload else "pipeline"),
            }.get(kind, kind.lower())
            self.health[key] = {"at": time.monotonic(), **payload}

    # -- snapshot for the UI -------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """A JSON-friendly copy of everything the UI needs (also served at ``/api/state``)."""
        with self._lock:
            t0 = self.t0_ns or 0
            out: dict[str, Any] = {
                "session": dict(self.session),
                "uptime_s": round(time.monotonic() - self.started_at, 1),
                "samples_seen": self.samples_seen,
                "t0_ns": self.t0_ns,
                "last_device_ns": self.last_device_ns,
                "elapsed_s": None
                if self.t0_ns is None or self.last_device_ns is None
                else round((self.last_device_ns - self.t0_ns) / 1e9, 2),
                "images": {
                    sid: {
                        "device_timestamp_ns": f.device_timestamp_ns,
                        "width": f.width,
                        "height": f.height,
                        "age_s": round(time.monotonic() - f.received_at, 2),
                    }
                    for sid, f in self.images.items()
                },
                "channels": {
                    key: {
                        "stream": ch.stream_id,
                        "name": ch.name,
                        "unit": ch.unit,
                        "n": len(ch.values),
                        "last": ch.values[-1] if ch.values else None,
                    }
                    for key, ch in self.channels.items()
                },
                "gaze": dict(self.gaze_points),
                "health": {k: _jsonable(v) for k, v in self.health.items()},
                "events": [
                    {
                        "kind": e.kind,
                        "name": e.name,
                        "t_s": None
                        if e.timestamp_source == "host_only"
                        else round((e.device_timestamp_ns - t0) / 1e9, 3),
                        "timestamp_source": e.timestamp_source,
                        "payload": _jsonable(e.payload),
                    }
                    for e in list(self.events)[-50:]
                ],
            }
        if self._stats_provider is not None:
            try:
                out["streams"] = {k: _jsonable(v) for k, v in self._stats_provider().items()}
            except Exception:
                out["streams"] = {}
        if self._proc_provider is not None:
            try:
                out["processors"] = {k: _jsonable(v) for k, v in self._proc_provider().items()}
            except Exception:
                out["processors"] = {}
        if self._bus_provider is not None:
            try:
                out["app_queues"] = {k: _jsonable(v) for k, v in self._bus_provider().items()}
            except Exception:
                out["app_queues"] = {}
        return out

    def trajectory(self, stream_id: str, last_s: float | None = None) -> list[list[float]]:
        """``[[x, y], …]`` of a pose stream's translation (for the trajectory plot)."""
        with self._lock:
            xs = self.channels.get(f"{stream_id}/x")
            ys = self.channels.get(f"{stream_id}/y")
            if xs is None or ys is None:
                return []
            cutoff = None
            if last_s is not None and self.last_device_ns is not None:
                cutoff = self.last_device_ns - int(last_s * 1e9)
            pts = [
                [x, y]
                for t, x, y in zip(xs.t_ns, xs.values, ys.values, strict=False)
                if cutoff is None or t >= cutoff
            ]
            return pts

    def pose_streams(self) -> list[str]:
        """Streams whose payload is a pose (``vio``, ``vio_high_frequency``…)."""
        with self._lock:
            return sorted(self._pose_streams)

    def series(
        self, keys: Iterable[str], last_s: float | None = None
    ) -> dict[str, list[list[float]]]:
        """Chart data for the given channel keys (seconds relative to ``t0``)."""
        with self._lock:
            t0 = self.t0_ns or 0
            now_s = ((self.last_device_ns or t0) - t0) / 1e9
            cutoff = None if last_s is None else now_s - last_s
            out: dict[str, list[list[float]]] = {}
            for key in keys:
                ch = self.channels.get(key)
                if ch is None:
                    continue
                pts = ch.points(t0)
                if cutoff is not None:
                    pts = [p for p in pts if p[0] >= cutoff]
                out[key] = pts
            return out

    def window(self, last_s: float) -> tuple[float, float]:
        """``(start, end)`` in seconds since ``t0`` of the most recent ``last_s`` seconds."""
        with self._lock:
            t0 = self.t0_ns or 0
            now_s = ((self.last_device_ns or t0) - t0) / 1e9
            return (max(0.0, now_s - last_s), max(now_s, last_s))

    def image(self, stream_id: str) -> ImageFrame | None:  # noqa: D102
        with self._lock:
            return self.images.get(stream_id)

    def image_streams(self) -> list[str]:  # noqa: D102
        with self._lock:
            return sorted(self.images)

    def channel_keys(self) -> list[str]:  # noqa: D102
        with self._lock:
            return sorted(self.channels)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _jsonable(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, deque)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "as_dict"):
        return _jsonable(v.as_dict())
    if hasattr(v, "__dict__"):
        return {k: _jsonable(x) for k, x in vars(v).items() if not k.startswith("_")}
    return str(v)


@dataclass
class ReplayView:
    """What the replay controls show (filled by the app from ``ReplayControl``)."""

    paused: bool = False
    speed: float = 1.0
    position_s: float | None = None
    range_s: tuple[float, float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
