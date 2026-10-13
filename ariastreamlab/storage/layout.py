"""On-disk layout of a session directory (docs/ARCHITECTURE.md §6)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ariastreamlab.core.errors import InvariantError, StorageError
from ariastreamlab.storage.writers import atomic_write_text

MANIFEST = "manifest.json"
EXPERIMENT = "experiment.json"
EXPERIMENT_SOURCE = "experiment.source.json"
RAW_SOURCE = "SOURCE"


@dataclass(frozen=True)
class SessionLayout:
    """Paths inside one session directory."""

    root: Path

    # -- directories -------------------------------------------------------------------
    @property
    def calibration(self) -> Path:  # noqa: D102
        return self.root / "calibration"

    @property
    def raw(self) -> Path:  # noqa: D102
        return self.root / "raw"

    @property
    def raw_mock(self) -> Path:  # noqa: D102
        return self.root / "raw_mock"

    @property
    def derived(self) -> Path:  # noqa: D102
        return self.root / "derived"

    @property
    def events(self) -> Path:  # noqa: D102
        return self.root / "events"

    @property
    def indexes(self) -> Path:  # noqa: D102
        return self.root / "indexes"

    @property
    def quality(self) -> Path:  # noqa: D102
        return self.root / "quality"

    @property
    def logs(self) -> Path:  # noqa: D102
        return self.root / "logs"

    # -- files ---------------------------------------------------------------------------
    @property
    def manifest(self) -> Path:  # noqa: D102
        return self.root / MANIFEST

    @property
    def experiment(self) -> Path:  # noqa: D102
        return self.root / EXPERIMENT

    @property
    def experiment_source(self) -> Path:  # noqa: D102
        return self.root / EXPERIMENT_SOURCE

    @property
    def events_file(self) -> Path:  # noqa: D102
        return self.events / "events.parquet"

    @property
    def timeline(self) -> Path:  # noqa: D102
        return self.indexes / "timeline.parquet"

    @property
    def quality_log(self) -> Path:  # noqa: D102
        return self.quality / "device_quality_stats.jsonl"

    @property
    def quality_summary(self) -> Path:  # noqa: D102
        return self.quality / "summary.json"

    @property
    def session_log(self) -> Path:  # noqa: D102
        return self.logs / "session.log"

    @property
    def raw_source(self) -> Path:  # noqa: D102
        return self.raw / RAW_SOURCE

    def derived_stream(self, stream_id: str) -> Path:
        """Directory of one derived stream."""
        return self.derived / stream_id

    def raw_mock_stream(self, stream_id: str) -> Path:
        """Directory of one mock raw stream."""
        return self.raw_mock / stream_id

    # -- lifecycle -----------------------------------------------------------------------
    def create(self) -> None:
        """Create the directory tree (the session root must not exist yet)."""
        if self.root.exists():
            raise StorageError(f"session directory already exists: {self.root}")
        for d in (
            self.root,
            self.calibration,
            self.raw,
            self.derived,
            self.events,
            self.indexes,
            self.quality,
            self.logs,
        ):
            d.mkdir(parents=True, exist_ok=False)

    @classmethod
    def open_existing(cls, root: Path | str) -> SessionLayout:
        """Layout of an existing session (validated by the presence of the manifest)."""
        p = Path(root)
        if not (p / MANIFEST).exists():
            raise StorageError(
                f"{p} is not a session directory (no {MANIFEST})",
                hint="pass the folder that contains manifest.json",
            )
        return cls(p)

    def guard_raw_write(self, path: Path) -> None:
        """Invariant I-2: nothing but the SDK recorder writes under ``raw/``."""
        try:
            path.resolve().relative_to(self.raw.resolve())
        except ValueError:
            return
        raise InvariantError(f"refusing to write {path} inside raw/ (raw data is immutable)")

    # -- helpers ---------------------------------------------------------------------------
    def freeze_config(self, resolved_json: str, source_json: str | None) -> str:
        """Write ``experiment.json`` (read-only) and ``experiment.source.json``; returns sha256."""
        atomic_write_text(self.experiment, resolved_json)
        os.chmod(self.experiment, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        if source_json is not None:
            atomic_write_text(self.experiment_source, source_json)
        return sha256_text(resolved_json)

    def write_json(self, path: Path, data: Any) -> None:
        """Atomic JSON write with the raw/ guard."""
        self.guard_raw_write(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, default=str))


def sha256_text(text: str) -> str:
    """Hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Hex digest of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
