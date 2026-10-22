"""The *Setup* page's model: what is installed here, what is missing, and how to fix it.

Everything the page does happens inside the Python environment the application runs in —
install Meta's SDK and ``projectaria_tools`` with pip, pair the glasses, install the
streaming certificates, start the hotspot, run the ``doctor`` checks. The one thing it cannot
do is create that environment: ``scripts/install.sh`` / ``install.ps1`` exist for that step.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.doctor import Check, run_checks

__all__ = [
    "ACTIONS",
    "Action",
    "CommandRun",
    "Package",
    "environment",
    "packages",
    "pip_install_argv",
    "run_checks",
]

SDK_PYTHON = ((3, 10), (3, 12))


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ariastreamlab")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


"""Python versions Meta's SDK wheels cover (inclusive)."""


@dataclass(frozen=True)
class Package:
    """One row of the packages table."""

    name: str
    module: str
    purpose: str
    pip: str
    required: bool
    version: str | None = None

    @property
    def installed(self) -> bool:  # noqa: D102
        return self.version is not None


PACKAGES: tuple[Package, ...] = (
    Package("numpy", "numpy", "arrays", "numpy", True),
    Package("pyarrow", "pyarrow", "Parquet storage", "pyarrow", True),
    Package("opencv", "cv2", "images, MP4", "opencv-python-headless", True),
    Package("pydantic", "pydantic", "configuration", "pydantic", True),
    Package("nicegui", "nicegui", "the application", "nicegui", True),
    Package("pywebview", "webview", "the desktop window", "pywebview", False),
    Package(
        "projectaria_client_sdk",
        "aria.sdk_gen2",
        "live acquisition from the glasses (also provides the aria_gen2 CLI)",
        "projectaria-client-sdk~=2.5",
        False,
    ),
    Package(
        "projectaria_tools",
        "projectaria_tools",
        "VRS replay and inspection",
        "projectaria-tools~=2.3",
        False,
    ),
    Package("rerun", "rerun", "optional 3-D viewer", "rerun-sdk~=0.33.0", False),
)


def _version_of(module: str) -> str | None:
    import importlib

    try:
        mod = importlib.import_module(module)
    except Exception:
        return None
    v = getattr(mod, "__version__", None)
    if v is None and module == "aria.sdk_gen2":
        try:
            from importlib.metadata import version

            v = version("projectaria-client-sdk")
        except Exception:
            v = "present"
    return str(v or "present")


def packages() -> list[Package]:
    """The packages table with the versions importable right now."""
    import importlib

    importlib.invalidate_caches()  # a pip install from the page must show up without a restart
    return [
        Package(p.name, p.module, p.purpose, p.pip, p.required, _version_of(p.module))
        for p in PACKAGES
    ]


def environment() -> dict[str, Any]:
    """Facts about the interpreter and the machine the application runs in."""
    py = sys.version_info[:2]
    ok = SDK_PYTHON[0] <= py <= SDK_PYTHON[1]
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return {
        "python": platform.python_version(),
        "executable": sys.executable,
        "virtualenv": venv,
        "prefix": sys.prefix,
        "sdk_compatible": ok,
        "sdk_python_note": ""
        if ok
        else (
            f"Meta's SDK has wheels for Python {SDK_PYTHON[0][0]}.{SDK_PYTHON[0][1]}–"
            f"{SDK_PYTHON[1][0]}.{SDK_PYTHON[1][1]}; this environment runs "
            f"{platform.python_version()}. Create one with a supported Python "
            "(scripts/install.sh) for live acquisition."
        ),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "ariastreamlab": _version(),
        "aria_gen2_cli": shutil.which("aria_gen2"),
        "cwd": str(Path.cwd()),
        "certs_dir": str(Path.home() / ".aria" / "streaming-certs" / "persistent"),
        "has_streaming_cert": (
            Path.home() / ".aria" / "streaming-certs" / "persistent" / "publisher-cert-name"
        ).exists(),
    }


def pip_install_argv(specs: Sequence[str], *, upgrade: bool = False) -> list[str]:
    """``python -m pip install …`` for this interpreter (uv when it is the installer here)."""
    argv = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        argv.append("--upgrade")
    return [*argv, *specs]


@dataclass
class Action:
    """A button on the page: a command run with its output streamed to the log."""

    key: str
    label: str
    description: str
    argv: Callable[[], list[str]]
    needs_cli: bool = False
    timeout_s: float = 600.0
    confirm: str | None = None
    """A confirmation question shown before running (restarts the hotspot, generates certs…)."""


def _cli(*args: str) -> Callable[[], list[str]]:
    def build() -> list[str]:
        exe = shutil.which("aria_gen2")
        if exe is None:
            raise RuntimeError("the aria_gen2 CLI is not installed in this environment")
        return [exe, *args]

    return build


