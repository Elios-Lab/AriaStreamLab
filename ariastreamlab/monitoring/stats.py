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
import time
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
    mean_rate_hz: float | None = None
    """``(received − 1) / (last − first)`` over the whole stream — the honest average."""
    outage_s: float = 0.0
    """Seconds inside the stream's span with no samples (gaps longer than the gap threshold)."""
    outages: tuple[tuple[float, float], ...] = ()
    """Up to 50 ``(start seconds since the first sample, length seconds)`` of those gaps."""


_MAX_MISSING = 100_000
_MAX_OUTAGES = 50


class _StreamTracker:
    __slots__ = (
        "confirmed_dropped",
        "first_device",
        "gap_threshold_ns",
        "last_device",
        "last_host",
        "max_device",
        "max_seq",
        "missing",
        "n",
        "nominal",
        "offset_min",
        "out_of_order",
        "outage_ns",
        "outages",
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
        # a gap longer than this is an outage, not jitter: 10 periods, at least one second
        period_ns = 0 if not nominal else 1e9 / nominal
        self.gap_threshold_ns = int(max(1e9, 10 * period_ns))
        self.outage_ns = 0
        self.outages: list[tuple[int, int]] = []  # (start device ns, length ns), capped

    @property
    def transport_dropped(self) -> int:
        return self.confirmed_dropped + len(self.missing)

    def observe(self, device_ns: int, host_ns: int, seq: int | None) -> None:
        self.n += 1
        if self.first_device is None:
            self.first_device = device_ns
        if self.max_device is not None and device_ns < self.max_device:
            self.out_of_order += 1
        elif self.max_device is not None and device_ns - self.max_device > self.gap_threshold_ns:
            gap = device_ns - self.max_device
            self.outage_ns += gap
            if len(self.outages) < _MAX_OUTAGES:
                self.outages.append((self.max_device, gap))
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

    def rate(self, now_host_ns: int | None = None) -> float | None:
        """Rate over the last window of data; ``0.0`` once the stream is stale in host time."""
        if (
            now_host_ns is not None
            and self.last_host is not None
            and now_host_ns - self.last_host > self.window_ns
        ):
            return 0.0
        if len(self.window) < 2:
            return None
        span = max(self.window) - min(self.window)
        return (len(self.window) - 1) / (span / 1e9) if span > 0 else None

    def mean_rate(self) -> float | None:
        if self.n < 2 or self.first_device is None or self.max_device is None:
            return None
        span = self.max_device - self.first_device
        return (self.n - 1) / (span / 1e9) if span > 0 else None


class StatsTracker:
    """Thread-safe accumulator fed with every sample that passes through the bus."""

    def __init__(
        self, nominal_rates: dict[str, float | None] | None = None, *, window_s: float = 5.0
    ) -> None:
        self._lock = threading.Lock()
        self._nominal = dict(nominal_rates or {})
        self._window_s = window_s
        self.paced = False
        """True for sources paced by the host clock (live, realtime mock, paced replay): then a
        stream that stopped delivering reports an effective rate of ``0`` after one window."""
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
        first = t.first_device or 0
        return StreamStats(
            sid,
            t.n,
            t.rate(time.monotonic_ns() if self.paced else None),
            t.nominal,
            t.out_of_order,
            t.transport_dropped,
            t.first_device,
            t.last_device,
            t.last_host,
            latency,
            t.mean_rate(),
            round(t.outage_ns / 1e9, 3),
            tuple((round((a - first) / 1e9, 3), round(b / 1e9, 3)) for a, b in t.outages),
        )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """JSON-friendly snapshot of everything."""
        return {sid: asdict(s) for sid, s in self.snapshot_all().items()}
