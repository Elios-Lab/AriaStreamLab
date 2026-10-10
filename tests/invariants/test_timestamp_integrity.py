"""Invariant I-3: device timestamps are never replaced, and I-1 groundwork: mock determinism.

The full I-1 test (mock → record → replay from raw → identical derived output) lands with
Phase 2; this file pins the properties it relies on.
"""

from __future__ import annotations

import dataclasses

import pytest

from ariastreamlab.acquisition.mock import iter_mock_samples
from ariastreamlab.config.models import MockConfig
from ariastreamlab.core.bus import DropPolicy, EventBus
from ariastreamlab.core.sample import Provenance, Sample


def test_sample_is_immutable_and_typed(provenance: Provenance) -> None:
    s = Sample("d", "rgb", 10, 20, 0, None, provenance)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.device_timestamp_ns = 11  # type: ignore[misc]
    with pytest.raises(TypeError, match="device_timestamp_ns must be an int"):
        Sample("d", "rgb", 10.0, 20, 0, None, provenance)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Sample("d", "rgb", 10, 20.0, 0, None, provenance)  # type: ignore[arg-type]
    assert s.timestamp_ns == 10


def test_bus_delivers_the_same_object_without_copying(provenance: Provenance) -> None:
    bus = EventBus()
    a = bus.subscribe("raw.*", policy=DropPolicy.DROP_NEWEST, maxsize=10)
    b = bus.subscribe("raw.rgb", policy=DropPolicy.LATEST)
    s = Sample("d", "rgb", 123_456_789, 1, 7, b"payload", provenance)
    bus.publish("raw.rgb", s)
    got_a, got_b = a.get_nowait(), b.get_nowait()
    assert got_a is s
    assert got_b is s
    assert got_a is not None
    assert got_a.device_timestamp_ns == 123_456_789


def test_mock_device_timestamps_do_not_depend_on_host_pacing() -> None:
    fast = MockConfig(seed=11, jitter_ms=0.2, realtime=False)
    slow = MockConfig(seed=11, jitter_ms=0.2, realtime=True)
    streams = {"eye_gaze": {}, "rgb": {"fps": 10}}
    a = [
        (s.stream_id, s.device_timestamp_ns, s.sequence_number)
        for s in iter_mock_samples(streams, duration_s=0.3, mock=fast)
    ]
    b = [
        (s.stream_id, s.device_timestamp_ns, s.sequence_number)
        for s in iter_mock_samples(streams, duration_s=0.3, mock=slow)
    ]
    assert a == b
