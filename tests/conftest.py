"""Shared fixtures. Unit tests never touch hardware; hardware tests are opt-in."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ARIASTREAMLAB_DASHBOARD", "0")  # tests enable it explicitly

from ariastreamlab.config.models import MockConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.sample import Provenance, Sample


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ARIA_HARDWARE") == "1":
        return
    skip = pytest.mark.skip(reason="needs Aria Gen 2 glasses; set ARIA_HARDWARE=1 to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def bus() -> EventBus:
    b = EventBus()
    yield b  # type: ignore[misc]
    b.close()


@pytest.fixture
def provenance() -> Provenance:
    return Provenance("mock", "test", "session-0")


@pytest.fixture
def make_sample(provenance: Provenance):
    def _make(
        stream_id: str = "imu_left",
        device_ns: int = 1_000,
        host_ns: int = 2_000,
        seq: int | None = 0,
        data: object = None,
    ) -> Sample:
        return Sample("mock-0", stream_id, device_ns, host_ns, seq, data, provenance)

    return _make


@pytest.fixture
def fast_mock() -> MockConfig:
    return MockConfig(seed=1, jitter_ms=0.0, realtime=False)