ACTIONS: tuple[Action, ...] = (
    Action(
        "install_sdk",
        "install the Aria SDK",
        "pip installs projectaria-client-sdk and projectaria-tools into this environment "
        "(the aria_gen2 command comes with them)",
        lambda: pip_install_argv(["projectaria-client-sdk~=2.5", "projectaria-tools~=2.3"]),
        timeout_s=1800,
    ),
    Action(
        "upgrade_self",
        "upgrade AriaStreamLab's dependencies",
        "pip install --upgrade of the packages the application needs (not the SDK)",
        lambda: pip_install_argv(["nicegui", "pywebview", "pyarrow", "numpy"], upgrade=True),
        timeout_s=1800,
    ),
    Action(
        "device_list",
        "list devices",
        "aria_gen2 device list — what the laptop can reach (USB, Wi-Fi)",
        _cli("device", "list"),
        needs_cli=True,
        timeout_s=120,
    ),
    Action(
        "auth_check",
        "check pairing",
        "aria_gen2 auth check — is this laptop paired with the glasses?",
        _cli("auth", "check"),
        needs_cli=True,
        timeout_s=120,
    ),
    Action(
        "auth_pair",
        "pair over USB",
        "aria_gen2 auth pair — connect the glasses with the cable, then accept the prompt in "
        "the Aria Companion App on the phone",
        _cli("auth", "pair"),
        needs_cli=True,
        timeout_s=300,
    ),
    Action(
        "install_certs",
        "install streaming certificates",
        "aria_gen2 streaming install-certs — generates a new certificate and installs it on "
        "the glasses and this laptop (needed once; the live run does it automatically when "
        "none exists)",
        _cli("streaming", "install-certs"),
        needs_cli=True,
        timeout_s=300,
        confirm="This generates a NEW certificate (the previous one stops working). Continue?",
    ),
    Action(
        "hotspot_status",
        "hotspot status",
        "aria_gen2 device hotspot status — is the glasses' hotspot up, on which network?",
        _cli("device", "hotspot", "status"),
        needs_cli=True,
        timeout_s=120,
    ),
    Action(
        "hotspot_start",
        "start the hotspot (5 GHz / 40 MHz)",
        "aria_gen2 device hotspot start — prints the network name and password to join",
        _cli(
            "device",
            "hotspot",
            "start",
            "--wifi-band",
            "BAND_5GHZ",
            "--channel-bandwidth",
            "BW_40MHZ",
        ),
        needs_cli=True,
        timeout_s=120,
        confirm="Restarting a running hotspot drops the laptop off it. Continue?",
    ),
    Action(
        "streaming_stop",
        "stop a stuck stream",
        "aria_gen2 streaming stop — after a crash that left the glasses streaming",
        _cli("streaming", "stop"),
        needs_cli=True,
        timeout_s=120,
    ),
)


@dataclass
class CommandRun:
    """A command running in the background with its output collected line by line."""

    argv: list[str]
    lines: list[str] = field(default_factory=list)
    returncode: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    error: str | None = None
    _proc: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:  # noqa: D102
        return self.finished_at is None

    @property
    def ok(self) -> bool:  # noqa: D102
        return self.returncode == 0 and self.error is None

    def text(self, last: int = 400) -> str:
        """The collected output (last ``last`` lines)."""
        with self._lock:
            return "\n".join(self.lines[-last:])

    def start(self, *, timeout_s: float = 600.0) -> CommandRun:
        """Run ``argv`` on a thread; output goes to :attr:`lines`."""

        def _timeout() -> None:
            if self.running and self._proc is not None:
                self.error = f"timed out after {timeout_s:g} s"
                self._proc.kill()

        def _main() -> None:
            watchdog: threading.Timer | None = None
            try:
                env = dict(os.environ, PYTHONUNBUFFERED="1", TERM="dumb", NO_COLOR="1")
                self._proc = subprocess.Popen(
                    self.argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    env=env,
                    bufsize=1,
                )
                watchdog = threading.Timer(timeout_s, _timeout)
                watchdog.daemon = True
                watchdog.start()
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    with self._lock:
                        self.lines.append(line.rstrip("\n"))
                self.returncode = self._proc.wait()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                self.finished_at = time.monotonic()

        threading.Thread(target=_main, name="setup-command", daemon=True).start()
        return self

    def cancel(self) -> None:  # noqa: D102
        if self._proc is not None and self.running:
            self._proc.kill()


def checks(*, with_device: bool = False) -> list[Check]:
    """The ``doctor`` checks (``with_device`` talks to the glasses: slow)."""
    return run_checks(with_device=with_device)
