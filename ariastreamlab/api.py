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

import contextlib
import json
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import Any

from ariastreamlab.acquisition.base import DeviceTransition, ReplayControl, Source
from ariastreamlab.acquisition.mock import MockSource
from ariastreamlab.acquisition.replay import make_replay_source
from ariastreamlab.config.loader import load_config, load_preset
from ariastreamlab.config.models import DashboardConfig, ExperimentConfig
from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.errors import AriaStreamLabError, ConfigError, ReplayError
from ariastreamlab.core.logging import configure_logging, get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import AnySample, Origin
from ariastreamlab.events.markers import DeviceTimeEstimator, Event, EventLog, Interval
from ariastreamlab.monitoring.health import HealthLog, directory_size
from ariastreamlab.monitoring.quality import QualitySummary, build_summary
from ariastreamlab.monitoring.stats import StatsTracker, StreamStats
from ariastreamlab.processing.graph import ProcessingGraph
from ariastreamlab.processing.runner import ProcessorStats
from ariastreamlab.storage.manifest import RawInfo, Status
from ariastreamlab.storage.session import Session, SessionInfo
from ariastreamlab.timing.clock import HostClock

SampleCallback = Callable[[AnySample], None]


def _dashboard_wanted(config: ExperimentConfig, explicit: bool | None) -> bool:
    if os.environ.get("ARIASTREAMLAB_DASHBOARD", "1") == "0":
        return False
    return config.dashboard.enabled if explicit is None else explicit


