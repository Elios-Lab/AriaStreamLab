"""IMU processors (:class:`~ariastreamlab.core.payloads.ImuPayload` in)."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ariastreamlab.core.payloads import ImuPayload, ScalarPayload
from ariastreamlab.processing.base import Emit, Inputs, OutputSpec, Processor, ProcessorContext

_Vec = tuple[float, float, float]


def _imu(inputs: Inputs) -> ImuPayload:
    d = inputs.primary.data
    if not isinstance(d, ImuPayload):
        raise TypeError(f"expected ImuPayload, got {type(d).__name__}")
    return d


def _ema(prev: _Vec, new: _Vec, a: float) -> _Vec:
    return (
        prev[0] + a * (new[0] - prev[0]),
        prev[1] + a * (new[1] - prev[1]),
        prev[2] + a * (new[2] - prev[2]),
    )


def _norm(v: _Vec) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


class Lowpass(Processor):
    """First-order low-pass (exponential moving average) on accelerometer and gyroscope.

    ``cutoff_hz`` is turned into the EMA coefficient from the measured sample spacing, so the
    filter behaves the same whatever the IMU rate; ``alpha`` sets the coefficient directly.
    """

    version = "1"
    description = "Exponential low-pass filter on accelerometer and gyroscope"
    outputs = {"*": OutputSpec(payload="imu", units={"accel": "m/s^2", "gyro": "rad/s"})}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        cutoff_hz: float | None = Field(default=5.0, gt=0)
        alpha: float | None = Field(default=None, gt=0, le=1)

    def setup(self, ctx: ProcessorContext) -> None:  # noqa: D102
        super().setup(ctx)
        self._accel: _Vec | None = None
        self._gyro: _Vec | None = None
        self._last_ts: int | None = None

    def _alpha(self, ts: int) -> float:
        p = self.params
        if p.alpha is not None:
            return float(p.alpha)
        if self._last_ts is None or ts <= self._last_ts:
            return 1.0
        dt = (ts - self._last_ts) / 1e9
        rc = 1.0 / (2 * math.pi * p.cutoff_hz)
        return float(dt / (rc + dt))

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        imu = _imu(inputs)
        ts = inputs.primary.device_timestamp_ns
        a = self._alpha(ts)
        self._last_ts = ts
        if self._accel is None or self._gyro is None:
            accel, gyro = imu.accel_msec2, imu.gyro_radsec
        else:
            accel = _ema(self._accel, imu.accel_msec2, a)
            gyro = _ema(self._gyro, imu.gyro_radsec, a)
        self._accel, self._gyro = accel, gyro
        return Emit(self.ctx.outputs[0], ImuPayload(accel, gyro, imu.temperature_c))


class Magnitude(Processor):
    """Norm of the acceleration (or angular-velocity) vector as a scalar."""

    version = "1"
    description = "Norm of the accelerometer or gyroscope vector"
    outputs = {"*": OutputSpec(payload="scalar")}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        of: Literal["accel", "gyro"] = "accel"
        remove_gravity: bool = Field(
            default=False, description="accel only: subtract 9.80665 m/s² from the norm"
        )

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        imu = _imu(inputs)
        if self.params.of == "gyro":
            return Emit(self.ctx.outputs[0], ScalarPayload(_norm(imu.gyro_radsec), "rad/s"))
        n = _norm(imu.accel_msec2)
        if self.params.remove_gravity:
            n -= 9.80665
        return Emit(self.ctx.outputs[0], ScalarPayload(n, "m/s^2"))


class MotionDetector(Processor):
    """1.0 while the head moves, 0.0 while it rests — gyro norm over a window vs a threshold.

    The output is emitted on every input sample when ``emit: "always"`` (a dense indicator)
    or only on transitions when ``emit: "changes"``.
    """

    version = "2"
    description = "Head motion indicator from the gyroscope norm (with hysteresis)"
    outputs = {"*": OutputSpec(payload="scalar", units={"value": "moving (0/1)"})}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        threshold: float = Field(default=0.15, gt=0, description="rad/s on the smoothed gyro norm")
        release: float | None = Field(
            default=None, gt=0, description="rad/s to go back to rest (default 0.7 × threshold)"
        )
        window_ms: float = Field(default=100.0, gt=0, description="smoothing window")
        emit: Literal["always", "changes"] = "always"

    def setup(self, ctx: ProcessorContext) -> None:  # noqa: D102
        super().setup(ctx)
        self._moving = False
        self._smooth = 0.0
        self._last_ts: int | None = None
        self._release = self.params.release or 0.7 * self.params.threshold
        self._emitted_once = False

    def on_parameters_changed(self, old: Any, new: Any) -> None:  # noqa: D102
        del old
        self._release = new.release or 0.7 * new.threshold

    def process(self, inputs: Inputs) -> Emit | None:  # noqa: D102
        imu = _imu(inputs)
        ts = inputs.primary.device_timestamp_ns
        n = _norm(imu.gyro_radsec)
        if self._last_ts is None or ts <= self._last_ts:
            self._smooth = n
        else:
            dt_ms = (ts - self._last_ts) / 1e6
            a = min(1.0, dt_ms / self.params.window_ms)
            self._smooth += a * (n - self._smooth)
        self._last_ts = ts
        was = self._moving
        if not was and self._smooth > self.params.threshold:
            self._moving = True
        elif was and self._smooth < self._release:
            self._moving = False
        if self.params.emit == "changes" and self._moving == was and self._emitted_once:
            return None
        self._emitted_once = True
        return Emit(
            self.ctx.outputs[0],
            ScalarPayload(1.0 if self._moving else 0.0, "moving"),
            {"gyro_norm_radsec": self._smooth},
        )
