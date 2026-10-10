"""AriaStreamLab — experiment framework for Meta Project Aria Gen 2 glasses."""

from importlib.metadata import PackageNotFoundError, version

from ariastreamlab.api import Experiment
from ariastreamlab.core.errors import AriaStreamLabError
from ariastreamlab.core.sample import DerivedSample, Sample

try:
    __version__ = version("ariastreamlab")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = ["AriaStreamLabError", "DerivedSample", "Experiment", "Sample", "__version__"]
