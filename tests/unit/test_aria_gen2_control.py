"""The adapter's control plane without glasses: CLI parsing, quality parser, plan, manager."""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ariastreamlab.acquisition.aria_gen2 import cli as cli_mod
from ariastreamlab.acquisition.aria_gen2 import device as device_mod
from ariastreamlab.acquisition.aria_gen2.cli import AriaCli, parse_hotspot, strip_ansi
from ariastreamlab.acquisition.aria_gen2.device import AriaGen2DeviceManager, health_from_status
from ariastreamlab.acquisition.aria_gen2.profiles import build_plan
from ariastreamlab.acquisition.aria_gen2.quality import parse_quality_stats
from ariastreamlab.acquisition.base import DeviceInfo, DeviceState
from ariastreamlab.config import load_dict
from ariastreamlab.config.models import DeviceConfig
from ariastreamlab.core.errors import ConfigError, DeviceError

HOTSPOT_OUT = "\x1b[32mWifi hotspot status\x1b[0m\nenabled: 1\nssid: Aria-M2FHCZP6\npassphrase: 10380804\nwifi band: BAND_5GHZ\nwifi channel bandwidth: BW_40MHZ\n"

QUALITY_JSON = {
    "data_type": "GetSensorHealthStats",
    "data": {
        "sensor_health_stats_list": [
            {
                "sensor_type": "IMU_1",
                "expected_frame_count": 0,
                "actual_frame_count": 16588,
                "dropped_frame_count": 0,
                "early_time_error_count": 0,
                "late_time_error_count": 0,
                "total_time_error_count": 0,
                "score": 100.0,
                "sequential_drops": 0,
            },
            {
                "sensor_type": "CAMERA_POV",
                "expected_frame_count": 100,
                "actual_frame_count": 95,
                "dropped_frame_count": 5,
                "early_time_error_count": 0,
                "late_time_error_count": 0,
                "total_time_error_count": 0,
                "score": 95.0,
                "sequential_drops": 2,
            },
            {
                "sensor_type": "MAG",
                "expected_frame_count": 0,
                "actual_frame_count": 5620,
                "dropped_frame_count": 0,
                "early_time_error_count": 1,
                "late_time_error_count": 1,
                "total_time_error_count": 2,
                "score": 99.9,
                "sequential_drops": 0,
            },
            {"sensor_type": "NEW_SENSOR_XYZ", "actual_frame_count": 3},
        ]
    },
}


# --------------------------------------------------------------------------- CLI wrapper


def _fake_cli(tmp_path: Path, script: str) -> str:
    exe = tmp_path / "aria_gen2"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_parse_hotspot_and_strip_ansi() -> None:
    assert strip_ansi("\x1b[1mcert_12\x1b[0m") == "cert_12"
    info = parse_hotspot(HOTSPOT_OUT)
    assert (info.ssid, info.password, info.device_ip) == ("Aria-M2FHCZP6", "10380804", None)
    assert "BAND_5GHZ" in info.raw_output
    assert parse_hotspot("nothing useful").ssid is None


def test_cli_run_arguments_and_parsers(tmp_path: Path) -> None:
    (tmp_path / "hotspot.txt").write_text(HOTSPOT_OUT)
    exe = _fake_cli(
        tmp_path,
        'echo "$@" > "$0.args"\n'
        f'case "$*" in *"hotspot start"*) cat "{tmp_path / "hotspot.txt"}";;\n'
        '*"install-certs"*) printf "Installed streaming certificate \\033[32mcert_7\\033[0m\\n";;\n'
        '*"auth check"*) echo "Device is authenticated";;\n'
        '*) echo "ok";; esac\n',
    )
    c = AriaCli(executable=exe, serial="1M0YDD5H980277")
    info = c.hotspot_start("5GHz", "40MHz")
    assert info.ssid == "Aria-M2FHCZP6"
    args = Path(exe + ".args").read_text().split()
    assert args == [
        "--serial",
        "1M0YDD5H980277",
        "device",
        "hotspot",
        "start",
        "--wifi-band",
        "BAND_5GHZ",
        "--channel-bandwidth",
        "BW_40MHZ",
    ]
    c.hotspot_start("2.4GHz", "20MHz", ble=True)
    args = Path(exe + ".args").read_text().split()
    assert args[:3] == ["--ble", "--serial", "1M0YDD5H980277"] and "BAND_2GHZ" in args
    assert c.install_streaming_certs() == "cert_7"
    assert c.auth_check() is True
    assert len(c.history) == 4 and c.history[0].ok
    with pytest.raises(DeviceError, match="serial"):
        AriaCli(executable=exe).run("device list", ble=True)
    c2 = AriaCli(executable=exe, device_ip="192.168.37.28")
    c2.run("device list")
    assert Path(exe + ".args").read_text().split()[:2] == ["--device-ip", "192.168.37.28"]


