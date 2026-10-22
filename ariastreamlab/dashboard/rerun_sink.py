"""Optional Rerun sink (``dashboard.rerun: true``): the session's streams in a Rerun viewer.

Rerun (https://rerun.io) is the viewer Meta's own tools use; it has no input widgets, so it is
not the dashboard, but its 3-D view of the VIO trajectory and its time scrubbing are worth
having next to it. The sink is a bus subscriber like the dashboard: it never back-pressures
acquisition (``DROP_OLDEST`` queues) and the session does not depend on it. ``pip install
'ariastreamlab[rerun]'`` provides the SDK.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

import numpy as np

from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.payloads import PosePayload
from ariastreamlab.core.sample import AnySample
from ariastreamlab.dashboard.state import channels_of

__all__ = ["RerunSink", "rerun_available"]

log = get_logger("ariastreamlab.rerun")


def rerun_available() -> bool:
    """True when the ``rerun`` package is installed."""
    import importlib.util

    return importlib.util.find_spec("rerun") is not None


class RerunSink:
    """Log raw and derived samples to Rerun on the ``device`` timeline (seconds).

    Args:
        bus: The experiment's bus.
        app_id: Rerun application id (the session id is a good choice).
        spawn: Start the Rerun viewer (``False`` in tests, where a memory recording is used).
        streams: Only these stream ids (``None`` ⇒ everything).
        image_fps: Image streams are rate-limited to this many frames per second.
        trail: Number of pose positions kept in the 3-D trail.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        app_id: str = "ariastreamlab",
        spawn: bool = True,
        streams: list[str] | None = None,
        image_fps: float = 10.0,
        trail: int = 2000,
    ) -> None:
        self._bus = bus
        self.app_id = app_id
        self.spawn = spawn
        self._wanted = None if streams is None else set(streams)
        self._min_image_interval = 1.0 / image_fps
        self._last_image_at: dict[str, float] = {}
        self._trails: dict[str, deque[tuple[float, float, float]]] = {}
        self._trail_len = trail
        self._subs: list[Subscription[Any]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.logged = 0
        self.recording: Any = None  # a MemoryRecording when spawn is False

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:  # noqa: D102
        if self._thread is not None:
            return
        import rerun as rr

        rr.init(self.app_id, spawn=self.spawn)
        if not self.spawn:
            self.recording = rr.memory_recording()
        self._subs = [
            self._bus.subscribe(
                "raw.*", maxsize=2048, policy=DropPolicy.DROP_OLDEST, name="rerun:raw"
            ),
            self._bus.subscribe(
                "derived.*", maxsize=2048, policy=DropPolicy.DROP_OLDEST, name="rerun:derived"
            ),
        ]
        self._thread = threading.Thread(target=self._run, name="rerun-sink", daemon=True)
        self._thread.start()
        log.info("rerun.started", app_id=self.app_id, spawn=self.spawn)

    def stop(self) -> None:  # noqa: D102
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None
        for s in self._subs:
            s.close()
        self._subs.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            got = False
            for sub in self._subs:
                for item in sub.drain():
                    got = True
                    try:
                        self._observe(item)
                    except Exception as exc:  # a logging failure must never stop acquisition
                        log.debug("rerun.log_failed", error=str(exc))
            if not got:
                self._stop.wait(0.05)

    # -- logging -----------------------------------------------------------------------
    def _observe(self, s: AnySample) -> None:
        import rerun as rr

        sid = s.stream_id
        if self._wanted is not None and sid not in self._wanted:
            return
        data = s.data
        rr.set_time("device", duration=s.device_timestamp_ns / 1e9)
        if isinstance(data, np.ndarray) and data.ndim in (2, 3) and data.dtype == np.uint8:
            now = time.monotonic()
            if now - self._last_image_at.get(sid, 0.0) < self._min_image_interval:
                return
            self._last_image_at[sid] = now
            rr.log(f"cameras/{sid}", rr.Image(data))
            self.logged += 1
            return
        if isinstance(data, PosePayload):
            t = data.translation_m
            w, x, y, z = data.quaternion_wxyz
            rr.log(
                f"poses/{sid}",
                rr.Transform3D(translation=t, rotation=rr.Quaternion(xyzw=[x, y, z, w])),
            )
            trail = self._trails.setdefault(sid, deque(maxlen=self._trail_len))
            trail.append((float(t[0]), float(t[1]), float(t[2])))
            rr.log(f"poses/{sid}/trail", rr.LineStrips3D([list(trail)]))
            self.logged += 1
            return
        if isinstance(data, Mapping) and {"x_norm", "y_norm"} <= set(data):
            if data.get("valid", True):
                rr.log(f"gaze/{sid}", rr.Points2D([[data["x"], data["y"]]], radii=8))
                self.logged += 1
            return
        scalars = {k: v for k, (v, _unit) in channels_of(data).items() if math.isfinite(v)}
        if scalars:
            rr.log(f"signals/{sid}", rr.Scalars(list(scalars.values())))
            self.logged += 1
