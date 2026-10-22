"""Processor contract (docs/ARCHITECTURE.md §4.4).

A processor is a small class with ``process(inputs)``. It receives :class:`Inputs` (one sample
per input stream, the *primary* input first) and returns nothing, one output or several. It
never learns whether its samples came from the glasses, a mock or a replay: the only
difference between those is ``sample.provenance.origin``, and well-behaved processors do not
read it (invariant I-1 in ``tests/invariants``).

Minimal example::

    from ariastreamlab.processing import Processor, Inputs, Emit
    from ariastreamlab.core.payloads import ScalarPayload

    class AccelNorm(Processor):
        version = "1"

        def process(self, inputs: Inputs):
            imu = inputs.primary.data
            n = sum(a * a for a in imu.accel_msec2) ** 0.5
            return Emit("accel_norm", ScalarPayload(n, "m/s^2"))

Returned values may be an :class:`Emit`, a ``(stream_id, data)`` or
``(stream_id, data, metadata)`` tuple, a ready :class:`DerivedSample`, a list of those, or
``None``. The runner completes the derived sample (timestamps, provenance, processor id).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from ariastreamlab.core.registry import StreamRegistry
from ariastreamlab.core.sample import AnySample, DerivedSample
from ariastreamlab.timing.clock import HostClock

__all__ = [
    "Emit",
    "Inputs",
    "Output",
    "OutputSpec",
    "Processor",
    "ProcessorContext",
]


@dataclass(frozen=True, slots=True)
class Emit:
    """One output of ``process()``.

    Attributes:
        stream_id: One of the processor's declared ``outputs``.
        data: Plain payload (NumPy array, a payload dataclass, a float, a dict…).
        metadata: Free-form extra information stored with the sample.
        timestamp_ns: Device-clock timestamp of the result; ``None`` ⇒ the primary input's.
    """

    stream_id: str
    data: Any
    metadata: Mapping[str, Any] | None = None
    timestamp_ns: int | None = None


Output = Emit | DerivedSample | tuple[str, Any] | tuple[str, Any, Mapping[str, Any]]


class Inputs(Mapping[str, AnySample]):
    """The samples handed to ``process()``: a read-only mapping ``stream_id -> sample``.

    ``inputs.primary`` is the sample that triggered the call (the first input stream in the
    configuration); the other entries are the secondary inputs matched by the join policy.
    ``inputs.dt_ns`` holds, per secondary stream, ``secondary.ts - primary.ts``.
    """

    __slots__ = ("_dt", "_items", "_primary_id")

    def __init__(
        self,
        primary_id: str,
        items: Mapping[str, AnySample],
        dt_ns: Mapping[str, int] | None = None,
    ) -> None:
        self._primary_id = primary_id
        self._items = dict(items)
        self._dt = dict(dt_ns or {})

    @property
    def primary_id(self) -> str:  # noqa: D102
        return self._primary_id

    @property
    def primary(self) -> AnySample:
        """The sample of the primary input stream."""
        return self._items[self._primary_id]

    @property
    def dt_ns(self) -> Mapping[str, int]:
        """``secondary.device_timestamp_ns - primary.device_timestamp_ns`` per secondary."""
        return self._dt

    def __getitem__(self, key: str) -> AnySample:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Inputs(primary={self._primary_id!r}, streams={list(self._items)})"


@dataclass(frozen=True, slots=True)
class OutputSpec:
    """Optional description of an output stream (used for ``_meta.json`` and the dashboard)."""

    payload: str = "any"
    units: Mapping[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class ProcessorContext:
    """What a processor can see besides its inputs.

    Attributes:
        processor_id: The ``id`` from the configuration.
        parameters: Validated parameters (a Pydantic model when the processor declares
            ``Parameters``, else the raw dict).
        inputs: Declared input stream ids, primary first.
        outputs: Declared output stream ids.
        registry: The stream registry (payload types, units, nominal rates).
        calibration: The device calibration as a plain dict (``cameras``, ``transform_device_cpf``
            …) when known; updated through :meth:`Processor.on_calibration`.
        session_dir: The session directory being written, or ``None``.
        clock: Host clock (``now_ns()`` monotonic, UTC anchor).
        log: A structlog logger bound to the processor id.
    """

    processor_id: str
    parameters: Any
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    registry: StreamRegistry
    calibration: dict[str, Any] | None = None
    session_dir: Path | None = None
    clock: HostClock = field(default_factory=HostClock)
    log: Any = field(default_factory=lambda: structlog.get_logger("ariastreamlab.processor"))


class Processor(ABC):
    """Base class of every processor (built-in, plugin or student-written).

    Class attributes:
        version: Bumped when the algorithm changes; stored with every output stream.
        description: One line for ``ariastreamlab processors``.
        Parameters: Optional Pydantic model; when set, ``parameters`` from the configuration
            are validated against it at start-up and ``ctx.parameters`` is an instance of it.
        outputs: Optional :class:`OutputSpec` per output stream id (``"*"`` for all).
    """

    version: ClassVar[str] = "1"
    description: ClassVar[str] = ""
    Parameters: ClassVar[type[BaseModel] | None] = None
    outputs: ClassVar[Mapping[str, OutputSpec]] = {}

    def setup(self, ctx: ProcessorContext) -> None:
        """Called once on the worker thread before the first ``process()``."""
        self.ctx = ctx

    @abstractmethod
    def process(self, inputs: Inputs) -> Output | Sequence[Output] | None:
        """Turn one set of inputs into zero or more outputs."""

    def on_calibration(self, calibration: dict[str, Any]) -> None:
        """Called when the device calibration becomes known (live: shortly after start)."""
        if hasattr(self, "ctx"):
            self.ctx.calibration = calibration

    def shutdown(self) -> None:  # noqa: B027 - optional hook
        """Called once when the graph stops."""

    def on_parameters_changed(self, old: Any, new: Any) -> None:  # noqa: B027 - optional hook
        """Called after a live parameter change (``Experiment.set_parameters``).

        ``self.params`` already returns ``new``. Override only if ``setup()`` derived state
        from the parameters; it runs on the caller's thread, so keep it quick and
        thread-safe (assigning a float is fine).
        """

    # -- convenience -------------------------------------------------------------------
    @property
    def params(self) -> Any:
        """Shortcut for ``self.ctx.parameters``."""
        return self.ctx.parameters

    @classmethod
    def output_spec(cls, stream_id: str) -> OutputSpec:
        """The declared spec of ``stream_id`` (``"*"`` as fallback)."""
        return cls.outputs.get(stream_id) or cls.outputs.get("*") or OutputSpec()