def test_cli_missing_and_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
    assert not AriaCli().available()
    with pytest.raises(DeviceError, match="not on PATH"):
        AriaCli().run("device list")
    exe = _fake_cli(tmp_path, 'echo "boom: no device" >&2\nexit 3\n')
    with pytest.raises(DeviceError, match="exit 3"):
        AriaCli(executable=exe).hotspot_start("5GHz", "40MHz")


# --------------------------------------------------------------------------- quality parser


def test_quality_parser_observed_format() -> None:
    q = parse_quality_stats(json.dumps(QUALITY_JSON))
    assert q.parse_error is None and q.data_type == "GetSensorHealthStats"
    by = {s.sensor_type: s for s in q.sensors}
    imu = by["IMU_1"]
    assert imu.actual == 16588 and imu.expected == 0 and not imu.expected_known
    assert imu.device_drop_fraction == 0.0 and imu.streams == ("imu_left", "imu_right")
    pov = by["CAMERA_POV"]
    assert pov.expected_known and pov.device_drop_fraction == pytest.approx(0.05)
    assert pov.sequential_drops == 2 and pov.streams == ("rgb",)
    assert by["MAG"].time_errors_total == 2
    unknown = by["NEW_SENSOR_XYZ"]
    assert (
        unknown.streams == () and unknown.dropped is None and unknown.raw["actual_frame_count"] == 3
    )
    assert q.by_stream()["rgb"] is pov
    assert q.total_device_dropped() == 5
    assert q.as_dict()["sensors"][1]["time_errors"]["total"] == 0


def test_quality_parser_is_defensive() -> None:
    assert parse_quality_stats(None).parse_error == "empty payload"
    assert "not JSON" in (parse_quality_stats("{oops").parse_error or "")
    assert parse_quality_stats("[1,2]").parse_error == "top level is not an object"
    assert parse_quality_stats('{"data": {}}').parse_error == "no sensor_health_stats_list"
    assert parse_quality_stats(QUALITY_JSON).sensors  # dict input works too
    assert parse_quality_stats(b'{"data": {"sensor_health_stats_list": []}}').sensors == ()


# --------------------------------------------------------------------------- plan


def _cfg(**over: Any):
    data: dict[str, Any] = {
        "experiment": {"name": "p"},
        "streams": {"rgb": {"fps": 10}, "imu": {}, "et": {}, "als": {}},
        "storage": {"root": "/tmp/x"},
    }
    data.update(over)
    return load_dict(data)


def test_build_plan_predefined_warnings(tmp_path: Path) -> None:
    plan = build_plan(_cfg(), tmp_path / "raw")
    assert plan.profile_name == "profile9" and plan.profile_json is None
    assert plan.interface == "wifi_sap" and plan.batch_period_ms == 800
    assert plan.record_vrs_path == str(tmp_path / "raw") and plan.server_port == 6768
    assert "als" not in plan.streams and "device_calibration" in plan.streams
    joined = " | ".join(plan.warnings)
    assert "als" in joined and "no live callback" in joined
    assert "et_left" in joined and "does not stream" in joined
    assert "rgb: profile9 delivers about 5 Hz" in joined
    plan2 = build_plan(_cfg(storage={"root": "/tmp/x", "save_raw_vrs": False}), tmp_path)
    assert plan2.record_vrs_path is None
    assert build_plan(_cfg(), None).record_vrs_path is None


