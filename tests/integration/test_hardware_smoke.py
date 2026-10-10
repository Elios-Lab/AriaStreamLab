"""Hardware-only checks. Run with ``ARIA_HARDWARE=1 pytest -m hardware`` on a paired laptop."""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.hardware


def test_sdk_importable() -> None:
    assert importlib.util.find_spec("aria.sdk_gen2") is not None, (
        "pip install 'ariastreamlab[aria]'"
    )
