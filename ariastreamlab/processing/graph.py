"""The processing graph: resolves, validates, wires and runs the configured processors."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ariastreamlab.config.models import ExperimentConfig, ProcessorConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ConfigError, ProcessingError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.registry import REGISTRY, StreamRegistry
from ariastreamlab.processing import plugins
from ariastreamlab.processing.base import Processor, ProcessorContext
from ariastreamlab.processing.runner import ProcessorStats, ProcessorWorker
from ariastreamlab.timing.clock import HostClock

__all__ = ["ProcessingGraph", "ResolvedProcessor", "resolve_processor", "validate_processors"]

log = get_logger("ariastreamlab.processing")


@dataclass(frozen=True)
class ResolvedProcessor:
    """A configuration entry turned into a class plus validated parameters."""

    cfg: ProcessorConfig
    cls: type[Processor]
    parameters: Any  # a Parameters model instance, or the raw dict

    @property
    def parameters_dict(self) -> dict[str, Any]:  # noqa: D102
        p = self.parameters
        if hasattr(p, "model_dump"):
            return dict(p.model_dump(mode="json"))
        return dict(p)

    def describe(self) -> dict[str, Any]:
        """JSON-able description for the manifest."""
        return {
            "id": self.cfg.id,
            "type": self.cfg.type,
            "class": f"{self.cls.__module__}:{self.cls.__qualname__}",
            "version": self.cls.version,
            "inputs": list(self.cfg.inputs),
            "outputs": list(self.cfg.outputs),
            "parameters": self.parameters_dict,
            "execution": self.cfg.execution.model_dump(mode="json"),
            "save_output": self.cfg.save_output,
        }


def resolve_processor(cfg: ProcessorConfig, processors_path: Path | None) -> ResolvedProcessor:
    """Resolve ``cfg.type`` and validate ``cfg.parameters`` against the class' ``Parameters``."""
    try:
        cls = plugins.resolve(cfg.type, processors_path)
    except ProcessingError as exc:
        raise ConfigError(f"processors[{cfg.id}].type: {exc}", hint=exc.hint) from exc
    params: Any = dict(cfg.parameters)
    model = cls.Parameters
    if model is not None:
        try:
            params = model.model_validate(cfg.parameters)
        except ValidationError as exc:
            lines = [
                f"{'.'.join(str(x) for x in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            ]
            raise ConfigError(
                f"processors[{cfg.id}].parameters ({cfg.type}): " + "; ".join(lines),
                hint=f"accepted parameters: {', '.join(model.model_fields)}",
            ) from exc
    return ResolvedProcessor(cfg, cls, params)


def validate_processors(
    config: ExperimentConfig, processors_path: Path | None = None
) -> list[ResolvedProcessor]:
    """Resolve every enabled processor (types and parameters) without running anything."""
    path = processors_path or config.processors_path
    return [resolve_processor(p, path) for p in config.processors if p.enabled]


class ProcessingGraph:
    """All processors of one experiment, each on its own worker thread.

    The configuration is already a validated DAG (unique outputs, known inputs, no cycles);
    this class resolves the classes, validates parameters, creates one
    :class:`ProcessorWorker` per processor and publishes a ``health.pipeline`` snapshot
    every ``health_interval_s``.

    ``lossless=True`` replaces the configured drop policies with back-pressure (the publisher
    waits). :class:`~ariastreamlab.api.Experiment` uses it whenever the source is not paced
    (max-speed replay, non-realtime mock), because then nobody is waiting for the data in
    real time and the point is to process every sample.
    """

    def __init__(
        self,
        config: ExperimentConfig,
        bus: EventBus,
        *,
        clock: HostClock | None = None,
        session_dir: Path | None = None,
        calibration: dict[str, Any] | None = None,
        registry: StreamRegistry = REGISTRY,
        processors_path: Path | None = None,
        health_interval_s: float = 1.0,
        lossless: bool = False,
    ) -> None:
        self.config = config
        self.lossless = lossless
        self.bus = bus
        self.clock = clock or HostClock()
        self.session_dir = session_dir
        self.calibration = calibration
        self.registry = registry
        self.health_interval_s = health_interval_s
        self.resolved = validate_processors(config, processors_path)
        self.workers: dict[str, ProcessorWorker] = {}
        self._order = _topological(self.resolved, set(config.enabled_streams()))
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        self._started = False
        for r in self.resolved:
            try:
                proc = r.cls()
            except Exception as exc:
                raise ProcessingError(
                    f"processors[{r.cfg.id}]: cannot instantiate {r.cls.__name__}: {exc}"
                ) from exc
            ctx = ProcessorContext(
                processor_id=r.cfg.id,
                parameters=r.parameters,
                inputs=tuple(r.cfg.inputs),
                outputs=tuple(r.cfg.outputs),
                registry=registry,
                calibration=calibration,
                session_dir=session_dir,
                clock=self.clock,
                log=get_logger(f"ariastreamlab.processor.{r.cfg.id}"),
            )
            self.workers[r.cfg.id] = ProcessorWorker(
                r.cfg, proc, ctx, bus, clock=self.clock, lossless=lossless
            )

    # -- lifecycle --------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.workers)

    def start(self) -> None:
        """Start every worker (downstream first, so no early output is missed)."""
        if self._started:
            return
        for pid in reversed(self._order):
            self.workers[pid].start()
        self._started = True
        if self.health_interval_s > 0:
            self._health_stop.clear()
            self._health_thread = threading.Thread(
                target=self._health_loop, name="pipeline-health", daemon=True
            )
            self._health_thread.start()
        log.info("pipeline.start", processors=self._order)

    def stop(self, timeout_per_processor: float = 10.0) -> None:
        """Stop in topological order so each stage drains before its consumers stop."""
        if not self._started:
            return
        self._health_stop.set()
        for pid in self._order:
            self.workers[pid].stop(timeout_per_processor)
        if self._health_thread is not None:
            self._health_thread.join(2)
            self._health_thread = None
        self._started = False
        self.bus.publish("health.pipeline", self.stats_dict())
        log.info("pipeline.stop", **{pid: w.stats().fired for pid, w in self.workers.items()})

    def _health_loop(self) -> None:
        while not self._health_stop.wait(self.health_interval_s):
            self.bus.publish("health.pipeline", self.stats_dict())

    # -- introspection ----------------------------------------------------------------
    @property
    def order(self) -> list[str]:
        """Processor ids, upstream first."""
        return list(self._order)

    def update_parameters(self, processor_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Change a running processor's parameters; returns the full new parameters."""
        worker = self.workers.get(processor_id)
        if worker is None:
            raise ConfigError(
                f"no processor {processor_id!r}",
                hint=f"processors: {', '.join(self.workers) or 'none'}",
            )
        return worker.update_parameters(changes)

    def parameters(self) -> dict[str, dict[str, Any]]:
        """Current parameters of every processor (as plain dicts)."""
        out = {}
        for pid, w in self.workers.items():
            p = w.ctx.parameters
            out[pid] = dict(p.model_dump(mode="json")) if hasattr(p, "model_dump") else dict(p)
        return out

    def stats(self) -> dict[str, ProcessorStats]:
        """Per-processor snapshot."""
        return {pid: w.stats() for pid, w in self.workers.items()}

    def stats_dict(self) -> dict[str, dict[str, Any]]:
        """JSON-able :meth:`stats`."""
        return {pid: s.as_dict() for pid, s in self.stats().items()}

    def describe(self) -> list[dict[str, Any]]:
        """Manifest entries (id, type, class, version, parameters, execution)."""
        out = []
        for r in self.resolved:
            d = r.describe()
            w = self.workers.get(r.cfg.id)
            if w is not None:
                d["execution"]["effective_queue_size"] = w.queue_size
            out.append(d)
        return out

    def stream_info(self) -> dict[str, dict[str, Any]]:
        """Provenance for every output stream (goes into the stream's ``_meta.json``)."""
        info: dict[str, dict[str, Any]] = {}
        for r in self.resolved:
            for sid in r.cfg.outputs:
                spec = r.cls.output_spec(sid)
                info[sid] = {
                    "processor": r.cfg.id,
                    "type": r.cfg.type,
                    "class": f"{r.cls.__module__}:{r.cls.__qualname__}",
                    "version": r.cls.version,
                    "parameters": r.parameters_dict,
                    "inputs": list(r.cfg.inputs),
                    "payload": spec.payload,
                    "units": dict(spec.units),
                    "description": spec.description,
                }
        return info

    def saved_outputs(self) -> list[str]:
        """Output streams whose processor has ``save_output: true``."""
        return [o for r in self.resolved if r.cfg.save_output for o in r.cfg.outputs]


def _topological(resolved: list[ResolvedProcessor], raw: set[str]) -> list[str]:
    producers: dict[str, str] = {o: r.cfg.id for r in resolved for o in r.cfg.outputs}
    deps: dict[str, set[str]] = {
        r.cfg.id: {producers[i] for i in r.cfg.inputs if i not in raw and i in producers}
        for r in resolved
    }
    order: list[str] = []
    done: set[str] = set()
    remaining: Mapping[str, set[str]] = deps
    while len(order) < len(deps):
        ready = sorted(pid for pid, d in remaining.items() if pid not in done and d <= done)
        if not ready:  # the config validator already rejects cycles
            raise ProcessingError("processing graph has a cycle")
        order.extend(ready)
        done.update(ready)
    return order
