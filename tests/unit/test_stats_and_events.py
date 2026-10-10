from __future__ import annotations

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
