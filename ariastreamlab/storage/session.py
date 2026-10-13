"""Session identity and lifecycle: directory, frozen config, manifest, recorders, finalisation."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ariastreamlab.config.loader import dump_resolved
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import Origin
from ariastreamlab.events.markers import EventLog
from ariastreamlab.monitoring.stats import StreamStats
from ariastreamlab.storage.derived import DerivedRecorder
from ariastreamlab.storage.events_store import write_events
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.manifest import (
    DeviceInfoRecord,
    Manifest,
    RawInfo,
    SoftwareInfo,
    Status,
    StreamRecord,
)
from ariastreamlab.storage.timeline import write_timeline
from ariastreamlab.timing.clock import ClockAnchor

_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")
log = get_logger("ariastreamlab.storage.session")


def session_folder_name(started_utc: datetime, experiment: str, participant: str) -> str:
    """``<timestamp>_<experiment>_<participant>`` with a filesystem-safe timestamp."""
    stamp = started_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{_SAFE.sub('-', experiment)}_{_SAFE.sub('-', participant)}"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Immutable identity of a session."""

    session_id: str
    experiment: str
    participant: str
    origin: Origin
    anchor: ClockAnchor
    started_utc: datetime
    folder_name: str = field(default="")

    @classmethod
    def create(
        cls, experiment: str, participant: str, origin: Origin, *, anchor: ClockAnchor | None = None
    ) -> SessionInfo:
        """Mint a new session id and timestamp."""
        a = anchor or ClockAnchor.now()
        started = datetime.fromtimestamp(a.utc_ns / 1e9, tz=timezone.utc)
        return cls(
            session_id=str(uuid.uuid4()),
            experiment=experiment,
            participant=participant,
            origin=origin,
            anchor=a,
            started_utc=started,
            folder_name=session_folder_name(started, experiment, participant),
        )


class Session:
    """A session directory being written.

    ``open()`` creates the tree, freezes the configuration and writes a ``running`` manifest;
    ``finalize()`` closes recorders, writes events, timeline and the final manifest. The object
    is also usable without a directory (``root=None``) for tests that only need identity.
    """

    def __init__(
        self,
        info: SessionInfo,
        config: ExperimentConfig,
        *,
        root: Path | None,
        source_json: str | None = None,
    ) -> None:
        self.info = info
        self.config = config
        self.layout: SessionLayout | None = None
        if root is not None:
            target = root / info.folder_name
            if target.exists():  # two sessions in the same second: keep both
                target = root / f"{info.folder_name}_{info.session_id[:8]}"
            self.layout = SessionLayout(target)
        self._source_json = source_json
        self.manifest: Manifest | None = None
        self.recorder: DerivedRecorder | None = None
        self._log_handler: logging.Handler | None = None
        self._finalized = False

    # -- lifecycle --------------------------------------------------------------------
    def open(
        self,
        bus: EventBus,
        *,
        raw_streams_to_file: list[str] | None = None,
        raw: RawInfo | None = None,
    ) -> None:
        """Create the directory, freeze the config, start the derived recorder."""
        if self.layout is None:
            return
        self.layout.create()
        resolved = dump_resolved(self.config)
        sha = self.layout.freeze_config(resolved, self._source_json)
        self.manifest = Manifest(
            session_id=self.info.session_id,
            origin=self.info.origin,
            experiment=self.info.experiment,
            participant=self.info.participant,
            started_utc=self.info.started_utc.isoformat(timespec="microseconds"),
            clock_anchor={
                "monotonic_ns": self.info.anchor.monotonic_ns,
                "utc_ns": self.info.anchor.utc_ns,
                "perf_counter_ns": self.info.anchor.perf_counter_ns,
            },
            config_sha256=sha,
            software=SoftwareInfo.collect(),
            raw=raw or RawInfo(),
            enabled_streams=list(self.config.enabled_streams()),
            processors=[p.model_dump(mode="json") for p in self.config.processors if p.enabled],
        )
        self.manifest.write(self.layout.manifest)
        self._attach_log_file()
        derived = [
            o for p in self.config.processors if p.enabled and p.save_output for o in p.outputs
        ]
        self.recorder = DerivedRecorder(
            bus,
            self.layout,
            self.config.storage,
            derived_streams=derived,
            raw_streams=raw_streams_to_file or [],
            nominal_rates={
                sid: REGISTRY.get(sid).nominal_rate_hz for sid in self.config.enabled_streams()
            },
        )
        self.recorder.start()
        log.info("session.directory", path=str(self.layout.root))

    def finalize(
        self,
        *,
        status: Status,
        events: EventLog,
        stream_stats: Mapping[str, StreamStats],
        ended_utc_ns: int,
        extra: Mapping[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> Path | None:
        """Flush everything and write the final manifest. Returns the session root."""
        if self.layout is None or self.manifest is None or self._finalized:
            return None if self.layout is None else self.layout.root
        self._finalized = True
        recorder_stats = {}
        if self.recorder is not None:
            self.recorder.close()
            recorder_stats = self.recorder.stats()
            if self.recorder.errors:
                self.manifest.warnings.extend(f"recorder: {e}" for e in self.recorder.errors)
        n_events = write_events(self.layout.events_file, events.all())
        n_timeline = write_timeline(self.layout)
        for sid, s in stream_stats.items():
            rec = recorder_stats.get(sid)
            self.manifest.streams[sid] = StreamRecord(
                kind="raw" if sid in REGISTRY else "derived",
                received=s.received,
                nominal_rate_hz=s.nominal_rate_hz,
                effective_rate_hz=s.effective_rate_hz,
                transport_dropped=s.transport_dropped,
                out_of_order=s.out_of_order,
                stored_samples=None if rec is None else rec.samples,
                stored_in=None
                if rec is None
                else str(Path(rec.directory).relative_to(self.layout.root)),
                first_device_ns=s.first_device_ns,
                last_device_ns=s.last_device_ns,
            )
        self.manifest.events = n_events
        if warnings:
            self.manifest.warnings.extend(warnings)
        if extra:
            for k, v in extra.items():
                if k == "device" and isinstance(v, dict):
                    self.manifest.device = DeviceInfoRecord(
                        **{**self.manifest.device.model_dump(), **v}
                    )
                else:
                    setattr(self.manifest, k, v)
        self.manifest.finalize(status, ended_utc_ns)
        self.manifest.write(self.layout.manifest)
        self._detach_log_file()
        log.info(
            "session.finalized",
            status=status,
            events=n_events,
            timeline_rows=n_timeline,
            path=str(self.layout.root),
        )
        return self.layout.root

    # -- logging -----------------------------------------------------------------------
    def _attach_log_file(self) -> None:
        if self.layout is None:
            return
        import structlog

        handler = logging.FileHandler(self.layout.session_log, encoding="utf-8")
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
        )
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _detach_log_file(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
