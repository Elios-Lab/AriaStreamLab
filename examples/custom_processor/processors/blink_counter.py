"""Example custom processor: count "blinks" as short gaps in valid eye-gaze samples.

Put this file in a ``processors/`` directory next to your experiment JSON and reference it
with ``"type": "blink_counter.py"`` (or ``"blink_counter.py:BlinkCounter"``). Run it with::

    ariastreamlab run examples/custom_processor/experiment.json --mock --duration 10
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ariastreamlab.core.payloads import GazePayload, ScalarPayload
from ariastreamlab.processing import Emit, Inputs, OutputSpec, Processor, ProcessorContext


class BlinkCounter(Processor):
    """Emit the running blink count every time a blink ends."""

    version = "1"
    description = "Counts short runs of invalid gaze samples as blinks"
    outputs = {"blinks": OutputSpec(payload="scalar", units={"value": "count"})}

    class Parameters(BaseModel):
        """Validated at start-up; ``ctx.parameters`` is an instance of this model."""

        min_ms: float = Field(default=50.0, gt=0, description="shortest gap that counts")
        max_ms: float = Field(default=500.0, gt=0, description="longer gaps are not blinks")

    def setup(self, ctx: ProcessorContext) -> None:
        super().setup(ctx)
        self.count = 0
        self.gap_start_ns: int | None = None
        ctx.log.info("blink_counter.ready", min_ms=self.params.min_ms, max_ms=self.params.max_ms)

    def process(self, inputs: Inputs) -> Emit | None:
        gaze = inputs.primary.data
        if not isinstance(gaze, GazePayload):
            raise TypeError("blink_counter needs the eye_gaze stream as input")
        ts = inputs.primary.device_timestamp_ns
        if not gaze.valid:
            if self.gap_start_ns is None:
                self.gap_start_ns = ts
            return None
        if self.gap_start_ns is None:
            return None
        gap_ms = (ts - self.gap_start_ns) / 1e6
        self.gap_start_ns = None
        if self.params.min_ms <= gap_ms <= self.params.max_ms:
            self.count += 1
            return Emit("blinks", ScalarPayload(float(self.count), "count"), {"gap_ms": gap_ms})
        return None

    def shutdown(self) -> None:
        self.ctx.log.info("blink_counter.done", blinks=self.count)
