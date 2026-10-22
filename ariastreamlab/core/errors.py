"""User-facing exception hierarchy.

Every error raised on purpose by AriaStreamLab derives from :class:`AriaStreamLabError` and
carries a message written for a person who may never have seen the Meta SDK. Wrap SDK
exceptions at the adapter boundary; never let a raw C++ stack trace be the first thing a
researcher reads.
"""

from __future__ import annotations


class AriaStreamLabError(Exception):
    """Base class for all AriaStreamLab errors."""

    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n  hint: {self.hint}" if self.hint else base


class ConfigError(AriaStreamLabError):
    """The experiment JSON is invalid or references something unknown."""


class RegistryError(AriaStreamLabError):
    """Unknown stream, parameter or payload type."""


class DeviceError(AriaStreamLabError):
    """The glasses could not be reached, configured or controlled."""

    recoverable: bool = False


class AcquisitionError(AriaStreamLabError):
    """A source failed while producing samples."""


class ProcessingError(AriaStreamLabError):
    """A processor failed to set up or to process an input."""


class StorageError(AriaStreamLabError):
    """A session directory could not be created, written or finalized."""


class ReplayError(AriaStreamLabError):
    """A recorded session could not be replayed."""


class InvariantError(AriaStreamLabError):
    """One of the architectural invariants (raw immutability, timestamp integrity…) was broken."""
