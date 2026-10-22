from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from ariastreamlab import Experiment
from ariastreamlab.acquisition.replay import PacedReplay, SessionReplaySource, make_replay_source
from ariastreamlab.config import load_dict
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ReplayError
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.timing.clock import FakeClock


class ListReplay(PacedReplay):
    """Replay a fixed list of timestamps (device ns)."""

    def __init__(self, ts: list[int], **kw: object) -> None:
        self._ts = ts
        super().__init__(streams=("s",), source_id="list", session_id="sid", device_id="d", **kw)  # type: ignore[arg-type]

    def _iter_from(self, start_ns: int | None) -> Iterator[Sample]:
        prov = Provenance("replay", "list", "sid")
        for i, t in enumerate(self._ts):
            if start_ns is None or t >= start_ns:
                yield Sample("d", "s", t, 0, i, i, prov)

    def time_range(self) -> tuple[int, int] | None:
        return (self._ts[0], self._ts[-1])


def test_max_speed_replays_everything_in_order() -> None:
    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10_000)
    src = ListReplay([i * 1_000_000 for i in range(100)], speed=0.0)
    src.start(bus)
    assert src.wait(5)
    src.stop()
    items = sub.drain()
    assert [s.device_timestamp_ns for s in items] == [i * 1_000_000 for i in range(100)]
    assert all(s.provenance.origin == "replay" for s in items)
    assert items[5].host_receive_timestamp_ns > 0
    assert src.stats().received["s"] == 100
    assert src.position_ns == 99_000_000


def test_realtime_pacing_uses_the_clock() -> None:
    clock = FakeClock(start_ns=0)
    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10_000)
    src = ListReplay([0, 100_000_000, 200_000_000], speed=2.0, clock=clock)
    src.start(bus)
    assert src.wait(5)
    src.stop()
    items = sub.drain()
    # at 2x, 200 ms of device time take 100 ms of host time
    assert [s.host_receive_timestamp_ns for s in items] == [0, 50_000_000, 100_000_000]


def test_window_and_seek_and_pause() -> None:
    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10_000)
    src = ListReplay(
        [i * 1_000_000 for i in range(50)], speed=0.0, start_ns=10_000_000, end_ns=20_000_000
    )
    src.start(bus)
    assert src.wait(5)
    src.stop()
    assert [s.sequence_number for s in sub.drain()] == list(range(10, 21))

    # pause / resume / seek on a realtime replay with a slow fake clock
    clock = FakeClock(start_ns=0)
    src2 = ListReplay([i * 10_000_000 for i in range(1000)], speed=1.0, clock=clock)
    src2.pause()
    src2.start(bus)
    time.sleep(0.1)
    assert sub.drain() == []
    assert src2.paused
    src2.seek(500_000_000)
    src2.resume()
    deadline = time.monotonic() + 5
    while src2.stats().received.get("s", 0) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    src2.stop()
    seqs = [s.sequence_number for s in sub.drain()]
    assert seqs[0] == 50
    assert seqs == list(range(50, 50 + len(seqs)))


def test_set_speed_and_invalid_values() -> None:
    src = ListReplay([0, 1], speed=1.0)
    src.set_speed(0.5)
    assert src.speed == 0.5
    with pytest.raises(ReplayError):
        src.set_speed(-1)
    with pytest.raises(ReplayError):
        ListReplay([0], speed=-2.0)
    with pytest.raises(ReplayError):
        src.start(EventBus()) or src.start(EventBus())


def _mock_session(tmp_path: Path, **storage: object) -> Path:
    cfg = load_dict(
        {
            "experiment": {"name": "rp", "participant": "P1"},
            "streams": {"rgb": {"fps": 10}, "imu_left": {}, "eye_gaze": {}, "audio": {}},
            "storage": {
                "root": str(tmp_path / "sessions"),
                "image_format": "png",
                "chunk_seconds": 1000,
                **storage,
            },
            "mock": {
                "seed": 5,
                "jitter_ms": 0.1,
                "out_of_order_probability": 0.05,
                "realtime": False,
            },
        }
    )
    exp = Experiment(cfg, mock=True, duration_s=1.0)
    with exp:
        exp.mark("trial_start")
        exp.run()
        root = exp.session_dir
    assert root is not None
    return root


