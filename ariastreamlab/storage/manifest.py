"""``manifest.json``: everything needed to reproduce a session (docs/ARCHITECTURE.md §8).

The manifest is written as soon as the session opens (``status: "running"``) and rewritten
atomically when it finalises (``"completed"`` / ``"crashed"``). A manifest still saying
``running`` with no process alive is how ``inspect`` recognises a crash.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ariastreamlab.storage.writers import atomic_write_text

Status = Literal["running", "completed", "crashed", "unknown"]


class SoftwareInfo(BaseModel):
    """Versions of everything involved."""

    model_config = ConfigDict(extra="allow")

    ariastreamlab: str = "unknown"
    git_commit: str | None = None
    python: str = Field(default_factory=lambda: sys.version.split()[0])
    platform: str = Field(default_factory=platform.platform)
    projectaria_client_sdk: str | None = None
    projectaria_tools: str | None = None
    numpy: str | None = None
    opencv: str | None = None

    @classmethod
    def collect(cls, repo_dir: Path | None = None) -> SoftwareInfo:
        """Read installed versions (best effort; never raises)."""

        def ver(pkg: str) -> str | None:
            try:
                return metadata.version(pkg)
            except metadata.PackageNotFoundError:
                return None

        return cls(
            ariastreamlab=ver("ariastreamlab") or "unknown",
            git_commit=_git_commit(repo_dir),
            projectaria_client_sdk=ver("projectaria_client_sdk"),
            projectaria_tools=ver("projectaria_tools"),
            numpy=ver("numpy"),
            opencv=ver("opencv-python-headless") or ver("opencv-python"),
        )


class DeviceInfoRecord(BaseModel):
    """What we know about the device that produced the raw data."""

    model_config = ConfigDict(extra="allow")

    serial: str | None = None
    firmware: str | None = None
    streaming_profile: str | None = None
    streaming_interface: str | None = None
    batch_period_ms: int | None = None
    transport_log: list[dict[str, Any]] = Field(default_factory=list)


class RawInfo(BaseModel):
    """The raw ground truth of the session."""

    kind: Literal["host_received_vrs", "on_device_vrs", "mock_files", "replay_of", "none"] = "none"
    files: list[str] = Field(default_factory=list)
    """Paths relative to the session root (``raw/<file>.vrs``)."""
    sha256: dict[str, str] = Field(default_factory=dict)
    source: str | None = None
    """For replay sessions: the path of the source recording / session."""


class StreamRecord(BaseModel):
    """Per-stream bookkeeping filled at finalize."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["raw", "derived"]
    received: int = 0
    nominal_rate_hz: float | None = None
    effective_rate_hz: float | None = None
    transport_dropped: int = 0
    out_of_order: int = 0
    stored_samples: int | None = None
    stored_in: str | None = None
    first_device_ns: int | None = None
    last_device_ns: int | None = None


class Manifest(BaseModel):
    """The manifest document."""

    model_config = ConfigDict(extra="allow")

    manifest_version: int = 1
    session_id: str
    status: Status = "running"
    origin: Literal["live", "replay", "mock"]
    experiment: str
    participant: str
    started_utc: str
    ended_utc: str | None = None
    duration_s: float | None = None
    clock_anchor: dict[str, int]
    config_sha256: str
    software: SoftwareInfo
    device: DeviceInfoRecord = Field(default_factory=DeviceInfoRecord)
    raw: RawInfo = Field(default_factory=RawInfo)
    enabled_streams: list[str] = Field(default_factory=list)
    streams: dict[str, StreamRecord] = Field(default_factory=dict)
    processors: list[dict[str, Any]] = Field(default_factory=list)
    events: int = 0
    warnings: list[str] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)

    def write(self, path: Path) -> None:
        """Atomic JSON write."""
        atomic_write_text(path, self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Read from disk."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def finalize(self, status: Status, ended_utc_ns: int) -> None:
        """Set the end time and status."""
        self.status = status
        ended = datetime.fromtimestamp(ended_utc_ns / 1e9, tz=timezone.utc)
        self.ended_utc = ended.isoformat(timespec="microseconds")
        started = datetime.fromisoformat(self.started_utc)
        self.duration_s = round((ended - started).total_seconds(), 3)


def _git_commit(repo_dir: Path | None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir or Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def manifest_status(session_root: Path) -> Status:
    """Status field of a session's manifest, ``"unknown"`` if unreadable."""
    try:
        data = json.loads((session_root / "manifest.json").read_text(encoding="utf-8"))
        return str(data.get("status", "unknown"))  # type: ignore[return-value]
    except (OSError, ValueError):
        return "unknown"
