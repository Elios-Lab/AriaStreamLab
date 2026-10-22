"""Quality monitoring: the health log and the end-of-session summary."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ariastreamlab.core.bus import EventBus
from ariastreamlab.monitoring.health import HealthLog, directory_size, jsonable
from ariastreamlab.monitoring.quality import QualitySummary, build_summary, format_bytes
from ariastreamlab.monitoring.stats import StreamStats


def _stats(
    sid: str, received: int, nominal: float | None, effective: float | None, **kw
) -> StreamStats:
    return StreamStats(
        stream_id=sid,
        received=received,
        effective_rate_hz=effective,
        nominal_rate_hz=nominal,
        out_of_order=kw.get("out_of_order", 0),
        transport_dropped=kw.get("transport_dropped", 0),
        first_device_ns=0,
        last_device_ns=kw.get("last_device_ns", 10_000_000_000),
        last_host_ns=kw.get("last_host_ns"),
        mean_host_latency_ms=None,
        mean_rate_hz=kw.get("mean_rate_hz", effective),
        outage_s=kw.get("outage_s", 0.0),
        outages=kw.get("outages", ()),
    )


def test_build_summary_puts_the_four_kinds_of_loss_side_by_side() -> None:
    summary = build_summary(
        stream_stats={
            "rgb": _stats("rgb", 5988, 10, 9.98, transport_dropped=9),
            "imu_left": _stats("imu_left", 479_400, 800, 799.0),
            "imu_right": _stats("imu_right", 479_400, 800, 799.0),
            "eye_gaze": _stats("eye_gaze", 150, 30, 20.0),
            "slam_front_left": _stats("slam_front_left", 0, 10, None),
            "gaze_rgb": _stats("gaze_rgb", 5900, None, 9.9),
        },
        raw_stream_ids={"rgb", "imu_left", "imu_right", "eye_gaze", "slam_front_left"},
        bus_subscriptions={
            "dash:raw": {"enqueued": 100, "delivered": 88, "dropped": 12},
            "recorder": {"enqueued": 100, "delivered": 100, "dropped": 0},
        },
        processor_stats={
            "gaze_on_rgb": {
                "fired": 5900,
                "outputs": 5900,
                "errors": 2,
                "queue_dropped": {"rgb": 40, "eye_gaze": 0},
                "exec_ms_p95": 0.5,
            }
        },
        device_quality={
            "sensors": [
                {"sensor_type": "CAMERA_POV", "streams": ["rgb"], "dropped": 3, "expected": 6000},
                {
                    "sensor_type": "IMU_1",
                    "streams": ["imu_left", "imu_right"],
                    "dropped": 1,
                    "expected": 480000,
                },
                {
                    "sensor_type": "IMU_2",
                    "streams": ["imu_left", "imu_right"],
                    "dropped": 2,
                    "expected": 480000,
                },
            ]
        },
        device_history=[
            {
                "t_s": 0,
                "battery_percent": 90,
                "skin_temperature_c": 30.0,
                "thermal_mitigation": False,
                "wifi_connected": True,
            },
            {
                "t_s": 412,
                "battery_percent": 60,
                "skin_temperature_c": 42.5,
                "thermal_mitigation": True,
                "wifi_connected": True,
            },
            {
                "t_s": 600,
                "battery_percent": 12,
                "skin_temperature_c": 41.0,
                "thermal_mitigation": True,
                "wifi_connected": False,
            },
        ],
        duration_s=600.2,
        disk_bytes=1_840_000_000,
        disk_free_bytes=1_000_000_000,
        peak_write_rate_bps=3_000_000,
        extra_warnings=["source: wifi rssi low"],
    )
    rows = {r.stream_id: r for r in summary.streams}
    assert [r.stream_id for r in summary.streams][-1] == "gaze_rgb"  # derived last
    assert rows["rgb"].device_dropped == 3 and not rows["rgb"].device_shared
    assert rows["imu_left"].device_dropped == 3 and rows["imu_left"].device_shared
    assert rows["rgb"].processor_dropped == 40 and rows["rgb"].transport_dropped == 9
    assert rows["gaze_rgb"].kind == "derived" and rows["gaze_rgb"].device_dropped is None
    assert summary.device["battery_percent_start"] == 90
    assert summary.device["skin_temperature_c_max"] == 42.5
    w = "\n".join(summary.warnings)
    assert "thermal mitigation active from 412 s" in w
    assert "battery at 12 %" in w
    assert "wifi reported disconnected" in w
    assert "slam_front_left: no samples received" in w
    assert "rgb: 3 samples dropped on the device" in w
    assert "imu_left: 3 samples dropped on the device (counter shared" in w
    assert "rgb: 9 samples lost in transport" in w
    assert "rgb: 40 samples dropped at processor queues" in w
    assert "eye_gaze: mean 20.0 Hz is below 80 % of the expected 30 Hz" in w
    assert "app queue 'dash:raw' dropped 12 items" in w
    assert "processor 'gaze_on_rgb' raised 2 error(s)" in w
    assert "only 1.00 GB free" in w
    assert "source: wifi rssi low" in w

    text = summary.text()
    assert "rgb " in text and "device-dropped 3 " in text and "mean 9.98 Hz (expected 10)" in text
    assert "imu_left" in text and "device-dropped 3 (shared)" in text
    assert "app queues   dash:raw dropped 12" in text
    assert "gaze_on_rgb 5900→5900 errors 2 p95 0.50 ms" in text
    assert "duration 600.2 s   disk 1.84 GB (free 1.00 GB)" in text
    assert "battery 90→12 %" in text and "skin temp max 42.5 °C" in text
    assert f"warnings: {len(summary.warnings)}" in text

    # round trip through the manifest's dict form
    again = QualitySummary.from_dict(json.loads(json.dumps(summary.as_dict())))
    assert again.text() == text
    assert (
        format_bytes(None) == "–"
        and format_bytes(512) == "512 B"
        and format_bytes(2.5e6) == "2.50 MB"
    )


@dataclass
class _Health:
    battery_percent: int
    skin_temperature_c: float
    thermal_mitigation: bool
    charging: bool = False
    wifi_connected: bool = True


class _Quality:
    def as_dict(self) -> dict:
        return {"sensors": [{"sensor_type": "CAMERA_POV", "streams": ["rgb"], "dropped": 1}]}


def test_health_log_records_snapshots_and_storage(tmp_path: Path) -> None:
    bus = EventBus()
    seen = bus.subscribe("health.storage", maxsize=10)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "x.bin").write_bytes(b"x" * 1000)
    hl = HealthLog(bus, session_dir=tmp_path, storage_interval_s=0.2)
    hl.start()
    try:
        bus.publish("health.device", _Health(90, 31.5, False))
        bus.publish("health.quality", _Quality())
        bus.publish("health.link", {"connections": 1, "data_path": "usb"})
        bus.publish("health.pipeline", {"p": {"fired": 1}})
        deadline = time.monotonic() + 3
        while (
            len(hl.device_history) < 1 or "quality" not in hl.latest
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        (tmp_path / "raw" / "y.bin").write_bytes(b"y" * 4000)
        msg = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for m in seen.drain():
                if m["bytes"] >= 5000:
                    msg = m
            if msg:
                break
            time.sleep(0.05)
    finally:
        hl.stop()
    assert msg is not None and msg["free_bytes"] is not None and msg["path"] == str(tmp_path)
    snap = hl.snapshot()
    assert snap["device"]["battery_percent"] == 90 and snap["device"]["thermal_mitigation"] is False
    assert snap["quality"]["sensors"][0]["dropped"] == 1
    assert snap["link"]["data_path"] == "usb" and snap["storage"]["bytes"] >= 5000
    assert hl.device_history[0]["skin_temperature_c"] == 31.5
    dev_lines = (tmp_path / "quality" / "device_health.jsonl").read_text().splitlines()
    q_lines = (tmp_path / "quality" / "device_quality_stats.jsonl").read_text().splitlines()
    assert json.loads(dev_lines[0])["battery_percent"] == 90 and "t_s" in json.loads(dev_lines[0])
    assert json.loads(q_lines[0])["sensors"][0]["streams"] == ["rgb"]
    assert directory_size(tmp_path / "raw") == 5000 and directory_size(tmp_path / "nope") == 0
    assert jsonable({"a": (1, Path("/x")), "b": _Health(1, 2.0, True)})["b"]["charging"] is False


def test_mock_session_writes_quality_summary(tmp_path: Path) -> None:
    from ariastreamlab.api import Experiment
    from ariastreamlab.config.loader import load_dict

    cfg = load_dict(
        {
            "extends": "preset:rgb_imu",
            "experiment": {"name": "q"},
            "storage": {"root": str(tmp_path)},
            "mock": {"realtime": False, "drop_probability": 0.05, "seed": 3},
        }
    )
    with Experiment(cfg, mock=True, duration_s=2.0) as exp:
        exp.run()
        root = exp.session_dir
    assert root is not None and exp.quality_summary is not None
    qs = exp.quality_summary
    assert qs.disk_bytes is not None and qs.disk_bytes > 0 and qs.duration_s is not None
    rows = {r.stream_id: r for r in qs.streams}
    assert rows["imu_left"].transport_dropped > 0  # the mock dropped 5 % in "transport"
    assert any("lost in transport" in w for w in qs.warnings)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["quality"]["streams"]["rgb"]["received"] == rows["rgb"].received
    assert manifest["quality"]["session"]["disk_bytes"] == qs.disk_bytes
    assert (root / "quality" / "summary.txt").read_text().startswith(qs.text().splitlines()[0])
    loaded = json.loads((root / "quality" / "summary.json").read_text())
    assert QualitySummary.from_dict(loaded).text() == qs.text()
    assert exp.summary()["quality"]["warnings"] == qs.warnings


def test_summary_coverage_device_counts_and_triage() -> None:
    """Hardware run 15: the glasses stopped streaming after 640 s; the summary must say so."""
    # 9.4 min of data at 800 Hz, then nothing until the stop 630 s later (host time)
    summary = build_summary(
        stream_stats={
            "imu_left": _stats(
                "imu_left",
                445_410,
                800,
                798.9,  # the stale window rate — not what the summary should print
                last_device_ns=564_000_000_000,
                last_host_ns=1_000_000_000_000,
                mean_rate_hz=789.7,
            ),
            "rgb": _stats(
                "rgb",
                2768,
                10,
                5.0,
                last_device_ns=564_000_000_000,
                last_host_ns=1_000_000_000_000,
                mean_rate_hz=4.9,
                outage_s=12.0,
                outages=((100.0, 12.0),),
            ),
        },
        raw_stream_ids={"imu_left", "rgb"},
        device_quality={
            "sensors": [
                {"sensor_type": "CAMERA_POV", "streams": ["rgb"], "dropped": 0, "actual": 2819},
                {
                    "sensor_type": "IMU_1",
                    "streams": ["imu_left", "imu_right"],
                    "dropped": 0,
                    "actual": 1,
                },
            ]
        },
        device_history=[  # the glasses are the access point: wifi_connected is always False
            {
                "t_s": 76,
                "skin_temperature_c": 29.1,
                "thermal_mitigation": False,
                "wifi_connected": False,
            },
            {
                "t_s": 640,
                "skin_temperature_c": 42.2,
                "thermal_mitigation": True,
                "wifi_connected": False,
            },
            {
                "t_s": 1270,
                "skin_temperature_c": 31.9,
                "thermal_mitigation": False,
                "wifi_connected": False,
            },
        ],
        duration_s=1275.6,
        streaming_s=1200.0,
        expected_rates_hz={"rgb": 5.0, "imu_left": 800.0},
        end_host_ns=1_000_000_000_000 + 630_000_000_000,
        extra_warnings=["stream stalled: no sample for 30 s after 445410 samples"],
        notes=["control moved to Wi-Fi (192.168.101.211)"],
    )
    rows = {r.stream_id: r for r in summary.streams}
    imu = rows["imu_left"]
    assert imu.span_s == 564.0 + 630.0 and imu.outage_s == 630.0
    assert imu.outages == [[564.0, 630.0]] and imu.coverage == round(564 / 1194, 3)
    assert imu.device_sent is None  # the IMU counter is shared between the two IMUs
    rgb = rows["rgb"]
    assert rgb.outages == [[100.0, 12.0], [564.0, 630.0]] and rgb.outage_s == 642.0
    assert rgb.device_sent == 2819 and rgb.expected_rate_hz == 5.0
    w = "\n".join(summary.warnings)
    assert (
        "imu_left: data for only 47% of the time — 1 outage(s), the first at 564 s lasting 630 s (until the stop)"
        in w
    )
    assert (
        "rgb: data for only 46% of the time — 2 outage(s), the first at 100 s lasting 12 s (until the stop)"
        in w
    )
    assert "rgb: the device counted 2819 samples, the host received 2768 (51 missing, 1.8%)" in w
    assert "thermal mitigation active from 640 s" in w
    assert "wifi reported disconnected" not in w  # never connected as a station: no warning
    assert "below 80 %" not in w  # RGB at 5 Hz is what profile9 delivers: expected, not a problem
    assert "stream stalled" in w
    assert summary.notes == ["control moved to Wi-Fi (192.168.101.211)"]
    text = summary.text()
    assert "mean 4.90 Hz (expected 5)" in text and "coverage 46% (642 s missing)" in text
    assert "duration 1275.6 s (streaming 1200.0 s)" in text
    assert "notes: 1" in text and "· control moved to Wi-Fi" in text
    assert QualitySummary.from_dict(summary.as_dict()).text() == text


def test_stats_tracker_outages_and_stale_rate(make_sample) -> None:
    from ariastreamlab.monitoring.stats import StatsTracker

    tracker = StatsTracker({"imu_left": 800.0})
    period = 1_250_000
    t = 0
    for i in range(400):  # 0.5 s of data
        tracker.observe(make_sample(device_ns=t, host_ns=t, seq=i))
        t += period
    t += 3_000_000_000  # a three-second hole
    for i in range(400, 800):
        tracker.observe(make_sample(device_ns=t, host_ns=t, seq=i))
        t += period
    s = tracker.snapshot("imu_left")
    assert s is not None
    assert s.outage_s == 3.001 and s.outages == ((0.499, 3.001),)  # the gap includes a period
    assert s.mean_rate_hz is not None and 190 < s.mean_rate_hz < 210  # 799 samples over 4 s
    assert s.effective_rate_hz is not None and 190 < s.effective_rate_hz < 210  # 5 s window
    tracker.paced = True  # live: the host clock is far ahead of the last sample ⇒ stale ⇒ 0
    assert tracker.snapshot("imu_left").effective_rate_hz == 0.0  # type: ignore[union-attr]
