"""Collect ``health.*`` snapshots during a session and keep an eye on the disk.

:class:`HealthLog` is a bus subscriber (like the dashboard) that remembers the latest
``health.device`` / ``health.link`` / ``health.quality`` / ``health.pipeline`` payloads,
appends the device and quality snapshots to JSON-lines files under the session's ``quality/``
directory (so a crash still leaves the history on disk), and publishes ``health.storage``
every few seconds: the session directory's size, the write rate and the free space of the
disk it lives on. Everything it keeps is plain data; the end-of-session summary is built from
it by :mod:`ariastreamlab.monitoring.quality`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.logging import get_logger

__all__ = ["HealthLog", "directory_size", "jsonable"]

log = get_logger("ariastreamlab.monitoring")

_TOPICS = ("health.device", "health.link", "health.quality", "health.pipeline")


def jsonable(value: Any) -> Any:
    """Plain-data view of a health payload (dataclasses, enums, paths, numpy scalars…)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "as_dict"):
        return jsonable(value.as_dict())
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if hasattr(value, "value") and hasattr(type(value), "__members__"):  # Enum
        return jsonable(value.value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            k: jsonable(getattr(value, k))
            for k in value.__dataclass_fields__
            if not k.startswith("_")
        }
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, Path):
        return str(value)
    return str(value)


def directory_size(path: Path) -> int:
    """Total bytes of the files under ``path`` (0 when it does not exist)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


class HealthLog:
    """Keep the latest health snapshots, log them, and watch the session directory.

    Args:
        bus: The experiment's bus.
        session_dir: Session root to measure and to log into (``quality/``); ``None`` for
            sessions without storage (then only the latest snapshots are kept).
        storage_interval_s: Period of the ``health.storage`` message.
        history: How many device-health snapshots to keep in memory for the summary.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        session_dir: Path | None = None,
        storage_interval_s: float = 5.0,
        history: int = 20_000,
    ) -> None:
        self._bus = bus
        self.session_dir = session_dir
        self.storage_interval_s = storage_interval_s
        self._history_max = history
        self._lock = threading.Lock()
        self.latest: dict[str, Any] = {}
        """Latest plain-data payload per kind (device, link, quality, pipeline, storage)."""
        self.device_history: list[dict[str, Any]] = []
        """Rows ``{"t_s", "battery_percent", "skin_temperature_c", "thermal_mitigation", …}``."""
        self.quality_count = 0
        self.started_at = time.monotonic()
        self._subs: list[tuple[str, Subscription[Any]]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._files: dict[str, Any] = {}
        self._last_size: tuple[float, int] | None = None
        self.storage_peak_rate_bps = 0.0

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:  # noqa: D102
        if self._thread is not None:
            return
        for topic in _TOPICS:
            self._subs.append(
                (
                    topic.split(".", 1)[1],
                    self._bus.subscribe(
                        topic, maxsize=256, policy=DropPolicy.DROP_OLDEST, name=f"health:{topic}"
                    ),
                )
            )
        if self.session_dir is not None:
            qdir = self.session_dir / "quality"
            try:
                qdir.mkdir(parents=True, exist_ok=True)
                self._files["device"] = (qdir / "device_health.jsonl").open("a", encoding="utf-8")
                self._files["quality"] = (qdir / "device_quality_stats.jsonl").open(
                    "a", encoding="utf-8"
                )
            except OSError as exc:
                log.warning("health.log_unavailable", error=str(exc))
        self._thread = threading.Thread(target=self._run, name="health-log", daemon=True)
        self._thread.start()

    def stop(self) -> None:  # noqa: D102
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None
        for _kind, sub in self._subs:
            sub.close()
        self._subs.clear()
        self._measure_storage(publish=False)
        for f in self._files.values():
            with contextlib.suppress(Exception):
                f.close()
        self._files.clear()

    # -- loop --------------------------------------------------------------------------
    def _run(self) -> None:
        next_storage = time.monotonic() + min(1.0, self.storage_interval_s)
        while not self._stop.is_set():
            got = False
            for kind, sub in self._subs:
                for item in sub.drain():
                    got = True
                    self._observe(kind, item)
            if time.monotonic() >= next_storage:
                self._measure_storage(publish=True)
                next_storage = time.monotonic() + self.storage_interval_s
            if not got:
                self._stop.wait(0.2)

    def _observe(self, kind: str, item: Any) -> None:
        payload = jsonable(item)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        t_s = round(time.monotonic() - self.started_at, 2)
        with self._lock:
            self.latest[kind] = payload
            if kind == "device":
                row = {
                    "t_s": t_s,
                    "battery_percent": payload.get("battery_percent"),
                    "charging": payload.get("charging"),
                    "skin_temperature_c": payload.get("skin_temperature_c"),
                    "thermal_mitigation": payload.get("thermal_mitigation"),
                    "wifi_connected": payload.get("wifi_connected"),
                }
                self.device_history.append(row)
                if len(self.device_history) > self._history_max:
                    del self.device_history[: len(self.device_history) - self._history_max]
            elif kind == "quality":
                self.quality_count += 1
        f = self._files.get(kind)
        if f is not None:
            with contextlib.suppress(Exception):
                f.write(json.dumps({"t_s": t_s, **payload}, default=str) + "\n")
                f.flush()

    def _measure_storage(self, *, publish: bool) -> None:
        if self.session_dir is None:
            return
        now = time.monotonic()
        size = directory_size(self.session_dir)
        rate = 0.0
        if self._last_size is not None:
            t0, s0 = self._last_size
            if now > t0:
                rate = max(0.0, (size - s0) / (now - t0))
        self._last_size = (now, size)
        self.storage_peak_rate_bps = max(self.storage_peak_rate_bps, rate)
        try:
            usage = shutil.disk_usage(self.session_dir)
            free = usage.free
        except OSError:
            free = None
        payload = {
            "path": str(self.session_dir),
            "bytes": size,
            "write_rate_bps": round(rate, 1),
            "free_bytes": free,
        }
        with self._lock:
            self.latest["storage"] = payload
        if publish:
            self._bus.publish("health.storage", payload)

    # -- views -------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Copy of the latest payloads (plain data)."""
        with self._lock:
            return {k: dict(v) for k, v in self.latest.items()}
