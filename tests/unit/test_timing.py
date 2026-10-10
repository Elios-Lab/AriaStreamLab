from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import pytest

from ariastreamlab.core.payloads import ImuPayload, PosePayload
from ariastreamlab.timing.clock import ClockAnchor, FakeClock
from ariastreamlab.timing.convert import (
    ms_to_ns,
    ns_to_timedelta,
    rate_to_period_ns,
    s_to_ns,
    timedelta_to_ns,
)
from ariastreamlab.timing.sync import (
    TimeSeriesBuffer,
    interp_imu,
    interp_pose,
    slerp_wxyz,
    temporal_join,
)


@dataclass(frozen=True)
class Stub:
    device_timestamp_ns: int
    value: float = 0.0


# ---------------------------------------------------------------- conversions
def test_timedelta_to_ns_is_exact_for_large_values() -> None:
    td = timedelta(days=3, seconds=4521, microseconds=999_999)
    ns = timedelta_to_ns(td)
    assert ns == (3 * 86400 + 4521) * 10**9 + 999_999_000
    assert ns_to_timedelta(ns) == td


def test_timedelta_roundtrip_many() -> None:
    for us in (0, 1, 999_999, 123_456_789, 10**12 + 7):
        td = timedelta(microseconds=us)
        assert ns_to_timedelta(timedelta_to_ns(td)) == td


def test_other_conversions() -> None:
    assert ms_to_ns(1234) == 1_234_000_000
    assert s_to_ns(1.5) == 1_500_000_000
    assert rate_to_period_ns(800) == 1_250_000
    assert rate_to_period_ns(30) == 33_333_333
    with pytest.raises(ValueError, match="positive"):
        rate_to_period_ns(0)


def test_clock_anchor_mapping() -> None:
    a = ClockAnchor(monotonic_ns=100, utc_ns=1_700_000_000_000_000_000, perf_counter_ns=5)
    assert a.monotonic_to_utc_ns(150) == 1_700_000_000_000_000_050
    assert a.utc_to_monotonic_ns(a.monotonic_to_utc_ns(999)) == 999
    assert a.utc_iso().startswith("2023-11-14T22:13:20")


def test_fake_clock_sleep_until_advances() -> None:
    c = FakeClock(start_ns=10)
    c.sleep_until_ns(500)
    assert c.now_ns() == 500
    c.sleep_until_ns(100)  # never goes backwards
    assert c.now_ns() == 500


# ---------------------------------------------------------------- buffer
def test_buffer_insert_out_of_order_and_lookups() -> None:
    buf: TimeSeriesBuffer[Stub] = TimeSeriesBuffer(capacity=10)
    for ts in (100, 300, 200, 500, 400):
        buf.push(Stub(ts))
    assert [s.device_timestamp_ns for s in buf.window(0, 1000)] == [100, 200, 300, 400, 500]
    assert buf.out_of_order == 2
    assert buf.previous(250).item.device_timestamp_ns == 200
    assert buf.previous(300).item.device_timestamp_ns == 300
    assert buf.previous(300, inclusive=False).item.device_timestamp_ns == 200
    assert buf.next(250).item.device_timestamp_ns == 300
    assert buf.next(500, inclusive=False) is None
    assert buf.previous(50) is None
    n = buf.nearest(260)
    assert n is not None
    assert n.item.device_timestamp_ns == 300
    assert n.dt_ns == 40
    assert buf.nearest(260, max_dt_ns=30) is None
    assert buf.nearest(240).item.device_timestamp_ns == 200
    assert buf.nearest(250).item.device_timestamp_ns == 200  # tie → earlier
    assert [s.device_timestamp_ns for s in buf.window(200, 400)] == [200, 300, 400]
    assert buf.latest().device_timestamp_ns == 500


def test_buffer_capacity_evicts_oldest() -> None:
    buf: TimeSeriesBuffer[Stub] = TimeSeriesBuffer(capacity=3)
    for ts in range(10):
        buf.push(Stub(ts))
    assert buf.first_ns == 7
    assert buf.last_ns == 9
    assert buf.evicted == 7
    assert buf.drop_before(9) == 2
    assert len(buf) == 1


def test_buffer_interpolate() -> None:
    buf: TimeSeriesBuffer[Stub] = TimeSeriesBuffer()
    buf.push(Stub(0, 0.0))
    buf.push(Stub(100, 10.0))
    lin = buf.interpolate(25, lambda a, b, al: a.value + (b.value - a.value) * al)
    assert lin == pytest.approx(2.5)
    assert buf.interpolate(100, lambda a, b, al: (a.value, b.value, al)) == (10.0, 10.0, 0.0)
    assert buf.interpolate(150, lambda a, b, al: 0) is None
    assert buf.interpolate(50, lambda a, b, al: 0, max_gap_ns=50) is None


def test_interp_imu_and_pose() -> None:
    a = ImuPayload((0, 0, 0), (0, 0, 0), 20.0)
    b = ImuPayload((2, 4, 6), (1, 1, 1), 30.0)
    m = interp_imu(a, b, 0.5)
    assert m.accel_msec2 == (1, 2, 3)
    assert m.gyro_radsec == (0.5, 0.5, 0.5)
    assert m.temperature_c == 25.0
    q0 = (1.0, 0.0, 0.0, 0.0)
    q90 = (math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4))
    q45 = slerp_wxyz(q0, q90, 0.5)
    assert q45 == pytest.approx((math.cos(math.pi / 8), 0.0, 0.0, math.sin(math.pi / 8)))
    p = interp_pose(PosePayload((0, 0, 0), q0), PosePayload((2, 0, 0), q90), 0.25)
    assert p.translation_m == (0.5, 0.0, 0.0)
    assert sum(x * x for x in p.quaternion_wxyz) == pytest.approx(1.0)


def test_temporal_join_modes() -> None:
    gaze: TimeSeriesBuffer[Stub] = TimeSeriesBuffer()
    imu: TimeSeriesBuffer[Stub] = TimeSeriesBuffer()
    for ts in (0, 33, 66, 100):
        gaze.push(Stub(ts))
    for ts in range(0, 101, 10):
        imu.push(Stub(ts))
    frame = Stub(50)
    r = temporal_join(frame, {"gaze": gaze, "imu": imu}, tolerance_ns=20)
    assert r is not None
    assert r.matches["gaze"].item.device_timestamp_ns == 66
    assert r.matches["imu"].item.device_timestamp_ns == 50
    assert r.max_abs_dt_ns == 16
    assert temporal_join(frame, {"gaze": gaze}, tolerance_ns=10) is None
    prev = temporal_join(frame, {"gaze": gaze}, tolerance_ns=20, mode="previous")
    assert prev is not None
    assert prev.matches["gaze"].item.device_timestamp_ns == 33
    latest = temporal_join(frame, {"gaze": gaze}, tolerance_ns=0, mode="latest")
    assert latest is not None
    assert latest.matches["gaze"].item.device_timestamp_ns == 100
    with pytest.raises(ValueError, match="join mode"):
        temporal_join(frame, {"gaze": gaze}, tolerance_ns=1, mode="bogus")
