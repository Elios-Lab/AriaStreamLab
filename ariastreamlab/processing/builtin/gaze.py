"""Eye-gaze processors.

The on-device gaze is a (yaw, pitch, depth) triple in the Central Pupil Frame (CPF). To draw it
on the RGB image we need two things from the device calibration (available as a plain dict in
``ctx.calibration``, written by the adapter without the SDK being needed here):

* ``transform_device_cpf`` — 4x4 pose of the CPF in the device frame;
* ``cameras["camera-rgb"]`` — ``transform_device_camera`` and the Fisheye624 projection
  parameters ``[f, cx, cy, k0..k5, p0, p1, s0..s3]``.

The projection below is a NumPy re-implementation of projectaria_tools' Fisheye624 model and
matches ``CameraCalibration.project()`` to 1e-12 px (tested in ``tests/integration``).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ariastreamlab.core.payloads import GazePayload
from ariastreamlab.processing.base import Emit, Inputs, OutputSpec, Processor

__all__ = ["ProjectToRgb", "fisheye624_project", "gaze_point_cpf"]


def gaze_point_cpf(yaw_rad: float, pitch_rad: float, depth_m: float) -> np.ndarray:
    """The 3-D point the gaze ray reaches at ``depth_m`` (CPF; same formula as the SDK)."""
    v = np.array([math.tan(yaw_rad), math.tan(pitch_rad), 1.0], dtype=float)
    v /= np.linalg.norm(v)
    return v * depth_m


def fisheye624_project(point_camera: np.ndarray, params: np.ndarray) -> np.ndarray | None:
    """Project a point in the camera frame (metres) to pixels; ``None`` when behind the camera."""
    x, y, z = (float(v) for v in point_camera[:3])
    if z <= 1e-9:
        return None
    f, cx, cy = params[0], params[1], params[2]
    k = params[3:9]
    p0, p1 = params[9], params[10]
    s = params[11:15]
    a, b = x / z, y / z
    r = math.hypot(a, b)
    th = math.atan(r)
    th2 = th * th
    radial, t = 1.0, th2
    for ki in k:
        radial += ki * t
        t *= th2
    th_div_r = 1.0 if r < 1e-8 else th / r
    xr, yr = radial * th_div_r * a, radial * th_div_r * b
    n2 = xr * xr + yr * yr
    tx = p0 * (n2 + 2 * xr * xr) + 2 * p1 * xr * yr
    ty = 2 * p0 * xr * yr + p1 * (n2 + 2 * yr * yr)
    px = xr + tx + s[0] * n2 + s[1] * n2 * n2
    py = yr + ty + s[2] * n2 + s[3] * n2 * n2
    return np.array([f * px + cx, f * py + cy], dtype=float)


class ProjectToRgb(Processor):
    """Gaze (CPF yaw/pitch/depth) → pixel coordinates in the RGB camera.

    Inputs: ``["rgb", "eye_gaze"]`` (the frame is primary so one output per frame) or just
    ``["eye_gaze"]`` (one output per gaze sample, no frame needed). Output payload: a dict with
    ``x``, ``y`` (pixels in the full-resolution RGB image), ``x_norm``, ``y_norm`` (0–1),
    ``depth_m``, ``valid`` and ``in_image``.
    """

    version = "1"
    description = "Project the on-device eye gaze onto the RGB image (needs calibration)"
    outputs = {
        "*": OutputSpec(
            payload="dict",
            units={"x": "px", "y": "px", "depth_m": "m"},
            description="gaze point in RGB pixel coordinates",
        )
    }

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        camera: str = "camera-rgb"
        default_depth_m: float = Field(
            default=1.0, gt=0, description="used when the gaze depth is missing or invalid"
        )
        gaze_input: str | None = Field(
            default=None, description="input stream carrying GazePayload (default: auto-detect)"
        )
        emit_invalid: bool = Field(default=True, description="emit rows with valid=false too")

    def setup(self, ctx: Any) -> None:  # noqa: D102
        super().setup(ctx)
        self._ready = False
        self._warned = False
        if ctx.calibration:
            self.on_calibration(ctx.calibration)

    def on_calibration(self, calibration: dict[str, Any]) -> None:  # noqa: D102
        super().on_calibration(calibration)
        cam = (calibration.get("cameras") or {}).get(self.params.camera)
        t_dev_cpf = calibration.get("transform_device_cpf")
        if not cam or t_dev_cpf is None or not cam.get("projection_params"):
            self._ready = False
            return
        self._params = np.asarray(cam["projection_params"], dtype=float)
        self._size = tuple(int(v) for v in cam["image_size"])
        t_dev_cam = np.asarray(cam["transform_device_camera"], dtype=float)
        self._t_cam_cpf = np.linalg.inv(t_dev_cam) @ np.asarray(t_dev_cpf, dtype=float)
        self._ready = True
        self.ctx.log.info("gaze.calibration_ready", camera=self.params.camera, size=self._size)

    def process(self, inputs: Inputs) -> Emit | None:  # noqa: D102
        gaze = self._gaze(inputs)
        if gaze is None:
            return None
        if not self._ready:
            if not self._warned:
                self._warned = True
                self.ctx.log.warning(
                    "gaze.no_calibration",
                    note="waiting for device_calibration (live: a few seconds after start)",
                )
            return None
        depth = gaze.depth_m
        if depth is None or not math.isfinite(depth) or depth <= 0:
            depth = self.params.default_depth_m
        p_cpf = np.append(gaze_point_cpf(gaze.yaw_rad, gaze.pitch_rad, depth), 1.0)
        p_cam = self._t_cam_cpf @ p_cpf
        uv = fisheye624_project(p_cam, self._params)
        valid = bool(gaze.valid) and uv is not None
        if not valid and not self.params.emit_invalid:
            return None
        w, h = self._size
        x, y = (float("nan"), float("nan")) if uv is None else (float(uv[0]), float(uv[1]))
        in_image = uv is not None and 0 <= x < w and 0 <= y < h
        gaze_sample = inputs[self._gaze_stream]
        return Emit(
            self.ctx.outputs[0],
            {
                "x": x,
                "y": y,
                "x_norm": x / w,
                "y_norm": y / h,
                "depth_m": float(depth),
                "valid": valid,
                "in_image": bool(in_image),
                "gaze_timestamp_ns": gaze_sample.device_timestamp_ns,
                "gaze_dt_ns": inputs.dt_ns.get(self._gaze_stream, 0),
            },
        )

    def _gaze(self, inputs: Inputs) -> GazePayload | None:
        if not hasattr(self, "_gaze_stream"):
            wanted = self.params.gaze_input
            for sid, s in inputs.items():
                if (wanted is None or sid == wanted) and isinstance(s.data, GazePayload):
                    self._gaze_stream = sid
                    break
            else:
                raise TypeError(
                    "no input carries a GazePayload; inputs must include eye_gaze "
                    f"(got {list(inputs)})"
                )
        d = inputs[self._gaze_stream].data
        return d if isinstance(d, GazePayload) else None
