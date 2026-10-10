from __future__ import annotations

import time
from collections import Counter

import numpy as np
import pytest

from ariastreamlab.acquisition.mock import MOCK_DEVICE_ID, MockSource, iter_mock_samples
from ariastreamlab.config.models import MockConfig
from ariastreamlab.core.bus import DropPolicy, EventBus
from ariastreamlab.core.errors import AcquisitionError
from ariastreamlab.core.payloads import AudioPayload, GazePayload, ImuPayload, PosePayload
from ariastreamlab.monitoring.stats import StatsTracker

STREAMS = {
    "rgb": {"fps": 10, "resolution": "2016x1512"},
    "imu_left": {"rate_hz": 800},
    "eye_gaze": {"rate_hz": 30},
    "vio": {"rate_hz": 10},
    "audio": {"sample_rate_hz": 16000},
    "slam_front_left": {"fps": 10},
}


def test_counts_match_rates_in_fast_mode(fast_mock: MockConfig) -> None:
    samples = list(iter_mock_samples(STREAMS, duration_s=2.0, mock=fast_mock))
    counts = Counter(s.stream_id for s in samples)
    assert counts["device_calibration"] == 1
    assert counts["rgb"] == 20
    assert counts["imu_left"] == 1600
    assert counts["eye_gaze"] == 60
    assert counts["vio"] == 20
    assert counts["audio"] == 100
    assert counts["slam_front_left"] == 20
    assert all(s.device_id == MOCK_DEVICE_ID and s.provenance.origin == "mock" for s in samples)


def test_payload_types_and_shapes(fast_mock: MockConfig) -> None:
    by_stream: dict[str, list] = {}
    for s in iter_mock_samples(STREAMS, duration_s=0.5, mock=fast_mock):
        by_stream.setdefault(s.stream_id, []).append(s)
    rgb = by_stream["rgb"][0].data
    assert isinstance(rgb, np.ndarray)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (378, 504, 3)
    slam = by_stream["slam_front_left"][0].data
    assert slam.shape == (128, 128)
    assert isinstance(by_stream["imu_left"][0].data, ImuPayload)
    assert isinstance(by_stream["eye_gaze"][0].data, GazePayload)
    assert isinstance(by_stream["vio"][0].data, PosePayload)
    audio = by_stream["audio"][0].data
    assert isinstance(audio, AudioPayload)
    assert audio.samples.shape == (320, 8)
    assert audio.timestamps_ns is not None
    assert by_stream["rgb"][0].metadata["camera_id"] == 64


def test_timestamps_are_monotonic_per_stream_without_jitter(fast_mock: MockConfig) -> None:
    last: dict[str, int] = {}
    seqs: dict[str, int] = {}
    for s in iter_mock_samples(STREAMS, duration_s=1.0, mock=fast_mock):
        if s.stream_id in last:
            assert s.device_timestamp_ns > last[s.stream_id]
            assert s.sequence_number == seqs[s.stream_id] + 1
        last[s.stream_id] = s.device_timestamp_ns
        seqs[s.stream_id] = s.sequence_number or 0
        assert (
            s.host_receive_timestamp_ns >= s.device_timestamp_ns - 10**12
        )  # host and device clocks differ by design


def test_deterministic_for_same_seed() -> None:
    cfg = MockConfig(seed=7, jitter_ms=0.2, realtime=False)
    a = list(iter_mock_samples({"rgb": {"fps": 10}, "imu_left": {}}, duration_s=0.5, mock=cfg))
    b = list(iter_mock_samples({"rgb": {"fps": 10}, "imu_left": {}}, duration_s=0.5, mock=cfg))
    assert [s.device_timestamp_ns for s in a] == [s.device_timestamp_ns for s in b]
    for x, y in zip(a, b, strict=True):
        if isinstance(x.data, np.ndarray):
            assert np.array_equal(x.data, y.data)
        else:
            assert x.data == y.data


def test_simulated_drops_and_out_of_order_are_counted() -> None:
    cfg = MockConfig(
        seed=3, jitter_ms=0.0, drop_probability=0.2, out_of_order_probability=0.1, realtime=False
    )
    bus = EventBus()
    sub = bus.subscribe("raw.imu_left", maxsize=10**6, policy=DropPolicy.DROP_NEWEST)
    src = MockSource({"imu_left": {}}, mock=cfg, duration_s=1.0)
    src.start(bus)
    assert src.wait(10)
    src.stop()
    items = sub.drain()
    stats = StatsTracker()
    for s in items:
        stats.observe(s)
    snap = stats.snapshot("imu_left")
    assert snap is not None
    src_stats = src.stats()
    assert src_stats.source_dropped["imu_left"] > 50
    assert snap.received + src_stats.source_dropped["imu_left"] == 800
    assert (
        snap.transport_dropped == src_stats.source_dropped["imu_left"]
    )  # gaps in seq == simulated drops
    assert snap.out_of_order > 10


def test_realtime_pacing_roughly_matches_wall_clock() -> None:
    cfg = MockConfig(seed=0, jitter_ms=0.0, realtime=True)
    bus = EventBus()
    sub = bus.subscribe("raw.eye_gaze", maxsize=1000)
    src = MockSource({"eye_gaze": {}}, mock=cfg, duration_s=0.5)
    t0 = time.monotonic()
    src.start(bus)
    assert src.wait(5)
    elapsed = time.monotonic() - t0
    src.stop()
    assert 0.45 <= elapsed <= 1.5
    assert 12 <= len(sub.drain()) <= 16


def test_stop_is_prompt_and_idempotent() -> None:
    cfg = MockConfig(realtime=True)
    src = MockSource({"imu_left": {}}, mock=cfg)
    src.start(EventBus())
    time.sleep(0.05)
    t0 = time.monotonic()
    src.stop()
    src.stop()
    assert time.monotonic() - t0 < 1.0
    assert src.finished.is_set()


def test_unknown_stream_rejected() -> None:
    with pytest.raises(AcquisitionError, match="unknown streams"):
        MockSource({"nope": {}})
