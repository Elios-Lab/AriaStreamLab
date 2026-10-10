"""Host clock and the anchor that relates it to UTC wall-clock time.

AriaStreamLab stamps every sample with ``time.monotonic_ns()`` (never jumps, never goes
backwards). To relate that to calendar time, a :class:`ClockAnchor` pairing the monotonic
clock with ``time.time_ns()`` is taken when a session opens and periodically afterwards, so
drift between the two can be analysed offline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClockAnchor:
    """A simultaneous reading of the host clocks.

    Attributes:
        monotonic_ns: ``time.monotonic_ns()``.
        utc_ns: ``time.time_ns()`` (Unix epoch, nanoseconds).
        perf_counter_ns: ``time.perf_counter_ns()`` (for completeness; same rate as monotonic on
            most platforms but a different origin).
    """

    monotonic_ns: int
    utc_ns: int
    perf_counter_ns: int

    @classmethod
    def now(cls) -> ClockAnchor:
        """Take an anchor. The three reads are not atomic; the skew is sub-microsecond."""
        return cls(time.monotonic_ns(), time.time_ns(), time.perf_counter_ns())

    def monotonic_to_utc_ns(self, monotonic_ns: int) -> int:
        """Map a monotonic timestamp to UTC nanoseconds using this anchor."""
        return self.utc_ns + (monotonic_ns - self.monotonic_ns)

    def utc_to_monotonic_ns(self, utc_ns: int) -> int:
        """Inverse of :meth:`monotonic_to_utc_ns`."""
        return self.monotonic_ns + (utc_ns - self.utc_ns)

    def utc_iso(self) -> str:
        """The anchor's UTC time as an ISO-8601 string with microseconds."""
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.utc_ns / 1e9, tz=timezone.utc).isoformat(
            timespec="microseconds"
        )


class HostClock:
    """Thin wrapper around the monotonic clock so tests can substitute a fake."""

    def now_ns(self) -> int:
        """Current host time in integer nanoseconds (monotonic)."""
        return time.monotonic_ns()

    def anchor(self) -> ClockAnchor:
        """Take a :class:`ClockAnchor`."""
        return ClockAnchor.now()

    def sleep_until_ns(self, target_ns: int) -> None:
        """Sleep until the monotonic clock reaches ``target_ns`` (coarse, then busy-wait < 1 ms)."""
        while True:
            remaining = target_ns - self.now_ns()
            if remaining <= 0:
                return
            if remaining > 2_000_000:
                time.sleep((remaining - 1_000_000) / 1e9)
            elif remaining > 50_000:
                time.sleep(0)
            # else: spin for the last ~50 µs


class FakeClock(HostClock):
    """Deterministic clock for tests: time only advances when told to."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self._now = start_ns

    def now_ns(self) -> int:  # noqa: D102
        return self._now

    def advance_ns(self, delta_ns: int) -> None:
        """Move the clock forward."""
        self._now += delta_ns

    def sleep_until_ns(self, target_ns: int) -> None:  # noqa: D102
        if target_ns > self._now:
            self._now = target_ns