class Experiment:
    """Owns one session: bus, source, processing graph, statistics, events and storage.

    Args:
        config: Validated configuration.
        mock: Use the synthetic source instead of the glasses.
        source: Inject any :class:`Source` (tests, custom replay).
        duration_s: Default run duration (``None`` ⇒ until stopped).
        persist: Write a session directory under ``config.storage.root``.
        source_json: The user's original JSON text, kept as ``experiment.source.json``.
        dashboard: Show the live dashboard (``None`` ⇒ ``config.dashboard.enabled``; the
            environment variable ``ARIASTREAMLAB_DASHBOARD=0`` turns it off everywhere, e.g.
            in CI or batch replays).
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
        dashboard: bool | None = None,
    ) -> None:
        self.config = config
        self.mock = mock
        self.dashboard_enabled = _dashboard_wanted(config, dashboard)
        self._dash_state: Any = None
        self._dash: Any = None
        self._injected_source = source
        self.prompt: Callable[[str], None] | None = None
        """Interactive step for live sessions (join the hotspot…); ``None`` ⇒ terminal ENTER."""
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
        self.health: HealthLog | None = None
        self.rerun: Any = None
        self._stopped_at_ns: int | None = None
        self.quality_summary: QualitySummary | None = None
        self._opened_at: float | None = None
        self.source: Source | None = None
        self.graph: ProcessingGraph | None = None
        self._tap: Subscription[Any] | None = None
        self._tap_derived: Subscription[Any] | None = None
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
        dashboard: bool | None = None,
    ) -> Experiment:
        """Load ``path`` and build the experiment."""
        p = Path(path)
        return cls(
            load_config(p),
            mock=mock,
            duration_s=duration_s,
            persist=persist,
            source_json=p.read_text(encoding="utf-8"),
            dashboard=dashboard,
        )

    @classmethod
    def from_preset(
        cls,
        name: str,
        *,
        mock: bool = True,
        duration_s: float | None = None,
        persist: bool = True,
        dashboard: bool | None = None,
    ) -> Experiment:
        """Build from a bundled preset (``"rgb_imu"``…)."""
        return cls(
            load_preset(name),
            mock=mock,
            duration_s=duration_s,
            persist=persist,
            dashboard=dashboard,
        )

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
        dashboard: bool | None = None,
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
        exp = cls(cfg, duration_s=duration_s, persist=persist, dashboard=dashboard)
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
        self.stats.paced = self.source.capabilities.is_paced
        self._start_tap()
        raw_to_file: list[str] = []
        raw = RawInfo()
        if origin == "mock":
            raw = RawInfo(kind="mock_files" if self.config.storage.save_mock_raw else "none")
            if self.config.storage.save_mock_raw:
                raw_to_file = list(self.source.capabilities.streams)
        elif origin == "replay":
            raw = RawInfo(kind="replay_of", source=str(self._replay_of))
        elif origin == "live":
            raw = RawInfo(kind="host_received_vrs" if self.config.storage.save_raw_vrs else "none")
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
        if origin == "live" and self.store.layout is not None and self.config.storage.save_raw_vrs:
            record_raw_to = getattr(self.source, "record_raw_to", None)
            if record_raw_to is not None:
                record_raw_to(self.store.layout.raw)
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
        self._opened_at = time.monotonic()
        self.health = HealthLog(self.bus, session_dir=self.session_dir)
        self.health.start()
        if self.dashboard_enabled:
            self._start_dashboard(origin)
        if self.config.dashboard.rerun:
            self._start_rerun()
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
        from ariastreamlab.acquisition.aria_gen2 import make_live_source

        return make_live_source(
            self.config,
            session_id=self.session.session_id,
            clock=self.clock,
            on_transition=self._on_device_transition,
            prompt=self.prompt,
            on_warning=self._on_source_warning,
        )

    def _on_source_warning(self, name: str, payload: Mapping[str, Any]) -> None:
        """A problem detected by the source (stall, thermal…) becomes a ``warning`` event."""
        self.events.mark(name, payload, kind="warning")

    def _on_device_transition(self, t: DeviceTransition) -> None:
        """Device state changes become ``device_state`` events (stored in events.parquet)."""
        device_ns, how = self.time_estimator.device_from_host(t.host_timestamp_ns)
        self.events.add(
            Event(
                "device_state",
                t.current.value,
                device_ns,
                t.host_timestamp_ns,
                {"previous": t.previous.value, "reason": t.reason, "transport": t.transport},
                how,
            )
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
            self._set_dash_state("running")
            self.source.start(self.bus)
            self.events.mark("acquisition_start", kind="session")
            # An unpaced source (max-speed replay, non-realtime mock) interprets ``duration``
            # as *data* time and finishes on its own; a host deadline would cut it short on a
            # slow machine.
            paced = self.source.capabilities.is_paced
            deadline = None if duration is None or not paced else time.monotonic() + duration
            next_anchor = time.monotonic() + self.CLOCK_ANCHOR_PERIOD_S
            while not self._stop.is_set():
                if self.source.finished.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if paced and time.monotonic() >= next_anchor:
                    self._anchor_clock()
                    next_anchor = time.monotonic() + self.CLOCK_ANCHOR_PERIOD_S
                self._stop.wait(0.1)
        except BaseException as exc:
            self._failed = exc
            raise
        finally:
            self.events.mark("acquisition_stop", kind="session")
            self._stopped_at_ns = self.clock.now_ns()
            self.source.stop()
            if self.graph is not None:
                self.graph.stop()  # drains what is queued, in topological order
            self._settle_stats()
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)

    def _settle_stats(self, timeout: float = 3.0) -> None:
        """Wait until the statistics tap has consumed everything published so far."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tap, derived = self._tap, self._tap_derived
            if (tap is None or len(tap) == 0) and (derived is None or len(derived) == 0):
                return
            time.sleep(0.01)

    CLOCK_ANCHOR_PERIOD_S = 60.0
    """How often a ``clock_anchor`` event (host monotonic ↔ UTC) is added to a paced session."""

    def _anchor_clock(self) -> None:
        """Record host-monotonic ↔ UTC (and the device offset estimate) so drift can be analysed."""
        self.events.mark(
            "anchor",
            {
                "monotonic_ns": self.clock.now_ns(),
                "utc_ns": time.time_ns(),
                "device_offset_ns": self.time_estimator.offset_ns,
            },
            kind="clock_anchor",
        )

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
        if self.health is not None:
            self.health.stop()
        self.quality_summary = self._build_quality_summary()
        if self.store is not None:
            status: Status = "crashed" if self._failed is not None else "completed"
            extra = self._manifest_extra()
            extra["quality"] = self.quality_summary.as_dict()
            root = self.store.finalize(
                status=status,
                events=self.events,
                stream_stats=self.stream_stats(),
                ended_utc_ns=time.time_ns(),
                extra=extra,
            )
            if root is not None and self.store.layout is not None and self.store.manifest:
                # the recorder flushed during finalize: measure the directory as it really is
                self.quality_summary.disk_bytes = directory_size(root)
                self.quality_summary.duration_s = self.store.manifest.duration_s
                self.store.manifest.quality = self.quality_summary.as_dict()
                self.store.manifest.write(self.store.layout.manifest)
                self._write_quality_files(self.store.layout.quality, self.quality_summary)
        if self.rerun is not None:
            self.rerun.stop()
        self._set_dash_state("crashed" if self._failed is not None else "finished")
        if self._dash_state is not None:
            self._dash_state.stop()
            if self._dash is not None:
                self._dash.detach(self._dash_state)
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

    def mark(
        self, name: str, payload: Mapping[str, Any] | None = None, *, at_ns: int | None = None
    ) -> Event:
        """Add a timestamped annotation (same time base as the sensor streams).

        Without ``at_ns`` the event is stamped now and its device time is estimated from the
        samples seen so far; pass a sample's ``device_timestamp_ns`` as ``at_ns`` to annotate
        exactly that instant (e.g. from a processor callback or a replay).
        """
        return self.events.mark(name, payload, at_ns=at_ns)

    def begin(
        self, name: str, payload: Mapping[str, Any] | None = None, *, at_ns: int | None = None
    ) -> str:
        """Open an interval annotation (``trial``, ``condition``…); returns its id."""
        return self.events.begin(name, payload, at_ns=at_ns)

    def end(
        self,
        name_or_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        at_ns: int | None = None,
    ) -> str:
        """Close an interval opened with :meth:`begin` (by id, or the latest with that name)."""
        return self.events.end(name_or_id, payload, at_ns=at_ns)

    def intervals(self, name: str | None = None) -> list[Interval]:
        """The ``begin``/``end`` pairs recorded so far."""
        return self.events.intervals(name)

    def set_parameters(self, processor_id: str, **changes: Any) -> dict[str, Any]:
        """Change a processor's parameters while it runs; recorded as a ``config_change`` event.

        The frozen ``experiment.json`` and the manifest keep the start-up values; the event
        (``events/events.parquet``, name ``processors.<id>.parameters``) carries the change
        and the full parameters afterwards, with the device time it took effect.
        """
        if self.graph is None:
            raise ConfigError("this experiment has no processors")
        new = self.graph.update_parameters(processor_id, changes)
        self.events.mark(
            f"processors.{processor_id}.parameters",
            {"processor": processor_id, "changes": dict(changes), "parameters": new},
            kind="config_change",
        )
        return new

    def _parameter_forms(self) -> dict[str, tuple[Any, dict[str, Any]]]:
        if self.graph is None:
            return {}
        return {
            pid: (type(w.processor).Parameters, dict(params))
            for (pid, w), params in zip(
                self.graph.workers.items(), self.graph.parameters().values(), strict=True
            )
        }

    def processor_parameters(self) -> dict[str, dict[str, Any]]:
        """Current parameters of every running processor."""
        return {} if self.graph is None else self.graph.parameters()

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
            "quality": None if self.quality_summary is None else self.quality_summary.as_dict(),
            "processors": None if self.graph is None else self.graph.stats_dict(),
            "recorder": None
            if self.store is None or self.store.recorder is None
            else {k: v.__dict__ for k, v in self.store.recorder.stats().items()},
        }

    @property
    def dashboard_url(self) -> str | None:
        """Where the live dashboard is served (``None`` when disabled)."""
        return None if self._dash is None else str(self._dash.url)

    # -- internals ---------------------------------------------------------------------
    def _start_dashboard(self, origin: str) -> None:
        from ariastreamlab.dashboard.app import dashboard_available, get_dashboard
        from ariastreamlab.dashboard.state import DashboardState

        if not dashboard_available():
            self._log.warning(
                "dashboard.unavailable",
                note="nicegui is not importable in this environment; reinstall AriaStreamLab "
                "(pip install -e .) — without the dashboard a stall is only visible in the "
                "terminal",
            )
            return
        assert self.session is not None
        assert self.source is not None
        cfg = self.config.dashboard
        state = DashboardState(
            self.bus,
            streams=cfg.show,
            image_fps=cfg.image_fps,
            stats_provider=self.stream_stats,
            bus_stats_provider=lambda: {
                s.name: {"enqueued": s.enqueued, "delivered": s.delivered, "dropped": s.dropped}
                for s in self.bus.stats().subscriptions
                if not s.name.startswith("health:")
            },
            processor_stats_provider=lambda: {
                k: v.as_dict() for k, v in self.processor_stats().items()
            },
        )
        state.session = {
            "session_id": self.session.session_id,
            "experiment": self.config.experiment.name,
            "participant": self.config.experiment.participant,
            "origin": origin,
            "directory": None if self.session_dir is None else str(self.session_dir),
            "state": "open",
        }
        state.start()
        dash = get_dashboard(cfg)
        dash.launcher = launch_experiment
        dash.replay_launcher = launch_replay
        if not dash.start(native=cfg.native):
            self._log.warning(
                "dashboard.unavailable",
                note="the session runs without a dashboard (is the port free?)",
                url=dash.url,
            )
            state.stop()
            self.dashboard_enabled = False
            return
        rng = None
        t0 = None
        time_range = getattr(self.source, "time_range", None)
        if self.replay_control is not None and time_range is not None:
            with contextlib.suppress(Exception):
                r = time_range()
                if r is not None:
                    t0 = int(r[0])
                    rng = (0.0, (int(r[1]) - int(r[0])) / 1e9)
        dash.attach(
            state,
            on_mark=self.mark,
            on_begin=self.begin,
            on_end=self.end,
            on_set_parameters=lambda pid, changes: self.set_parameters(pid, **changes),
            parameter_forms=self._parameter_forms,
            replay=self.replay_control,
            replay_range_s=rng,
            replay_t0_ns=t0,
        )
        self._dash_state, self._dash = state, dash
        self._log.info("dashboard.url", url=dash.url)
        if (
            cfg.open_browser
            and not cfg.native
            and os.environ.get("ARIASTREAMLAB_OPEN_BROWSER", "1") != "0"
        ):
            import webbrowser

            threading.Thread(
                target=lambda: webbrowser.open(dash.url + "monitor"),
                name="open-browser",
                daemon=True,
            ).start()

    def _start_rerun(self) -> None:
        from ariastreamlab.dashboard.rerun_sink import RerunSink, rerun_available

        if not rerun_available():
            self._log.warning(
                "rerun.unavailable",
                hint="pip install 'ariastreamlab[rerun]' (dashboard.rerun is set)",
            )
            return
        assert self.session is not None
        spawn = os.environ.get("ARIASTREAMLAB_RERUN_SPAWN", "1") != "0"
        self.rerun = RerunSink(
            self.bus,
            app_id=f"ariastreamlab/{self.session.session_id[:8]}",
            spawn=spawn,
            streams=self.config.dashboard.show,
            image_fps=self.config.dashboard.image_fps,
        )
        try:
            self.rerun.start()
        except Exception as exc:
            self._log.warning("rerun.failed", error=str(exc))
            self.rerun = None

    def _set_dash_state(self, value: str) -> None:
        if self._dash_state is not None:
            self._dash_state.session["state"] = value

    def _build_quality_summary(self) -> QualitySummary:
        """The end-of-session quality summary (also stored under ``quality/``)."""
        health = self.health
        storage = health.snapshot().get("storage", {}) if health is not None else {}
        recorder = None
        if self.store is not None and self.store.recorder is not None:
            recorder = {k: v.__dict__ for k, v in self.store.recorder.stats().items()}
        warnings: list[str] = []
        notes: list[str] = []
        expected: dict[str, float] = {}
        streaming_s = None
        if self.source is not None:
            warnings.extend(getattr(self.source, "warnings", []) or [])
            notes.extend(getattr(self.source, "notes", []) or [])
            plan = getattr(self.source, "plan", None)
            if plan is not None:
                expected = dict(getattr(plan, "expected_rates_hz", {}) or {})
            first = getattr(self.source, "first_sample_host_ns", None)
            if first is not None and self._stopped_at_ns is not None:
                streaming_s = round((self._stopped_at_ns - first) / 1e9, 1)
        quality = health.snapshot().get("quality") if health is not None else None
        return build_summary(
            stream_stats=self.stream_stats(),
            raw_stream_ids=set(REGISTRY.ids()),
            bus_subscriptions={
                s.name: {"enqueued": s.enqueued, "delivered": s.delivered, "dropped": s.dropped}
                for s in self.bus.stats().subscriptions
                if not s.name.startswith("health:")
            },
            processor_stats=None if self.graph is None else self.graph.stats_dict(),
            device_quality=quality if isinstance(quality, dict) else None,
            device_history=None if health is None else health.device_history,
            recorder_stats=recorder,
            duration_s=None
            if self._opened_at is None
            else round(time.monotonic() - self._opened_at, 1),
            disk_bytes=storage.get("bytes"),
            disk_free_bytes=storage.get("free_bytes"),
            peak_write_rate_bps=None if health is None else round(health.storage_peak_rate_bps),
            extra_warnings=warnings,
            notes=notes,
            expected_rates_hz=expected,
            end_host_ns=self._stopped_at_ns,
            streaming_s=streaming_s,
        )

    @staticmethod
    def _write_quality_files(directory: Path, summary: QualitySummary) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "summary.json").write_text(
                json.dumps(summary.as_dict(), indent=2, default=str) + "\n", encoding="utf-8"
            )
            (directory / "summary.txt").write_text(summary.text() + "\n", encoding="utf-8")
        except OSError as exc:
            get_logger("ariastreamlab.experiment").warning(
                "quality.summary_unwritable", error=str(exc)
            )

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
        if self.session is not None and self.session.origin == "live" and src is not None:
            device_record = getattr(src, "device_record", None)
            if device_record is not None:
                extra["device"] = device_record()
            extra["raw"] = self._live_raw_info()
            warnings = list(getattr(src, "warnings", []) or [])
            if warnings:
                extra["warnings"] = warnings
            notes = list(getattr(src, "notes", []) or [])
            if notes:
                extra["notes"] = notes
        if self.graph is not None and len(self.graph):
            extra["pipeline"] = self.graph.stats_dict()
        return extra

    def _live_raw_info(self) -> RawInfo:
        """``manifest.raw`` for a live session: the VRS files the SDK wrote, with checksums."""
        from ariastreamlab.storage.layout import sha256_file

        files = list(getattr(self.source, "vrs_files", []) or [])
        root = self.session_dir
        if not files or root is None or not self.config.storage.save_raw_vrs:
            return RawInfo(
                kind="none" if not self.config.storage.save_raw_vrs else "host_received_vrs"
            )
        rel = [
            str(Path(f).relative_to(root)) if Path(f).is_relative_to(root) else str(f)
            for f in files
        ]
        return RawInfo(
            kind="host_received_vrs",
            files=rel,
            sha256={r: sha256_file(Path(f)) for r, f in zip(rel, files, strict=True)},
        )

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

        self._tap_derived = derived

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
            # closed: account for whatever was still queued so the final counts are complete
            for extra in self._tap.drain():
                self.stats.observe(extra)
                self.time_estimator.observe(extra)
            for d in derived.drain():
                self.stats.observe(d)
            derived.close()

        self._tap_thread = threading.Thread(target=_loop, name="stats-tap", daemon=True)
        self._tap_thread.start()


