"""Core data model: samples, payloads, stream registry, event bus and errors."""

from ariastreamlab.core.bus import BusStats, DropPolicy, EventBus, Subscription, SubscriptionStats
from ariastreamlab.core.errors import (
    AcquisitionError,
    AriaStreamLabError,
    ConfigError,
    DeviceError,
    InvariantError,
    ProcessingError,
    RegistryError,
    ReplayError,
    StorageError,
)
from ariastreamlab.core.registry import REGISTRY, ParameterSpec, StreamDescriptor, StreamRegistry
from ariastreamlab.core.sample import AnySample, DerivedSample, Origin, Provenance, Sample

__all__ = [
    "REGISTRY",
    "AcquisitionError",
    "AnySample",
    "AriaStreamLabError",
    "BusStats",
    "ConfigError",
    "DerivedSample",
    "DeviceError",
    "DropPolicy",
    "EventBus",
    "InvariantError",
    "Origin",
    "ParameterSpec",
    "ProcessingError",
    "Provenance",
    "RegistryError",
    "ReplayError",
    "Sample",
    "StorageError",
    "StreamDescriptor",
    "StreamRegistry",
    "Subscription",
    "SubscriptionStats",
]
