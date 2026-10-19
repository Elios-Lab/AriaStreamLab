"""Control plane for Aria Gen 2: discovery, connection, hotspot, streaming start/stop.

Implements :class:`~ariastreamlab.acquisition.base.DeviceManager` with the sequence verified
on hardware (docs/ARCHITECTURE.md §0.10, "untethered sequence"):

1. connect over the USB network link (``DeviceClient.usb_network_devices()``), or over a
   known IP (``device.connection.device_ip``);
2. optionally start the hotspot with an explicit band (``aria_gen2 device hotspot start``,
   over USB or BLE) and let the laptop join it;
3. push ``HttpStreamingConfig`` and start streaming (the data path is chosen by the
   streaming interface, the control path stays where it is);
4. once the device has connected to our server, learn its Wi-Fi control address from the
   stream source IP and reconnect over it, so stop/status/quality stats keep working after
   the cable is unplugged.

Every SDK call goes through :meth:`_call`, which records the transport used and converts
SDK exceptions into :class:`DeviceError` with a hint for the student.
"""

from __future__ import annotations

import platform
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ariastreamlab.acquisition.aria_gen2.cli import AriaCli, local_streaming_cert_name
from ariastreamlab.acquisition.aria_gen2.quality import QualityStats, parse_quality_stats
from ariastreamlab.acquisition.base import (
    DeviceHealth,
    DeviceInfo,
    DeviceManager,
    DeviceState,
    DeviceStateMachine,
    DeviceTransition,
    HotspotInfo,
    StreamingPlan,
)
from ariastreamlab.config.models import DeviceConfig
from ariastreamlab.core.errors import DeviceError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.timing.clock import HostClock

__all__ = ["CONTROL_PORT", "AriaGen2DeviceManager", "tcp_reachable"]

log = get_logger("ariastreamlab.acquisition.aria_gen2.device")

CONTROL_PORT = 23456
"""The device's control endpoint (recording manager); IP-based on every transport."""


