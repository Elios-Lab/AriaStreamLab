from __future__ import annotations

import pytest

from ariastreamlab.core.bus import EventBus
from ariastreamlab.events.markers import DeviceTimeEstimator, Event, EventLog
from ariastreamlab.monitoring.stats import StatsTracker
from ariastreamlab.timing.clock import FakeClock


def test_stats_rate_gaps_and_order(make_sample) -> None:
    tracker = StatsTracker({"imu_left": 800.0})
    period = 1_250_000
    seq = 0
    for i in range(100):
        if i in (40, 41, 42):  # three samples lost in transport
            seq += 1
            continue
        tracker.observe(make_sample(device_ns=i * period, host_ns=i * period + 5_000_000, seq=seq))
        seq += 1
    tracker.observe(
        make_sample(device_ns=50 * period, host_ns=100 * period, seq=seq)
    )  # late arrival
    s = tracker.snapshot("imu_left")
    assert s is not None
    assert s.received == 98
    assert s.transport_dropped == 3
    assert s.out_of_order == 1
    assert s.nominal_rate_hz == 800.0
    assert s.effective_rate_hz is not None
    assert 780 < s.effective_rate_hz < 800
    assert s.first_device_ns == 0
    assert s.last_device_ns == 50 * period
    assert tracker.snapshot("nope") is None
    assert "imu_left" in tracker.as_dict()


def test_stats_latency_is_offset_free(make_sample) -> None:
    tracker = StatsTracker()
    # host clock is 1e12 ahead of device clock; transport latency alternates 5 ms / 25 ms
    for i in range(10):
        lat = 5_000_000 if i % 2 == 0 else 25_000_000
        tracker.observe(make_sample(device_ns=i * 10**6, host_ns=10**12 + i * 10**6 + lat, seq=i))
    s = tracker.snapshot("imu_left")
    assert s is not None
    assert s.mean_host_latency_ms is not None
    assert abs(s.mean_host_latency_ms - 20.0) < 1e-6  # last sample: 25 ms − 5 ms minimum


def test_event_log_estimates_device_time(make_sample) -> None:
    bus = EventBus()
    sub = bus.subscribe("events.*")
    est = DeviceTimeEstimator()
    clock = FakeClock(start_ns=5_000_000)
    log = EventLog(bus, est, clock)
    ev0 = log.mark("before_any_sample")
    assert ev0.timestamp_source == "host_only"
    assert ev0.device_timestamp_ns == 5_000_000
    est.observe(make_sample(device_ns=1_000_000, host_ns=4_000_000))  # offset = 3 ms
    clock.advance_ns(1_000_000)
    ev1 = log.mark("trial_start", {"trial": 1})
    assert ev1.timestamp_source == "estimated"
    assert ev1.device_timestamp_ns == 6_000_000 - 3_000_000
    assert ev1.payload == {"trial": 1}
    assert ev1.payload_json() == '{"trial": 1}'
    est.set_offset(0)
    assert log.mark("x").device_timestamp_ns == clock.now_ns()
    assert len(log) == 3
    assert [e.name for e in log.all()] == [
        "before_any_sample",
        "trial_start",
        "x",
    ]
    delivered = sub.drain()
    assert len(delivered) == 3
    assert all(isinstance(e, Event) for e in delivered)


def test_event_log_explicit_time_and_intervals(make_sample) -> None:
    from ariastreamlab.events.markers import pair_intervals

    bus = EventBus()
    est = DeviceTimeEstimator()
    clock = FakeClock(start_ns=10_000_000)
    log = EventLog(bus, est, clock)
    est.observe(make_sample(device_ns=1_000_000, host_ns=10_000_000))  # offset 9 ms
    exact = log.mark("fixation", {"n": 1}, at_ns=1_500_000)
    assert exact.timestamp_source == "device" and exact.device_timestamp_ns == 1_500_000

    iid = log.begin("trial", {"trial": 1, "condition": "A"})
    assert iid == "trial-1" and log.open_intervals() == {iid: "trial"}
    clock.advance_ns(2_000_000)
    iid2 = log.begin("trial", {"trial": 2})
    clock.advance_ns(1_000_000)
    assert log.end("trial", {"correct": True}) == iid2  # the latest open one with that name
    assert log.end(iid, at_ns=9_000_000) == iid
    assert log.open_intervals() == {}
    with pytest.raises(ValueError, match="no open interval"):
        log.end("trial")

    ivs = log.intervals()
    assert [i.interval_id for i in ivs] == [iid, iid2]
    one = {i.interval_id: i for i in ivs}
    assert one[iid].payload == {"trial": 1, "condition": "A"}
    assert one[iid].end_ns == 9_000_000 and one[iid].end_source == "device"
    assert one[iid2].payload == {"trial": 2, "correct": True}
    assert one[iid2].duration_s == 0.001
    assert log.intervals("other") == []
    # an open interval is reported with end_ns None; an end without a start is ignored
    open_iid = log.begin("block")
    assert [i.end_ns for i in log.intervals("block")] == [None]
    events = [e for e in log.all() if e.name != "fixation"]
    events.append(Event("marker", "ghost", 1, 1, {"phase": "end", "interval": "ghost-9"}))
    assert {i.interval_id for i in pair_intervals(events)} == {iid, iid2, open_iid}
