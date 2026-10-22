"""``AriaGen2Source``: live acquisition from the glasses (docs/ARCHITECTURE.md §4.3).

Data path: Meta's ``aria.stream_receiver.StreamReceiver`` (``StreamDataInterface`` with
Python/hardware H.265 decoding for RGB, raw mono SLAM, optional VRS recording) runs our
``AriaGen2HttpServer`` on ``0.0.0.0:<port>``; the device pushes to ``oatmeal_server.local``.
Typed callbacks are bound by :class:`CallbackBinder` — the same code the SDK replay backend
uses, so a live sample and its replayed twin differ only in ``provenance``.

Control path: :class:`AriaGen2DeviceManager`. Once the device has connected to the server,
the stream's source address is tried as a Wi-Fi control address (verified: port 23456 is
open there, not on the hotspot gateway), so the cable can be unplugged afterwards.

A monitor thread polls the device status, the quality statistics and the link, and
publishes ``health.device`` / ``health.quality`` / ``health.link`` on the bus.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.aria_gen2.callbacks import CallbackBinder
from ariastreamlab.acquisition.aria_gen2.device import AriaGen2DeviceManager, _jsonable
from ariastreamlab.acquisition.aria_gen2.profiles import build_plan
from ariastreamlab.acquisition.base import (
    DeviceState,
    DeviceTransition,
    Source,
    SourceCapabilities,
    SourceStats,
    StreamingPlan,
)
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import DeviceError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.sample import Provenance
from ariastreamlab.timing.clock import HostClock

__all__ = ["AriaGen2Source"]

log = get_logger("ariastreamlab.acquisition.aria_gen2")


class AriaGen2Source(Source):
    """Live source. ``start()`` performs the whole connect → stream sequence.

    Args:
        config: The experiment configuration (device, streams, storage).
        session_id: Session UUID for provenance.
        clock: Host clock.
        manager: A ready device manager (tests inject a fake); built from ``config`` otherwise.
        on_transition: Receives every device state change (the experiment turns them into
            events).
        health_interval_s: Period of the ``health.device`` snapshot.
        quality_interval_s: Period of the ``health.quality`` snapshot.
        prompt: Shown when the user must act (join the hotspot); blocks until done. Default:
            print and wait for ENTER on the terminal; the dashboard supplies a dialog.
    """

    def __init__(
        self,
        config: ExperimentConfig,
        *,
        session_id: str,
        clock: HostClock | None = None,
        manager: AriaGen2DeviceManager | None = None,
        on_transition: Callable[[DeviceTransition], None] | None = None,
        health_interval_s: float = 5.0,
        quality_interval_s: float = 10.0,
        no_data_timeout_s: float = 20.0,
        prompt: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.clock = clock or HostClock()
        self.manager = manager or AriaGen2DeviceManager(
            config.device, clock=self.clock, on_transition=on_transition, prompt=prompt
        )
        if manager is not None and on_transition is not None:
            manager.machine.on_transition(on_transition)
        self.health_interval_s = health_interval_s
        self.quality_interval_s = quality_interval_s
        self.no_data_timeout_s = no_data_timeout_s
        self.raw_dir: Path | None = None
        self.plan: StreamingPlan = build_plan(config, None)
        self.capabilities = SourceCapabilities(
            origin="live",
            streams=self.plan.streams,
            can_record_raw=config.storage.save_raw_vrs,
            is_paced=True,
            supports_replay_control=False,
        )
        self.calibration: dict[str, Any] | None = None
        self.device_id: str = config.device.serial or "aria-gen2"
        self._bus: EventBus | None = None
        self._binder: CallbackBinder | None = None
        self._receiver: Any = None
        self._server: Any = None
        self._time_sync: Any = None
        self._raw_messages: Counter[str] = Counter()
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self.data_path: str = "none"
        self.stream_source_ip: str | None = None
        self.warnings: list[str] = list(self.plan.warnings)
        self.vrs_files: list[Path] = []
        self.first_sample_host_ns: int | None = None
        self.link: dict[str, Any] = {}
        self._last_ticsync: dict[str, Any] = {"valid": False}
        self._last_queues: dict[str, int] = {}

    # -- configuration before start ------------------------------------------------------
    def record_raw_to(self, raw_dir: Path) -> None:
        """Write the host-received VRS into ``raw_dir`` (the SDK names the file)."""
        self.raw_dir = raw_dir
        self.plan = build_plan(self.config, raw_dir)

    # -- Source ------------------------------------------------------------------------
    def start(self, bus: EventBus) -> None:  # noqa: D102
        if self._started:
            raise DeviceError("AriaGen2Source.start() called twice")
        self._started = True
        self._bus = bus
        self._stop.clear()
        mgr = self.manager
        for w in self.plan.warnings:
            log.warning("plan.warning", note=w)
        try:
            if mgr.state in (DeviceState.DISCONNECTED, DeviceState.DISCOVERED):
                mgr.connect()
            self.device_id = mgr.serial or self.device_id
            if self.plan.interface == "wifi_sap" and self.config.device.connection.hotspot.start:
                mgr.prepare_hotspot()
            mgr.prepare_certs()  # before the server loads the local certificate
            self._start_receiver(bus)
            mgr.configure_streaming(self.plan)
            mgr.start_streaming()
        except Exception as exc:
            self._teardown_receiver()
            if isinstance(exc, DeviceError):
                mgr.fail(exc)
                self.finished.set()
                raise
            err = DeviceError(f"live start failed: {type(exc).__name__}: {exc}")
            mgr.fail(err)
            self.finished.set()
            raise err from exc
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="aria-monitor", daemon=True
        )
        self._monitor.start()
        log.info(
            "live.started",
            device=self.device_id,
            profile=self.plan.profile_name,
            interface=self.plan.interface,
            batch_period_ms=self.plan.batch_period_ms,
            vrs_dir=self.plan.record_vrs_path,
        )

    def stop(self) -> None:  # noqa: D102
        if not self._started:
            self.finished.set()
            return
        self._stop.set()
        mgr = self.manager
        # The TimeSyncRef is only safe to read while the device connection is up: reading it
        # after the device disconnected crashed the interpreter twice (runs 11–12), before
        # and after stop_server(). Drop it before anything can close the connection.
        self._time_sync = None
        if self._monitor is not None:
            self._monitor.join(3)
        if (
            mgr.transport == "usb"
            and self.stream_source_ip
            and self.data_path == "wifi"
            and not self._usb_link_present()
        ):
            log.info("live.stop_over_wifi", ip=self.stream_source_ip)
            mgr.reconnect_over_ip(self.stream_source_ip)
        with contextlib.suppress(DeviceError):
            if mgr.state in (DeviceState.STREAMING, DeviceState.STOPPING):
                mgr.stop_streaming()
        # let the last batches arrive and the SDK's queue processors go quiet, then release
        # the receiver (which finalises the VRS). Tearing down while an image is still being
        # decoded on an SDK thread crashed the interpreter (hardware run 10).
        self._wait_connections_closed(timeout=5.0)
        self._wait_quiescent(quiet_s=1.5, timeout=20.0)
        self._teardown_receiver()
        self._collect_vrs_files()
        if self._monitor is not None:
            self._monitor.join(5)
            self._monitor = None
        self.finished.set()
        self._started = False
        log.info(
            "live.stopped", vrs_files=[str(p) for p in self.vrs_files], data_path=self.data_path
        )

    def stats(self) -> SourceStats:  # noqa: D102
        binder = self._binder
        received = dict(binder.received) if binder is not None else {}
        extra: dict[str, Any] = {
            "data_path": self.data_path,
            "stream_source_ip": self.stream_source_ip,
            "control_transport": self.manager.transport,
            "control_ip": self.manager.control_ip,
            "profile": self.plan.profile_name,
            "interface": self.plan.interface,
            "batch_period_ms": self.plan.batch_period_ms,
            "callback_errors": dict(binder.errors) if binder is not None else {},
            "raw_messages": dict(self._raw_messages),
            "sdk_queues": self._queue_sizes(),
            "ticsync": dict(self._last_ticsync),
            "warnings": list(self.warnings),
        }
        return SourceStats(received, {}, extra)

    # -- manifest helpers --------------------------------------------------------------
    def device_record(self) -> dict[str, Any]:
        """What goes into ``manifest.device``."""
        st = {k: _jsonable(v) for k, v in self.manager.last_status.items()}
        return {
            "serial": self.device_id,
            "model": "Aria Gen 2",
            "firmware": st.get("firmware_version") or st.get("os_version"),
            "device_mode": st.get("device_mode"),
            "status": st,
            "streaming_cert": self.manager.cert_name,
            "profile": self.manager.streaming_info.get("profile") or self.plan.profile_name,
            "streaming_interface": self.plan.interface,
            "batch_period_ms": self.plan.batch_period_ms,
            "hotspot_ssid": None if self.manager.hotspot is None else self.manager.hotspot.ssid,
            "control_transport": self.manager.transport,
            "data_path": self.data_path,
            "transport_log": self.manager.transport_log[-50:],
        }

    # -- internals: receiver ------------------------------------------------------------
    def _start_receiver(self, bus: EventBus) -> None:
        import aria.sdk_gen2 as sdk
        import aria.stream_receiver as stream_receiver

        server_cfg = sdk.HttpServerConfig()
        server_cfg.address = "0.0.0.0"
        server_cfg.port = int(self.plan.server_port)
        receiver = stream_receiver.StreamReceiver(enable_image_decoding=True)
        receiver.set_server_config(server_cfg)
        if self.plan.record_vrs_path:
            Path(self.plan.record_vrs_path).mkdir(parents=True, exist_ok=True)
            receiver.record_to_vrs(self.plan.record_vrs_path)
        self._binder = CallbackBinder(
            bus,
            Provenance("live", "aria_gen2", self.session_id),
            self.device_id,
            set(self.plan.streams),
            self.clock,
        )
        self._binder.bind(receiver)
        receiver.register_raw_message_callback(self._raw_message)
        self._size_sdk_queues(receiver)
        self._receiver = receiver
        try:
            self._server = receiver.start_server()
        except Exception as exc:
            raise DeviceError(
                f"cannot start the streaming server on port {self.plan.server_port}: {exc}",
                hint="is another AriaStreamLab / aria_streaming_viewer running? "
                "Change device.connection.server_port or stop it",
            ) from exc
        self._time_sync = receiver.get_time_sync()

    def _size_sdk_queues(self, receiver: Any) -> None:
        """Size the SDK's per-stream queues for the batch period.

        With ``batch_period_ms = 800`` one batch carries ~24 gaze/hand samples and ~640 IMU
        samples, while Meta's defaults are 10 and 800: run 11 showed the eye-gaze queue
        dropping 25 of 455 samples at the SDK level. Keep room for three batches.
        """
        batches = 3.0
        seconds = max(self.plan.batch_period_ms, 200) / 1000.0 * batches
        sizes = {
            "imu": int(max(800, 800 * seconds)),
            "vio_high_freq": int(max(800, 800 * seconds)),
            "eye_gaze": int(max(32, 30 * seconds)),
            "hand_pose": int(max(32, 30 * seconds)),
            "vio": int(max(32, 10 * seconds)),
            "rgb": 100,
            "slam": 1000,
            "et": 100,
        }
        for name, size in sizes.items():
            setter = getattr(receiver, f"set_{name}_queue_size", None)
            if setter is not None:
                with contextlib.suppress(Exception):
                    setter(size)
        log.debug("live.sdk_queue_sizes", **sizes)

    def _raw_message(self, message: Any, *_: Any) -> None:
        with contextlib.suppress(Exception):  # never let a counter break an SDK thread
            self._raw_messages[str(getattr(message, "id", "?")).rsplit(".", 1)[-1]] += 1

    def _teardown_receiver(self) -> None:
        self._last_queues = self._queue_sizes()
        receiver, self._receiver, self._server = self._receiver, None, None
        self._time_sync = None
        if receiver is None:
            return

        def _do() -> None:
            with contextlib.suppress(Exception):
                receiver.stop_server()

        t = threading.Thread(target=_do, name="aria-receiver-stop", daemon=True)
        t.start()
        t.join(30)
        if t.is_alive():
            log.error("live.receiver_stop_hung")
            self.warnings.append("the streaming server did not stop within 30 s")

    def _wait_connections_closed(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._connections():
                return
            time.sleep(0.25)

    def _wait_quiescent(self, *, quiet_s: float, timeout: float) -> None:
        """Return once no callback has fired for ``quiet_s`` (bounded by ``timeout``)."""
        binder = self._binder
        if binder is None:
            return
        deadline = time.monotonic() + timeout
        quiet_ns = int(quiet_s * 1e9)
        while time.monotonic() < deadline:
            last = binder.last_host_ns
            if last == 0 or self.clock.now_ns() - last >= quiet_ns:
                return
            time.sleep(0.1)
        log.warning("live.not_quiescent", note=f"callbacks still firing after {timeout:g} s")

    def _collect_vrs_files(self) -> None:
        if not self.plan.record_vrs_path:
            return
        d = Path(self.plan.record_vrs_path)
        files = sorted(d.glob("*.vrs"))
        # the SDK writer flushes when the handler is released; wait for sizes to settle
        last = None
        for _ in range(40):
            sizes = [os.path.getsize(f) for f in files]
            if sizes and sizes == last:
                break
            last = sizes
            time.sleep(0.25)
            files = sorted(d.glob("*.vrs"))
        self.vrs_files = files
        if not files:
            self.warnings.append("no VRS file was written by the SDK")

    # -- internals: monitor -------------------------------------------------------------
    def _connections(self) -> list[dict[str, Any]]:
        server = self._server
        if server is None:
            return []
        try:
            return [dict(c) for c in server.connections()]
        except Exception:
            return []

    def _monitor_loop(self) -> None:
        mgr = self.manager
        bus = self._bus
        assert bus is not None
        next_health = 0.0
        next_quality = time.monotonic() + 2.0
        wifi_control_tried = False
        no_data_warned = False
        t_start = time.monotonic()
        while not self._stop.wait(1.0):
            now = time.monotonic()
            conns = self._connections()
            total = sum(self._binder.received.values()) if self._binder else 0
            if conns:
                ip = conns[0].get("client_ip")
                if ip and ip != self.stream_source_ip:
                    self.stream_source_ip = str(ip)
                    self.data_path = "usb" if self._is_usb_ip(str(ip)) else "wifi"
                    log.info("live.device_connected", client_ip=ip, data_path=self.data_path)
                    if self.data_path == "usb" and self.plan.interface != "usb":
                        msg = (
                            f"the device pushes data over the USB link ({ip}) although the "
                            f"plan asked for {self.plan.interface}: the laptop is probably "
                            "not on the hotspot; keep the cable in for this session"
                        )
                        self.warnings.append(msg)
                        log.warning("live.data_over_usb", note=msg)
                # Control hand-over to Wi-Fi. DeviceClient.connect() holds the interpreter
                # lock for its whole duration (21 s in run 13) and every SDK callback stalls
                # meanwhile, so it is done only when needed: when the USB link disappears
                # (``on_unplug``, default), immediately (``early``), or never.
                mode = self.config.device.connection.wifi_control
                if (
                    not wifi_control_tried
                    and mode != "never"
                    and self.data_path == "wifi"
                    and mgr.transport != "wifi"
                    and self.stream_source_ip
                ):
                    usb_present = self._usb_link_present() if mode == "on_unplug" else True
                    if mode == "early" or not usb_present:
                        wifi_control_tried = True
                        if not usb_present:
                            log.warning(
                                "live.usb_unplugged",
                                note="USB link gone; moving control to Wi-Fi "
                                "(data may pause while the connection is made)",
                            )
                        if mgr.reconnect_over_ip(self.stream_source_ip):
                            self.warnings.append(
                                f"control moved to Wi-Fi ({self.stream_source_ip})"
                                + ("" if not usb_present else "; the USB cable can be unplugged")
                            )
            if total and self.first_sample_host_ns is None:
                self.first_sample_host_ns = self.clock.now_ns()
            if not total and not no_data_warned and now - t_start > self.no_data_timeout_s:
                no_data_warned = True
                msg = (
                    f"no data received {self.no_data_timeout_s:g} s after start_streaming "
                    f"(server connections: {len(conns)})"
                )
                self.warnings.append(msg)
                log.warning(
                    "live.no_data",
                    note=msg,
                    hint="is the laptop on the hotspot / same network? firewall on port "
                    f"{self.plan.server_port}? does the device resolve oatmeal_server.local? "
                    f"streaming cert told to the device: {mgr.cert_name} (a mismatch with "
                    "~/.aria/streaming-certs/persistent fails the TLS handshake silently; "
                    "run `aria_gen2 streaming install-certs`)",
                )
            if conns:  # only while the device is connected (see stop())
                self._last_ticsync = self._ticsync()
            self.link = {
                "connections": len(conns),
                "data_path": self.data_path,
                "stream_source_ip": self.stream_source_ip,
                "control_transport": mgr.transport,
                "samples": total,
                "ticsync": dict(self._last_ticsync),
                "sdk_queues": self._queue_sizes(),
            }
            bus.publish("health.link", dict(self.link))
            if now >= next_health:
                next_health = now + self.health_interval_s
                try:
                    health = mgr.get_status()
                    bus.publish("health.device", health)
                    if health.thermal_mitigation:
                        log.warning(
                            "device.thermal_mitigation", skin_temp_c=health.skin_temperature_c
                        )
                except DeviceError as exc:
                    log.warning("device.status_failed", error=str(exc))
            if now >= next_quality:
                next_quality = now + self.quality_interval_s
                q = mgr.quality()
                if q is not None:
                    bus.publish("health.quality", q)

    def _usb_link_present(self) -> bool:
        try:
            client = self.manager._ensure_client()
            return bool(list(client.usb_network_devices()))
        except Exception:
            return False

    def _is_usb_ip(self, ip: str) -> bool:
        try:
            client = self.manager._ensure_client()
            return any(str(t.ip) == ip for t in client.usb_network_devices())
        except Exception:
            return False

    def _queue_sizes(self) -> dict[str, int]:
        r = self._receiver
        if r is None:
            return self._last_queues
        out: dict[str, int] = {}
        for name in ("rgb", "slam", "et", "imu", "vio", "vio_high_freq", "eye_gaze", "hand_pose"):
            fn = getattr(r, f"get_{name}_queue_size", None)
            if fn is None:
                continue
            with contextlib.suppress(Exception):
                out[name] = int(fn())
        return out

    def _ticsync(self) -> dict[str, Any]:
        ts = self._time_sync
        if ts is None:
            return self._last_ticsync
        try:
            return {"connected": bool(ts.is_connected()), "valid": bool(ts.is_valid())}
        except Exception:
            return {"valid": False}