def test_build_plan_named_and_custom(tmp_path: Path) -> None:
    plan = build_plan(
        _cfg(
            device={
                "profile": {"mode": "named", "name": "profile10"},
                "connection": {"streaming_interface": "usb"},
            }
        ),
        None,
    )
    assert plan.profile_name == "profile10" and plan.batch_period_ms == 0
    assert any("not been measured" in w for w in plan.warnings)
    with pytest.raises(ConfigError, match="custom_json"):
        build_plan(_cfg(device={"profile": {"mode": "custom"}}), None)
    with pytest.raises(ConfigError, match="not found"):
        build_plan(
            _cfg(device={"profile": {"mode": "custom", "custom_json": str(tmp_path / "no.json")}}),
            None,
        )
    pj = tmp_path / "p.json"
    pj.write_text('{"profile": "x"}')
    plan = build_plan(_cfg(device={"profile": {"mode": "custom", "custom_json": str(pj)}}), None)
    assert plan.profile_json == '{"profile": "x"}' and plan.profile_name is None


# --------------------------------------------------------------------------- fake SDK + manager


class FakeStatus:
    battery_level = 77
    charger_connected = True
    charging = True
    wifi_enabled = True
    wifi_connected = False
    wifi_ip_address = ""
    wifi_ssid = ""
    thermal_mitigation_triggered = False
    skin_temp_celsius = 31.5
    device_mode = "DeviceMode.Research"


class FakeInfo:
    is_streaming = True
    profile_name = "profile9"
    streaming_interface = "StreamingInterface.WIFI_SAP"


class FakeDevice:
    def __init__(self, serial: str, log: list[str], fail_stop: bool = False) -> None:
        self._serial, self.log, self.fail_stop = serial, log, fail_stop
        self.config: Any = None

    def serial(self) -> str:
        return self._serial

    def status(self) -> FakeStatus:
        self.log.append("status")
        return FakeStatus()

    def set_streaming_config(self, cfg: Any) -> None:
        self.config = cfg
        self.log.append("set_streaming_config")

    def start_streaming(self) -> None:
        self.log.append("start_streaming")

    def stop_streaming(self) -> None:
        self.log.append("stop_streaming")
        if self.fail_stop:
            raise RuntimeError("device gone")

    def get_streaming_info(self) -> FakeInfo:
        return FakeInfo()

    def get_data_quality_stats(self) -> str:
        return json.dumps(QUALITY_JSON)


class FakeTarget:
    def __init__(self, ip: str = "", serial: str = "") -> None:
        self.ip, self.serial = ip, serial


class FakeClient:
    def __init__(self, sdk: FakeSdk) -> None:
        self.sdk = sdk

    def usb_network_devices(self) -> list[FakeTarget]:
        return list(self.sdk.usb)

    def connect(self, target: FakeTarget) -> FakeDevice:
        self.sdk.log.append(f"connect ip={target.ip} serial={target.serial}")
        if target.ip in self.sdk.refuse:
            raise RuntimeError("Operation canceled")
        return FakeDevice("1M0YDD5H980277", self.sdk.log, fail_stop=self.sdk.fail_stop)

    def disconnect_all(self) -> None:
        self.sdk.log.append("disconnect_all")


class FakeStreamingConfig:
    def __init__(self) -> None:
        self.profile_name = ""
        self.profile_json = ""
        self.streaming_interface = None
        self.batch_period_ms = -1
        self.streaming_cert_name = ""


