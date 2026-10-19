"""The live source end to end against a fake SDK: receiver, callbacks, monitor, manifest."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ariastreamlab import Experiment
from ariastreamlab.acquisition import aria_gen2
from ariastreamlab.acquisition.aria_gen2 import device as device_mod
from ariastreamlab.acquisition.base import DeviceState
from ariastreamlab.config import load_dict
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import DeviceError
from ariastreamlab.core.payloads import ImuPayload
from ariastreamlab.storage.events_store import read_events
from ariastreamlab.storage.manifest import Manifest
from ariastreamlab.storage.reader import SessionReader
from tests.unit.test_aria_gen2_control import FakeCli, FakeSdk

# --------------------------------------------------------------------------- fake receiver


class FakeTimeSync:
    def is_connected(self) -> bool:
        return True

    def is_valid(self) -> bool:
        return True


class FakeServer:
    def __init__(self, receiver: FakeStreamReceiver) -> None:
        self.receiver = receiver

    def connections(self) -> list[dict[str, str]]:
        return list(self.receiver.conns)


class FakeStreamReceiver:
    """Mimics ``aria.stream_receiver.StreamReceiver`` closely enough for the adapter."""

    instances: list[FakeStreamReceiver] = []

    def __init__(self, enable_image_decoding: bool = True) -> None:
        self.enable_image_decoding = enable_image_decoding
        self.callbacks: dict[str, Any] = {}
        self.vrs_dir: str | None = None
        self.server_config: Any = None
        self.started = False
        self.stopped = False
        self.conns: list[dict[str, str]] = []
        FakeStreamReceiver.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("register_") and name.endswith("_callback"):
            key = name[len("register_") : -len("_callback")]

            def register(cb: Any) -> None:
                self.callbacks[key] = cb

            return register
        if name.startswith("get_") and name.endswith("_queue_size"):
            return lambda: 3
        raise AttributeError(name)

    def set_server_config(self, cfg: Any) -> None:
        self.server_config = cfg

    def record_to_vrs(self, path: str) -> None:
        self.vrs_dir = path

    def start_server(self) -> FakeServer:
        self.started = True
        if self.vrs_dir:
            self._vrs = Path(self.vrs_dir) / "2026-10-14-10:00:00.vrs"
            self._vrs.write_bytes(b"VRS\x00")  # the writer leaves a stub until released
        return FakeServer(self)

    def get_time_sync(self) -> FakeTimeSync:
        return FakeTimeSync()

    def stop_server(self) -> None:
        self.stopped = True
        if self.vrs_dir:
            self._vrs.write_bytes(b"VRS" + b"\x00" * 5000)  # flushed on release

    # -- test helpers: the "device" pushing data --
    def connect_device(self, ip: str = "192.168.37.28") -> None:
        self.conns = [{"client_ip": ip, "device_serial": "1M0YDD5H980277"}]

    def push_imu(self, n: int, start_ns: int = 1_000_000_000) -> None:
        cb = self.callbacks["imu"]
        for i in range(n):
            m = types.SimpleNamespace(
                capture_timestamp_ns=start_ns + i * 1_250_000,
                accel_msec2=[0.0, 0.0, 9.81],
                gyro_radsec=[0.01, 0.0, 0.0],
                temperature=30.0,
            )
            cb(m, "imu-left")

    def push_rgb(self, n: int, start_ns: int = 1_000_000_000) -> None:
        cb = self.callbacks["rgb"]
        for i in range(n):
            img = types.SimpleNamespace(to_numpy_array=lambda: np.zeros((12, 16, 3), np.uint8))
            rec = types.SimpleNamespace(
                camera_id=64,
                frame_number=i,
                capture_timestamp_ns=start_ns + i * 100_000_000,
                exposure_duration=0.004,
                gain=1.0,
                arrival_timestamp_ns=0,
            )
            cb(img, rec)

    def push_raw(self, n: int) -> None:
        cb = self.callbacks.get("raw_message")
        if cb is None:
            return
        for _ in range(n):
            cb(types.SimpleNamespace(id="MessageType.ALS_EVENT"), 0)


class FakeHttpServerConfig:
    def __init__(self) -> None:
        self.address = ""
        self.port = 0


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSdk:
    sdk = FakeSdk()
    sdk.HttpServerConfig = FakeHttpServerConfig
    aria_pkg = types.ModuleType("aria")
    aria_pkg.__path__ = []  # type: ignore[attr-defined]
    sr = types.ModuleType("aria.stream_receiver")
    sr.StreamReceiver = FakeStreamReceiver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aria", aria_pkg)
    monkeypatch.setitem(sys.modules, "aria.sdk_gen2", sdk)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "aria.stream_receiver", sr)
    monkeypatch.setattr(aria_gen2, "sdk_available", lambda: True)
    monkeypatch.setattr(device_mod, "_import_sdk", lambda: sdk)
    monkeypatch.setattr(device_mod, "local_streaming_cert_name", lambda: None)
    monkeypatch.setattr(
        device_mod, "tcp_reachable", lambda ip, port=23456, timeout=3.0: ip == "192.168.37.28"
    )
    FakeStreamReceiver.instances.clear()
    return sdk


def _config(tmp_path: Path, **over: Any):
    data: dict[str, Any] = {
        "experiment": {"name": "live", "participant": "P9"},
        "streams": {"rgb": {"fps": 10}, "imu": {}},
        "processors": [
            {"id": "mag", "type": "imu.magnitude", "inputs": ["imu_left"], "outputs": ["mag"]}
        ],
        "device": {"connection": {"hotspot": {"start": False}}},
        "storage": {"root": str(tmp_path / "sessions"), "image_format": "png"},
        "dashboard": {"enabled": False},
    }
    data.update(over)
    return load_dict(data)


def _feed(receiver: FakeStreamReceiver, stop: threading.Event) -> None:
    receiver.connect_device()
    t = 1_000_000_000
    while not stop.is_set():
        receiver.push_imu(80, t)
        receiver.push_rgb(1, t)
        receiver.push_raw(2)
        t += 100_000_000
        time.sleep(0.1)


def test_live_experiment_end_to_end(
    fake_sdk: FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    original = aria_gen2.make_live_source

    def fast(config: Any, **kw: Any) -> Any:
        kw["manager"] = device_mod.AriaGen2DeviceManager(config.device, sdk=fake_sdk, cli=FakeCli())
        return original(config, health_interval_s=0.5, quality_interval_s=0.5, **kw)

    monkeypatch.setattr(aria_gen2, "make_live_source", fast)
    health: list[Any] = []
    exp = Experiment(cfg, duration_s=2.5)
    with exp:
        assert exp.session is not None and exp.session.origin == "live"
        sub = exp.bus.subscribe("health.*", maxsize=1000)
        stop = threading.Event()
        feeder: threading.Thread | None = None
        src = exp.source
        assert src is not None and src.capabilities.origin == "live" and src.capabilities.is_paced

        def start_feeding() -> None:
            nonlocal feeder
            deadline = time.monotonic() + 5
            while not FakeStreamReceiver.instances and time.monotonic() < deadline:
                time.sleep(0.01)
            while time.monotonic() < deadline and not FakeStreamReceiver.instances[0].started:
                time.sleep(0.01)
            feeder = threading.Thread(
                target=_feed, args=(FakeStreamReceiver.instances[0], stop), daemon=True
            )
            feeder.start()

        threading.Thread(target=start_feeding, daemon=True).start()
        exp.run()
        stop.set()
        if feeder is not None:
            feeder.join(2)
        health = sub.drain()
        stats = exp.stream_stats()
        src_stats = src.stats()
        root = exp.session_dir
    assert root is not None
    rec = FakeStreamReceiver.instances[0]
    assert rec.started and rec.stopped and rec.vrs_dir == str(root / "raw")
    assert rec.server_config.port == 6768 and rec.server_config.address == "0.0.0.0"
    assert set(rec.callbacks) >= {"imu", "rgb", "raw_message", "device_calib"}
    assert "slam" not in rec.callbacks  # not enabled ⇒ not registered
    assert stats["imu_left"].received >= 800 and stats["rgb"].received >= 10
    assert stats["mag"].received >= 800
    assert (
        src_stats.extra["data_path"] == "wifi"
        and src_stats.extra["stream_source_ip"] == "192.168.37.28"
    )
    assert src_stats.extra["control_transport"] == "wifi"  # moved to Wi-Fi during the run
    assert src_stats.extra["raw_messages"]["ALS_EVENT"] >= 2
    assert src_stats.extra["ticsync"] == {"connected": True, "valid": True}
    assert src.link["sdk_queues"]["imu"] == 3  # last snapshot before the receiver was released
    kinds = {type(h).__name__ for h in health}
    assert "DeviceHealth" in kinds and "QualityStats" in kinds and "dict" in kinds
    # manifest: device record, raw VRS with checksum, state events
    m = Manifest.load(root / "manifest.json")
    assert m.status == "completed" and m.origin == "live"
    assert m.device.serial == "1M0YDD5H980277"
    assert m.raw.kind == "host_received_vrs" and m.raw.files == ["raw/2026-10-14-10:00:00.vrs"]
    assert len(m.raw.sha256["raw/2026-10-14-10:00:00.vrs"]) == 64
    assert (root / "raw" / "2026-10-14-10:00:00.vrs").stat().st_size > 5000
    assert any("Wi-Fi" in w for w in m.warnings)
    extra = m.model_dump()
    assert extra["device"]["profile"] == "profile9" and extra["device"]["data_path"] == "wifi"
    evs = read_events(root / "events" / "events.parquet")
    states = [e.name for e in evs if e.kind == "device_state"]
    assert states[:4] == ["DISCOVERED", "CONNECTED", "STREAM_READY", "STREAMING"]
    assert states[-2:] == ["STOPPING", "CONNECTED"]
    # live samples are stored only as derived data; the raw ground truth is the VRS
    reader = SessionReader(root)
    assert "mag" in reader.streams and "imu_left" not in reader.streams
    rows = list(reader.iter_stream("mag"))
    assert rows[0].provenance.origin == "replay" and abs(rows[0].data.value - 9.81) < 1e-6
    assert fake_sdk.log.count("start_streaming") == 1 and fake_sdk.log.count("stop_streaming") == 1


def test_live_start_failure_marks_session_crashed(
    fake_sdk: FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sdk.usb = []  # nothing to connect to
    cfg = _config(tmp_path)
    original = aria_gen2.make_live_source
    monkeypatch.setattr(
        aria_gen2,
        "make_live_source",
        lambda config, **kw: original(
            config,
            manager=device_mod.AriaGen2DeviceManager(config.device, sdk=fake_sdk, cli=FakeCli()),
            **kw,
        ),
    )
    exp = Experiment(cfg, duration_s=1.0)
    with pytest.raises(DeviceError, match="no Aria Gen 2 glasses found"):
        with exp:
            exp.run()
    root = exp.session_dir
    assert root is not None
    m = Manifest.load(root / "manifest.json")
    assert m.status == "crashed"
    assert exp.source is not None and exp.source.manager.state is DeviceState.ERROR  # type: ignore[attr-defined]
    assert not FakeStreamReceiver.instances  # the server is never started without a device


def test_live_source_direct_with_bus(fake_sdk: FakeSdk, tmp_path: Path) -> None:
    from ariastreamlab.acquisition.aria_gen2.adapter import AriaGen2Source

    cfg = _config(tmp_path, streams={"imu": {}, "eye_gaze": {}})
    mgr = device_mod.AriaGen2DeviceManager(cfg.device, sdk=fake_sdk, cli=FakeCli())
    src = AriaGen2Source(
        cfg, session_id="s1", manager=mgr, health_interval_s=0.2, no_data_timeout_s=0.5
    )
    src.record_raw_to(tmp_path / "raw")
    bus = EventBus()
    sub = bus.subscribe("raw.imu_left", maxsize=10000)
    src.start(bus)
    with pytest.raises(DeviceError, match="twice"):
        src.start(bus)
    rec = FakeStreamReceiver.instances[0]
    assert set(rec.callbacks) >= {"imu", "eye_gaze"} and "rgb" not in rec.callbacks
    time.sleep(1.2)  # no device ⇒ "no data" warning
    assert any("no data received" in w for w in src.warnings)
    rec.connect_device(ip="192.168.68.165")  # the USB link address ⇒ data path usb
    rec.push_imu(10)
    time.sleep(1.2)
    assert src.data_path == "usb" and mgr.transport == "usb"
    src.stop()
    src.stop()  # idempotent
    items = sub.drain()
    assert len(items) == 10 and isinstance(items[0].data, ImuPayload)
    assert items[0].provenance.origin == "live" and items[0].device_id == "1M0YDD5H980277"
    assert items[3].device_timestamp_ns == 1_000_000_000 + 3 * 1_250_000
    assert items[0].sequence_number == 0 and items[9].sequence_number == 9
    assert src.vrs_files == [tmp_path / "raw" / "2026-10-14-10:00:00.vrs"]
    d = src.device_record()
    assert d["serial"] == "1M0YDD5H980277" and d["control_transport"] == "usb"
    assert json.dumps(d)  # JSON-able for the manifest
    assert src.finished.is_set()
    bus.close()
