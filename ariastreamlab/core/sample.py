"""The internal sample representation shared by live acquisition, replay and mock sources.

Design rules (see docs/ARCHITECTURE.md §4.1 and §8):

* ``device_timestamp_ns`` is the timestamp the glasses attached to the measurement. It is
  copied verbatim from the SDK and never rewritten by anything downstream.
* ``host_receive_timestamp_ns`` is ``time.monotonic_ns()`` on the laptop when the sample
  entered AriaStreamLab. It is a second, independent field.
* ``provenance.origin`` is the only field that differs between a live sample and the same
  sample replayed from the raw recording (invariant I-1).
* Payloads (``data``) are plain Python / NumPy objects, never SDK objects, so processors and
  tests never need the Meta SDK installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

Origin = Literal["live", "replay", "mock"]

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a sample came from.

    Attributes:
        origin: ``"live"`` (glasses), ``"replay"`` (from a recorded session) or ``"mock"``.
        source_id: Identifier of the concrete source, e.g. ``"aria_gen2"``,
            ``"vrs:raw/aria_stream.vrs"`` or ``"mock"``.
        session_id: UUID of the session that produced the sample.
    """

    origin: Origin
    source_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class Sample:
    """One observation from one stream of one device.

    Attributes:
        device_id: Device serial number (``"mock-0"`` for the mock source).
        stream_id: Identifier from :mod:`ariastreamlab.core.registry`, e.g. ``"rgb"``.
        device_timestamp_ns: Device-clock timestamp in integer nanoseconds (never rewritten).
        host_receive_timestamp_ns: ``time.monotonic_ns()`` when the sample entered the host.
        sequence_number: Per-stream counter; the device's frame number when the SDK gives one.
        data: Plain payload (``numpy.ndarray`` for images, small dataclasses otherwise).
        metadata: Free-form extra information (exposure, gain, batch size, camera label…).
        provenance: See :class:`Provenance`.
    """

    device_id: str
    stream_id: str
    device_timestamp_ns: int
    host_receive_timestamp_ns: int
    sequence_number: int | None
    data: Any
    provenance: Provenance
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def __post_init__(self) -> None:
        if not isinstance(self.device_timestamp_ns, int):
            raise TypeError(
                "device_timestamp_ns must be an int"
                f" (got {type(self.device_timestamp_ns).__name__})"
            )
        if not isinstance(self.host_receive_timestamp_ns, int):
            raise TypeError("host_receive_timestamp_ns must be an int")

    @property
    def timestamp_ns(self) -> int:
        """Alias of ``device_timestamp_ns`` so Sample and DerivedSample share a time API."""
        return self.device_timestamp_ns


@dataclass(frozen=True, slots=True)
class DerivedSample:
    """Output of a processor, with enough provenance to reconstruct how it was produced.

    Attributes:
        device_id: Device the inputs came from.
        stream_id: Output stream id (declared in the processor's ``outputs``).
        timestamp_ns: Device-clock timestamp chosen by the processor (default: the primary
            input's ``device_timestamp_ns``). Same domain as :attr:`Sample.device_timestamp_ns`.
        source_streams: Input stream ids, primary first.
        source_timestamps_ns: Device timestamps of the inputs, same order.
        processor_id: ``id`` of the processor instance from the configuration.
        processing_start_ns: Host monotonic time when ``process()`` started.
        processing_end_ns: Host monotonic time when ``process()`` returned.
        data: Plain payload.
        provenance: Inherited from the primary input.
        metadata: Free-form.
    """

    device_id: str
    stream_id: str
    timestamp_ns: int
    source_streams: tuple[str, ...]
    source_timestamps_ns: tuple[int, ...]
    processor_id: str
    processing_start_ns: int
    processing_end_ns: int
    data: Any
    provenance: Provenance
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    @property
    def device_timestamp_ns(self) -> int:
        """Alias so derived samples can be fed to processors like raw samples."""
        return self.timestamp_ns

    @property
    def processing_latency_ns(self) -> int:
        """Host time spent inside the processor."""
        return self.processing_end_ns - self.processing_start_ns


AnySample = Sample | DerivedSample
