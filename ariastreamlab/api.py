"""The minimal Python API: :class:`Experiment`.

::

    from ariastreamlab import Experiment

    exp = Experiment.from_json("experiment.json", mock=True)
    with exp:
        exp.subscribe("rgb", lambda s: print(s.data.shape))
        exp.mark("trial_start", {"trial": 1})
        exp.run(duration_s=10)
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import Any

from ariastreamlab.acquisition.base import Source
from ariastreamlab.acquisition.mock import MockSource
from ariastreamlab.config.loader import load_config, load_preset
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.errors import AriaStreamLabError, ConfigError
from ariastreamlab.core.logging import configure_logging, get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import AnySample, Origin
from ariastreamlab.events.markers import DeviceTimeEstimator, Event, EventLog
from ariastreamlab.monitoring.stats import StatsTracker, StreamStats
from ariastreamlab.storage.session import SessionInfo
from ariastreamlab.timing.clock import HostClock

SampleCallback = Callable[[AnySample], None]


class Experiment:
    """Owns one session: bus, source, statistics and events.

    Processors, storage and the dashboard plug in here in the next phases.

    Args:
        config: Validated configuration.
        mock: Use the synthetic source instead of the glasses.
        source: Inject any :class:`Source` (tests, replay).
        duration_s: Default run duration (``None`` ⇒ until stopped).
    """

    def __init__(
        self,
        config: ExperimentConfig,
        *,
        mock: bool = False,
        source: Source | None = None,
        duration_s: float | None = None,
    ) -> None:
        self.config = config
        self.mock = mock
        self._injected_source = source
        self.duration_s = duration_s
        self.clock = HostClock()
        self.bus = EventBus()
        self.stats = StatsTracker(
            {sid: REGISTRY.get(sid).nominal_rate_hz for sid in config.enabled_streams()}
        )
        self.time_estimator = DeviceTimeEstimator()
        self.events = EventLog(self.bus, self.time_estimator, self.clock)
        self.session: SessionInfo | None = None
        self.source: Source | None = None
        self._tap: Subscription[Any] | None = None
        self._tap_thread: threading.Thread | None = None
        self._subscriptions: list[tuple[Subscription[Any], threading.Thread]] = []
        self._stop = threading.Event()
        self._opened = False
        self._log = get_logger("ariastreamlab.experiment")
        configure_logging(config.logging.level, json_lines=config.logging.format == "json")

    # -- constructors ----------------------------------------------------------------
    @classmethod
    def from_json(
        cls, path: str | Path, *, mock: bool = False, duration_s: float | None = None
    ) -> Experiment:
        """Load ``path`` and build the experiment."""
        return cls(load_config(path), mock=mock, duration_s=duration_s)

    @classmethod
    def from_preset(
        cls, name: str, *, mock: bool = True, duration_s: float | None = None
    ) -> Experiment:
        """Build from a bundled preset (``"rgb_imu"``…)."""
        return cls(load_preset(name), mock=mock, duration_s=duration_s)

    # -- lifecycle --------------------------------------------------------------------
    def open(self) -> Experiment:
        """Create the session and the source without starting acquisition."""
        if self._opened:
            return self
        origin: Origin = "mock" if self.mock else "live"
        if self._injected_source is not None:
            origin = self._injected_source.capabilities.origin
        self.session = SessionInfo.create(
            self.config.experiment.name, self.config.experiment.participant, origin
        )
        self.source = self._build_source()
        self._start_tap()
        self.events.add(
            Event(
                "session",
                "open",
                self.session.anchor.monotonic_ns,
                self.session.anchor.monotonic_ns,
                {
                    "session_id": self.session.session_id,
                    "origin": origin,
                    "utc": self.session.anchor.utc_iso(),
                },
                "host_only",
            )
        )
        self._opened = True
        self._log.info(
            "session.open",
            session_id=self.session.session_id,
            origin=origin,
            streams=list(self.config.enabled_streams()),
        )
        return self

    def _build_source(self) -> Source:
        assert self.session is not None
        if self._injected_source is not None:
            return self._injected_source
        if self.mock:
            return MockSource(
                self.config.enabled_streams(),
                mock=self.config.mock,
                session_id=self.session.session_id,
                duration_s=self.duration_s,
                clock=self.clock,
            )
        raise ConfigError(
            "live acquisition from Aria Gen 2 glasses is implemented in Phase 4 of the plan",
            hint="run with mock=True / `--mock` for now (see docs/ARCHITECTURE.md §12)",
        )

    def run(self, duration_s: float | None = None, *, install_signal_handler: bool = True) -> None:
        """Start the source; block until ``duration_s`` elapses, the source finishes or Ctrl-C."""
        if not self._opened:
            self.open()
        assert self.source is not None
        duration = self.duration_s if duration_s is None else duration_s
        previous_handler: Any = None
        if install_signal_handler and threading.current_thread() is threading.main_thread():

            def _on_sigint(_signum: int, _frame: FrameType | None) -> None:
                if self._stop.is_set():  # second Ctrl-C: give up waiting politely
                    raise KeyboardInterrupt
                self._log.warning(
                    "interrupt.received", note="stopping (press Ctrl-C again to force)"
                )
                self._stop.set()

            previous_handler = signal.signal(signal.SIGINT, _on_sigint)
        try:
            self.source.start(self.bus)
            self.events.mark("acquisition_start", kind="session")
            deadline = None if duration is None else time.monotonic() + duration
            while not self._stop.is_set():
                if self.source.finished.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._stop.wait(0.1)
        finally:
            self.events.mark("acquisition_stop", kind="session")
            self.source.stop()
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)

    def stop(self) -> None:
        """Ask :meth:`run` to return (thread-safe)."""
        self._stop.set()

    def close(self) -> None:
        """Stop everything and release the bus. Idempotent."""
        if not self._opened:
            return
        self._stop.set()
        if self.source is not None:
            self.source.stop()
        for sub, thread in self._subscriptions:
            sub.close()
            thread.join(timeout=2)
        self._subscriptions.clear()
        if self._tap is not None:
            self._tap.close()
        if self._tap_thread is not None:
            self._tap_thread.join(timeout=2)
        self.bus.close()
        self._log.info("session.close", **{k: v.received for k, v in self.stream_stats().items()})
        self._opened = False

    def __enter__(self) -> Experiment:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- user-facing operations ----------------------------------------------------------
    def subscribe(
        self,
        stream_id: str,
        callback: SampleCallback,
        *,
        policy: DropPolicy | str = DropPolicy.LATEST,
        queue_size: int = 16,
    ) -> Subscription[Any]:
        """Call ``callback(sample)`` on a dedicated thread for every sample of ``stream_id``.

        ``stream_id`` may be a raw stream (``"rgb"``), a derived stream or a glob (``"imu_*"``).
        """
        pol = DropPolicy(policy)
        if pol is DropPolicy.BLOCK:
            raise ConfigError(
                "subscribe(): 'block' would stall acquisition; use latest/drop_oldest/drop_newest"
            )
        topic = stream_id if "." in stream_id else f"*.{stream_id}"
        sub = self.bus.subscribe(topic, maxsize=queue_size, policy=pol, name=f"user:{stream_id}")

        def _loop() -> None:
            while not sub.closed:
                item = sub.get(timeout=0.5)
                if item is None:
                    continue
                try:
                    callback(item)
                except Exception:
                    self._log.exception("subscriber.error", stream=stream_id)

        t = threading.Thread(target=_loop, name=f"subscriber-{stream_id}", daemon=True)
        t.start()
        self._subscriptions.append((sub, t))
        return sub

    def mark(self, name: str, payload: Mapping[str, Any] | None = None) -> Event:
        """Add a timestamped annotation (same time base as the sensor streams)."""
        return self.events.mark(name, payload)

    def stream_stats(self) -> dict[str, StreamStats]:
        """Per-stream rates and drop counters."""
        return self.stats.snapshot_all()

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary for the CLI and the manifest."""
        assert self.session is not None
        src = self.source.stats() if self.source is not None else None
        return {
            "session_id": self.session.session_id,
            "origin": self.session.origin,
            "streams": self.stats.as_dict(),
            "source": None
            if src is None
            else {"received": dict(src.received), "dropped": dict(src.source_dropped)},
            "bus": {
                s.name: {
                    "enqueued": s.enqueued,
                    "delivered": s.delivered,
                    "dropped": s.dropped,
                    "depth": s.depth,
                }
                for s in self.bus.stats().subscriptions
            },
            "events": len(self.events),
        }

    # -- internals ---------------------------------------------------------------------
    def _start_tap(self) -> None:
        self._tap = self.bus.subscribe(
            "raw.*", maxsize=65_536, policy=DropPolicy.DROP_OLDEST, name="stats-tap"
        )
        derived = self.bus.subscribe(
            "derived.*", maxsize=65_536, policy=DropPolicy.DROP_OLDEST, name="stats-tap-derived"
        )

        def _loop() -> None:
            assert self._tap is not None
            while not self._tap.closed:
                item = self._tap.get(timeout=0.25)
                if item is not None:
                    self.stats.observe(item)
                    self.time_estimator.observe(item)
                for d in derived.drain():
                    self.stats.observe(d)
            derived.close()

        self._tap_thread = threading.Thread(target=_loop, name="stats-tap", daemon=True)
        self._tap_thread.start()


__all__ = ["AriaStreamLabError", "Experiment"]