__all__ = [
    "AriaStreamLabError",
    "Experiment",
    "launch_experiment",
    "launch_replay",
    "serve_app",
    "serve_designer",
]


def launch_experiment(
    config: ExperimentConfig,
    *,
    mock: bool,
    duration_s: float | None,
    prompt: Callable[[str], None] | None = None,
) -> Experiment:
    """Build an :class:`Experiment` for the dashboard's *Run* button (its ``launcher``)."""
    exp = Experiment(config, mock=mock, duration_s=duration_s, dashboard=True)
    exp.prompt = prompt
    return exp


def launch_replay(path: Path, *, speed: float = 1.0) -> Experiment:
    """Build a replay :class:`Experiment` for the app's Sessions page (its ``replay_launcher``)."""
    return Experiment.replay(path, speed=speed, dashboard=True)


def serve_app(
    config_path: Path | None = None,
    *,
    port: int | None = None,
    native: bool = True,
    open_browser: bool = True,
    page: str = "",
) -> Any:
    """Run the AriaStreamLab application (``ariastreamlab`` with no arguments).

    With ``native=True`` the interface opens in a desktop window and this call **blocks**
    until the window is closed (it must run on the main thread). With ``native=False`` the
    server starts in the background, the browser is opened on ``page`` and the dashboard is
    returned so the caller can wait (``ariastreamlab design``).
    """
    from ariastreamlab.dashboard.app import get_dashboard
    from ariastreamlab.dashboard.designer import Designer

    base = DashboardConfig() if port is None else DashboardConfig(port=port)
    dash = get_dashboard(base)
    dash.launcher = launch_experiment
    dash.replay_launcher = launch_replay
    if config_path is not None:
        dash.designer = Designer.from_file(config_path)
    else:
        dash.designer = Designer.from_preset("base", base_dir=Path.cwd())
    if native:
        try:
            dash.serve(native=True)  # blocks until the window is closed
        finally:
            dash.stop_run()
        return dash
    if not dash.start():
        raise AriaStreamLabError(
            f"cannot start the dashboard on {dash.url}", hint="is the port in use? try --port"
        )
    url = dash.url + page
    if open_browser and os.environ.get("ARIASTREAMLAB_OPEN_BROWSER", "1") != "0":
        import webbrowser

        threading.Thread(
            target=lambda: webbrowser.open(url), name="open-browser", daemon=True
        ).start()
    return dash


def serve_designer(
    config_path: Path | None = None, *, port: int | None = None, open_browser: bool = True
) -> Any:
    """``ariastreamlab design``: the designer page in the browser (server in the background)."""
    return serve_app(config_path, port=port, native=False, open_browser=open_browser, page="design")