def tcp_reachable(ip: str, port: int = CONTROL_PORT, timeout: float = 3.0) -> bool:
    """True when ``ip:port`` accepts a TCP connection."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def status_to_dict(status: Any) -> dict[str, Any]:
    """``DeviceStatus`` → plain dict (public, non-callable attributes)."""
    out: dict[str, Any] = {}
    for name in dir(status):
        if name.startswith("_"):
            continue
        try:
            value = getattr(status, name)
        except Exception:
            continue
        if not callable(value):
            out[name] = value
    return out


def health_from_status(status: Mapping[str, Any]) -> DeviceHealth:
    """Pick the fields the dashboard and manifest use out of a status dict."""
    return DeviceHealth(
        battery_percent=_opt_int(status.get("battery_level")),
        charging=_opt_bool(status.get("charging")),
        skin_temperature_c=_opt_float(status.get("skin_temp_celsius")),
        thermal_mitigation=_opt_bool(status.get("thermal_mitigation_triggered")),
        wifi_connected=_opt_bool(status.get("wifi_connected")),
        wifi_ssid=_opt_str(status.get("wifi_ssid")),
        wifi_ip=_opt_str(status.get("wifi_ip_address")),
        device_mode=_opt_str(status.get("device_mode")),
        raw={k: _jsonable(v) for k, v in status.items()},
    )


class AriaGen2DeviceManager(DeviceManager):
    """The device-side state machine (see the module docstring for the sequence)."""

    def __init__(
        self,
        config: DeviceConfig,
        *,
        clock: HostClock | None = None,
        cli: AriaCli | None = None,
        sdk: Any | None = None,
        on_transition: Callable[[DeviceTransition], None] | None = None,
        prompt: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or HostClock()
        self.machine = DeviceStateMachine()
        if on_transition is not None:
            self.machine.on_transition(on_transition)
        self.machine.on_transition(self._log_transition)
        self._sdk = sdk if sdk is not None else _import_sdk()
        self.cli = cli or AriaCli(serial=config.serial, device_ip=config.connection.device_ip)
        self._prompt = prompt or _default_prompt
        self._client: Any = None
        self._device: Any = None
        self._lock = threading.RLock()
        self.serial: str | None = config.serial
        self.transport: str | None = None
        """Transport of the current control connection: ``"usb"``, ``"wifi"`` or ``"ip"``."""
        self.control_ip: str | None = None
        self.hotspot: HotspotInfo | None = None
        self.cert_name: str | None = None
        self.plan: StreamingPlan | None = None
        self.transport_log: list[dict[str, Any]] = []
        self.last_status: dict[str, Any] = {}
        self.streaming_info: dict[str, Any] = {}

    # -- helpers -------------------------------------------------------------------------
    def _now(self) -> int:
        return self.clock.now_ns()

    def _log_transition(self, t: DeviceTransition) -> None:
        log.info(
            "device.state",
            previous=t.previous.value,
            current=t.current.value,
            reason=t.reason,
            transport=t.transport,
        )

    def _record(self, op: str, **details: Any) -> None:
        self.transport_log.append(
            {
                "op": op,
                "transport": self.transport,
                "ip": self.control_ip,
                "t_ns": self._now(),
                **details,
            }
        )

    def _call(self, op: str, fn: Callable[[], Any], *, hint: str | None = None) -> Any:
        t0 = time.monotonic()
        try:
            result = fn()
        except DeviceError:
            raise
        except Exception as exc:
            self._record(op, error=f"{type(exc).__name__}: {exc}")
            raise DeviceError(
                f"{op} failed over {self.transport or 'no connection'}: "
                f"{type(exc).__name__}: {exc}",
                hint=hint,
            ) from exc
        self._record(op, seconds=round(time.monotonic() - t0, 3))
        return result

    def _require_device(self) -> Any:
        if self._device is None:
            raise DeviceError("not connected to the glasses", hint="call connect() first")
        return self._device

    # -- DeviceManager --------------------------------------------------------------------
    def discover(self) -> list[DeviceInfo]:
        """USB network links first; a configured IP counts as a device too."""
        found: list[DeviceInfo] = []
        client = self._ensure_client()
        try:
            for t in client.usb_network_devices():
                found.append(DeviceInfo(serial=str(t.serial), transport="usb", ip=str(t.ip)))
        except Exception as exc:
            log.debug("device.usb_discovery_failed", error=str(exc))
        ip = self.config.connection.device_ip
        if ip:
            found.append(
                DeviceInfo(
                    serial=self.serial or "?",
                    transport="wifi",
                    ip=ip,
                    extra={"reachable": tcp_reachable(ip)},
                )
            )
        if found and self.machine.state is DeviceState.DISCONNECTED:
            self.machine.transition(DeviceState.DISCOVERED, reason="discover", now_ns=self._now())
        return found

    def connect(self, target: DeviceInfo | None = None) -> None:
        """Connect over USB (default) or to ``target.ip`` / the configured IP."""
        with self._lock:
            client = self._ensure_client()
            sdk = self._sdk
            devices: list[DeviceInfo] = []
            if target is None:
                devices = self.discover()
                pref = self.config.connection.preferred_control
                wifi_first = pref == "wifi"
                devices.sort(
                    key=lambda d: (d.transport != "wifi") if wifi_first else (d.transport != "usb")
                )
                if not devices:
                    raise DeviceError(
                        "no Aria Gen 2 glasses found",
                        hint="plug the USB cable in (the link shows up as a USB network device), "
                        "or set device.connection.device_ip; check `aria_gen2 device list` and "
                        "`aria_gen2 auth check`",
                    )
                target = devices[0]
            if target.transport == "usb":
                # Over the USB link the verified call is connect() with an empty target
                # (0.4 s in every probe run); DeviceTarget(ip=<usb link ip>) hung for a
                # minute and failed with "(9) Failed to read data from socket". The serial
                # is only passed when several devices are plugged in.
                many = sum(1 for d in devices if d.transport == "usb") > 1
                dt = sdk.DeviceTarget(serial=target.serial) if many else sdk.DeviceTarget()
                self.control_ip = None
            elif target.ip:
                dt = sdk.DeviceTarget(ip=target.ip)
                self.control_ip = target.ip
            else:
                dt = sdk.DeviceTarget(serial=target.serial)
                self.control_ip = None
            self.transport = target.transport if target.transport in ("usb", "wifi") else "ip"
            self._device = self._call(
                "connect",
                lambda: client.connect(dt),
                hint="is the device paired with this laptop (`aria_gen2 auth pair`)? "
                "Over Wi-Fi the control port 23456 must be reachable",
            )
            try:
                self.serial = str(self._device.serial())
            except Exception:
                self.serial = self.serial or target.serial
            self.machine.transition(
                DeviceState.CONNECTED,
                reason=f"connected ({self.transport})",
                transport=self.transport,
                now_ns=self._now(),
            )
            self.get_status()

    def reconnect_over_ip(self, ip: str, *, label: str = "wifi") -> bool:
        """Open a second control connection over ``ip`` (used after the stream reveals it)."""
        if not tcp_reachable(ip):
            self._record("reconnect_over_ip", ip=ip, reachable=False)
            return False
        with self._lock:
            client = self._ensure_client()
            try:
                dev = client.connect(self._sdk.DeviceTarget(ip=ip))
                status = status_to_dict(dev.status())
            except Exception as exc:
                self._record("reconnect_over_ip", ip=ip, error=f"{type(exc).__name__}: {exc}")
                log.warning("device.wifi_control_failed", ip=ip, error=str(exc))
                return False
            self._device = dev
            self.transport = label
            self.control_ip = ip
            self.last_status = status
            self._record("reconnect_over_ip", ip=ip, ok=True)
            log.info("device.control_over_wifi", ip=ip)
            return True

    def disconnect(self) -> None:  # noqa: D102
        with self._lock:
            if self._client is not None:
                try:
                    self._client.disconnect_all()
                except Exception:
                    log.debug("device.disconnect_all_failed", exc_info=True)
            self._device = None
            if self.machine.state is not DeviceState.DISCONNECTED:
                self.machine.transition(
                    DeviceState.DISCONNECTED, reason="disconnect", now_ns=self._now()
                )

    def get_status(self) -> DeviceHealth:  # noqa: D102
        dev = self._require_device()
        status = status_to_dict(self._call("status", dev.status))
        self.last_status = status
        return health_from_status(status)

    def prepare_hotspot(self) -> HotspotInfo:
        """Start the hotspot (explicit band) and ask for / perform the join."""
        hs = self.config.connection.hotspot
        pref = self.config.connection.preferred_control
        use_ble = pref == "ble" or (pref == "auto" and self._device is None and bool(self.serial))
        info = self.cli.hotspot_start(hs.band, hs.bandwidth, ble=use_ble)
        self.hotspot = info
        self._record("hotspot_start", ble=use_ble, ssid=info.ssid)
        joined = False
        if hs.auto_join and platform.system() == "Darwin" and info.ssid and info.password:
            joined = _macos_join_wifi(info.ssid, info.password)
            self._record("hotspot_join", auto=True, ok=joined)
        if not joined:
            self._prompt(
                f"Join the glasses' Wi-Fi hotspot from this laptop now:\n"
                f"    network  {info.ssid}\n    password {info.password}\n"
                "then press ENTER."
            )
            self._record("hotspot_join", auto=False)
        if self.machine.state in (DeviceState.CONNECTED, DeviceState.DISCOVERED):
            self.machine.transition(
                DeviceState.NETWORK_CONFIGURED,
                reason=f"hotspot {info.ssid}",
                transport="ble" if use_ble else self.transport,
                now_ns=self._now(),
            )
        return info

    def prepare_certs(self) -> str | None:
        """Make sure the device and this host share a streaming certificate; return its name.

        ``aria_gen2 streaming install-certs`` generates a *new* certificate every time and
        installs it on both sides, so it is run only when this host has none
        (``install_certs: "auto"``), or always/never as configured. It must run **before**
        the receiver starts: the SDK server loads the local certificate at ``start_server()``
        and the device validates against the name it is told (hardware runs 8–9).
        """
        mode = self.config.connection.install_certs
        local = local_streaming_cert_name()
        if mode == "always" or (mode == "auto" and local is None):
            try:
                self.cert_name = self.cli.install_streaming_certs()
                self._record("install_streaming_certs", cert=self.cert_name)
            except DeviceError as exc:
                log.warning("device.install_certs_failed", error=str(exc))
                self._record("install_streaming_certs", error=str(exc))
                self.cert_name = local
        else:
            self.cert_name = local
            self._record("streaming_certs", cert=local, reused=True)
        if self.cert_name is None:
            log.warning(
                "device.no_streaming_cert",
                note="no certificate on this host; start_streaming will try the device's own",
            )
        return self.cert_name

    def configure_streaming(self, plan: StreamingPlan) -> None:
        """Push ``HttpStreamingConfig`` (certificate name from :meth:`prepare_certs`)."""
        dev = self._require_device()
        sdk = self._sdk
        self.plan = plan
        if self.cert_name is None:
            self.prepare_certs()
        cfg = sdk.HttpStreamingConfig()
        if plan.profile_json:
            cfg.profile_json = plan.profile_json
        elif plan.profile_name:
            cfg.profile_name = plan.profile_name
        cfg.streaming_interface = {
            "usb": sdk.StreamingInterface.USB_NCM,
            "wifi_sta": sdk.StreamingInterface.WIFI_STA,
            "wifi_sap": sdk.StreamingInterface.WIFI_SAP,
        }[plan.interface]
        cfg.batch_period_ms = int(plan.batch_period_ms)
        if self.cert_name:
            cfg.streaming_cert_name = self.cert_name
        self._call("set_streaming_config", lambda: dev.set_streaming_config(cfg))
        self.machine.transition(
            DeviceState.STREAM_READY,
            reason=f"{plan.profile_name or 'custom profile'} over {plan.interface}",
            transport=self.transport,
            now_ns=self._now(),
        )

    def start_streaming(self) -> None:  # noqa: D102
        dev = self._require_device()
        self._call(
            "start_streaming",
            dev.start_streaming,
            hint="if the device says it is already streaming, run `aria_gen2 streaming stop`",
        )
        self.machine.transition(
            DeviceState.STREAMING,
            reason="start_streaming",
            transport=self.transport,
            now_ns=self._now(),
        )
        try:
            info = dev.get_streaming_info()
            self.streaming_info = {
                "is_streaming": bool(info.is_streaming),
                "profile": str(info.profile_name),
                "interface": str(info.streaming_interface),
            }
        except Exception:
            self.streaming_info = {}

    def stop_streaming(self) -> None:  # noqa: D102
        if self.machine.state is DeviceState.STREAMING:
            self.machine.transition(
                DeviceState.STOPPING,
                reason="stop_streaming",
                transport=self.transport,
                now_ns=self._now(),
            )
        dev = self._device
        if dev is None:
            return
        try:
            self._call("stop_streaming", dev.stop_streaming)
        except DeviceError as exc:
            log.error("device.stop_streaming_failed", error=str(exc))
            self.machine.fail(exc, now_ns=self._now(), transport=self.transport)
            raise
        if self.machine.state is DeviceState.STOPPING:
            self.machine.transition(
                DeviceState.CONNECTED,
                reason="stopped",
                transport=self.transport,
                now_ns=self._now(),
            )

    def quality_stats(self) -> Mapping[str, Any] | None:  # noqa: D102
        q = self.quality()
        return None if q is None else q.as_dict()

    def quality(self) -> QualityStats | None:
        """Parsed ``get_data_quality_stats()`` (``None`` when the call fails)."""
        dev = self._device
        if dev is None:
            return None
        try:
            raw = dev.get_data_quality_stats()
        except Exception as exc:
            log.debug("device.quality_stats_failed", error=str(exc))
            return None
        return parse_quality_stats(raw)

    def fail(self, error: DeviceError) -> None:
        """Record an error state (used by the source when the data path dies)."""
        if self.machine.state is not DeviceState.ERROR:
            self.machine.fail(error, now_ns=self._now(), transport=self.transport)

    # -- internals ----------------------------------------------------------------------
    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = self._sdk.DeviceClient()
        return self._client


def _import_sdk() -> Any:
    try:
        import aria.sdk_gen2 as sdk
    except ImportError as exc:
        raise DeviceError(
            "the Project Aria client SDK is not installed in this environment",
            hint="pip install 'ariastreamlab[aria]' (requires Meta's projectaria_client_sdk wheel)",
        ) from exc
    return sdk


def _default_prompt(message: str) -> None:
    print(f"\n>>> {message}", flush=True)  # noqa: T201 - interactive step
    try:
        input()
    except EOFError:
        time.sleep(5)


def _macos_join_wifi(ssid: str, password: str, interface: str = "en0") -> bool:
    try:
        proc = subprocess.run(
            ["networksetup", "-setairportnetwork", interface, ssid, password],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("hotspot.auto_join_failed", error=str(exc))
        return False
    ok = proc.returncode == 0 and "Error" not in proc.stdout
    log.info("hotspot.auto_join", ssid=ssid, ok=ok, output=proc.stdout.strip()[:200])
    return ok


def _opt_int(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _opt_bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _opt_str(v: Any) -> str | None:
    return None if v is None else str(v)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)
