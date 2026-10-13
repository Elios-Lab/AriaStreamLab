#!/usr/bin/env python3
"""Raw Project Aria Gen 2 SDK probe — answers the assumptions A-1…A-8 of docs/ARCHITECTURE.md.

This script deliberately uses ONLY ``aria.sdk_gen2`` / ``projectaria_tools`` / the bundled
``aria_gen2`` CLI, never AriaStreamLab, so that its results are independent of the framework.
Run it on the laptop that is paired with the glasses:

    python scripts/probe_sdk.py --out probe_report.json                 # USB, 20 s stream
    python scripts/probe_sdk.py --interface wifi_sap --hotspot           # untethered attempt
    python scripts/probe_sdk.py --ip 192.168.x.y --interface wifi_sta    # device already on Wi-Fi

Each step prints PASS / FAIL / SKIP and the report is written as JSON. Nothing here is
destructive: it never deletes recordings and never changes device profiles.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Step:
    id: str
    title: str
    status: str = "SKIP"  # PASS | FAIL | SKIP
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Report:
    started_utc: str
    host: dict[str, Any]
    args: dict[str, Any]
    steps: list[Step] = field(default_factory=list)

    def step(self, id_: str, title: str) -> Step:
        s = Step(id_, title)
        self.steps.append(s)
        print(f"\n=== [{id_}] {title}")
        return s

    def finish(self, s: Step, status: str, **details: Any) -> None:
        s.status = status
        s.details.update(details)
        print(f"    -> {status}")
        for k, v in details.items():
            text = json.dumps(v, default=str)
            print(f"       {k}: {text[:400]}{'…' if len(text) > 400 else ''}")

    def fail(self, s: Step, exc: BaseException) -> None:
        s.status = "FAIL"
        s.error = f"{type(exc).__name__}: {exc}"
        print(f"    -> FAIL: {s.error}")
        print("       " + traceback.format_exc().replace("\n", "\n       "))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_PORT = 23456  # "recording manager" port the SDK uses for IP control (seen in CLI logs)


def strip_ansi(text: str) -> str:
    """Remove colour escape codes from CLI output."""
    return _ANSI.sub("", text)


def tcp_reachable(ip: str, port: int, timeout: float = 3.0) -> bool:
    """True when a TCP connection to ``ip:port`` succeeds within ``timeout``."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _hotspot_gateway() -> str:
    """Default gateway on macOS (the glasses' IP once the laptop joined the hotspot)."""
    try:
        gw = subprocess.run(
            ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""
    for line in gw.splitlines():
        if "gateway:" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _mac_wifi_ssid() -> str:
    """Current Wi-Fi SSID on macOS (best effort)."""
    for cmd in (["networksetup", "-getairportnetwork", "en0"], ["ipconfig", "getsummary", "en0"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        m = re.search(r"(?:Current Wi-Fi Network|SSID)\s*:\s*(.+)", out)
        if m:
            return m.group(1).strip()
    return ""


def run_cli(*args: str, timeout: float = 90) -> dict[str, Any]:
    """Run the bundled ``aria_gen2`` CLI and capture everything.

    Each element of ``args`` may contain spaces (``"device list"``); it is split into
    separate argv entries, because the CLI treats ``"device list"`` as one unknown token.
    """
    exe = shutil.which("aria_gen2")
    if exe is None:
        raise RuntimeError("aria_gen2 CLI not on PATH (is the virtualenv activated?)")
    argv = [tok for arg in args for tok in arg.split()]
    t0 = time.monotonic()
    proc = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": ["aria_gen2", *argv],
        "returncode": proc.returncode,
        "stdout": strip_ansi(proc.stdout),
        "stderr": strip_ansi(proc.stderr),
        "seconds": round(time.monotonic() - t0, 2),
    }


def host_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
    }
    try:
        import importlib.metadata as md

        for pkg in ("projectaria_client_sdk", "projectaria_tools", "rerun-sdk", "numpy"):
            try:
                info[pkg] = md.version(pkg)
            except md.PackageNotFoundError:
                info[pkg] = None
    except Exception as exc:
        info["metadata_error"] = str(exc)
    return info


def _safe_usb_net(client: Any) -> list[Any]:
    try:
        return list(client.usb_network_devices())
    except Exception:
        return []


def status_to_dict(status: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in dir(status):
        if name.startswith("_"):
            continue
        try:
            value = getattr(status, name)
        except Exception:
            continue
        if callable(value):
            continue
        out[name] = value
    return out


class CallbackCounter:
    """Counts samples per stream and records first/last device timestamps (ns)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count: Counter[str] = Counter()
        self.first_ts: dict[str, int] = {}
        self.last_ts: dict[str, int] = {}
        self.host_first: dict[str, int] = {}
        self.host_last: dict[str, int] = {}
        self.out_of_order: Counter[str] = Counter()
        self.image_shapes: dict[str, Any] = {}
        self.image_errors: dict[str, str] = {}
        self.pixel_formats: dict[str, str] = {}
        self.threads: defaultdict[str, set[int]] = defaultdict(set)
        self.calibration_received = False
        self.decode_ms: list[float] = []

    def hit(self, stream: str, ts_ns: int) -> None:
        now = time.monotonic_ns()
        with self.lock:
            self.count[stream] += 1
            self.threads[stream].add(threading.get_ident())
            if stream not in self.first_ts:
                self.first_ts[stream] = ts_ns
                self.host_first[stream] = now
            elif ts_ns < self.last_ts[stream]:
                self.out_of_order[stream] += 1
            self.last_ts[stream] = ts_ns
            self.host_last[stream] = now

    def summary(self) -> dict[str, Any]:
        with self.lock:
            out: dict[str, Any] = {}
            for stream, n in sorted(self.count.items()):
                span_s = (self.last_ts[stream] - self.first_ts[stream]) / 1e9
                out[stream] = {
                    "received": n,
                    "device_span_s": round(span_s, 3),
                    "effective_rate_hz": round((n - 1) / span_s, 2) if span_s > 0 else None,
                    "out_of_order": self.out_of_order[stream],
                    "callback_threads": len(self.threads[stream]),
                    "image_shape": self.image_shapes.get(stream),
                }
            out["_calibration_received"] = self.calibration_received
            out["_image_errors"] = dict(self.image_errors)
            out["_pixel_formats"] = dict(self.pixel_formats)
            if self.decode_ms:
                arr = sorted(self.decode_ms)
                out["_to_numpy_ms"] = {
                    "n": len(arr),
                    "p50": round(arr[len(arr) // 2], 3),
                    "p95": round(arr[int(len(arr) * 0.95)], 3),
                    "max": round(arr[-1], 3),
                }
            return out


def timedelta_ns(td: Any) -> int:
    return (td.days * 86400 + td.seconds) * 1_000_000_000 + td.microseconds * 1000


def register_all(handler: Any, counter: CallbackCounter, camera_labels: dict[int, str]) -> None:
    """Register one counting callback per typed stream on a StreamDataInterface."""

    try:
        from aria.image_decoder import ImageDecoder

        py_decoder: Any = ImageDecoder(hw_accel=platform.system() == "Darwin")
    except Exception as exc:
        py_decoder = None
        counter.image_errors["_ImageDecoder"] = f"{type(exc).__name__}: {exc}"

    def image_cb(stream_prefix: str) -> Any:
        def cb(image_data: Any, record: Any) -> None:
            label = camera_labels.get(record.camera_id, f"{stream_prefix}-{record.camera_id}")
            counter.hit(label, record.capture_timestamp_ns)
            if label in counter.image_shapes and len(counter.decode_ms) >= 200:
                return
            try:
                fmt = str(image_data.get_pixel_format())
                counter.pixel_formats[label] = fmt
                t0 = time.perf_counter()
                arr = image_data.to_numpy_array()
                counter.decode_ms.append((time.perf_counter() - t0) * 1e3)
                counter.image_shapes[label] = [list(arr.shape), str(arr.dtype), "sdk"]
            except Exception as exc:
                counter.image_errors[label] = f"to_numpy_array: {type(exc).__name__}: {exc}"
                if py_decoder is None:
                    return
                # Second chance: the Python H.265 decoder StreamReceiver uses for RGB.
                try:
                    t0 = time.perf_counter()
                    ok = py_decoder.decode_image(image_data, record)
                    dt = (time.perf_counter() - t0) * 1e3
                    if ok:
                        arr = image_data.to_numpy_array()
                        counter.decode_ms.append(dt)
                        counter.image_shapes[label] = [list(arr.shape), str(arr.dtype), "python"]
                    else:
                        counter.image_errors[label] += " | ImageDecoder.decode_image returned False"
                except Exception as exc2:
                    counter.image_errors[label] += f" | ImageDecoder: {type(exc2).__name__}: {exc2}"

        return cb

    handler.register_rgb_callback(image_cb("rgb"))
    handler.register_slam_callback(image_cb("slam"))
    handler.register_et_callback(image_cb("et"))
    handler.register_imu_callback(lambda d, label: counter.hit(label, d.capture_timestamp_ns))
    handler.register_magnetometer_callback(
        lambda d, label: counter.hit(label, d.capture_timestamp_ns)
    )
    handler.register_barometer_callback(lambda d: counter.hit("baro0", d.capture_timestamp_ns))
    handler.register_gps_callback(lambda d: counter.hit("gps", d.capture_timestamp_ns))
    handler.register_ppg_callback(lambda d: counter.hit("ppg", d.capture_timestamp_ns))
    handler.register_audio_callback(
        lambda _d, rec, _nch: (
            counter.hit("mic", int(rec.capture_timestamps_ns[0]))
            if len(rec.capture_timestamps_ns)
            else None
        )
    )
    handler.register_eye_gaze_callback(
        lambda g: counter.hit("eyegaze", timedelta_ns(g.tracking_timestamp))
    )
    handler.register_hand_pose_callback(
        lambda h: counter.hit("handtracking", timedelta_ns(h.tracking_timestamp))
    )
    handler.register_vio_callback(lambda v: counter.hit("vio", v.capture_timestamp_ns))
    handler.register_vio_high_frequency_callback(
        lambda p: counter.hit("vio_high_frequency", timedelta_ns(p.tracking_timestamp))
    )

    def calib_cb(_calib: Any) -> None:
        counter.calibration_received = True

    handler.register_device_calib_callback(calib_cb)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--serial", default="", help="device serial (optional)")
    parser.add_argument("--ip", default="", help="device IP: connect over Wi-Fi instead of USB")
    parser.add_argument("--interface", choices=["usb", "wifi_sta", "wifi_sap"], default="usb")
    parser.add_argument("--hotspot", action="store_true", help="run `device hotspot start` (A-2)")
    parser.add_argument(
        "--unplug",
        action="store_true",
        help="after joining the hotspot and reconnecting over it, ask to unplug the USB cable "
        "before start_streaming, so control AND data must go over Wi-Fi (A-1, A-3)",
    )
    parser.add_argument("--profile", default="profile9")
    parser.add_argument("--batch-period-ms", type=int, default=None, help="default: 0/200/800")
    parser.add_argument("--duration", type=float, default=20.0, help="streaming seconds")
    parser.add_argument("--port", type=int, default=6768)
    parser.add_argument(
        "--vrs", default="", help="VRS output path (default: ./probe_<utc>.vrs in the current dir)"
    )
    parser.add_argument(
        "--raw-interface",
        action="store_true",
        help="use a bare StreamDataInterface(True) instead of Meta's StreamReceiver "
        "(runs 4-6 did this; it decodes nothing and left a 96-byte VRS)",
    )
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--out", default="probe_report.json")
    args = parser.parse_args()

    report = Report(
        started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        host=host_info(),
        args=vars(args),
    )
    print(json.dumps(report.host, indent=2))

    # ---------------------------------------------------------------- A-0 import
    s = report.step("A-0", "import aria.sdk_gen2 and projectaria_tools")
    try:
        import aria.sdk_gen2 as sdk
        from projectaria_tools.core import data_provider

        report.finish(
            s,
            "PASS",
            sdk_members=len([n for n in dir(sdk) if not n.startswith("_")]),
            has_replay=hasattr(sdk, "create_replay_source"),
            has_observer=hasattr(sdk.DeviceClient, "set_observer"),
        )
    except Exception as exc:
        report.fail(s, exc)
        _write(report, args.out)
        return 1

    camera_labels = {
        1: "slam-front-left",
        2: "slam-front-right",
        4: "slam-side-left",
        8: "slam-side-right",
        16: "camera-et-left",
        32: "camera-et-right",
        64: "camera-rgb",
    }

    # ---------------------------------------------------------------- pre-flight
    client = sdk.DeviceClient()
    cfg = sdk.DeviceClientConfig()
    cfg.enable_device_monitoring = True
    client.set_client_config(cfg)
    device = None

    s = report.step("PRE", "pre-flight: USB devices seen by the SDK and by the CLI, pairing state")
    try:

        def guarded(fn: Any) -> Any:
            try:
                return fn()
            except Exception as exc:
                return f"ERROR {type(exc).__name__}: {exc}"

        usb_raw = guarded(client.usb_devices)
        usb = usb_raw if isinstance(usb_raw, list) else []
        usb_net_raw = guarded(client.usb_network_devices)
        usb_net = usb_net_raw if isinstance(usb_net_raw, list) else []
        cli_list = guarded(lambda: run_cli("device list"))
        auth = guarded(lambda: run_cli("auth check"))
        report.finish(
            s,
            "PASS" if (usb or usb_net) else "FAIL",
            usb_devices=[list(x) for x in usb] if usb else usb_raw,
            usb_network_devices=[{"ip": t.ip, "serial": t.serial} for t in usb_net]
            if usb_net
            else usb_net_raw,
            cli_device_list=cli_list,
            cli_auth_check=auth,
        )
        if not usb and not usb_net and not args.ip:
            print(
                "\n    >>> The SDK sees no glasses over USB. Before re-running, check:\n"
                "    >>>   1. glasses powered on and connected with a USB-C *data* cable "
                "(try another cable/port);\n"
                "    >>>   2. `aria_doctor` reports OK (USB networking / ports);\n"
                "    >>>   3. `aria_gen2 device list` lists the device;\n"
                "    >>>   4. `aria_gen2 auth check` says authenticated "
                "(else `aria_gen2 auth pair` + accept in the Companion App).\n"
                "    >>> If the glasses are already on Wi-Fi, pass --ip <device-ip> instead."
            )
    except Exception as exc:
        report.fail(s, exc)

    # ---------------------------------------------------------------- connect
    s = report.step("A-1a", f"DeviceClient.connect({'ip=' + args.ip if args.ip else 'usb'})")
    try:
        target = sdk.DeviceTarget(ip=args.ip, serial=args.serial)
        t0 = time.monotonic()
        device = client.connect(target)
        report.finish(
            s,
            "PASS",
            seconds=round(time.monotonic() - t0, 2),
            serial=device.serial(),
            connection_id=device.connection_id(),
        )
    except Exception as exc:
        report.fail(s, exc)
        _write(report, args.out)
        return 1

    # ---------------------------------------------------------------- status
    s = report.step("STATUS", "Device.status() fields")
    status: dict[str, Any] = {}
    try:
        status = status_to_dict(device.status())
        report.finish(s, "PASS", **status)
    except Exception as exc:
        report.fail(s, exc)

    # ---------------------------------------------------------------- A-6 observer
    s = report.step("A-6", "DeviceClient.set_observer(python object) accepted?")
    events: list[str] = []

    class Observer:
        def on_connection_state_changed(self, *a: Any, **kw: Any) -> None:
            events.append(f"on_connection_state_changed{a}{kw}")

        def __call__(self, *a: Any, **kw: Any) -> None:
            events.append(f"call{a}{kw}")

    try:
        client.set_observer(Observer())
        report.finish(s, "PASS", note="accepted; see 'observer_events' at the end")
    except Exception as exc:
        report.fail(s, exc)

    # ---------------------------------------------------------------- profiles
    s = report.step("PROFILES", "Device.device_profiles()")
    try:
        profiles = device.device_profiles()
        listing = {
            key: [(p.name, str(p.type)) for p in lst.profiles] for key, lst in profiles.items()
        }
        report.finish(s, "PASS", profiles=listing)
    except Exception as exc:
        report.fail(s, exc)

    # ---------------------------------------------------------------- A-2 hotspot
    hotspot_ip = ""
    if args.hotspot:
        s = report.step("A-2", "aria_gen2 device hotspot start / status")
        try:
            start = run_cli("device hotspot start")
            time.sleep(3)
            stat = run_cli("device hotspot status")
            report.finish(
                s,
                "PASS" if start["returncode"] == 0 else "FAIL",
                hotspot_start=start,
                hotspot_status=stat,
            )
            text = start["stdout"] + stat["stdout"]
            ssid = (re.search(r"ssid:\s*(\S+)", text) or [None, None])[1]
            passphrase = (re.search(r"passphrase:\s*(\S+)", text) or [None, None])[1]
            s.details["hotspot_ssid"] = ssid
            print(
                f"\n    >>> Join the glasses' hotspot from this laptop's Wi-Fi menu now:"
                f"\n    >>>     SSID: {ssid}   password: {passphrase}"
                "\n    >>> then press ENTER (the script checks that the Mac really joined)."
            )
            input()
            hotspot_ip = _hotspot_gateway()
            s.details["default_gateway_after_join"] = hotspot_ip
            s.details["mac_wifi_ssid"] = _mac_wifi_ssid()
            if not hotspot_ip:
                print(
                    "    >>> No default gateway found; the laptop is apparently not on the hotspot"
                )
            else:
                s.details[f"gateway_tcp_{CONTROL_PORT}"] = tcp_reachable(hotspot_ip, CONTROL_PORT)
        except Exception as exc:
            report.fail(s, exc)

    # ---------------------------------------------------------------- A-1 wifi control
    control_ip = args.ip or hotspot_ip or (status.get("wifi_ip_address") or "")
    if args.interface != "usb" and not args.ip and control_ip:
        s = report.step("A-1b", f"re-connect over Wi-Fi: DeviceTarget(ip={control_ip})")
        try:
            reachable = tcp_reachable(control_ip, CONTROL_PORT)
            s.details[f"tcp_reachable_{CONTROL_PORT}"] = reachable
            if not reachable:
                print(
                    f"    >>> {control_ip}:{CONTROL_PORT} is not reachable from this laptop: "
                    "the Mac must be on the same Wi-Fi network as the glasses "
                    "(the device's SSID is in the STATUS step). Continuing over USB."
                )
                raise RuntimeError(f"{control_ip}:{CONTROL_PORT} unreachable (different network?)")
            dev2 = client.connect(sdk.DeviceTarget(ip=control_ip))
            report.finish(s, "PASS", serial=dev2.serial(), connection_id=dev2.connection_id())
            device = dev2
            if args.unplug:
                print(
                    "\n    >>> Wi-Fi control connection established. UNPLUG the USB cable now,"
                    "\n    >>> wait 3 s, then press ENTER. Everything must now go over Wi-Fi."
                )
                input()
                time.sleep(1)
                s2 = report.step("A-1b2", "status over Wi-Fi after unplugging USB")
                try:
                    st = status_to_dict(device.status())
                    report.finish(
                        s2,
                        "PASS",
                        usb_network_devices=[
                            {"ip": t.ip, "serial": t.serial} for t in client.usb_network_devices()
                        ],
                        **st,
                    )
                except Exception as exc:
                    report.fail(s2, exc)
        except Exception as exc:
            report.fail(s, exc)
            print("    (continuing with the USB connection)")

    # ---------------------------------------------------------------- streaming
    interface_map = {
        "usb": sdk.StreamingInterface.USB_NCM,
        "wifi_sta": sdk.StreamingInterface.WIFI_STA,
        "wifi_sap": sdk.StreamingInterface.WIFI_SAP,
    }
    batch = args.batch_period_ms
    if batch is None:
        batch = {"usb": 0, "wifi_sta": 200, "wifi_sap": 800}[args.interface]

    vrs_path = args.vrs or os.path.abspath(
        f"probe_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.vrs"
    )

    s = report.step(
        "CERTS", "install streaming certs once (so start_streaming does not rotate them)"
    )
    cert_name = ""
    try:
        res = run_cli("streaming install-certs")
        m = re.search(r"cert_\d+", res["stdout"] + res["stderr"])
        if m:
            cert_name = m.group(0)
        report.finish(s, "PASS" if res["returncode"] == 0 else "FAIL", cert_name=cert_name, **res)
    except Exception as exc:
        report.fail(s, exc)

    counter = CallbackCounter()
    server = None
    handler_box: dict[str, Any] = {}
    time_sync = sdk.TimeSyncRef()

    receiver_obj: Any = None
    receiver_kind = "raw StreamDataInterface" if args.raw_interface else "StreamReceiver"
    s = report.step("SERVER", f"{receiver_kind} on 0.0.0.0:{args.port} with record_to_vrs")
    try:
        server_cfg = sdk.HttpServerConfig()
        server_cfg.address = "0.0.0.0"
        server_cfg.port = args.port

        if args.raw_interface:

            def factory() -> Any:
                handler = sdk.StreamDataInterface(True)
                handler.record_to_vrs(vrs_path)
                register_all(handler, counter, camera_labels)
                handler_box["handler"] = handler
                return handler

            server = sdk.AriaGen2HttpServer(server_cfg, factory, time_sync)
        else:
            # Meta's own high-level receiver: StreamDataInterface(False) for the external wheel,
            # Python/HW H.265 decoding interposed on image callbacks, VRS recording, and a
            # teardown that releases the handler (which is what finalises the VRS file).
            import aria.stream_receiver as stream_receiver

            receiver_obj = stream_receiver.StreamReceiver(enable_image_decoding=True)
            receiver_obj.set_server_config(server_cfg)
            receiver_obj.record_to_vrs(vrs_path)
            register_all(receiver_obj, counter, camera_labels)
            server = receiver_obj.start_server()
            time_sync = receiver_obj.get_time_sync() or time_sync
        report.finish(s, "PASS", vrs_path=vrs_path, receiver=receiver_kind)
    except Exception as exc:
        report.fail(s, exc)

    s = report.step("A-1c", f"set_streaming_config + start_streaming over {args.interface}")
    try:
        scfg = sdk.HttpStreamingConfig()
        scfg.profile_name = args.profile
        scfg.streaming_interface = interface_map[args.interface]
        scfg.batch_period_ms = batch
        if cert_name:
            scfg.streaming_cert_name = cert_name
        device.set_streaming_config(scfg)
        t0 = time.monotonic()
        device.start_streaming()
        report.finish(
            s, "PASS", seconds_to_return=round(time.monotonic() - t0, 2), batch_period_ms=batch
        )
    except Exception as exc:
        report.fail(s, exc)

    s = report.step("STREAM", f"receive for {args.duration}s; sample counts, rates, threads")
    quality_dumps: list[Any] = []
    try:
        t_end = time.monotonic() + args.duration
        first_conn: float | None = None
        while time.monotonic() < t_end:
            time.sleep(2.0)
            conns = server.connections() if server is not None else []
            if conns and first_conn is None:
                first_conn = time.monotonic()
            # A-5: data quality stats during the session
            try:
                raw = device.get_data_quality_stats()
                quality_dumps.append(raw)
            except Exception as exc:
                quality_dumps.append(f"ERROR {type(exc).__name__}: {exc}")
            print(
                f"    t-{t_end - time.monotonic():5.1f}s  connections={len(conns)}  "
                f"samples={sum(counter.count.values())}  "
                f"ticsync_valid={time_sync.is_valid()}"
            )
        summary = counter.summary()
        info = device.get_streaming_info()
        report.finish(
            s,
            "PASS" if sum(counter.count.values()) > 0 else "FAIL",
            streams=summary,
            connections=server.connections() if server is not None else None,
            data_path=(
                "usb-ncm"
                if any(
                    c.get("client_ip") == t.ip
                    for c in (server.connections() if server is not None else [])
                    for t in _safe_usb_net(client)
                )
                else "wifi"
            )
            if server is not None and server.connections()
            else "none",
            streaming_info={
                "is_streaming": info.is_streaming,
                "profile": info.profile_name,
                "interface": info.streaming_interface,
            },
            ticsync={"connected": time_sync.is_connected(), "valid": time_sync.is_valid()},
        )
    except Exception as exc:
        report.fail(s, exc)

    s = report.step("A-5", "get_data_quality_stats() raw payload (format?)")
    try:
        last = quality_dumps[-1] if quality_dumps else None
        parsed: Any = None
        if isinstance(last, str):
            try:
                parsed = json.loads(last)
            except json.JSONDecodeError:
                parsed = None
        report.finish(
            s,
            "PASS" if isinstance(last, str) and not last.startswith("ERROR") else "FAIL",
            type=type(last).__name__,
            is_json=parsed is not None,
            raw=last,
        )
    except Exception as exc:
        report.fail(s, exc)

    # A-8: decode cost
    s = report.step("A-8", "to_numpy_array() cost on decoded frames")
    dec = counter.summary().get("_to_numpy_ms")
    report.finish(s, "PASS" if dec else "SKIP", to_numpy_ms=dec)

    # ---------------------------------------------------------------- A-1e wireless control
    # The IP the glasses streamed FROM is the one to try for control over Wi-Fi: run 5 showed
    # the hotspot gateway (192.168.133.253) does not listen on 23456 while the stream came
    # from 192.168.37.28.
    stream_ip = ""
    if server is not None:
        for c in server.connections():
            if c.get("client_ip") and c["client_ip"] not in {t.ip for t in _safe_usb_net(client)}:
                stream_ip = c["client_ip"]
    if args.interface != "usb" and stream_ip:
        s = report.step("A-1e", f"control over Wi-Fi via the streaming source IP {stream_ip}")
        try:
            reachable = tcp_reachable(stream_ip, CONTROL_PORT)
            s.details[f"tcp_reachable_{CONTROL_PORT}"] = reachable
            if not reachable:
                raise RuntimeError(f"{stream_ip}:{CONTROL_PORT} not reachable")
            dev3 = client.connect(sdk.DeviceTarget(ip=stream_ip))
            st3 = status_to_dict(dev3.status())
            report.finish(s, "PASS", serial=dev3.serial(), **st3)
            device = dev3
            if args.unplug:
                print(
                    "\n    >>> Control over Wi-Fi works and the stream is still running."
                    "\n    >>> UNPLUG the USB cable now, wait 5 s, then press ENTER."
                )
                input()
                time.sleep(1)
                s2 = report.step("A-1f", "status + stream health over Wi-Fi after unplugging USB")
                try:
                    before = sum(counter.count.values())
                    time.sleep(5)
                    after = sum(counter.count.values())
                    st = status_to_dict(device.status())
                    report.finish(
                        s2,
                        "PASS" if after > before else "FAIL",
                        samples_in_5s_after_unplug=after - before,
                        connections=server.connections() if server is not None else None,
                        usb_network_devices=[
                            {"ip": t.ip, "serial": t.serial} for t in _safe_usb_net(client)
                        ],
                        **st,
                    )
                except Exception as exc:
                    report.fail(s2, exc)
        except Exception as exc:
            report.fail(s, exc)

    s = report.step("A-1d", "stop_streaming over the current control transport")
    try:
        t0 = time.monotonic()
        device.stop_streaming()
        time.sleep(2)

        def _teardown() -> None:
            if receiver_obj is not None:
                receiver_obj.stop_server()  # clears callbacks, stops the server, drops the handler
            elif server is not None:
                handler = handler_box.get("handler")
                if handler is not None:
                    handler.clear_callbacks()
                server.stop()
                server.join()

        # server.join() was seen to hang in a sandbox without a device; bound it.
        th = threading.Thread(target=_teardown, name="probe-teardown", daemon=True)
        th.start()
        th.join(30)
        report.finish(
            s,
            "PASS" if not th.is_alive() else "FAIL",
            seconds=round(time.monotonic() - t0, 2),
            teardown_completed=not th.is_alive(),
        )
    except Exception as exc:
        report.fail(s, exc)

    # Run 5 left a 96-byte VRS: the SDK's writer finalises the file when the
    # StreamDataInterface is destroyed (Meta's StreamReceiver.stop_server sets it to None).
    handler_box.clear()
    server = None
    receiver_obj = None
    import gc

    gc.collect()

    # ---------------------------------------------------------------- A-4 VRS
    s = report.step("A-4a", "host-recorded VRS opens with projectaria_tools")
    try:
        size = -1
        for _ in range(10):  # up to 10 s for the writer to flush after the handler is released
            time.sleep(1)
            size = os.path.getsize(vrs_path) if os.path.exists(vrs_path) else -1
            if size > 4096:
                break
        s.details["vrs_path"] = vrs_path
        s.details["vrs_bytes"] = size
        if size <= 0:
            raise RuntimeError(f"VRS file missing or empty: {vrs_path} ({size} bytes)")
        provider = data_provider.create_vrs_data_provider(vrs_path)
        if provider is None:
            raise RuntimeError(
                f"create_vrs_data_provider returned None for {vrs_path} ({size} bytes); "
                "keep the file, it is needed to diagnose this"
            )
        streams = provider.get_all_streams()
        per_stream: dict[str, Any] = {}
        for sid in streams:
            label = provider.get_label_from_stream_id(sid)
            n = provider.get_num_data(sid)
            per_stream[label] = {
                "stream_id": str(sid),
                "num_data": n,
                "nominal_rate_hz": provider.get_nominal_rate_hz(sid),
            }
        # decode one image per camera
        decoded: dict[str, Any] = {}
        for sid in streams:
            label = provider.get_label_from_stream_id(sid)
            if label.startswith(("camera-", "slam-")) and provider.get_num_data(sid) > 0:
                img, rec = provider.get_image_data_by_index(sid, 0)
                arr = img.to_numpy_array()
                decoded[label] = [list(arr.shape), str(arr.dtype), rec.capture_timestamp_ns]
        report.finish(
            s,
            "PASS",
            vrs_bytes=size,
            streams=per_stream,
            decoded_first_frames=decoded,
            has_calibration=provider.get_device_calibration() is not None,
        )
    except Exception as exc:
        report.fail(s, exc)

    if not args.skip_replay:
        s = report.step("A-4b", "sdk.create_replay_source replays the host VRS at max speed")
        try:
            counter2 = CallbackCounter()
            handler2 = sdk.StreamDataInterface(True)
            register_all(handler2, counter2, camera_labels)
            rcfg = sdk.ReplaySourceConfig()
            rcfg.filename = vrs_path
            rcfg.speed_multiplier = 0.0
            t0 = time.monotonic()
            session = sdk.create_replay_source(rcfg, handler2)
            if session is None:
                raise RuntimeError("create_replay_source returned None")
            while not session.is_finished() and time.monotonic() - t0 < 120:
                time.sleep(0.2)
            session.stop()
            handler2.clear_callbacks()
            report.finish(
                s,
                "PASS",
                seconds=round(time.monotonic() - t0, 2),
                streams=counter2.summary(),
                live_vs_replay_counts={
                    k: (counter.count.get(k), counter2.count.get(k))
                    for k in sorted(set(counter.count) | set(counter2.count))
                },
            )
        except Exception as exc:
            report.fail(s, exc)

    report.steps.append(Step("OBSERVER", "observer events seen", "PASS", {"events": events}))
    with contextlib.suppress(Exception):
        client.disconnect_all()
    _write(report, args.out)
    return 0


def _write(report: Report, path: str) -> None:
    data = asdict(report)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    print("\n" + "=" * 72)
    for s in report.steps:
        print(f"{s.status:5} {s.id:9} {s.title}")
    print(f"\nReport written to {path} — please share it (it contains no credentials).")


if __name__ == "__main__":
    sys.exit(main())
