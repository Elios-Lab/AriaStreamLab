"""Parser for ``Device.get_data_quality_stats()`` (docs/ARCHITECTURE.md §0.7, §0.9).

Observed payload (SDK 2.5.1)::

    {"data_type": "GetSensorHealthStats",
     "data": {"sensor_health_stats_list": [
        {"sensor_type": "IMU_1", "expected_frame_count": 0, "actual_frame_count": 12345,
         "dropped_frame_count": 0, "early_time_error_count": 0, "late_time_error_count": 0,
         "total_time_error_count": 0, "score": 100.0, "sequential_drops": 0}, ...]}}

During streaming ``expected_frame_count`` is 0 for every sensor, so an expected count of 0
means *unknown*, never "100 % loss". Unknown fields are kept verbatim in ``raw``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SENSOR_TO_STREAMS", "QualityStats", "SensorQuality", "parse_quality_stats"]

#: Device sensor type → our stream ids (only where the mapping is certain).
SENSOR_TO_STREAMS: dict[str, tuple[str, ...]] = {
    "CAMERA_POV": ("rgb",),
    "CAMERA_SLAM_1": ("slam_front_left",),
    "CAMERA_SLAM_2": ("slam_front_right",),
    "CAMERA_SLAM_3": ("slam_side_left",),
    "CAMERA_SLAM_4": ("slam_side_right",),
    "IMU_1": ("imu_left", "imu_right"),  # which physical IMU is 1/2 is not documented
    "IMU_2": ("imu_left", "imu_right"),
    "MAG": ("magnetometer",),
    "BARO": ("barometer",),
    "PPG": ("ppg",),
    "AUDIO": ("audio",),
    "ET_RESULT": ("eye_gaze",),
    "HT_RESULT": ("hand_tracking",),
    "VIO_RESULT": ("vio",),
    "VIO_HIGH_FREQUENCY_POSE": ("vio_high_frequency",),
    "ALS": ("als",),
    "TEMPERATURE_ACROPOLIS": ("temperature",),
    "TEMPERATURE_SOC": ("temperature",),
}


@dataclass(frozen=True)
class SensorQuality:
    """One sensor's counters as the device reports them."""

    sensor_type: str
    actual: int | None
    expected: int | None
    dropped: int | None
    sequential_drops: int | None
    time_errors_early: int | None
    time_errors_late: int | None
    time_errors_total: int | None
    score: float | None
    streams: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def expected_known(self) -> bool:
        """``expected_frame_count`` is only filled after a session ends."""
        return bool(self.expected)

    @property
    def device_drop_fraction(self) -> float | None:
        """Dropped / (actual + dropped), when both are known."""
        if self.dropped is None or self.actual is None or self.actual + self.dropped == 0:
            return None
        return self.dropped / (self.actual + self.dropped)

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return {
            "sensor_type": self.sensor_type,
            "streams": list(self.streams),
            "actual": self.actual,
            "expected": self.expected,
            "dropped": self.dropped,
            "sequential_drops": self.sequential_drops,
            "time_errors": {
                "early": self.time_errors_early,
                "late": self.time_errors_late,
                "total": self.time_errors_total,
            },
            "score": self.score,
        }


@dataclass(frozen=True)
class QualityStats:
    """A parsed ``get_data_quality_stats()`` answer."""

    sensors: tuple[SensorQuality, ...]
    data_type: str | None
    raw_text: str
    parse_error: str | None = None

    def by_stream(self) -> dict[str, SensorQuality]:
        """First sensor record claiming each stream id."""
        out: dict[str, SensorQuality] = {}
        for s in self.sensors:
            for sid in s.streams:
                out.setdefault(sid, s)
        return out

    def total_device_dropped(self) -> int:
        """Sum of ``dropped_frame_count`` over all sensors."""
        return sum(s.dropped or 0 for s in self.sensors)

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return {
            "data_type": self.data_type,
            "parse_error": self.parse_error,
            "sensors": [s.as_dict() for s in self.sensors],
        }


def _int(d: dict[str, Any], key: str) -> int | None:
    v = d.get(key)
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def parse_quality_stats(payload: str | bytes | dict[str, Any] | None) -> QualityStats:
    """Parse the JSON text the device returns; never raises."""
    if payload is None:
        return QualityStats((), None, "", "empty payload")
    text = (
        payload
        if isinstance(payload, str)
        else (
            payload.decode("utf-8", "replace")
            if isinstance(payload, bytes)
            else json.dumps(payload)
        )
    )
    try:
        doc = json.loads(text) if isinstance(payload, (str, bytes)) else payload
    except json.JSONDecodeError as exc:
        return QualityStats((), None, text[:4000], f"not JSON: {exc}")
    if not isinstance(doc, dict):
        return QualityStats((), None, text[:4000], "top level is not an object")
    data = doc.get("data", doc)
    items = data.get("sensor_health_stats_list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return QualityStats((), doc.get("data_type"), text[:4000], "no sensor_health_stats_list")
    sensors: list[SensorQuality] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        st = str(item.get("sensor_type", "?"))
        score = item.get("score")
        sensors.append(
            SensorQuality(
                sensor_type=st,
                actual=_int(item, "actual_frame_count"),
                expected=_int(item, "expected_frame_count"),
                dropped=_int(item, "dropped_frame_count"),
                sequential_drops=_int(item, "sequential_drops"),
                time_errors_early=_int(item, "early_time_error_count"),
                time_errors_late=_int(item, "late_time_error_count"),
                time_errors_total=_int(item, "total_time_error_count"),
                score=None if score is None else float(score),
                streams=SENSOR_TO_STREAMS.get(st, ()),
                raw=dict(item),
            )
        )
    return QualityStats(tuple(sensors), doc.get("data_type"), text[:4000])
