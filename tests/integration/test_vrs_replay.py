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


def test_gaze_projection_matches_projectaria_tools() -> None:
    """Our NumPy Fisheye624 + CPF chain against the SDK's own camera model."""
    from projectaria_tools.core import mps

    from ariastreamlab.acquisition.aria_gen2 import convert as cv
    from ariastreamlab.acquisition.aria_gen2 import vrs_replay
    from ariastreamlab.processing.builtin.gaze import fisheye624_project, gaze_point_cpf

    provider = vrs_replay.open_provider(VRS)
    calib = provider.get_device_calibration()
    d = cv.calibration_to_dict(calib)
    assert d["transform_device_cpf"] is not None and "camera-rgb" in d["cameras"]
    cam = calib.get_camera_calib("camera-rgb")
    params = np.asarray(d["cameras"]["camera-rgb"]["projection_params"])
    t_cam_cpf = np.linalg.inv(
        np.asarray(d["cameras"]["camera-rgb"]["transform_device_camera"])
    ) @ np.asarray(d["transform_device_cpf"])
    rng = np.random.default_rng(0)
    for _ in range(50):
        yaw, pitch, depth = rng.uniform(-0.5, 0.5), rng.uniform(-0.4, 0.4), rng.uniform(0.3, 5)
        p_cpf = gaze_point_cpf(yaw, pitch, depth)
        ref_cpf = np.asarray(mps.get_eyegaze_point_at_depth(yaw, pitch, depth))
        assert np.allclose(p_cpf, ref_cpf, atol=1e-9)
        p_cam = (t_cam_cpf @ np.append(p_cpf, 1.0))[:3]
        ref_cam = (
            cam.get_transform_device_camera().inverse() @ calib.get_transform_device_cpf()
        ) @ ref_cpf
        assert np.allclose(p_cam, np.asarray(ref_cam).ravel(), atol=1e-9)
        ours = fisheye624_project(p_cam, params)
        ref = cam.project_no_checks(p_cam)
        assert ours is not None and np.allclose(ours, ref, atol=1e-6)


@pytest.mark.slow
def test_gaze_on_rgb_processor_over_a_real_recording(tmp_path: Path) -> None:
    from ariastreamlab.config import load_dict
    from ariastreamlab.storage.reader import SessionReader

    cfg = load_dict(
        {
            "experiment": {"name": "gaze", "participant": "P"},
            "streams": {"rgb": {"fps": 10}, "eye_gaze": {}},
            "processors": [
                {
                    "id": "g",
                    "type": "gaze.project_to_rgb",
                    "inputs": ["rgb", "eye_gaze"],
                    "outputs": ["gaze_rgb"],
                    "execution": {"join": "nearest", "tolerance_ms": 40},
                }
            ],
            "storage": {"root": str(tmp_path / "s")},
        }
    )
    rep = Experiment.replay(VRS, config=cfg, speed=0.0, start_s=2.0, end_s=6.0)
    with rep:
        rep.run()
        st = rep.processor_stats()["g"]
        root = rep.session_dir
    assert st.errors == 0 and st.fired >= 15  # the sample recording has RGB at 5 fps
    assert root is not None
    rows = list(SessionReader(root).iter_stream("gaze_rgb"))
    assert len(rows) == st.outputs
    inside = [r for r in rows if r.data["in_image"]]
    assert len(inside) >= len(rows) // 2
    r = inside[0]
    assert 0 <= r.data["x"] < 2560 and 0 <= r.data["y"] < 1920
    assert abs(r.data["gaze_dt_ns"]) <= 40_000_000
