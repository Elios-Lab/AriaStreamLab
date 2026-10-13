"""The minimal Python API: :class:`Experiment`.

::

    from ariastreamlab import Experiment

    exp = Experiment.from_json("experiment.json", mock=True)
    with exp:
        exp.subscribe("rgb", lambda s: print(s.data.shape))
        exp.mark("trial_start", {"trial": 1})
        exp.run(duration_s=10)

    replay = Experiment.replay("sessions/20261013T…_mock_experiment_P000", speed=2.0)
    with replay:
        replay.run()
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import Any

from ariastreamlab.acquisition.base import ReplayControl, Source
from ariastreamlab.acquisition.mock import MockSource
from ariastreamlab.acquisition.replay import make_replay_source
from ariastreamlab.config.loader import load_config, load_preset
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.errors import AriaStreamLabError, ConfigError, ReplayError
from ariastreamlab.core.logging import configure_logging, get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import AnySample, Origin
from ariastreamlab.events.markers import DeviceTimeEstimator, Event, EventLog
from ariastreamlab.monitoring.stats import StatsTracker, StreamStats
from ariastreamlab.processing.graph import ProcessingGraph
from ariastreamlab.processing.runner import ProcessorStats
from ariastreamlab.storage.manifest import RawInfo, Status
from ariastreamlab.storage.session import Session, SessionInfo
from ariastreamlab.timing.clock import HostClock

SampleCallback = Callable[[AnySample], None]


class Experiment:
    """Owns one session: bus, source, processing graph, statistics, events and storage.

    Args:
        config: Validated configuration.
        mock: Use the synthetic source instead of the glasses.
        source: Inject any :class:`Source` (tests, custom replay).
        duration_s: Default run duration (``None`` ⇒ until stopped).
        persist: Write a session directory under ``config.storage.root``.
        source_json: The user's original JSON text, kept as ``experiment.source.json``.
    """

    def __init__(
        self,
        config: ExperimentConfig,
        *,
        mock: bool = False,
        source: Source | None = None,
        duration_s: float | None = None,
        persist: bool = True,
        source_json: str | None = None,
    ) -> None:
        self.config = config
        self.mock = mock
        self._injected_source = source
        self.duration_s = duration_s
        self.persist = persist
        self._source_json = source_json
        self._replay_of: Path | None = None
        self._replay_opts: dict[str, Any] = {}
        self.clock = HostClock()
        self.bus = EventBus()
        self.stats = StatsTracker(
            {sid: REGISTRY.get(sid).nominal_rate_hz for sid in config.enabled_streams()}
        )
        self.time_estimator = DeviceTimeEstimator()
        self.events = EventLog(self.bus, self.time_estimator, self.clock)
        self.session: SessionInfo | None = None
        self.store: Session | None = None
        self.source: Source | None = None
        self.graph: ProcessingGraph | None = None
        self._tap: Subscription[Any] | None = None
        self._tap_thread: threading.Thread | None = None
        self._subscriptions: list[tuple[Subscription[Any], threading.Thread]] = []
        self._stop = threading.Event()
        self._opened = False
        self._failed: BaseException | None = None
        self._log = get_logger("ariastreamlab.experiment")
        configure_logging(config.logging.level, json_lines=config.logging.format == "json")

    # -- constructors ----------------------------------------------------------------
    @classmethod
    def from_json(
        cls,
        path: str | Path,
        *,
        mock: bool = False,
        duration_s: float | None = None,
        persist: bool = True,
    ) -> Experiment:
        """Load ``path`` and build the experiment."""
        p = Path(path)
        return cls(
            load_config(p),
            mock=mock,
            duration_s=duration_s,
            persist=persist,
            source_json=p.read_text(encoding="utf-8"),
        )

    @classmethod
    def from_preset(
        cls, name: str, *, mock: bool = True, duration_s: float | None = None, persist: bool = True
    ) -> Experiment:
        """Build from a bundled preset (``"rgb_imu"``…)."""
        return cls(load_preset(name), mock=mock, duration_s=duration_s, persist=persist)

    @classmethod
    def replay(
        cls,
        path: str | Path,
        *,
        config: ExperimentConfig | str | Path | None = None,
        speed: float | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
        backend: str | None = None,
        streams: list[str] | None = None,
        duration_s: float | None = None,
        persist: bool = True,
    ) -> Experiment:
        """Replay a recorded session directory or a ``.vrs`` file.

        Without ``config`` a session directory replays with its own frozen ``experiment.json``
        (same processors, same settings); a bare VRS file uses the ``full_capture`` preset.
        """
        p = Path(path)
        explicit_config = True
        if config is None:
            frozen = p / "experiment.json"
            if frozen.exists():
                cfg = load_config(frozen)
            else:
                cfg = load_preset("full_capture")
                explicit_config = False  # a bare file: replay everything it contains
        elif isinstance(config, ExperimentConfig):
            cfg = config
        else:
            cfg = load_config(config)
        if streams is None and explicit_config:
            # the configuration says which streams the experiment uses; everything else
            # in the recording stays untouched (same rule as a live run)
            streams = list(cfg.enabled_streams())
        rp = cfg.replay
        speed = rp.speed if speed is None else speed
        start_s = rp.start_s if start_s is None else start_s
        end_s = rp.end_s if end_s is None else end_s
        backend = rp.backend if backend is None else backend
        if speed == 0 and duration_s is not None and end_s is None:
            # max speed: "duration" can only mean recorded time, so make it the window end
            end_s = (start_s or 0.0) + duration_s
            duration_s = None
        exp = cls(cfg, duration_s=duration_s, persist=persist)
        exp._replay_of = p
        exp._replay_opts = {
            "speed": speed,
            "start_s": start_s,
            "end_s": end_s,
            "backend": backend,
            "streams": streams,
        }
        return exp

    # -- lifecycle --------------------------------------------------------------------
    def open(self) -> Experiment:
        """Create the session and the source without starting acquisition."""
        if self._opened:
            return self
        origin: Origin = "mock" if self.mock else "live"
        if self._replay_of is not None:
            origin = "replay"
        elif self._injected_source is not None:
            origin = self._injected_source.capabilities.origin
        self.session = SessionInfo.create(
            self.config.experiment.name, self.config.experiment.participant, origin
        )
        self.source = self._build_source()
        self._start_tap()
        raw_to_file: list[str] = []
        raw = RawInfo()
        if origin == "mock":
            raw = RawInfo(kind="mock_files" if self.config.storage.save_mock_raw else "none")
            if self.config.storage.save_mock_raw:
                raw_to_file = list(self.source.capabilities.streams)
        elif origin == "replay":
            raw = RawInfo(kind="replay_of", source=str(self._replay_of))
        self.store = Session(
            self.session,
            self.config,
            root=self.config.storage.root if self.persist else None,
            source_json=self._source_json,
        )
        # the graph is built before the store opens so the manifest and the derived streams'
        # _meta.json carry the resolved processor classes, versions and parameters
        self.graph = ProcessingGraph(
            self.config,
            self.bus,
            clock=self.clock,
            session_dir=None,
            calibration=self._initial_calibration(),
            lossless=not self.source.capabilities.is_paced,
        )
        self.store.open(
            self.bus,
            raw_streams_to_file=raw_to_file,
            raw=raw,
            processors=self.graph.describe(),
            derived_info=self.graph.stream_info(),
        )
        if self.store.layout is not None:
            self.graph.session_dir = self.store.layout.root
            for w in self.graph.workers.values():
                w.ctx.session_dir = self.store.layout.root
        if origin == "replay" and self.store.layout is not None:
            self.store.layout.raw_source.write_text(f"{self._replay_of}\n", encoding="utf-8")
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
            streams=list(self.source.capabilities.streams),
            directory=None if self.store.layout is None else str(self.store.layout.root),
        )
        return self

    def _build_source(self) -> Source:
        assert self.session is not None
        if self._injected_source is not None:
            return self._injected_source
        if self._replay_of is not None:
            opts = self._replay_opts
            try:
                return make_replay_source(
                    self._replay_of, session_id=self.session.session_id, clock=self.clock, **opts
                )
            except ReplayError:
                raise
            except Exception as exc:
                raise ReplayError(f"cannot replay {self._replay_of}: {exc}") from exc
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
            if self.graph is not None:
                self.graph.start()
            self.source.start(self.bus)
            self.events.mark("acquisition_start", kind="session")
            # An unpaced source (max-speed replay, non-realtime mock) interprets ``duration``
            # as *data* time and finishes on its own; a host deadline would cut it short on a
            # slow machine.
            paced = self.source.capabilities.is_paced
            deadline = None if duration is None or not paced else time.monotonic() + duration
            while not self._stop.is_set():
                if self.source.finished.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._stop.wait(0.1)
        except BaseException as exc:
            self._failed = exc
            raise
        finally:
            self.events.mark("acquisition_stop", kind="session")
            self.source.stop()
            if self.graph is not None:
                self.graph.stop()  # drains what is queued, in topological order
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)

    def stop(self) -> None:
        """Ask :meth:`run` to return (thread-safe)."""
        self._stop.set()

    def close(self) -> Path | None:
        """Stop everything, finalise the session directory, release the bus. Idempotent."""
        if not self._opened:
            return None
        self._stop.set()
        if self.source is not None:
            self.source.stop()
        if self.graph is not None:
            self.graph.stop()
        for sub, thread in self._subscriptions:
            sub.close()
            thread.join(timeout=2)
        self._subscriptions.clear()
        if self._tap is not None:
            self._tap.close()
        if self._tap_thread is not None:
            self._tap_thread.join(timeout=5)
        root: Path | None = None
        if self.store is not None:
            status: Status = "crashed" if self._failed is not None else "completed"
            root = self.store.finalize(
                status=status,
                events=self.events,
                stream_stats=self.stream_stats(),
                ended_utc_ns=time.time_ns(),
                extra=self._manifest_extra(),
            )
        self.bus.close()
        self._log.info("session.close", **{k: v.received for k, v in self.stream_stats().items()})
        self._opened = False
        return root

    def __enter__(self) -> Experiment:
        return self.open()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        if exc is not None and self._failed is None:
            self._failed = exc
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

    @property
    def replay_control(self) -> ReplayControl | None:
        """Pause/seek/speed controls when the source is a replay, else ``None``."""
        src = self.source
        if src is not None and src.capabilities.supports_replay_control:
            return src  # type: ignore[return-value]
        return None

    @property
    def session_dir(self) -> Path | None:
        """The session directory being written (``None`` without persistence)."""
        return None if self.store is None or self.store.layout is None else self.store.layout.root

    def processor_stats(self) -> dict[str, ProcessorStats]:
        """Per-processor counters and timings (empty without processors)."""
        return {} if self.graph is None else self.graph.stats()

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
            "directory": None if self.session_dir is None else str(self.session_dir),
            "streams": self.stats.as_dict(),
            "source": None
            if src is None
            else {
                "received": dict(src.received),
                "dropped": dict(src.source_dropped),
                **dict(src.extra),
            },
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
            "processors": None if self.graph is None else self.graph.stats_dict(),
            "recorder": None
            if self.store is None or self.store.recorder is None
            else {k: v.__dict__ for k, v in self.store.recorder.stats().items()},
        }

    # -- internals ---------------------------------------------------------------------
    def _manifest_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        src = self.source
        if src is not None:
            st = src.stats()
            extra["source"] = {
                "received": dict(st.received),
                "dropped": dict(st.source_dropped),
                **{
                    k: v
                    for k, v in st.extra.items()
                    if isinstance(v, (int, float, str, bool, type(None)))
                },
            }
            calib = getattr(src, "calibration", None)
            if isinstance(calib, dict) and self.store is not None and self.store.layout is not None:
                self.store.layout.write_json(
                    self.store.layout.calibration / "device_calibration.json", calib
                )
        if self.session is not None and self.session.origin == "replay" and src is not None:
            extra["device"] = {"serial": getattr(src, "device_id", None)}
        if self.graph is not None and len(self.graph):
            extra["pipeline"] = self.graph.stats_dict()
        return extra

    def _initial_calibration(self) -> dict[str, Any] | None:
        """Calibration known before start (VRS replay reads it from the file)."""
        calib = getattr(self.source, "calibration", None)
        return calib if isinstance(calib, dict) else None

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
                    for extra in self._tap.drain():
                        self.stats.observe(extra)
                        self.time_estimator.observe(extra)
                for d in derived.drain():
                    self.stats.observe(d)
            derived.close()

        self._tap_thread = threading.Thread(target=_loop, name="stats-tap", daemon=True)
        self._tap_thread.start()


__all__ = ["AriaStreamLabError", "Experiment"]
