"""Image processors (NumPy ``HxW`` or ``HxWx3`` uint8 in, same out)."""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ariastreamlab.processing.base import Emit, Inputs, OutputSpec, Processor

_INTERP = {
    "nearest": cv2.INTER_NEAREST,
    "linear": cv2.INTER_LINEAR,
    "area": cv2.INTER_AREA,
    "cubic": cv2.INTER_CUBIC,
}


def _image(inputs: Inputs) -> np.ndarray:
    img = inputs.primary.data
    if not isinstance(img, np.ndarray) or img.ndim not in (2, 3):
        raise TypeError(f"expected an HxW or HxWxC image, got {type(img).__name__}")
    return img


class Resize(Processor):
    """Resize a frame to ``width x height`` or by ``scale`` (OpenCV)."""

    version = "1"
    description = "Resize an image to width x height or by a scale factor"
    outputs = {"*": OutputSpec(payload="image", description="resized frame")}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        width: int | None = Field(default=None, ge=1)
        height: int | None = Field(default=None, ge=1)
        scale: float | None = Field(default=None, gt=0, le=1.0)
        interpolation: Literal["nearest", "linear", "area", "cubic"] = "area"

        @model_validator(mode="after")
        def _one_of(self) -> Resize.Parameters:
            if self.scale is None and (self.width is None or self.height is None):
                raise ValueError("give either 'scale' or both 'width' and 'height'")
            if self.scale is not None and (self.width is not None or self.height is not None):
                raise ValueError("'scale' and 'width'/'height' are mutually exclusive")
            return self

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        img = _image(inputs)
        p = self.params
        if p.scale is not None:
            size = (max(1, round(img.shape[1] * p.scale)), max(1, round(img.shape[0] * p.scale)))
        else:
            size = (p.width, p.height)
        out = cv2.resize(img, size, interpolation=_INTERP[p.interpolation])
        return Emit(self.ctx.outputs[0], out, {"source_shape": list(img.shape)})


class Grayscale(Processor):
    """RGB → single-channel luminance (passes grayscale input through unchanged)."""

    version = "1"
    description = "Convert an RGB frame to grayscale"
    outputs = {"*": OutputSpec(payload="image", description="grayscale frame")}

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        img = _image(inputs)
        if img.ndim == 3 and img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)  # sources deliver RGB order
        return Emit(self.ctx.outputs[0], img)


class Crop(Processor):
    """Crop a fixed rectangle (``x``, ``y``, ``width``, ``height`` in pixels)."""

    version = "1"
    description = "Crop a fixed rectangle out of each frame"
    outputs = {"*": OutputSpec(payload="image", description="cropped frame")}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int = Field(ge=0)
        y: int = Field(ge=0)
        width: int = Field(ge=1)
        height: int = Field(ge=1)

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        img = _image(inputs)
        p = self.params
        h, w = img.shape[:2]
        if p.x >= w or p.y >= h:
            raise ValueError(f"crop origin ({p.x}, {p.y}) is outside a {w}x{h} frame")
        out = np.ascontiguousarray(img[p.y : p.y + p.height, p.x : p.x + p.width])
        return Emit(self.ctx.outputs[0], out, {"crop": [p.x, p.y, p.width, p.height]})
