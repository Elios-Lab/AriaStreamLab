"""One worker thread per processor: input queues, join policy, stats, error isolation."""

from __future__ import annotations

import math
import threading
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import ValidationError

from ariastreamlab.config.models import ProcessorConfig
from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.errors import ConfigError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.sample import AnySample, DerivedSample, Sample
from ariastreamlab.processing.base import Emit, Inputs, Output, Processor, ProcessorContext
from ariastreamlab.timing.clock import HostClock
from ariastreamlab.timing.sync import TimeSeriesBuffer, temporal_join

__all__ = ["ProcessorStats", "ProcessorWorker"]

log = get_logger("ariastreamlab.processing")

CALIBRATION_STREAM = "device_calibration"


@dataclass
class ProcessorStats:
    """Counters and timings of one processor (published on ``health.pipeline``)."""

    processor_id: str
    type: str
    state: str = "created"  # created | running | stopped | failed
    inputs_received: dict[str, int] = field(default_factory=dict)
    queue_dropped: dict[str, int] = field(default_factory=dict)
    queue_depth: int = 0
    pending: int = 0
    fired: int = 0
    outputs: int = 0
    join_misses: int = 0
    errors: int = 0
    last_error: str | None = None
    exec_ms_mean: float | None = None
    exec_ms_p50: float | None = None
    exec_ms_p95: float | None = None
    exec_ms_p99: float | None = None
    exec_ms_max: float | None = None
    latency_ms_mean: float | None = None
    latency_ms_p95: float | None = None

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return asdict(self)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    k = (len(sorted_values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _last_at_least(buf: TimeSeriesBuffer[AnySample], ts: int) -> bool:
    last = buf.last_ns
    return last is not None and last >= ts


def _host_ns(sample: AnySample) -> int:
    """Host time at which a sample became available (receive time / processing end)."""
    if isinstance(sample, Sample):
        return sample.host_receive_timestamp_ns
    return sample.processing_end_ns


class ProcessorWorker:
    """Runs one processor on its own thread.

    The first input stream is the *primary*: every primary sample triggers one ``process()``
    call. Secondary streams are buffered and matched to the primary by the configured join
    policy (``latest`` / ``previous`` / ``nearest`` within ``tolerance_ms``).
    """

    SECONDARY_QUEUE = 4096
    LOSSLESS_QUEUE = 256
    SECONDARY_BUFFER = 8192
    EXEC_WINDOW = 512
    LOG_EVERY = 100

    def __init__(
        self,
        cfg: ProcessorConfig,
        processor: Processor,
        ctx: ProcessorContext,
        bus: EventBus,
        *,
        clock: HostClock | None = None,
        lossless: bool = False,
    ) -> None:
        self.cfg = cfg
        self.lossless = lossless
        self.processor = processor
        self.ctx = ctx
        self.bus = bus
        self.clock = clock or HostClock()
        self.id = cfg.id
        self.primary = cfg.inputs[0]
        self.secondaries = tuple(cfg.inputs[1:])
        self.outputs = frozenset(cfg.outputs)
        primary_rate = (
            ctx.registry.get(self.primary).nominal_rate_hz if self.primary in ctx.registry else None
        )
        self.queue_size = cfg.execution.effective_queue_size(primary_rate)
        """Primary queue length: ``execution.queue_size`` or half a second of the input."""
        self._tolerance_ns = int(cfg.execution.tolerance_ms * 1e6)
        self._join = cfg.execution.join
        self._primary_sub: Subscription[Any] | None = None
        self._secondary_subs: dict[str, Subscription[Any]] = {}
        self._calib_sub: Subscription[Any] | None = None
        self._buffers: dict[str, TimeSeriesBuffer[AnySample]] = {
            s: TimeSeriesBuffer(self.SECONDARY_BUFFER) for s in self.secondaries
        }
        self._pending: deque[tuple[AnySample, int]] = deque()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._received: Counter[str] = Counter()
        self._fired = 0
        self._outputs = 0
        self._join_misses = 0
        self._errors = 0
        self._pending_dropped = 0
        self._last_error: str | None = None
        self._exec_ns: deque[int] = deque(maxlen=self.EXEC_WINDOW)
        self._latency_ns: deque[int] = deque(maxlen=self.EXEC_WINDOW)
        self._unknown_outputs: set[str] = set()
        self._state = "created"
        self._setup_error: BaseException | None = None
        self._started = threading.Event()

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        """Subscribe to the inputs and start the thread (``setup()`` runs on the thread)."""
        if self._thread is not None:
            return
        ex = self.cfg.execution
        # lossless (max-speed replay, non-realtime mock): the publisher waits instead of
        # dropping, so re-processing a recording touches every sample
        self._primary_sub = self.bus.subscribe(
            f"*.{self.primary}",
            maxsize=max(self.queue_size, self.LOSSLESS_QUEUE) if self.lossless else self.queue_size,
            policy=DropPolicy.BLOCK if self.lossless else ex.drop_policy,
            name=f"proc:{self.id}:{self.primary}",
        )
        for s in self.secondaries:
            self._secondary_subs[s] = self.bus.subscribe(
                f"*.{s}",
                maxsize=self.SECONDARY_QUEUE,
                policy=DropPolicy.DROP_OLDEST,
                name=f"proc:{self.id}:{s}",
            )
        self._calib_sub = self.bus.subscribe(
            f"raw.{CALIBRATION_STREAM}",
            maxsize=1,
            policy=DropPolicy.LATEST,
            name=f"proc:{self.id}:calibration",
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"proc-{self.id}", daemon=True)
        self._thread.start()
        self._started.wait(10)
        if self._setup_error is not None:
            raise self._setup_error

    def stop(self, timeout: float = 10.0) -> None:
        """Stop after draining what is queued (bounded by ``timeout``)."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
            self._thread = None
        for sub in self._all_subs():
            sub.close()

    @property
    def alive(self) -> bool:  # noqa: D102
        return self._thread is not None and self._thread.is_alive()

    def _all_subs(self) -> list[Subscription[Any]]:
        subs = [s for s in (self._primary_sub, self._calib_sub) if s is not None]
        subs.extend(self._secondary_subs.values())
        return subs

    # -- the loop -----------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self.processor.setup(self.ctx)
            if self.ctx.calibration is not None:
                self.processor.on_calibration(self.ctx.calibration)
        except BaseException as exc:
            self._setup_error = exc
            self._state = "failed"
            self._started.set()
            return
        self._state = "running"
        self._started.set()
        assert self._primary_sub is not None
        try:
            while True:
                item = self._primary_sub.get(timeout=0.05)
                self._poll_calibration()
                self._drain_secondaries()
                if item is not None:
                    self._accept(item)
                    for extra in self._primary_sub.drain():
                        self._accept(extra)
                self._service_pending(final=False)
                if self._stop.is_set() and item is None and len(self._primary_sub) == 0:
                    break
            # drain: whatever is still pending fires with what we have
            self._drain_secondaries()
            self._service_pending(final=True)
        finally:
            try:
                self.processor.shutdown()
            except Exception:
                log.exception("processor.shutdown_failed", processor=self.id)
            if self._state != "failed":
                self._state = "stopped"

    def _poll_calibration(self) -> None:
        if self._calib_sub is None:
            return
        item = self._calib_sub.get_nowait()
        if item is None:
            return
        self._received[CALIBRATION_STREAM] += 1
        calib = item.data if isinstance(item.data, dict) else None
        if calib is not None:
            self.ctx.calibration = calib
            try:
                self.processor.on_calibration(calib)
            except Exception as exc:
                self._record_error(exc, "on_calibration")

    def _drain_secondaries(self) -> None:
        for sid, sub in self._secondary_subs.items():
            items = sub.drain()
            if items:
                self._received[sid] += len(items)
                self._buffers[sid].extend(items)

    def _accept(self, item: AnySample) -> None:
        self._received[self.primary] += 1
        if not self.secondaries or self._join == "latest":
            self._fire(item)
            return
        # time-based joins ("previous", "nearest") wait until every secondary stream has
        # reached the primary's timestamp (or a bounded host time passes), so the result
        # depends on the data, not on which thread delivered first
        self._pending.append((item, self.clock.now_ns()))
        limit = max(self.queue_size, self.LOSSLESS_QUEUE if self.lossless else 1)
        while len(self._pending) > limit:
            self._pending.popleft()
            self._pending_dropped += 1

    def _service_pending(self, *, final: bool) -> None:
        if not self._pending:
            return
        now = self.clock.now_ns()
        wait_ns = self._tolerance_ns + 20_000_000
        while self._pending:
            item, arrived = self._pending[0]
            ts = item.device_timestamp_ns
            complete = all(_last_at_least(b, ts) for b in self._buffers.values())
            if complete or final or now - arrived >= wait_ns:
                self._pending.popleft()
                self._fire(item)
            else:
                break

    # -- firing -------------------------------------------------------------------------
    def _fire(self, primary: AnySample) -> None:
        items: dict[str, AnySample] = {self.primary: primary}
        dt: dict[str, int] = {}
        if self.secondaries:
            joined = temporal_join(
                primary, self._buffers, tolerance_ns=self._tolerance_ns, mode=self._join
            )
            if joined is None:
                self._join_misses += 1
                return
            for sid, m in joined.matches.items():
                items[sid] = m.item
                dt[sid] = m.dt_ns
        inputs = Inputs(self.primary, items, dt)
        start = self.clock.now_ns()
        try:
            result = self.processor.process(inputs)
        except Exception as exc:
            self._record_error(exc, "process")
            return
        end = self.clock.now_ns()
        with self._lock:
            self._fired += 1
            self._exec_ns.append(end - start)
            self._latency_ns.append(end - _host_ns(primary))
        if result is None:
            return
        outs: Sequence[Output]
        if isinstance(result, (Emit, DerivedSample)) or (
            isinstance(result, tuple) and result and isinstance(result[0], str)
        ):
            outs = [result]
        else:
            outs = list(result)  # type: ignore[arg-type]
        source_streams = (self.primary, *self.secondaries)
        source_ts = tuple(items[s].device_timestamp_ns for s in source_streams if s in items)
        for out in outs:
            ds = self._to_derived(out, primary, source_streams, source_ts, start, end)
            if ds is None:
                continue
            self.bus.publish(f"derived.{ds.stream_id}", ds)
            with self._lock:
                self._outputs += 1

    def _to_derived(
        self,
        out: Output,
        primary: AnySample,
        source_streams: tuple[str, ...],
        source_ts: tuple[int, ...],
        start: int,
        end: int,
    ) -> DerivedSample | None:
        if isinstance(out, DerivedSample):
            if out.stream_id not in self.outputs:
                self._unknown_output(out.stream_id)
                return None
            return out
        if isinstance(out, Emit):
            sid, data, meta, ts = out.stream_id, out.data, out.metadata, out.timestamp_ns
        elif isinstance(out, tuple) and len(out) in (2, 3) and isinstance(out[0], str):
            sid, data = out[0], out[1]
            meta = out[2] if len(out) == 3 else None
            ts = None
        else:
            self._record_error(
                TypeError(f"process() returned {type(out).__name__}, expected Emit/tuple"),
                "output",
            )
            return None
        if sid not in self.outputs:
            self._unknown_output(sid)
            return None
        return DerivedSample(
            device_id=primary.device_id,
            stream_id=sid,
            timestamp_ns=primary.device_timestamp_ns if ts is None else ts,
            source_streams=source_streams,
            source_timestamps_ns=source_ts,
            processor_id=self.id,
            processing_start_ns=start,
            processing_end_ns=end,
            data=data,
            provenance=primary.provenance,
            metadata=dict(meta) if meta else {},
        )

    def _unknown_output(self, sid: str) -> None:
        self._record_error(
            ValueError(f"output stream {sid!r} is not declared in outputs {sorted(self.outputs)}"),
            "output",
            once_key=sid,
        )

    def _record_error(self, exc: BaseException, where: str, once_key: str | None = None) -> None:
        with self._lock:
            self._errors += 1
            self._last_error = f"{where}: {type(exc).__name__}: {exc}"
            n = self._errors
        if once_key is not None:
            if once_key in self._unknown_outputs:
                return
            self._unknown_outputs.add(once_key)
        if n == 1 or n % self.LOG_EVERY == 0:
            log.error(
                "processor.error",
                processor=self.id,
                where=where,
                error=str(exc),
                error_type=type(exc).__name__,
                count=n,
                exc_info=exc if n == 1 else None,
            )

    # -- stats --------------------------------------------------------------------------
    def update_parameters(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a live parameter change; returns the full new parameters as a dict.

        Validated against the processor's ``Parameters`` model (a ``ConfigError`` names the
        problem); the processor's ``on_parameters_changed`` hook runs afterwards.
        """
        old = self.ctx.parameters
        model = type(self.processor).Parameters
        if model is not None:
            current = old.model_dump() if hasattr(old, "model_dump") else dict(old)
            try:
                new: Any = model.model_validate({**current, **changes})
            except ValidationError as exc:
                lines = [
                    f"{'.'.join(str(x) for x in e['loc']) or '<root>'}: {e['msg']}"
                    for e in exc.errors()
                ]
                raise ConfigError(
                    f"processors[{self.cfg.id}].parameters: " + "; ".join(lines),
                    hint=f"accepted parameters: {', '.join(model.model_fields)}",
                ) from exc
            as_dict = dict(new.model_dump(mode="json"))
        else:
            new = {**dict(old), **changes}
            as_dict = dict(new)
        self.ctx.parameters = new
        self.processor.on_parameters_changed(old, new)
        log.info("processor.parameters_changed", processor=self.cfg.id, changes=dict(changes))
        return as_dict

    def stats(self) -> ProcessorStats:
        """A snapshot of the counters."""
        with self._lock:
            exec_ms = sorted(x / 1e6 for x in self._exec_ns)
            lat_ms = sorted(x / 1e6 for x in self._latency_ns)
            st = ProcessorStats(
                processor_id=self.id,
                type=self.cfg.type,
                state=self._state,
                inputs_received=dict(self._received),
                fired=self._fired,
                outputs=self._outputs,
                join_misses=self._join_misses,
                errors=self._errors,
                last_error=self._last_error,
            )
        dropped = {s.name.rsplit(":", 1)[-1]: s.dropped for s in self._all_subs()}
        if self._pending_dropped:
            dropped[self.primary] = dropped.get(self.primary, 0) + self._pending_dropped
        st.queue_dropped = {k: v for k, v in dropped.items() if v}
        st.queue_depth = 0 if self._primary_sub is None else len(self._primary_sub)
        st.pending = len(self._pending)
        if exec_ms:
            st.exec_ms_mean = sum(exec_ms) / len(exec_ms)
            st.exec_ms_p50 = _percentile(exec_ms, 0.5)
            st.exec_ms_p95 = _percentile(exec_ms, 0.95)
            st.exec_ms_p99 = _percentile(exec_ms, 0.99)
            st.exec_ms_max = exec_ms[-1]
        if lat_ms:
            st.latency_ms_mean = sum(lat_ms) / len(lat_ms)
            st.latency_ms_p95 = _percentile(lat_ms, 0.95)
        return st
