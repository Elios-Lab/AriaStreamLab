"""Payload ↔ table-row conversion and the Arrow schemas of every tabular stream kind.

Every tabular file starts with the same three columns so any stream can be joined on time:
``device_timestamp_ns`` (int64), ``host_timestamp_ns`` (int64) and ``sequence_number``
(int64, −1 when unknown). Images are not tabular: they get an index file with the same three
columns plus ``frame_index`` / ``file`` (see :mod:`ariastreamlab.storage.writers`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa

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
from ariastreamlab.core.sample import AnySample, Sample

TIME_COLUMNS = [
    pa.field("device_timestamp_ns", pa.int64(), nullable=False),
    pa.field("host_timestamp_ns", pa.int64(), nullable=False),
    pa.field("sequence_number", pa.int64(), nullable=False),
]

_F = pa.float64()
_F32 = pa.float32()


def _schema(*fields: pa.Field) -> pa.Schema:
    return pa.schema([*TIME_COLUMNS, *fields])


SCHEMAS: dict[str, pa.Schema] = {
    "imu": _schema(
        pa.field("accel_x", _F),
        pa.field("accel_y", _F),
        pa.field("accel_z", _F),
        pa.field("gyro_x", _F),
        pa.field("gyro_y", _F),
        pa.field("gyro_z", _F),
        pa.field("temperature_c", _F),
    ),
    "mag": _schema(pa.field("mag_x", _F), pa.field("mag_y", _F), pa.field("mag_z", _F)),
    "baro": _schema(pa.field("pressure_pa", _F), pa.field("temperature_c", _F)),
    "gaze": _schema(
        pa.field("yaw_rad", _F),
        pa.field("pitch_rad", _F),
        pa.field("depth_m", _F),
        pa.field("valid", pa.bool_()),
        pa.field("yaw_low_rad", _F),
        pa.field("yaw_high_rad", _F),
        pa.field("pitch_low_rad", _F),
        pa.field("pitch_high_rad", _F),
    ),
    "pose": _schema(
        pa.field("tx_m", _F),
        pa.field("ty_m", _F),
        pa.field("tz_m", _F),
        pa.field("qw", _F),
        pa.field("qx", _F),
        pa.field("qy", _F),
        pa.field("qz", _F),
        pa.field("vx_msec", _F),
        pa.field("vy_msec", _F),
        pa.field("vz_msec", _F),
        pa.field("wx_radsec", _F),
        pa.field("wy_radsec", _F),
        pa.field("wz_radsec", _F),
        pa.field("quality", _F),
    ),
    "hand": _schema(
        pa.field("left_landmarks_m", pa.list_(_F32)),
        pa.field("right_landmarks_m", pa.list_(_F32)),
        pa.field("left_confidence", _F),
        pa.field("right_confidence", _F),
    ),
    "audio": _schema(
        pa.field("sample_rate_hz", pa.int32()),
        pa.field("n_samples", pa.int32()),
        pa.field("n_channels", pa.int32()),
        pa.field("dtype", pa.string()),
        pa.field("samples", pa.binary()),
        pa.field("timestamps_ns", pa.binary()),
    ),
    "scalar": _schema(pa.field("value", _F), pa.field("unit", pa.string())),
    "gps": _schema(
        pa.field("latitude_deg", _F),
        pa.field("longitude_deg", _F),
        pa.field("altitude_m", _F),
        pa.field("accuracy_m", _F),
        pa.field("speed_msec", _F),
        pa.field("utc_time_ms", pa.int64()),
    ),
    "dict": _schema(pa.field("json", pa.string())),
}
"""Arrow schema per payload kind (see ``StreamDescriptor.payload``)."""


def payload_kind_of(data: Any) -> str:
    """Infer the payload kind of an object carried in ``Sample.data``."""
    if isinstance(data, np.ndarray) and data.dtype == np.uint8 and data.ndim in (2, 3):
        return "image"
    if isinstance(data, ImuPayload):
        return "imu"
    if isinstance(data, MagnetometerPayload):
        return "mag"
    if isinstance(data, BarometerPayload):
        return "baro"
    if isinstance(data, GazePayload):
        return "gaze"
    if isinstance(data, PosePayload):
        return "pose"
    if isinstance(data, HandPayload):
        return "hand"
    if isinstance(data, AudioPayload):
        return "audio"
    if isinstance(data, ScalarPayload):
        return "scalar"
    if isinstance(data, GpsPayload):
        return "gps"
    if isinstance(data, (int, float, np.integer, np.floating)) and not isinstance(data, bool):
        return "scalar"
    return "dict"


def _nan(v: float | None) -> float:
    return math.nan if v is None else float(v)


def _opt3(v: tuple[float, float, float] | None) -> tuple[float, float, float]:
    return (math.nan, math.nan, math.nan) if v is None else (float(v[0]), float(v[1]), float(v[2]))


def _time_fields(sample: AnySample) -> dict[str, Any]:
    if isinstance(sample, Sample):
        host = sample.host_receive_timestamp_ns
        seq = -1 if sample.sequence_number is None else int(sample.sequence_number)
    else:
        host = sample.processing_end_ns
        seq = -1
    return {
        "device_timestamp_ns": int(sample.device_timestamp_ns),
        "host_timestamp_ns": int(host),
        "sequence_number": seq,
    }


def sample_to_row(sample: AnySample, kind: str) -> dict[str, Any]:
    """Flatten a sample's payload into a row for ``SCHEMAS[kind]``."""
    row = _time_fields(sample)
    d = sample.data
    if kind == "imu":
        assert isinstance(d, ImuPayload)
        row.update(
            accel_x=d.accel_msec2[0],
            accel_y=d.accel_msec2[1],
            accel_z=d.accel_msec2[2],
            gyro_x=d.gyro_radsec[0],
            gyro_y=d.gyro_radsec[1],
            gyro_z=d.gyro_radsec[2],
            temperature_c=_nan(d.temperature_c),
        )
    elif kind == "mag":
        assert isinstance(d, MagnetometerPayload)
        row.update(mag_x=d.mag_tesla[0], mag_y=d.mag_tesla[1], mag_z=d.mag_tesla[2])
    elif kind == "baro":
        assert isinstance(d, BarometerPayload)
        row.update(pressure_pa=d.pressure_pa, temperature_c=_nan(d.temperature_c))
    elif kind == "gaze":
        assert isinstance(d, GazePayload)
        row.update(
            yaw_rad=d.yaw_rad,
            pitch_rad=d.pitch_rad,
            depth_m=d.depth_m,
            valid=d.valid,
            yaw_low_rad=_nan(d.yaw_low_rad),
            yaw_high_rad=_nan(d.yaw_high_rad),
            pitch_low_rad=_nan(d.pitch_low_rad),
            pitch_high_rad=_nan(d.pitch_high_rad),
        )
    elif kind == "pose":
        assert isinstance(d, PosePayload)
        v, w = _opt3(d.linear_velocity_msec), _opt3(d.angular_velocity_radsec)
        row.update(
            tx_m=d.translation_m[0],
            ty_m=d.translation_m[1],
            tz_m=d.translation_m[2],
            qw=d.quaternion_wxyz[0],
            qx=d.quaternion_wxyz[1],
            qy=d.quaternion_wxyz[2],
            qz=d.quaternion_wxyz[3],
            vx_msec=v[0],
            vy_msec=v[1],
            vz_msec=v[2],
            wx_radsec=w[0],
            wy_radsec=w[1],
            wz_radsec=w[2],
            quality=_nan(d.quality),
        )
    elif kind == "hand":
        assert isinstance(d, HandPayload)
        row.update(
            left_landmarks_m=None
            if d.left_landmarks_m is None
            else np.asarray(d.left_landmarks_m, dtype=np.float32).ravel().tolist(),
            right_landmarks_m=None
            if d.right_landmarks_m is None
            else np.asarray(d.right_landmarks_m, dtype=np.float32).ravel().tolist(),
            left_confidence=_nan(d.left_confidence),
            right_confidence=_nan(d.right_confidence),
        )
    elif kind == "audio":
        assert isinstance(d, AudioPayload)
        samples = np.ascontiguousarray(d.samples)
        row.update(
            sample_rate_hz=int(d.sample_rate_hz),
            n_samples=int(samples.shape[0]),
            n_channels=int(samples.shape[1]) if samples.ndim == 2 else 1,
            dtype=str(samples.dtype),
            samples=samples.tobytes(),
            timestamps_ns=b""
            if d.timestamps_ns is None
            else np.ascontiguousarray(d.timestamps_ns, dtype=np.int64).tobytes(),
        )
    elif kind == "scalar":
        if isinstance(d, ScalarPayload):
            row.update(value=float(d.value), unit=d.unit)
        else:
            row.update(value=float(d), unit="")
    elif kind == "gps":
        assert isinstance(d, GpsPayload)
        row.update(
            latitude_deg=d.latitude_deg,
            longitude_deg=d.longitude_deg,
            altitude_m=d.altitude_m,
            accuracy_m=_nan(d.accuracy_m),
            speed_msec=_nan(d.speed_msec),
            utc_time_ms=-1 if d.utc_time_ms is None else int(d.utc_time_ms),
        )
    elif kind == "dict":
        row.update(json=json.dumps(d, default=_json_default, sort_keys=True))
    else:
        raise ValueError(f"no tabular schema for payload kind {kind!r}")
    return row


