"""Realtime processing graph: processors, plugin discovery, workers (Phase 3).

Public API for writing a processor::

    from ariastreamlab.processing import Processor, Inputs, Emit, OutputSpec

See docs/tutorials/custom_processors.md.
"""

from ariastreamlab.processing.base import (
    Emit,
    Inputs,
    Output,
    OutputSpec,
    Processor,
    ProcessorContext,
)
from ariastreamlab.processing.graph import (
    ProcessingGraph,
    ResolvedProcessor,
    resolve_processor,
    validate_processors,
)
from ariastreamlab.processing.plugins import BUILTINS, ProcessorInfo, list_processors, resolve
from ariastreamlab.processing.runner import ProcessorStats, ProcessorWorker

__all__ = [
    "BUILTINS",
    "Emit",
    "Inputs",
    "Output",
    "OutputSpec",
    "ProcessingGraph",
    "Processor",
    "ProcessorContext",
    "ProcessorInfo",
    "ProcessorStats",
    "ProcessorWorker",
    "ResolvedProcessor",
    "list_processors",
    "resolve",
    "resolve_processor",
    "validate_processors",
]
