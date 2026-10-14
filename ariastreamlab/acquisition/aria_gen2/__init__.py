"""The only package that imports Meta's SDK (``aria.sdk_gen2``, ``projectaria_tools``).

Everything here is imported lazily by the rest of the framework, so a laptop without the
SDK can still run mock experiments, replay stored sessions and process derived data.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ariastreamlab.core.errors import DeviceError

if TYPE_CHECKING:
    from ariastreamlab.acquisition.aria_gen2.adapter import AriaGen2Source
    from ariastreamlab.acquisition.base import DeviceTransition
    from ariastreamlab.config.models import ExperimentConfig
    from ariastreamlab.timing.clock import HostClock

__all__ = ["make_live_source", "sdk_available", "tools_available"]


def sdk_available() -> bool:
    """True when ``aria.sdk_gen2`` can be imported."""
    return importlib.util.find_spec("aria") is not None and (
        importlib.util.find_spec("aria.sdk_gen2") is not None
    )


def tools_available() -> bool:
    """True when ``projectaria_tools`` can be imported."""
    return importlib.util.find_spec("projectaria_tools") is not None


def make_live_source(
    config: ExperimentConfig,
    *,
    session_id: str,
    clock: HostClock | None = None,
    on_transition: Callable[[DeviceTransition], None] | None = None,
    **kw: Any,
) -> AriaGen2Source:
    """Build the live source (imports the SDK now; raises :class:`DeviceError` if absent)."""
    if not sdk_available():
        raise DeviceError(
            "live acquisition needs Meta's Project Aria client SDK, which is not installed here",
            hint="pip install 'ariastreamlab[aria]' on the laptop that talks to the glasses, "
            "or run with --mock",
        )
    from ariastreamlab.acquisition.aria_gen2.adapter import AriaGen2Source

    return AriaGen2Source(
        config, session_id=session_id, clock=clock, on_transition=on_transition, **kw
    )