def _json_default(o: Any) -> Any:
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.integer, np.floating)):
        return o.item()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def _none_if_nan(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def row_to_payload(row: Mapping[str, Any], kind: str) -> Any:
    """Inverse of :func:`sample_to_row` (used by readers and session replay)."""
    if kind == "imu":
        return ImuPayload(
            (row["accel_x"], row["accel_y"], row["accel_z"]),
            (row["gyro_x"], row["gyro_y"], row["gyro_z"]),
            _none_if_nan(row["temperature_c"]),
        )
    if kind == "mag":
        return MagnetometerPayload((row["mag_x"], row["mag_y"], row["mag_z"]))
    if kind == "baro":
        return BarometerPayload(row["pressure_pa"], _none_if_nan(row["temperature_c"]))
    if kind == "gaze":
        return GazePayload(
            row["yaw_rad"],
            row["pitch_rad"],
            row["depth_m"],
            bool(row["valid"]),
            _none_if_nan(row["yaw_low_rad"]),
            _none_if_nan(row["yaw_high_rad"]),
            _none_if_nan(row["pitch_low_rad"]),
            _none_if_nan(row["pitch_high_rad"]),
        )
    if kind == "pose":
        v = (row["vx_msec"], row["vy_msec"], row["vz_msec"])
        w = (row["wx_radsec"], row["wy_radsec"], row["wz_radsec"])
        return PosePayload(
            (row["tx_m"], row["ty_m"], row["tz_m"]),
            (row["qw"], row["qx"], row["qy"], row["qz"]),
            None if any(math.isnan(x) for x in v) else v,
            None if any(math.isnan(x) for x in w) else w,
            _none_if_nan(row["quality"]),
        )
    if kind == "hand":
        left = row.get("left_landmarks_m")
        right = row.get("right_landmarks_m")
        return HandPayload(
            None if left is None else np.asarray(left, dtype=np.float32).reshape(-1, 3),
            None if right is None else np.asarray(right, dtype=np.float32).reshape(-1, 3),
            _none_if_nan(row["left_confidence"]),
            _none_if_nan(row["right_confidence"]),
        )
    if kind == "audio":
        n, c = int(row["n_samples"]), int(row["n_channels"])
        samples = (
            np.frombuffer(row["samples"], dtype=np.dtype(row["dtype"])).reshape(n, c)
            if c > 1
            else np.frombuffer(row["samples"], dtype=np.dtype(row["dtype"]))
        )
        ts_raw = row["timestamps_ns"]
        ts = None if not ts_raw else np.frombuffer(ts_raw, dtype=np.int64)
        return AudioPayload(samples, int(row["sample_rate_hz"]), ts)
    if kind == "scalar":
        return ScalarPayload(row["value"], row.get("unit") or "")
    if kind == "gps":
        utc = row["utc_time_ms"]
        return GpsPayload(
            row["latitude_deg"],
            row["longitude_deg"],
            row["altitude_m"],
            _none_if_nan(row["accuracy_m"]),
            _none_if_nan(row["speed_msec"]),
            None if utc is None or utc < 0 else int(utc),
        )
    if kind == "dict":
        return json.loads(row["json"])
    raise ValueError(f"no tabular schema for payload kind {kind!r}")


def rows_to_table(rows: list[dict[str, Any]], kind: str) -> pa.Table:
    """Build an Arrow table for ``kind`` from rows produced by :func:`sample_to_row`."""
    schema = SCHEMAS[kind]
    columns = {f.name: [r.get(f.name) for r in rows] for f in schema}
    return pa.table(columns, schema=schema)


__all__ = [
    "SCHEMAS",
    "TIME_COLUMNS",
    "payload_kind_of",
    "row_to_payload",
    "rows_to_table",
    "sample_to_row",
]
