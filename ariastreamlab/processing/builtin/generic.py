"""Processors that work on any payload."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ariastreamlab.processing.base import Emit, Inputs, OutputSpec, Processor, ProcessorContext


class Passthrough(Processor):
    """Re-publish the input unchanged under a new name (useful to save or display it)."""

    version = "1"
    description = "Copy the input to a derived stream unchanged"
    outputs = {"*": OutputSpec(payload="any")}

    def process(self, inputs: Inputs) -> Emit:  # noqa: D102
        return Emit(self.ctx.outputs[0], inputs.primary.data, inputs.primary.metadata)


class Downsample(Processor):
    """Keep every ``every``-th sample, or at most ``rate_hz`` samples per second."""

    version = "1"
    description = "Reduce the rate of a stream (every N-th sample or a target rate)"
    outputs = {"*": OutputSpec(payload="any")}

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="forbid")
        every: int | None = Field(default=None, ge=1)
        rate_hz: float | None = Field(default=None, gt=0)

        @model_validator(mode="after")
        def _one_of(self) -> Downsample.Parameters:
            if (self.every is None) == (self.rate_hz is None):
                raise ValueError("give exactly one of 'every' or 'rate_hz'")
            return self

    def setup(self, ctx: ProcessorContext) -> None:  # noqa: D102
        super().setup(ctx)
        self._n = 0
        self._last_ts: int | None = None
        self._min_dt_ns = None if self.params.rate_hz is None else int(1e9 / self.params.rate_hz)

    def process(self, inputs: Inputs) -> Emit | None:  # noqa: D102
        s = inputs.primary
        if self._min_dt_ns is None:
            keep = self._n % self.params.every == 0
            self._n += 1
        else:
            ts = s.device_timestamp_ns
            keep = self._last_ts is None or ts - self._last_ts >= self._min_dt_ns
            if keep:
                self._last_ts = ts
        return Emit(self.ctx.outputs[0], s.data, s.metadata) if keep else None
