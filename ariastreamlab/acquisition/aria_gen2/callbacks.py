"""Bind SDK callbacks (``StreamDataInterface`` / ``StreamReceiver``) to the event bus.

Used by the SDK replay source now and by the live adapter in Phase 4, so both paths produce
identical samples. Callbacks run on SDK threads — several of them at once for the small
sensors (10–13 threads observed for baro/mag/ppg/audio) — so every piece of per-stream state
is guarded by a lock, and the callback bodies only timestamp, convert and enqueue.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from ariastreamlab.acquisition.aria_gen2 import convert as cv
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.timing.clock import HostClock

log = get_logger("ariastreamlab.acquisition.aria_gen2.callbacks")

IMU_LABELS = {"imu-left": "imu_left", "imu-right": "imu_right"}


class CallbackBinder:
    """Registers typed callbacks on an SDK handler and publishes :class:`Sample` objects.

    Args:
        bus: Destination bus (``raw.<stream_id>`` topics).
        provenance: Provenance of the produced samples.
        device_id: Device serial.
        streams: Stream ids to deliver (others are ignored, not registered when possible).
        clock: Host clock.
    """

    def __init__(
        self,
        bus: EventBus,
        provenance: Provenance,
        device_id: str,
        streams: set[str] | None = None,
        clock: HostClock | None = None,
    ) -> None:
        self._bus = bus
        self._prov = provenance
        self.device_id = device_id
        self._wanted = streams
        self._clock = clock or HostClock()
        self._lock = threading.Lock()
        self._seq: Counter[str] = Counter()
        self.received: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.calibration: dict[str, Any] | None = None

    # -- publishing -----------------------------------------------------------------
    def _emit(
        self,
        stream_id: str,
        device_ts: int,
        data: Any,
        metadata: dict[str, Any] | None = None,
        seq: int | None = None,
    ) -> None:
        # calibration is metadata every consumer needs; it is never filtered out
        if (
            self._wanted is not None
            and stream_id != "device_calibration"
            and stream_id not in self._wanted
        ):
            return
        host = self._clock.now_ns()
        with self._lock:
            if seq is None:
                seq = self._seq[stream_id]
            self._seq[stream_id] = seq + 1
            self.received[stream_id] += 1
        self._bus.publish(
            f"raw.{stream_id}",
            Sample(
                self.device_id,
                stream_id,
                int(device_ts),
                host,
                seq,
                data,
                self._prov,
                metadata or {},
            ),
        )

    def _guard(self, stream_id: str, fn: Callable[..., None]) -> Callable[..., None]:
        def wrapped(*args: Any, **kwargs: Any) -> None:
            try:
                fn(*args, **kwargs)
            except Exception:
                with self._lock:
                    self.errors[stream_id] += 1
                if self.errors[stream_id] <= 3:
                    log.exception("callback.failed", stream=stream_id)

        return wrapped

    def wants(self, stream_id: str) -> bool:
        """True when ``stream_id`` should be delivered."""
        return self._wanted is None or stream_id in self._wanted

    # -- registration ------------------------------------------------------------------
    def bind(self, handler: Any) -> None:
        """Register every relevant callback on ``handler`` (``register_*_callback`` API)."""

        def image(image_data: Any, record: Any) -> None:
            sid = cv.CAMERA_ID_TO_STREAM.get(int(record.camera_id))
            if sid is None or not self.wants(sid):
                return
            self._emit(
                sid,
                int(record.capture_timestamp_ns),
                cv.image_to_payload(image_data),
                cv.image_metadata(record),
                seq=int(record.frame_number),
            )

        if any(self.wants(s) for s in ("rgb",)):
            handler.register_rgb_callback(self._guard("rgb", image))
        if any(
            self.wants(s)
            for s in ("slam_front_left", "slam_front_right", "slam_side_left", "slam_side_right")
        ):
            handler.register_slam_callback(self._guard("slam", image))
        if any(self.wants(s) for s in ("et_left", "et_right")):
            handler.register_et_callback(self._guard("et", image))

        def imu(m: Any, label: str) -> None:
            sid = IMU_LABELS.get(label, label.replace("-", "_"))
            self._emit(sid, int(m.capture_timestamp_ns), cv.imu_to_payload(m))

        if self.wants("imu_left") or self.wants("imu_right"):
            handler.register_imu_callback(self._guard("imu", imu))
        if self.wants("magnetometer"):
            handler.register_magnetometer_callback(
                self._guard(
                    "magnetometer",
                    lambda m, _label: self._emit(
                        "magnetometer", int(m.capture_timestamp_ns), cv.mag_to_payload(m)
                    ),
                )
            )
        if self.wants("barometer"):
            handler.register_barometer_callback(
                self._guard(
                    "barometer",
                    lambda b: self._emit(
                        "barometer", int(b.capture_timestamp_ns), cv.baro_to_payload(b)
                    ),
                )
            )
        if self.wants("ppg"):
            handler.register_ppg_callback(
                self._guard(
                    "ppg",
                    lambda p: self._emit("ppg", int(p.capture_timestamp_ns), cv.ppg_to_payload(p)),
                )
            )
        if self.wants("gps"):
            handler.register_gps_callback(
                self._guard(
                    "gps",
                    lambda g: self._emit("gps", int(g.capture_timestamp_ns), cv.gps_to_payload(g)),
                )
            )
        if self.wants("audio"):
            handler.register_audio_callback(
                self._guard(
                    "audio",
                    lambda d, r, n: self._emit(
                        "audio", cv.audio_timestamp_ns(r), cv.audio_to_payload(d, r, n)
                    ),
                )
            )
        if self.wants("eye_gaze"):
            handler.register_eye_gaze_callback(
                self._guard(
                    "eye_gaze",
                    lambda g: self._emit(
                        "eye_gaze", cv.gaze_timestamp_ns(g), cv.gaze_to_payload(g)
                    ),
                )
            )
        if self.wants("hand_tracking"):
            handler.register_hand_pose_callback(
                self._guard(
                    "hand_tracking",
                    lambda h: self._emit(
                        "hand_tracking", cv.hand_timestamp_ns(h), cv.hand_to_payload(h)
                    ),
                )
            )
        if self.wants("vio"):
            handler.register_vio_callback(
                self._guard(
                    "vio",
                    lambda v: self._emit(
                        "vio", int(v.capture_timestamp_ns), cv.vio_to_payload(v), cv.vio_metadata(v)
                    ),
                )
            )
        if self.wants("vio_high_frequency"):
            handler.register_vio_high_frequency_callback(
                self._guard(
                    "vio_high_frequency",
                    lambda p: self._emit(
                        "vio_high_frequency", cv.vio_hf_timestamp_ns(p), cv.vio_hf_to_payload(p)
                    ),
                )
            )

        def calib(c: Any) -> None:
            d = cv.calibration_to_dict(c)
            self.calibration = d
            self._emit(
                "device_calibration",
                self._clock.now_ns(),
                d,
                {"note": "device timestamp unknown for calibration; host time used"},
            )

        handler.register_device_calib_callback(self._guard("device_calibration", calib))
