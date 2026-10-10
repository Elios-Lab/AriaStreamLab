"""Session identity and lifecycle (storage layout and manifest arrive in Phase 2)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ariastreamlab.core.sample import Origin
from ariastreamlab.timing.clock import ClockAnchor

_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")


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
