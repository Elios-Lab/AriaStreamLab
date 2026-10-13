"""``projectaria_tools`` objects → AriaStreamLab payloads.

Shared by the VRS replay sources (Phase 2) and the live adapter (Phase 4), so live and replayed
samples are built by the same code. Everything here is a pure function of the SDK object;
timestamps are converted with integer arithmetic only.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

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
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.timing.convert import timedelta_to_ns

CAMERA_ID_TO_STREAM: dict[int, str] = {
    1: "slam_front_left",
    2: "slam_front_right",
    4: "slam_side_left",
    8: "slam_side_right",
    16: "et_left",
    32: "et_right",
    64: "rgb",
}
"""``ImageDataRecord.camera_id`` → stream id (values from Meta's ``StreamReceiver``)."""

LABEL_TO_STREAM: dict[str, str] = {d.native_label: d.id for d in REGISTRY}
LABEL_TO_STREAM.update(
    {
        "imu-left": "imu_left",
        "imu-right": "imu_right",
        "mag0": "magnetometer",
        "baro0": "barometer",
        "mic": "audio",
        "eyegaze": "eye_gaze",
        "handtracking": "hand_tracking",
        "temperature": "temperature",
        "als": "als",
    }
)


def _t3(v: Any) -> tuple[float, float, float]:
    a = np.asarray(v, dtype=np.float64).ravel()
    return (float(a[0]), float(a[1]), float(a[2]))


def _quat_wxyz(rotation: Any) -> tuple[float, float, float, float]:
    """projectaria_tools ``SO3.to_quat()`` returns ``[[w, x, y, z]]``."""
    q = np.asarray(rotation.to_quat(), dtype=np.float64).ravel()
    return (float(q[0]), float(q[1]), float(q[2]), float(q[3]))


def image_to_payload(image_data: Any) -> np.ndarray:
    """Decoded ``ImageData`` → ``uint8`` array (HxW mono or HxWx3 RGB), no copy when possible."""
    arr = image_data.to_numpy_array()
    if arr.ndim == 3 and arr.shape[2] == 4:  # RGBA from some decoders
        arr = np.ascontiguousarray(arr[:, :, :3])
    out: np.ndarray = arr if arr.dtype == np.uint8 else arr.astype(np.uint8)
    return out


def image_metadata(record: Any) -> dict[str, Any]:
    """Exposure/gain/frame info of an ``ImageDataRecord``."""
    return {
        "camera_id": int(record.camera_id),
        "frame_number": int(record.frame_number),
        "exposure_s": float(record.exposure_duration),
        "gain": float(record.gain),
        "arrival_timestamp_ns": int(record.arrival_timestamp_ns),
    }


def imu_to_payload(m: Any) -> ImuPayload:
    """``MotionData`` → :class:`ImuPayload`."""
    temp = float(m.temperature) if getattr(m, "temperature", None) is not None else None
    return ImuPayload(_t3(m.accel_msec2), _t3(m.gyro_radsec), temp)


def mag_to_payload(m: Any) -> MagnetometerPayload:
    """``MotionData`` (magnetometer) → :class:`MagnetometerPayload`."""
    return MagnetometerPayload(_t3(m.mag_tesla))


def baro_to_payload(b: Any) -> BarometerPayload:
    """``BarometerData`` → :class:`BarometerPayload`."""
    return BarometerPayload(
        float(b.pressure), float(b.temperature) if b.temperature is not None else None
    )


def gaze_to_payload(g: Any) -> GazePayload:
    """``EyeGaze`` → :class:`GazePayload`."""
    return GazePayload(
        float(g.yaw),
        float(g.pitch),
        float(g.depth),
        bool(g.combined_gaze_valid),
        float(g.yaw_low),
        float(g.yaw_high),
        float(g.pitch_low),
        float(g.pitch_high),
    )


def gaze_timestamp_ns(g: Any) -> int:
    """Device timestamp of an ``EyeGaze`` (``tracking_timestamp`` is a timedelta)."""
    return timedelta_to_ns(g.tracking_timestamp)


def vio_to_payload(v: Any) -> PosePayload:
    """``FrontendOutput`` (VIO) → :class:`PosePayload` (odometry ← body IMU)."""
    tf = v.transform_odometry_bodyimu
    quality = None
    pq = getattr(v, "pose_quality", None)
    if pq is not None:
        name = getattr(pq, "name", str(pq)).upper()
        quality = {"GOOD": 1.0, "BAD": 0.0, "UNKNOWN": math.nan}.get(name.split(".")[-1], math.nan)
    return PosePayload(
        _t3(tf.translation()),
        _quat_wxyz(tf.rotation()),
        _t3(v.linear_velocity_in_odometry),
        _t3(v.angular_velocity_in_bodyimu),
        quality,
    )


def vio_metadata(v: Any) -> dict[str, Any]:
    """Status / quality flags of a VIO result."""
    return {
        "status": str(getattr(v, "status", "")),
        "pose_quality": str(getattr(v, "pose_quality", "")),
        "visual_tracking_quality": str(getattr(v, "visual_tracking_quality", "")),
        "frame_id": int(getattr(v, "frame_id", -1)),
    }


def vio_hf_to_payload(p: Any) -> PosePayload:
    """``OpenLoopTrajectoryPose`` → :class:`PosePayload` (odometry ← device)."""
    tf = p.transform_odometry_device
    return PosePayload(
        _t3(tf.translation()),
        _quat_wxyz(tf.rotation()),
        _t3(p.device_linear_velocity_odometry),
        _t3(p.angular_velocity_device),
        float(p.quality_score),
    )


def vio_hf_timestamp_ns(p: Any) -> int:
    """Device timestamp of an ``OpenLoopTrajectoryPose``."""
    return timedelta_to_ns(p.tracking_timestamp)


def hand_to_payload(h: Any) -> HandPayload:
    """``HandTrackingResult`` → :class:`HandPayload` (21×3 landmarks per hand, device frame)."""

    def lm(hand: Any) -> np.ndarray | None:
        if hand is None:
            return None
        return np.asarray(hand.landmark_positions_device, dtype=np.float32).reshape(-1, 3)

    def conf(hand: Any) -> float | None:
        return None if hand is None else float(hand.confidence)

    return HandPayload(lm(h.left_hand), lm(h.right_hand), conf(h.left_hand), conf(h.right_hand))


def hand_timestamp_ns(h: Any) -> int:
    """Device timestamp of a ``HandTrackingResult``."""
    return timedelta_to_ns(h.tracking_timestamp)


def audio_to_payload(
    audio_data: Any, record: Any, num_channels: int | None = None, sample_rate_hz: int = 16000
) -> AudioPayload:
    """``AudioData`` + ``AudioDataRecord`` → :class:`AudioPayload` (``n_samples × n_channels``)."""
    raw = np.asarray(audio_data.data)
    ts = np.asarray(record.capture_timestamps_ns, dtype=np.int64)
    n = len(ts) if len(ts) else 0
    ch = int(num_channels) if num_channels else (int(raw.size // n) if n else 1)
    samples = raw.reshape(n, ch) if n and raw.size == n * ch else raw.reshape(-1, max(ch, 1))
    if samples.dtype != np.int16:
        samples = np.clip(samples, -32768, 32767).astype(np.int16)
    return AudioPayload(samples, int(sample_rate_hz), ts if n else None)


def audio_timestamp_ns(record: Any) -> int:
    """Device timestamp of the first sample of an audio chunk."""
    ts = record.capture_timestamps_ns
    return int(ts[0]) if len(ts) else 0


def ppg_to_payload(p: Any) -> ScalarPayload:
    """``PpgData`` → :class:`ScalarPayload`."""
    return ScalarPayload(float(p.value), "raw")


def als_to_payload(a: Any) -> ScalarPayload:
    """``AlsData`` → lux as the scalar (full channel set goes to metadata)."""
    return ScalarPayload(float(a.lux), "lux")


def als_metadata(a: Any) -> dict[str, Any]:
    """The other ALS channels."""
    keys = (
        "cct",
        "red_channel_normalized",
        "green_channel_normalized",
        "blue_channel_normalized",
        "ir_channel_normalized",
        "uv_channel_normalized",
        "exposure_time_us",
    )
    return {k: float(getattr(a, k)) for k in keys if hasattr(a, k)}


def temperature_to_payload(t: Any) -> ScalarPayload:
    """``TemperatureData`` → °C."""
    return ScalarPayload(float(t.temperature_celsius), "degC")


def gps_to_payload(g: Any) -> GpsPayload:
    """``GpsData`` → :class:`GpsPayload`."""
    return GpsPayload(
        float(g.latitude),
        float(g.longitude),
        float(g.altitude),
        float(g.accuracy),
        float(g.speed),
        int(g.utc_time_ms) if getattr(g, "utc_time_ms", None) is not None else None,
    )


def calibration_to_dict(calib: Any) -> dict[str, Any]:
    """``DeviceCalibration`` → JSON-able summary (per-camera intrinsics + labels)."""
    out: dict[str, Any] = {"labels": list(calib.get_all_labels()), "cameras": {}}
    try:
        out["device_subtype"] = str(calib.get_device_subtype())
        out["origin_label"] = str(calib.get_origin_label())
    except Exception:
        pass
    for label in calib.get_camera_labels():
        cam = calib.get_camera_calib(label)
        if cam is None:
            continue
        try:
            out["cameras"][label] = {
                "image_size": [int(x) for x in cam.get_image_size()],
                "focal_lengths": [float(x) for x in cam.get_focal_lengths()],
                "principal_point": [float(x) for x in cam.get_principal_point()],
                "model": str(cam.get_model_name()) if hasattr(cam, "get_model_name") else None,
                "projection_params": [float(x) for x in cam.projection_params()]
                if hasattr(cam, "projection_params")
                else None,
                "transform_device_camera": np.asarray(
                    cam.get_transform_device_camera().to_matrix(), dtype=float
                ).tolist(),
            }
        except Exception as exc:
            out["cameras"][label] = {"error": str(exc)}
    return out