def test_session_replay_reproduces_mock_session_exactly(tmp_path: Path) -> None:
    root = _mock_session(tmp_path)
    original: dict[str, list[Sample]] = {}
    bus = EventBus()
    # re-generate the original samples for comparison (deterministic mock)
    from ariastreamlab.acquisition.mock import iter_mock_samples
    from ariastreamlab.config.models import MockConfig

    for s in iter_mock_samples(
        {"rgb": {"fps": 10}, "imu_left": {}, "eye_gaze": {}, "audio": {}},
        duration_s=1.0,
        mock=MockConfig(seed=5, jitter_ms=0.1, out_of_order_probability=0.05, realtime=False),
    ):
        original.setdefault(s.stream_id, []).append(s)
    sub = bus.subscribe("raw.*", maxsize=100_000)
    src = SessionReplaySource(root, session_id="new", speed=0.0)
    assert set(src.capabilities.streams) == {
        "rgb",
        "imu_left",
        "eye_gaze",
        "audio",
        "device_calibration",
    }
    src.start(bus)
    assert src.wait(30)
    src.stop()
    replayed: dict[str, list[Sample]] = {}
    for s in sub.drain():
        replayed.setdefault(s.stream_id, []).append(s)
    for sid in ("imu_left", "eye_gaze", "rgb", "audio"):
        o, r = sorted(original[sid], key=lambda s: s.device_timestamp_ns), replayed[sid]
        assert [s.device_timestamp_ns for s in o] == [s.device_timestamp_ns for s in r], sid
        assert [s.sequence_number for s in o] == [s.sequence_number for s in r], sid
        for a, b in zip(o, r, strict=True):
            if isinstance(a.data, np.ndarray):
                assert np.array_equal(a.data, b.data)
            elif sid == "audio":
                assert np.array_equal(a.data.samples, b.data.samples)
            else:
                assert a.data == b.data
    assert replayed["rgb"][0].provenance.origin == "replay"
    assert replayed["rgb"][0].device_id == "mock-0"
    # a stream subset never drops the calibration (processors such as gaze projection need it)
    subset = SessionReplaySource(root, session_id="sub", speed=0.0, streams=["imu_left"])
    assert set(subset.capabilities.streams) == {"imu_left", "device_calibration"}
    assert src.time_range() is not None


def test_experiment_replay_of_mock_session_writes_a_replay_session(tmp_path: Path) -> None:
    root = _mock_session(tmp_path)
    rep = Experiment.replay(root, speed=0.0)
    with rep:
        assert rep.session is not None
        assert rep.session.origin == "replay"
        ctl = rep.replay_control
        assert ctl is not None
        rep.run()
        stats = rep.stream_stats()
        new_root = rep.session_dir
    assert stats["imu_left"].received == 800
    assert stats["rgb"].received == 10
    assert new_root is not None and new_root != root
    from ariastreamlab.storage.manifest import Manifest

    m = Manifest.load(new_root / "manifest.json")
    assert m.origin == "replay"
    assert m.raw.kind == "replay_of"
    assert m.raw.source == str(root)
    assert (new_root / "raw" / "SOURCE").read_text().strip() == str(root)
    assert m.device.serial == "mock-0"
    assert m.streams["imu_left"].received == 800


def test_replay_with_window_and_stream_subset(tmp_path: Path) -> None:
    root = _mock_session(tmp_path)
    rep = Experiment.replay(
        root, speed=0.0, start_s=0.25, end_s=0.5, streams=["imu_left"], persist=False
    )
    with rep:
        rep.run()
        stats = rep.stream_stats()
    assert set(stats) == {"imu_left"}
    assert 195 <= stats["imu_left"].received <= 205


def test_make_replay_source_errors(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="neither"):
        make_replay_source(tmp_path / "nope", session_id="x")
    root = _mock_session(tmp_path, save_mock_raw=False)
    with pytest.raises(ReplayError, match="no raw stream"):
        make_replay_source(root, session_id="x")
