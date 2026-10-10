"""Temporal lookup and multi-stream synchronisation utilities.

Streams on Aria run at different rates (RGB 10 Hz, IMU 800 Hz, gaze 30 Hz…) and samples do
**not** arrive in timestamp order across streams (and occasionally within one). Everything
here therefore works on device timestamps, tolerates out-of-order insertion and never assumes
two streams share sample instants.

Typical use inside a processor::

    buf = TimeSeriesBuffer(capacity=2000)       # one per secondary stream
    ...
    gaze = buf.nearest(rgb_sample.device_timestamp_ns, max_dt_ns=20_000_000)
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from ariastreamlab.core.payloads import ImuPayload, PosePayload


class Timestamped(Protocol):
    """Anything with a device timestamp (``Sample``, ``DerivedSample``, test stubs)."""

    @property
    def device_timestamp_ns(self) -> int: ...  # noqa: D102


T = TypeVar("T", bound=Timestamped)
P = TypeVar("P")


@dataclass(frozen=True, slots=True)
class Match(Generic[T]):
    """Result of a temporal lookup."""

    item: T
    dt_ns: int
    """``item.device_timestamp_ns - query_ns`` (negative when the item is earlier)."""


class TimeSeriesBuffer(Generic[T]):
    """Bounded buffer of timestamped items kept sorted by ``device_timestamp_ns``.

    Insertion is ``O(log n)`` for the search plus ``O(n)`` for the list insert, which is far
    cheaper than it sounds for the few thousand items a realtime buffer holds. Late
    (out-of-order) arrivals are inserted at the right place and counted.
    """

    def __init__(self, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._ts: list[int] = []
        self._items: list[T] = []
        self.out_of_order = 0
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._ts)

    def push(self, item: T) -> None:
        """Insert keeping the buffer sorted; evicts the oldest when over capacity."""
        ts = item.device_timestamp_ns
        if not self._ts or ts >= self._ts[-1]:
            self._ts.append(ts)
            self._items.append(item)
        else:
            self.out_of_order += 1
            i = bisect.bisect_right(self._ts, ts)
            self._ts.insert(i, ts)
            self._items.insert(i, item)
        if len(self._ts) > self.capacity:
            del self._ts[0]
            del self._items[0]
            self.evicted += 1

    def extend(self, items: Iterable[T]) -> None:
        """Push many."""
        for it in items:
            self.push(it)

    @property
    def first_ns(self) -> int | None:
        """Oldest timestamp held."""
        return self._ts[0] if self._ts else None

    @property
    def last_ns(self) -> int | None:
        """Newest timestamp held."""
        return self._ts[-1] if self._ts else None

    def latest(self) -> T | None:
        """Most recent item."""
        return self._items[-1] if self._items else None

    # -- lookups -----------------------------------------------------------------------
    def previous(self, ts_ns: int, *, inclusive: bool = True) -> Match[T] | None:
        """Latest item with timestamp ``<= ts_ns`` (``<`` when not inclusive)."""
        i = (bisect.bisect_right if inclusive else bisect.bisect_left)(self._ts, ts_ns) - 1
        if i < 0:
            return None
        return Match(self._items[i], self._ts[i] - ts_ns)

    def next(self, ts_ns: int, *, inclusive: bool = True) -> Match[T] | None:
        """Earliest item with timestamp ``>= ts_ns`` (``>`` when not inclusive)."""
        i = (bisect.bisect_left if inclusive else bisect.bisect_right)(self._ts, ts_ns)
        if i >= len(self._ts):
            return None
        return Match(self._items[i], self._ts[i] - ts_ns)

    def nearest(self, ts_ns: int, *, max_dt_ns: int | None = None) -> Match[T] | None:
        """Closest item in time; ``None`` if farther than ``max_dt_ns``."""
        prev, nxt = self.previous(ts_ns), self.next(ts_ns)
        best: Match[T] | None
        if prev is None:
            best = nxt
        elif nxt is None:
            best = prev
        else:
            best = prev if abs(prev.dt_ns) <= abs(nxt.dt_ns) else nxt
        if best is not None and max_dt_ns is not None and abs(best.dt_ns) > max_dt_ns:
            return None
        return best

    def window(self, start_ns: int, end_ns: int) -> list[T]:
        """All items with ``start_ns <= ts <= end_ns``."""
        lo = bisect.bisect_left(self._ts, start_ns)
        hi = bisect.bisect_right(self._ts, end_ns)
        return self._items[lo:hi]

    def interpolate(
        self, ts_ns: int, interp: Callable[[T, T, float], P], *, max_gap_ns: int | None = None
    ) -> P | None:
        """Interpolate between the bracketing items with ``interp(before, after, alpha)``.

        Returns ``None`` outside the buffer's range or when the bracket is wider than
        ``max_gap_ns``. An exact hit returns ``interp(item, item, 0.0)``.
        """
        prev, nxt = self.previous(ts_ns), self.next(ts_ns)
        if prev is None or nxt is None:
            return None
        a, b = prev.item, nxt.item
        gap = b.device_timestamp_ns - a.device_timestamp_ns
        if max_gap_ns is not None and gap > max_gap_ns:
            return None
        alpha = 0.0 if gap == 0 else (ts_ns - a.device_timestamp_ns) / gap
        return interp(a, b, alpha)

    def drop_before(self, ts_ns: int) -> int:
        """Discard items older than ``ts_ns``; returns how many were dropped."""
        i = bisect.bisect_left(self._ts, ts_ns)
        del self._ts[:i]
        del self._items[:i]
        return i

    def clear(self) -> None:
        """Empty the buffer."""
        self._ts.clear()
        self._items.clear()


# -- payload interpolators --------------------------------------------------------------


def lerp3(a: Sequence[float], b: Sequence[float], alpha: float) -> tuple[float, float, float]:
    """Linear interpolation of two 3-vectors."""
    return (
        a[0] + (b[0] - a[0]) * alpha,
        a[1] + (b[1] - a[1]) * alpha,
        a[2] + (b[2] - a[2]) * alpha,
    )


def slerp_wxyz(
    q0: Sequence[float], q1: Sequence[float], alpha: float
) -> tuple[float, float, float, float]:
    """Spherical linear interpolation between two unit quaternions (w, x, y, z)."""
    w0, x0, y0, z0 = q0
    w1, x1, y1, z1 = q1
    dot = w0 * w1 + x0 * x1 + y0 * y1 + z0 * z1
    if dot < 0.0:  # take the short arc
        w1, x1, y1, z1, dot = -w1, -x1, -y1, -z1, -dot
    if dot > 0.9995:  # nearly parallel: lerp + normalise
        w, x, y, z = (
            w0 + (w1 - w0) * alpha,
            x0 + (x1 - x0) * alpha,
            y0 + (y1 - y0) * alpha,
            z0 + (z1 - z0) * alpha,
        )
        n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
        return (w / n, x / n, y / n, z / n)
    theta0 = math.acos(dot)
    sin0 = math.sin(theta0)
    theta = theta0 * alpha
    s1 = math.sin(theta) / sin0
    s0 = math.cos(theta) - dot * s1
    return (w0 * s0 + w1 * s1, x0 * s0 + x1 * s1, y0 * s0 + y1 * s1, z0 * s0 + z1 * s1)


def interp_imu(a: ImuPayload, b: ImuPayload, alpha: float) -> ImuPayload:
    """Linear interpolation of IMU samples."""
    temp = None
    if a.temperature_c is not None and b.temperature_c is not None:
        temp = a.temperature_c + (b.temperature_c - a.temperature_c) * alpha
    return ImuPayload(
        lerp3(a.accel_msec2, b.accel_msec2, alpha), lerp3(a.gyro_radsec, b.gyro_radsec, alpha), temp
    )


def interp_pose(a: PosePayload, b: PosePayload, alpha: float) -> PosePayload:
    """Lerp translation, slerp rotation."""
    lin = None
    if a.linear_velocity_msec is not None and b.linear_velocity_msec is not None:
        lin = lerp3(a.linear_velocity_msec, b.linear_velocity_msec, alpha)
    ang = None
    if a.angular_velocity_radsec is not None and b.angular_velocity_radsec is not None:
        ang = lerp3(a.angular_velocity_radsec, b.angular_velocity_radsec, alpha)
    return PosePayload(
        lerp3(a.translation_m, b.translation_m, alpha),
        slerp_wxyz(a.quaternion_wxyz, b.quaternion_wxyz, alpha),
        lin,
        ang,
        a.quality if alpha < 0.5 else b.quality,
    )


# -- multi-stream join ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JoinResult(Generic[T]):
    """Output of :func:`temporal_join`: the primary item and one match per secondary stream."""

    primary: T
    matches: Mapping[str, Match[T]]

    @property
    def max_abs_dt_ns(self) -> int:
        """Largest absolute time difference among the matches (0 when there are none)."""
        return max((abs(m.dt_ns) for m in self.matches.values()), default=0)


def temporal_join(
    primary: T,
    secondaries: Mapping[str, TimeSeriesBuffer[T]],
    *,
    tolerance_ns: int,
    mode: str = "nearest",
) -> JoinResult[T] | None:
    """Join one primary sample with the closest sample of each secondary stream.

    Args:
        primary: The sample that triggers the join (e.g. an RGB frame).
        secondaries: Buffers keyed by stream id (e.g. ``{"eye_gaze": buf}``).
        tolerance_ns: Maximum ``|dt|`` accepted for every secondary.
        mode: ``"nearest"`` (closest in time), ``"previous"`` (latest not after the primary)
            or ``"latest"`` (most recent item regardless of time, tolerance ignored).

    Returns:
        ``None`` when any secondary has no acceptable match, else a :class:`JoinResult`.
    """
    ts = primary.device_timestamp_ns
    matches: dict[str, Match[T]] = {}
    for name, buf in secondaries.items():
        m: Match[T] | None
        if mode == "nearest":
            m = buf.nearest(ts, max_dt_ns=tolerance_ns)
        elif mode == "previous":
            m = buf.previous(ts)
            if m is not None and -m.dt_ns > tolerance_ns:
                m = None
        elif mode == "latest":
            last = buf.latest()
            m = None if last is None else Match(last, last.device_timestamp_ns - ts)
        else:
            raise ValueError(f"unknown join mode {mode!r}")
        if m is None:
            return None
        matches[name] = m
    return JoinResult(primary, matches)
