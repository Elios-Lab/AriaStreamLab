"""Exact conversions into integer nanoseconds.

The SDK delivers time in three shapes: ``int`` nanoseconds (``capture_timestamp_ns``),
``datetime.timedelta`` (``tracking_timestamp`` of eye gaze / hand tracking / VIO high-frequency)
and ``int`` milliseconds (``start_time_ms``). Everything here is integer arithmetic; no float
passes through, so a timestamp survives a round-trip unchanged.
"""

from __future__ import annotations

from datetime import timedelta

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def timedelta_to_ns(td: timedelta) -> int:
    """Convert a ``timedelta`` to integer nanoseconds exactly (no float rounding)."""
    return (td.days * 86_400 + td.seconds) * NS_PER_S + td.microseconds * NS_PER_US


def ns_to_timedelta(ns: int) -> timedelta:
    """Inverse of :func:`timedelta_to_ns` (sub-microsecond part is truncated)."""
    return timedelta(microseconds=ns // NS_PER_US)


def ms_to_ns(ms: int) -> int:
    """Milliseconds to nanoseconds."""
    return int(ms) * NS_PER_MS


def us_to_ns(us: int) -> int:
    """Microseconds to nanoseconds."""
    return int(us) * NS_PER_US


def s_to_ns(seconds: float) -> int:
    """Seconds (float, e.g. from a config file) to nanoseconds, rounded to the nearest ns."""
    return round(seconds * NS_PER_S)


def ns_to_s(ns: int) -> float:
    """Nanoseconds to float seconds (for display and plotting only)."""
    return ns / NS_PER_S


def rate_to_period_ns(rate_hz: float) -> int:
    """Nominal sample period of a stream."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be positive")
    return round(NS_PER_S / rate_hz)
