"""Replay of a real Aria Gen 2 recording.

Needs ``projectaria_tools`` (``pip install 'ariastreamlab[aria]'``) and a recording at
``tests/data/aria_gen2_sample.vrs`` (see tests/data/README.md); skipped otherwise. The
``sdk`` backend additionally needs the client SDK.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from ariastreamlab import Experiment
from ariastreamlab.core.bus import DropPolicy, EventBus
from ariastreamlab.core.payloads import (
    AudioPayload,
    GazePayload,
    ImuPayload,
    PosePayload,
    ScalarPayload,
)
from ariastreamlab.storage.manifest import Manifest

VRS = Path(__file__).resolve().parents[1] / "data" / "aria_gen2_sample.vrs"
HAS_TOOLS = importlib.util.find_spec("projectaria_tools") is not None
HAS_SDK = (
    importlib.util.find_spec("aria") is not None
    and importlib.util.find_spec("aria.sdk_gen2") is not None
)

pytestmark = pytest.mark.skipif(
    not (HAS_TOOLS and VRS.exists()),
    reason="needs projectaria_tools and tests/data/aria_gen2_sample.vrs",
)


@pytest.fixture(scope="module")
def expected_counts() -> dict[str, int]:
    from ariastreamlab.acquisition.aria_gen2 import vrs_replay

    provider = vrs_replay.open_provider(VRS)
    return {
        ours: provider.get_num_data(sid) for ours, sid in vrs_replay.vrs_streams(provider).items()
    }


@pytest.mark.slow
def test_tools_backend_replays_every_record_with_correct_payloads(
    expected_counts: dict[str, int],
) -> None:
    from ariastreamlab.acquisition.aria_gen2.vrs_replay import VrsReplaySource

    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10**6, policy=DropPolicy.DROP_NEWEST)
    src = VrsReplaySource(
        VRS,
        session_id="t",
        speed=0.0,
        streams=[
            "imu_left",
            "eye_gaze",
            "vio",
            "audio",
            "ppg",
            "slam_front_left",
            "als",
            "temperature",
        ],
    )
    assert src.device_id == "1M0YDD5H980277"
    assert src.calibration is not None and "camera-rgb" in src.calibration["cameras"]
    src.start(bus)
    assert src.wait(300)
    src.stop()
    items = sub.drain()
    counts = Counter(s.stream_id for s in items)
    for sid in (
        "imu_left",
        "eye_gaze",
        "vio",
        "audio",
        "ppg",
        "slam_front_left",
        "als",
        "temperature",
    ):
        assert counts[sid] == expected_counts[sid], sid
    assert counts["device_calibration"] == 1
    by = {}
    for s in items:
        by.setdefault(s.stream_id, s)
    assert isinstance(by["imu_left"].data, ImuPayload)
    assert isinstance(by["eye_gaze"].data, GazePayload)
    assert isinstance(by["vio"].data, PosePayload)
    assert abs(sum(q * q for q in by["vio"].data.quaternion_wxyz) - 1.0) < 1e-3
    assert isinstance(by["audio"].data, AudioPayload)
    assert by["audio"].data.samples.shape[1] == 8
    assert isinstance(by["ppg"].data, ScalarPayload)
    assert by["als"].data.unit == "lux"
    assert by["temperature"].data.unit == "degC"
    slam = by["slam_front_left"].data
    assert isinstance(slam, np.ndarray) and slam.shape == (512, 512) and slam.dtype == np.uint8
    # per-stream device timestamps are monotonic and sequence numbers consecutive
    imu = [s for s in items if s.stream_id == "imu_left"]
    ts = [s.device_timestamp_ns for s in imu]
    assert ts == sorted(ts)
    assert [s.sequence_number for s in imu] == list(range(len(imu)))
    assert all(s.provenance.origin == "replay" for s in items)


def test_tools_backend_window_and_seek() -> None:
    from ariastreamlab.acquisition.aria_gen2.vrs_replay import VrsReplaySource

    bus = EventBus()
    sub = bus.subscribe("raw.imu_left", maxsize=10**6)
    src = VrsReplaySource(VRS, session_id="t", speed=0.0, streams=["imu_left"])
    rng = src.time_range()
    assert rng is not None
    src._start_ns = rng[0] + 2_000_000_000
    src._end_ns = rng[0] + 3_000_000_000
    src.start(bus)
    assert src.wait(120)
    src.stop()
    items = sub.drain()
    assert 790 <= len(items) <= 810
    assert items[0].device_timestamp_ns >= rng[0] + 2_000_000_000
    assert items[-1].device_timestamp_ns <= rng[0] + 3_000_000_000


@pytest.mark.slow
def test_experiment_replay_of_vrs_writes_session_with_calibration(tmp_path: Path) -> None:
    from ariastreamlab.config import load_dict

    cfg = load_dict(
        {
            "experiment": {"name": "vrs", "participant": "P"},
            "streams": {"imu": {}, "eye_gaze": {}},
            "storage": {"root": str(tmp_path / "s")},
        }
    )
    rep = Experiment.replay(VRS, config=cfg, speed=0.0, end_s=2.0)
    with rep:
        rep.run()
        stats = rep.stream_stats()
        root = rep.session_dir
    assert root is not None
    m = Manifest.load(root / "manifest.json")
    assert m.origin == "replay"
    assert m.device.serial == "1M0YDD5H980277"
    assert m.raw.kind == "replay_of" and m.raw.source == str(VRS)
    assert (root / "calibration" / "device_calibration.json").exists()
    assert 1550 <= stats["imu_left"].received <= 1650
    assert "slam_front_left" not in stats  # only the configured streams were replayed


@pytest.mark.slow
@pytest.mark.skipif(not HAS_SDK, reason="needs the Aria client SDK")
def test_sdk_backend_matches_tools_backend(expected_counts: dict[str, int]) -> None:
    from ariastreamlab.acquisition.aria_gen2.vrs_replay import SdkReplaySource

    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=10**6, policy=DropPolicy.DROP_NEWEST)
    src = SdkReplaySource(
        VRS,
        session_id="t",
        speed=0.0,
        streams=["imu_left", "imu_right", "eye_gaze", "vio", "barometer"],
    )
    src.start(bus)
    assert src.wait(300)
    src.stop()
    counts = Counter(s.stream_id for s in sub.drain())
    # The SDK engine dispatches through bounded per-stream queues, exactly like a live
    # device: at unbounded speed it occasionally drops a handful of samples itself
    # ("eye-gaze Stopped. Enqueued: 623, Processed: 616, Dropped: 7" in its own log).
    # The ``tools`` backend (the default) is exact; the ``sdk`` backend is "as live".
    for sid in ("imu_left", "imu_right", "eye_gaze", "vio", "barometer"):
        assert expected_counts[sid] - 20 <= counts[sid] <= expected_counts[sid], sid
    assert counts["device_calibration"] == 1
    assert src.stats().extra["callback_errors"] == {}
