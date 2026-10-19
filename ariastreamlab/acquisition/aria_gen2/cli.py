"""Thin wrapper around Meta's ``aria_gen2`` command-line tool.

Some control-plane operations have no Python API in the client SDK (hotspot control, BLE,
pairing status, certificate installation). This module runs the bundled CLI as a subprocess
and parses the few lines we rely on. Everything observed on hardware is documented in
docs/ARCHITECTURE.md §0.8–0.11; the parsers are deliberately tolerant and keep the raw
output so a format change is visible rather than silent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.base import HotspotInfo
from ariastreamlab.core.errors import DeviceError
from ariastreamlab.core.logging import get_logger

log = get_logger("ariastreamlab.acquisition.aria_gen2.cli")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

BAND_FLAGS = {"2.4GHz": "BAND_2GHZ", "5GHz": "BAND_5GHZ", "6GHz": "BAND_6GHZ"}
BANDWIDTH_FLAGS = {
    "20MHz": "BW_20MHZ",
    "40MHz": "BW_40MHZ",
    "80MHz": "BW_80MHZ",
    "160MHz": "BW_160MHZ",
}


def strip_ansi(text: str) -> str:
    """Remove terminal colour codes (the CLI colours its output even when piped)."""
    return _ANSI.sub("", text)


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:  # noqa: D102
        return self.returncode == 0

    @property
    def text(self) -> str:
        """Stdout and stderr together (the CLI logs on both)."""
        return self.stdout + "\n" + self.stderr

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "seconds": self.seconds,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
        }


@dataclass
class AriaCli:
    """Runs ``aria_gen2`` with the global options the configuration implies.

    Args:
        executable: Path of the CLI; found on ``PATH`` by default.
        serial: Passed as ``--serial`` (required by ``--ble``).
        device_ip: Passed as ``--device-ip`` when control should go over a known address.
        timeout_s: Per-command timeout.
    """

    executable: str | None = None
    serial: str | None = None
    device_ip: str | None = None
    timeout_s: float = 90.0
    history: list[CliResult] = field(default_factory=list)

    @staticmethod
    def find() -> str | None:
        """The CLI on ``PATH`` (``None`` when the SDK is not installed in this environment)."""
        return shutil.which("aria_gen2")

    def available(self) -> bool:  # noqa: D102
        return (self.executable or self.find()) is not None

    def run(self, *args: str, ble: bool = False, timeout_s: float | None = None) -> CliResult:
        """Run ``aria_gen2 <args>``; each element of ``args`` may contain spaces."""
        exe = self.executable or self.find()
        if exe is None:
            raise DeviceError(
                "the `aria_gen2` command-line tool is not on PATH",
                hint="install the client SDK here: pip install 'ariastreamlab[aria]'",
            )
        argv = [exe]
        if ble:
            if not self.serial:
                raise DeviceError(
                    "BLE control needs the device serial", hint="set device.serial in the config"
                )
            argv += ["--ble", "--serial", self.serial]
        elif self.device_ip:
            argv += ["--device-ip", self.device_ip]
        elif self.serial:
            argv += ["--serial", self.serial]
        argv += [tok for arg in args for tok in arg.split()]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_s or self.timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(
                f"`{' '.join(argv[1:])}` did not finish within {timeout_s or self.timeout_s:g} s"
            ) from exc
        res = CliResult(
            tuple(argv[1:]),
            proc.returncode,
            strip_ansi(proc.stdout),
            strip_ansi(proc.stderr),
            round(time.monotonic() - t0, 2),
        )
        self.history.append(res)
        log.debug("aria_gen2", argv=res.argv, returncode=res.returncode, seconds=res.seconds)
        return res

    # -- parsed operations ----------------------------------------------------------------
    def hotspot_start(self, band: str, bandwidth: str, *, ble: bool = False) -> HotspotInfo:
        """``device hotspot start`` with explicit band/bandwidth (the firmware alternates)."""
        res = self.run(
            "device hotspot start",
            f"--wifi-band {BAND_FLAGS[band]}",
            f"--channel-bandwidth {BANDWIDTH_FLAGS[bandwidth]}",
            ble=ble,
            timeout_s=120,
        )
        if not res.ok:
            raise DeviceError(
                f"`device hotspot start` failed (exit {res.returncode}): {_last_line(res.text)}",
                hint="is the device paired (`aria_gen2 auth check`) and in range?",
            )
        info = parse_hotspot(res.text)
        if info.ssid is None:
            raise DeviceError(
                "`device hotspot start` succeeded but printed no SSID",
                hint="run `aria_gen2 device hotspot status` by hand and report the output",
            )
        return info

    def hotspot_status(self, *, ble: bool = False) -> HotspotInfo:
        """``device hotspot status``."""
        res = self.run("device hotspot status", ble=ble)
        return parse_hotspot(res.text)

    def install_streaming_certs(self) -> str | None:
        """``streaming install-certs`` (device + local host); returns the installed cert name.

        The name the device must be told is the one our server will present, i.e. the label
        in ``~/.aria/streaming-certs/persistent/publisher-cert-name`` (what the SDK reads via
        ``getDefaultServerCerts()``). The CLI output can mention other names (the previous
        cert), so it is only a fallback — a mismatch makes the device's TLS handshake fail
        silently and no data ever arrives (hardware run 8).
        """
        res = self.run("streaming install-certs", timeout_s=120)
        if not res.ok:
            raise DeviceError(
                f"`streaming install-certs` failed (exit {res.returncode}): {_last_line(res.text)}"
            )
        installed = local_streaming_cert_name()
        if installed is not None:
            return installed
        m = re.search(r"cert_\d+", res.text)
        return m.group(0) if m else None

    def auth_check(self, *, ble: bool = False) -> bool:
        """``auth check``: True when the device reports the laptop as authenticated."""
        res = self.run("auth check", ble=ble)
        return (
            res.ok
            and "authenticated" in res.text.lower()
            and "not authenticated" not in (res.text.lower())
        )

    def device_list(self, *, ble_scan: bool = False) -> CliResult:
        """``device list`` (with ``--ble-scan`` to probe paired BLE devices in range)."""
        args = ["device list"]
        if ble_scan:
            args.insert(0, "--ble-scan")
        return self.run(*args, timeout_s=60)


PERSISTENT_CERTS_DIR = Path.home() / ".aria" / "streaming-certs" / "persistent"
PUBLISHER_CERT_NAME_FILE = "publisher-cert-name"


def local_streaming_cert_name(certs_dir: Path | None = None) -> str | None:
    """The name of the streaming certificate installed on this host (``None`` if none)."""
    f = (certs_dir or PERSISTENT_CERTS_DIR) / PUBLISHER_CERT_NAME_FILE
    try:
        name = f.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def parse_hotspot(text: str) -> HotspotInfo:
    """Parse the ``hotspot start/status`` output.

    Observed format (SDK 2.5.1)::

        Wifi hotspot status
        enabled: 1
        ssid: Aria-M2FHCZP6
        passphrase: 10380804
        wifi band: BAND_5GHZ
        wifi channel bandwidth: BW_40MHZ
    """
    clean = strip_ansi(text)

    def grab(key: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(\S.*?)\s*$", clean, re.M | re.I)
        return m.group(1) if m else None

    ssid = grab("ssid")
    return HotspotInfo(
        ssid=ssid,
        password=grab("passphrase") or grab("password"),
        device_ip=grab("ip") or grab("device ip"),
        raw_output=clean.strip()[-2000:],
    )


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "(no output)"
