"""Dashboard: the bus collector (no NiceGUI needed) and the served page (NiceGUI, mock run)."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.payloads import GazePayload, ImuPayload
from ariastreamlab.core.sample import DerivedSample, Provenance
from ariastreamlab.dashboard.state import DashboardState, channels_of, encode_jpeg
from ariastreamlab.events.markers import Event


def _wait(pred: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def test_channels_of_payloads() -> None:
    imu = channels_of(ImuPayload((1.0, 2.0, 3.0), (0.1, 0.2, 0.3)))
    assert set(imu) == {"accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"}
    assert imu["accel_z"] == (3.0, "m/s^2")
    gaze = channels_of(GazePayload(0.1, -0.2, 1.5))
    assert gaze["yaw"][0] == pytest.approx(0.1)
    assert channels_of({"a": 1, "b": "text", "c": 2.5}) == {"a": (1.0, ""), "c": (2.5, "")}
    assert channels_of(3.0) == {"value": (3.0, "")}
    assert channels_of("nope") == {}


def test_encode_jpeg_downscales_and_reports_size() -> None:
    img = np.zeros((300, 1280, 3), dtype=np.uint8)
    url, w, h = encode_jpeg(img, max_width=640)
    assert url.startswith("data:image/jpeg;base64,")
    assert (w, h) == (640, 150)
    url, w, h = encode_jpeg(np.zeros((40, 50), dtype=np.uint8))
    assert (w, h) == (50, 40)


def test_state_collects_everything(make_sample, provenance: Provenance) -> None:
    bus = EventBus()
    state = DashboardState(bus, image_fps=1000.0, max_channel_hz=0)
    state.start()
    try:
        t0 = 1_000_000_000_000
        for i in range(50):
            ts = t0 + i * 1_250_000
            bus.publish(
                "raw.imu_left",
                make_sample(
                    "imu_left", ts, ts + 5_000_000, i, ImuPayload((0, 0, 9.8 + i), (0, 0, 0))
                ),
            )
        bus.publish(
            "raw.rgb",
            make_sample("rgb", t0, t0 + 1, 0, np.full((60, 80, 3), 128, dtype=np.uint8)),
        )
        bus.publish(
            "derived.gaze_rgb",
            DerivedSample(
                "mock-0",
                "gaze_rgb",
                t0 + 10,
                ("rgb", "eye_gaze"),
                (t0, t0 + 10),
                "gaze_on_rgb",
                0,
                1,
                {"x_norm": 0.25, "y_norm": 0.75, "valid": True, "in_image": True},
                provenance,
            ),
        )
        bus.publish("health.pipeline", {"queue_drops": 3})
        bus.publish("health.link", {"connections": 1, "data_path": "usb"})
        bus.publish(
            "events.marker",
            Event("marker", "trial_start", t0 + 20, t0 + 25, {"n": 1}, "device"),
        )
        bus.publish(
            "events.session",
            Event("session", "acquisition_start", 5, 5, {}, "host_only"),
        )
        _wait(lambda: state.samples_seen >= 52 and len(state.events) == 2)
        _wait(lambda: "pipeline" in state.health and "link" in state.health)
    finally:
        state.stop()

    snap = state.snapshot()
    assert snap["t0_ns"] == t0
    assert snap["images"]["rgb"]["width"] == 80 and snap["images"]["rgb"]["height"] == 60
    assert state.image("rgb") is not None and state.image("rgb").data_url.startswith("data:")
    assert snap["gaze"]["gaze_rgb"]["x_norm"] == 0.25
    assert snap["channels"]["imu_left/accel_z"]["n"] == 50
    assert snap["channels"]["imu_left/accel_z"]["unit"] == "m/s^2"
    assert snap["health"]["pipeline"]["queue_drops"] == 3
    assert snap["health"]["link"]["data_path"] == "usb"
    kinds = {(e["kind"], e["name"], e["t_s"]) for e in snap["events"]}
    assert ("marker", "trial_start", 0.0) in kinds
    assert ("session", "acquisition_start", None) in kinds  # host-only time is not shown
    json.dumps(snap)  # everything is JSON-serialisable

    series = state.series(["imu_left/accel_z", "missing"], last_s=0.01)
    assert "missing" not in series
    pts = series["imu_left/accel_z"]
    assert pts and pts[-1][1] == pytest.approx(9.8 + 49)
    assert all(p[0] >= pts[-1][0] - 0.01 for p in pts)
    lo, hi = state.window(0.01)
    assert lo == pytest.approx(hi - 0.01) and hi == pytest.approx(49 * 1.25e-3)


def test_state_thins_high_rate_channels_and_skips_nan(make_sample) -> None:
    bus = EventBus()
    state = DashboardState(bus, max_channel_hz=100.0)
    state.start()
    try:
        for i in range(800):  # 1 s of 800 Hz
            ts = i * 1_250_000
            bus.publish(
                "raw.imu_left",
                make_sample("imu_left", ts, ts, i, ImuPayload((math.nan, 0, 1), (0, 0, 0))),
            )
        _wait(lambda: state.samples_seen == 800)
    finally:
        state.stop()
    keys = state.channel_keys()
    assert "imu_left/accel_x" not in keys  # all NaN → never created
    n = state.snapshot()["channels"]["imu_left/accel_z"]["n"]
    assert 95 <= n <= 101


def test_state_filters_to_requested_streams(make_sample) -> None:
    bus = EventBus()
    state = DashboardState(bus, streams=["imu_right"])
    state.start()
    try:
        bus.publish(
            "raw.imu_left", make_sample("imu_left", 1, 1, 0, ImuPayload((0, 0, 1), (0, 0, 0)))
        )
        bus.publish(
            "raw.imu_right", make_sample("imu_right", 2, 2, 0, ImuPayload((0, 0, 1), (0, 0, 0)))
        )
        _wait(lambda: "imu_right/accel_z" in state.channel_keys())
        time.sleep(0.05)
    finally:
        state.stop()
    assert "imu_left/accel_z" not in state.channel_keys()


@pytest.mark.slow
def test_dashboard_server_serves_page_and_state(tmp_path, monkeypatch) -> None:
    pytest.importorskip("nicegui")
    from ariastreamlab.api import Experiment
    from ariastreamlab.config.loader import load_dict

    monkeypatch.setenv("ARIASTREAMLAB_DASHBOARD", "1")
    monkeypatch.setenv("ARIASTREAMLAB_OPEN_BROWSER", "0")
    port = 18765 + (os.getpid() % 500)
    cfg = load_dict(
        {
            "extends": "preset:gaze_experiment",
            "experiment": {"name": "dash_test"},
            "mock": {"seed": 3, "realtime": True},
            "dashboard": {"enabled": True, "port": port, "open_browser": False},
        }
    )
    exp = Experiment(cfg, mock=True, duration_s=3.0, persist=False)
    with exp:
        assert exp.dashboard_url == f"http://127.0.0.1:{port}/"
        t = threading.Thread(target=exp.run, daemon=True)
        t.start()
        time.sleep(1.5)
        html = urllib.request.urlopen(exp.dashboard_url, timeout=5).read().decode()
        assert "AriaStreamLab" in html
        for page in ("monitor", "design", "sessions"):
            body = urllib.request.urlopen(exp.dashboard_url + page, timeout=5).read().decode()
            assert "AriaStreamLab" in body, page
        state = json.loads(
            urllib.request.urlopen(exp.dashboard_url + "api/state", timeout=5).read()
        )
        t.join(10)
    assert state["session"]["experiment"] == "dash_test"
    assert state["session"]["state"] == "running"
    assert "rgb" in state["images"]
    assert "gaze_rgb" in state["gaze"]
    assert "eye_gaze/yaw" in state["channels"]
    assert isinstance(state["streams"]["rgb"], dict) and state["streams"]["rgb"]["received"] > 0
    assert "gaze_on_rgb" in state["processors"]
    assert "pipeline" in state["health"]
    # the server survives the session: a second experiment attaches to the same process-wide server
    exp2 = Experiment(cfg, mock=True, duration_s=0.5, persist=False)
    with exp2:
        assert exp2.dashboard_url == exp.dashboard_url
        exp2.run()
    state = json.loads(urllib.request.urlopen(exp.dashboard_url + "api/state", timeout=5).read())
    assert state["session"]["state"] == "finished"


def test_dashboard_disabled_by_env(monkeypatch) -> None:
    from ariastreamlab.api import Experiment

    monkeypatch.setenv("ARIASTREAMLAB_DASHBOARD", "0")
    exp = Experiment.from_preset("mock_experiment", duration_s=0.2, persist=False, dashboard=True)
    assert exp.dashboard_enabled is False
    with exp:
        assert exp.dashboard_url is None
        exp.run()


def test_state_audio_level_trajectory_and_app_queues(make_sample) -> None:
    from ariastreamlab.core.payloads import AudioPayload, PosePayload

    bus = EventBus()
    state = DashboardState(
        bus,
        max_channel_hz=0,
        bus_stats_provider=lambda: {"dash:raw": {"enqueued": 10, "delivered": 8, "dropped": 2}},
    )
    state.start()
    try:
        audio = AudioPayload(np.full((320, 2), 3277, dtype=np.int16), 16000)
        bus.publish("raw.audio", make_sample("audio", 10, 10, 0, audio))
        for i in range(5):
            pose = PosePayload((float(i), float(2 * i), 1.0), (1.0, 0.0, 0.0, 0.0))
            bus.publish("raw.vio", make_sample("vio", 100 + i, 100 + i, i, pose))
        # a dict output with x/y (pixels) is not a pose
        bus.publish(
            "derived.gaze_rgb",
            DerivedSample(
                "m",
                "gaze_rgb",
                200,
                ("rgb",),
                (200,),
                "g",
                0,
                1,
                {"x": 5.0, "y": 6.0},
                make_sample().provenance,
            ),
        )
        _wait(lambda: state.samples_seen == 7)
    finally:
        state.stop()
    snap = state.snapshot()
    assert snap["channels"]["audio/level_db"]["unit"] == "dBFS"
    assert -21 < snap["channels"]["audio/level_db"]["last"] < -19  # 0.1 full scale ≈ −20 dBFS
    assert state.pose_streams() == ["vio"]
    assert state.trajectory("vio") == [[float(i), float(2 * i)] for i in range(5)]
    assert state.trajectory("vio", last_s=97e-9) == [[3.0, 6.0], [4.0, 8.0]]  # window ends at t=200
    assert state.trajectory("gaze_rgb") == [[5.0, 6.0]]  # data exists, but it is not a pose
    assert snap["app_queues"]["dash:raw"]["dropped"] == 2


def test_rerun_sink_logs_to_a_memory_recording(monkeypatch) -> None:
    pytest.importorskip("rerun")
    from ariastreamlab.api import Experiment
    from ariastreamlab.config.loader import load_dict

    monkeypatch.setenv("ARIASTREAMLAB_RERUN_SPAWN", "0")
    cfg = load_dict(
        {
            "extends": "preset:full_capture",
            "experiment": {"name": "rr"},
            "dashboard": {"rerun": True, "show": ["rgb", "imu_left", "vio", "eye_gaze"]},
            "mock": {"realtime": False, "seed": 2},
        }
    )
    with Experiment(cfg, mock=True, duration_s=0.5, persist=False) as exp:
        exp.run()
        sink = exp.rerun
        assert sink is not None and sink.recording is not None
        time.sleep(0.2)
    assert sink.logged > 50
    assert 1 <= len(sink._trails["vio"]) <= 6  # 10 Hz × 0.5 s; the sink may drop (DROP_OLDEST)


def test_sessions_browser_model(tmp_path: Path) -> None:
    from ariastreamlab.api import Experiment
    from ariastreamlab.config.loader import load_dict
    from ariastreamlab.dashboard.sessions import list_sessions, session_details

    assert list_sessions(tmp_path / "nowhere") == []
    cfg = load_dict(
        {
            "extends": "preset:rgb_imu",
            "experiment": {"name": "browse", "participant": "P7"},
            "storage": {"root": str(tmp_path)},
            "mock": {"realtime": False, "seed": 1},
        }
    )
    with Experiment(cfg, mock=True, duration_s=0.5) as exp:
        trial = exp.begin("trial", {"n": 1})
        exp.run()
        exp.end(trial)
        root = exp.session_dir
    (tmp_path / "junk").mkdir()  # no manifest: ignored
    broken = tmp_path / "zz_broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json")
    rows = list_sessions(tmp_path)
    assert [r.name for r in rows] == ["zz_broken", root.name]  # type: ignore[union-attr]
    assert rows[0].problem is not None and "unreadable" in rows[0].as_row()["status"]
    good = rows[1]
    assert (good.experiment, good.participant, good.origin, good.status) == (
        "browse",
        "P7",
        "mock",
        "completed",
    )
    assert good.streams >= 3 and good.events >= 4 and good.duration_s is not None
    row = good.as_row()
    assert row["duration"].endswith(" s") and row["path"] == str(root)
    d = session_details(root)  # type: ignore[arg-type]
    assert d.manifest["experiment"] == "browse" and d.size_bytes > 0
    assert "rgb" in d.summary_text and "session " in d.summary_text
    assert any(e["name"] == "trial" and e["kind"] == "marker" for e in d.events)
    assert d.intervals and d.intervals[0]["name"] == "trial" and d.intervals[0]["end_s"]
    assert d.problems == []
    bad = session_details(broken)
    assert bad.problems and "manifest" in bad.problems[0]
