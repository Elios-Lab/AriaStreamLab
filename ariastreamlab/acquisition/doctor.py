"""Environment checks for ``ariastreamlab doctor`` (no hardware needed for most of them)."""

from __future__ import annotations

import importlib
import importlib.util
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Check", "run_checks"]


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ariastreamlab")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


@dataclass(frozen=True)
class Check:
    """One line of the doctor report."""

    name: str
    status: str  # ok | warn | fail | skip
    detail: str

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _module_version(name: str) -> str | None:
    try:
        mod = importlib.import_module(name)
    except Exception:
        return None
    return str(getattr(mod, "__version__", "present"))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def run_checks(
    *, storage_root: Path | None = None, server_port: int = 6768, with_device: bool = False
) -> list[Check]:
    """Collect the checks; ``with_device`` adds the (slow) ``aria_gen2 auth check``."""
    out: list[Check] = []
    py = sys.version_info
    out.append(
        Check(
            "python",
            "ok" if (3, 10) <= py[:2] <= (3, 12) else "warn",
            f"{platform.python_version()} on {platform.system()} {platform.machine()}"
            + ("" if (3, 10) <= py[:2] <= (3, 12) else " (the SDK wheels cover 3.10–3.12)"),
        )
    )
    out.append(Check("ariastreamlab", "ok", _version()))
    for name, label, required in (
        ("numpy", "numpy", True),
        ("pyarrow", "pyarrow (Parquet storage)", True),
        ("cv2", "opencv (images, MP4)", True),
        ("pydantic", "pydantic", True),
        ("nicegui", "nicegui (dashboard)", True),
    ):
        v = _module_version(name)
        out.append(
            Check(
                label,
                "ok" if v else ("fail" if required else "warn"),
                v or ("missing — pip install -e . again" if required else "missing"),
            )
        )
    sdk_v = _module_version("aria.sdk_gen2") and _module_version("aria")
    tools_v = _module_version("projectaria_tools")
    out.append(
        Check(
            "Project Aria client SDK",
            "ok" if sdk_v else "warn",
            sdk_v
            or "not installed — needed for live acquisition (pip install 'ariastreamlab[aria]')",
        )
    )
    out.append(
        Check(
            "projectaria_tools",
            "ok" if tools_v else "warn",
            tools_v or "not installed — needed to replay/inspect VRS files",
        )
    )
    cli = shutil.which("aria_gen2")
    out.append(
        Check("aria_gen2 CLI", "ok" if cli else "warn", cli or "not on PATH (comes with the SDK)")
    )
    out.append(
        Check(
            f"streaming port {server_port}",
            "ok" if _port_free(server_port) else "fail",
            "free" if _port_free(server_port) else "in use — another receiver is running?",
        )
    )
    if platform.system() == "Darwin":
        out.append(
            Check(
                "macOS firewall",
                "skip",
                "cannot be checked from here; allow incoming connections for python if the "
                "device never connects",
            )
        )
    root = storage_root or Path("./sessions")
    try:
        usage = shutil.disk_usage(root if root.exists() else Path.cwd())
        free_gb = usage.free / 1e9
        out.append(
            Check(
                "disk space",
                "ok" if free_gb > 10 else "warn",
                f"{free_gb:.1f} GB free at {root} (profile9 ≈ 1.4 MB/s ≈ 5 GB/hour)",
            )
        )
    except OSError as exc:
        out.append(Check("disk space", "warn", str(exc)))
    if with_device and cli:
        from ariastreamlab.acquisition.aria_gen2.cli import AriaCli

        try:
            res = AriaCli().run("device list", timeout_s=60)
            out.append(
                Check(
                    "aria_gen2 device list",
                    "ok" if res.ok else "warn",
                    (res.stdout.strip().splitlines() or ["(no output)"])[-1][:120],
                )
            )
            res = AriaCli().run("auth check", timeout_s=60)
            text = res.text.lower()
            authenticated = res.ok and "authenticated" in text and "not authenticated" not in text
            out.append(
                Check(
                    "pairing (auth check)",
                    "ok" if authenticated else "warn",
                    "authenticated"
                    if authenticated
                    else "not authenticated — `aria_gen2 auth pair`",
                )
            )
        except Exception as exc:
            out.append(Check("device", "warn", f"{type(exc).__name__}: {exc}"))
    elif with_device:
        out.append(Check("device", "skip", "aria_gen2 CLI not available"))
    return out
