"""Plain payload types carried in :attr:`Sample.data`.

They are deliberately small, SDK-free dataclasses (or bare ``numpy.ndarray`` for images) so
that processors, recorders, the dashboard and the tests work identically for live, replay and
mock data. The Aria adapter converts ``projectaria_tools`` objects into these at the boundary.

Units follow the SDK: metres, seconds, radians, m/s², rad/s, tesla, Pascal, °C.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

ImagePayload = NDArray[np.uint8]
"""HxW (mono) or HxWx3 (RGB, channel order RGB) ``uint8`` array."""


@dataclass(frozen=True, slots=True)
class ImuPayload:
    """One IMU sample.

    Attributes:
        accel_msec2: Linear acceleration (x, y, z) in m/s².
        gyro_radsec: Angular velocity (x, y, z) in rad/s.
        temperature_c: Sensor temperature in °C when available.
    """

    accel_msec2: tuple[float, float, float]
    gyro_radsec: tuple[float, float, float]
    temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class MagnetometerPayload:
    """Magnetic field (x, y, z) in tesla."""

    mag_tesla: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class BarometerPayload:
    """Barometric pressure in Pascal and temperature in °C."""

    pressure_pa: float
    temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class GazePayload:
    """Eye gaze in the Central Pupil Frame (CPF), as produced on-device.

    Attributes:
        yaw_rad: Horizontal gaze angle.
        pitch_rad: Vertical gaze angle.
        depth_m: Estimated fixation depth (metres); may be ``nan`` when invalid.
        valid: Whether the combined gaze is valid.
        yaw_low_rad, yaw_high_rad, pitch_low_rad, pitch_high_rad: Uncertainty bounds.
    """

    yaw_rad: float
    pitch_rad: float
    depth_m: float
    valid: bool = True
    yaw_low_rad: float | None = None
    yaw_high_rad: float | None = None
    pitch_low_rad: float | None = None
    pitch_high_rad: float | None = None


@dataclass(frozen=True, slots=True)
class PosePayload:
    """A rigid transform (translation + unit quaternion) with optional velocities.

    Attributes:
        translation_m: (x, y, z) in metres, odometry frame.
        quaternion_wxyz: Rotation as (w, x, y, z).
        linear_velocity_msec: Optional (x, y, z) m/s.
        angular_velocity_radsec: Optional (x, y, z) rad/s.
        quality: Optional tracking quality in [0, 1].
    """

    translation_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]
    linear_velocity_msec: tuple[float, float, float] | None = None
    angular_velocity_radsec: tuple[float, float, float] | None = None
    quality: float | None = None


@dataclass(frozen=True, slots=True)
class HandPayload:
    """Hand tracking result: one 21x3 landmark array per visible hand, device frame, metres."""

    left_landmarks_m: NDArray[np.float32] | None
    right_landmarks_m: NDArray[np.float32] | None
    left_confidence: float | None = None
    right_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AudioPayload:
    """A chunk of multi-channel audio.

    Attributes:
        samples: ``(n_samples, n_channels)`` array (int16 or float32).
        sample_rate_hz: Sampling rate.
        timestamps_ns: Device timestamp of every sample (``n_samples``), when provided.
    """

    samples: NDArray[np.int16] | NDArray[np.float32]
    sample_rate_hz: int
    timestamps_ns: NDArray[np.int64] | None = None


@dataclass(frozen=True, slots=True)
class ScalarPayload:
    """A single named scalar (barometer-like sensors, derived indicators, ALS…)."""

    value: float
    unit: str = ""


@dataclass(frozen=True, slots=True)
class GpsPayload:
    """GNSS fix."""

    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    accuracy_m: float | None = None
    speed_msec: float | None = None
    utc_time_ms: int | None = None