class FakeSdk(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self.log: list[str] = []
        self.usb: list[FakeTarget] = [FakeTarget(ip="192.168.68.165", serial="1M0YDD5H980277")]
        self.refuse: set[str] = set()
        self.fail_stop = False
        self.DeviceTarget = FakeTarget
        self.DeviceClient = lambda: FakeClient(self)
        self.HttpStreamingConfig = FakeStreamingConfig
        self.StreamingInterface = types.SimpleNamespace(
            USB_NCM="USB_NCM", WIFI_STA="WIFI_STA", WIFI_SAP="WIFI_SAP"
        )


class FakeCli:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cert = "cert_3"

    def hotspot_start(self, band: str, bandwidth: str, *, ble: bool = False) -> Any:
        self.calls.append(f"hotspot {band} {bandwidth} ble={ble}")
        return parse_hotspot(HOTSPOT_OUT)

    def install_streaming_certs(self) -> str | None:
        self.calls.append("install-certs")
        if self.cert == "fail":
            raise DeviceError("no device")
        return self.cert


@pytest.fixture(autouse=True)
def _no_local_certs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "local_streaming_cert_name", lambda: None)


def test_local_streaming_cert_name(tmp_path: Path) -> None:
    from ariastreamlab.acquisition.aria_gen2.cli import local_streaming_cert_name

    assert local_streaming_cert_name(tmp_path) is None
    (tmp_path / "publisher-cert-name").write_text("cert_1791891036001238000\n")
    assert local_streaming_cert_name(tmp_path) == "cert_1791891036001238000"


def test_manager_prefers_the_local_cert_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sdk, cli, seen = FakeSdk(), FakeCli(), []
    monkeypatch.setattr(device_mod, "local_streaming_cert_name", lambda: "cert_local")
    mgr = _manager(sdk, cli, seen)
    mgr.connect()
    mgr.configure_streaming(build_plan(_cfg(), tmp_path))
    assert mgr.cert_name == "cert_local"  # reused: install-certs would rotate it
    assert "install-certs" not in cli.calls
    assert mgr._device.config.streaming_cert_name == "cert_local"
    mgr2 = _manager(sdk, FakeCli(), seen, connection={"install_certs": "always"})
    mgr2.connect()
    assert mgr2.prepare_certs() == "cert_3"


def _manager(
    sdk: FakeSdk, cli: FakeCli, transitions: list[str], **dev: Any
) -> AriaGen2DeviceManager:
    return AriaGen2DeviceManager(
        DeviceConfig(**dev),
        sdk=sdk,
        cli=cli,
        on_transition=lambda t: transitions.append(t.current.value),
        prompt=lambda msg: transitions.append("PROMPT"),
    )


def test_manager_full_sequence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sdk, cli, seen = FakeSdk(), FakeCli(), []
    mgr = _manager(sdk, cli, seen)
    assert mgr.state is DeviceState.DISCONNECTED
    found = mgr.discover()
    assert found == [DeviceInfo("1M0YDD5H980277", "usb", "192.168.68.165")]
    mgr.connect()
    assert mgr.state is DeviceState.CONNECTED and mgr.transport == "usb"
    assert mgr.serial == "1M0YDD5H980277" and mgr.last_status["battery_level"] == 77
    h = mgr.get_status()
    assert h.battery_percent == 77 and h.skin_temperature_c == 31.5 and h.charging is True
    info = mgr.prepare_hotspot()
    assert info.ssid == "Aria-M2FHCZP6" and seen[-2:] == ["PROMPT", "NETWORK_CONFIGURED"]
    assert cli.calls[0] == "hotspot 5GHz 40MHz ble=False"
    plan = build_plan(_cfg(), tmp_path)
    mgr.configure_streaming(plan)
    assert mgr.state is DeviceState.STREAM_READY and mgr.cert_name == "cert_3"
    cfg = mgr._device.config
    assert (
        cfg.profile_name,
        cfg.streaming_interface,
        cfg.batch_period_ms,
        cfg.streaming_cert_name,
    ) == ("profile9", "WIFI_SAP", 800, "cert_3")
    mgr.start_streaming()
    assert mgr.state is DeviceState.STREAMING and mgr.streaming_info["profile"] == "profile9"
    # the stream reveals the Wi-Fi control address
    monkeypatch.setattr(
        device_mod, "tcp_reachable", lambda ip, port=23456, timeout=3.0: ip == "192.168.37.28"
    )
    assert mgr.reconnect_over_ip("192.168.133.253") is False  # hotspot gateway: port closed
    assert mgr.reconnect_over_ip("192.168.37.28") is True
    assert mgr.transport == "wifi" and mgr.control_ip == "192.168.37.28"
    q = mgr.quality()
    assert q is not None and q.by_stream()["rgb"].dropped == 5
    assert mgr.quality_stats()["sensors"][0]["sensor_type"] == "IMU_1"
    mgr.stop_streaming()
    assert mgr.state is DeviceState.CONNECTED
    mgr.disconnect()
    assert mgr.state is DeviceState.DISCONNECTED
    ops = [e["op"] for e in mgr.transport_log]
    assert ops[:5] == ["connect", "status", "status", "hotspot_start", "hotspot_join"]
    assert ("reconnect_over_ip" in ops and ops[-2:] == ["stop_streaming", "disconnect_all"]) or ops[
        -1
    ] == "stop_streaming"
    assert seen[0] == "DISCOVERED" and "STREAMING" in seen and seen[-1] == "DISCONNECTED"
    assert sdk.log[0] == "connect ip= serial="  # USB: empty target, as verified on hardware


