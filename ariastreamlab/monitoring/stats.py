"""Per-stream statistics: rates, ordering and drop accounting.

Four kinds of loss are kept apart because they have different causes and fixes
(docs/ARCHITECTURE.md §11):

* **device** drops — reported by the glasses (parsed from the SDK's quality stats);
* **transport** drops — gaps in the device sequence numbers of what the host received;
* **app-queue** drops — bounded bus subscriptions that overflowed (see ``EventBus.stats``);
* **processor** drops — inputs a processor discarded under its drop policy.

This module computes the second kind and the rates; the others are joined in by
:mod:`ariastreamlab.monitoring.health`.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from ariastreamlab.core.sample import AnySample, Sample


@dataclass(frozen=True, slots=True)
class StreamStats:
    """Snapshot for one stream."""

    stream_id: str
    received: int
    effective_rate_hz: float | None
    nominal_rate_hz: float | None
    out_of_order: int
    transport_dropped: int
    first_device_ns: int | None
    last_device_ns: int | None
    last_host_ns: int | None
    mean_host_latency_ms: float | None
    """Mean of ``host_receive − device`` after removing the clock offset estimate (live only)."""


_MAX_MISSING = 100_000


class _StreamTracker:
    __slots__ = (
        "confirmed_dropped",
        "first_device",
        "last_device",
        "last_host",
        "max_device",
        "max_seq",
        "missing",
        "n",
        "nominal",
        "offset_min",
        "out_of_order",
        "window",
        "window_ns",
    )

    def __init__(self, nominal: float | None, window_s: float) -> None:
        self.n = 0
        self.nominal = nominal
        self.first_device: int | None = None
        self.last_device: int | None = None
        self.max_device: int | None = None
        self.last_host: int | None = None
        self.max_seq: int | None = None
        self.missing: set[int] = set()  # sequence numbers not (yet) seen below max_seq
        self.confirmed_dropped = 0  # holes evicted from `missing` because it grew too large
        self.out_of_order = 0
        self.window: deque[int] = deque()
        self.window_ns = int(window_s * 1e9)
        self.offset_min: int | None = None  # min(host - device) ≈ clock offset + min latency

    @property
    def transport_dropped(self) -> int:
        return self.confirmed_dropped + len(self.missing)

    def observe(self, device_ns: int, host_ns: int, seq: int | None) -> None:
        self.n += 1
        if self.first_device is None:
            self.first_device = device_ns
        if self.max_device is not None and device_ns < self.max_device:
            self.out_of_order += 1
        self.max_device = device_ns if self.max_device is None else max(self.max_device, device_ns)
        self.last_device = device_ns
        self.last_host = host_ns
        if seq is not None:
            self._observe_seq(seq)
        w = self.window
        w.append(device_ns)
        cutoff = self.max_device - self.window_ns
        while w and w[0] < cutoff:
            w.popleft()
        delta = host_ns - device_ns
        self.offset_min = delta if self.offset_min is None else min(self.offset_min, delta)

    def _observe_seq(self, seq: int) -> None:
        if self.max_seq is None:
            self.max_seq = seq
            return
        if seq > self.max_seq:
            gap = seq - self.max_seq - 1
            if gap > 0:
                if gap > _MAX_MISSING:  # absurd jump (device restart?): count, do not enumerate
                    self.confirmed_dropped += gap
                else:
                    self.missing.update(range(self.max_seq + 1, seq))
                    if len(self.missing) > _MAX_MISSING:
                        evict = sorted(self.missing)[: len(self.missing) - _MAX_MISSING]
                        self.missing.difference_update(evict)
                        self.confirmed_dropped += len(evict)
            self.max_seq = seq
        else:
            self.missing.discard(seq)  # a late arrival fills its hole; duplicates are ignored

    def rate(self) -> float | None:
        if len(self.window) < 2:
            return None
        span = max(self.window) - min(self.window)
        return (len(self.window) - 1) / (span / 1e9) if span > 0 else None


class StatsTracker:
    """Thread-safe accumulator fed with every sample that passes through the bus."""

    def __init__(
        self, nominal_rates: dict[str, float | None] | None = None, *, window_s: float = 5.0
    ) -> None:
        self._lock = threading.Lock()
        self._nominal = dict(nominal_rates or {})
        self._window_s = window_s
        self._trackers: dict[str, _StreamTracker] = {}
        self._latency_sum: dict[str, float] = {}
        self._latency_n: dict[str, int] = {}

    def set_nominal(self, stream_id: str, rate_hz: float | None) -> None:
        """Declare the nominal rate of a stream (for the dashboard's expected-vs-actual)."""
        with self._lock:
            self._nominal[stream_id] = rate_hz
            if stream_id in self._trackers:
                self._trackers[stream_id].nominal = rate_hz

    def observe(self, sample: AnySample) -> None:
        """Record one sample."""
        sid = sample.stream_id
        seq = sample.sequence_number if isinstance(sample, Sample) else None
        host = (
            sample.host_receive_timestamp_ns
            if isinstance(sample, Sample)
            else sample.processing_end_ns
        )
        with self._lock:
            t = self._trackers.get(sid)
            if t is None:
                t = self._trackers[sid] = _StreamTracker(self._nominal.get(sid), self._window_s)
            t.observe(sample.device_timestamp_ns, host, seq)

    def snapshot(self, stream_id: str) -> StreamStats | None:
        """Snapshot for one stream."""
        with self._lock:
            t = self._trackers.get(stream_id)
            if t is None:
                return None
            return self._snap(stream_id, t)

    def snapshot_all(self) -> dict[str, StreamStats]:
        """Snapshot for every stream seen so far."""
        with self._lock:
            return {sid: self._snap(sid, t) for sid, t in self._trackers.items()}

    def _snap(self, sid: str, t: _StreamTracker) -> StreamStats:
        latency = None
        if t.offset_min is not None and t.last_host is not None and t.last_device is not None:
            # relative latency of the last sample vs the fastest ever seen; clock-offset free
            latency = ((t.last_host - t.last_device) - t.offset_min) / 1e6
        return StreamStats(
            sid,
            t.n,
            t.rate(),
            t.nominal,
            t.out_of_order,
            t.transport_dropped,
            t.first_device,
            t.last_device,
            t.last_host,
            latency,
        )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """JSON-friendly snapshot of everything."""
        return {sid: asdict(s) for sid, s in self.snapshot_all().items()}