def test_manager_connection_errors_and_ip_target() -> None:
    sdk, cli, seen = FakeSdk(), FakeCli(), []
    sdk.usb = []
    mgr = _manager(sdk, cli, seen)
    with pytest.raises(DeviceError, match="no Aria Gen 2 glasses found"):
        mgr.connect()
    mgr2 = _manager(sdk, cli, seen, connection={"device_ip": "10.0.0.9"})
    sdk.refuse.add("10.0.0.9")
    with pytest.raises(DeviceError, match="connect failed over"):
        mgr2.connect()
    sdk.refuse.clear()
    mgr2.connect()
    assert mgr2.transport == "wifi" and mgr2.control_ip == "10.0.0.9"
    with pytest.raises(DeviceError, match="not connected"):
        _manager(sdk, cli, seen).get_status()


def test_manager_cert_failure_is_tolerated_and_stop_failure_is_error(tmp_path: Path) -> None:
    sdk, cli, seen = FakeSdk(), FakeCli(), []
    cli.cert = "fail"
    sdk.fail_stop = True
    mgr = _manager(sdk, cli, seen)
    mgr.connect()
    mgr.configure_streaming(build_plan(_cfg(), tmp_path))
    assert mgr.cert_name is None and mgr._device.config.streaming_cert_name == ""
    mgr.start_streaming()
    with pytest.raises(DeviceError, match="stop_streaming failed"):
        mgr.stop_streaming()
    assert mgr.state is DeviceState.ERROR and mgr.machine.last_error is not None


def test_health_from_status_tolerates_garbage() -> None:
    h = health_from_status({"battery_level": "x", "skin_temp_celsius": None, "weird": object()})
    assert h.battery_percent is None and h.skin_temperature_c is None
    assert isinstance(h.raw["weird"], str)


def test_manager_requires_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aria", None)
    monkeypatch.setitem(sys.modules, "aria.sdk_gen2", None)
    with pytest.raises(DeviceError, match="not installed"):
        AriaGen2DeviceManager(DeviceConfig())


def test_make_live_source_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    from ariastreamlab.acquisition import aria_gen2

    monkeypatch.setattr(aria_gen2, "sdk_available", lambda: False)
    with pytest.raises(DeviceError, match="client SDK"):
        aria_gen2.make_live_source(_cfg(), session_id="s")


@pytest.mark.skipif(os.name == "nt", reason="shell scripts")
def test_doctor_checks_run() -> None:
    from ariastreamlab.cli.doctor import run_checks

    checks = {c.name: c for c in run_checks(server_port=0)}
    assert checks["python"].status in ("ok", "warn")
    assert checks["pyarrow (Parquet storage)"].status == "ok"
    assert checks["streaming port 0"].status == "ok"
