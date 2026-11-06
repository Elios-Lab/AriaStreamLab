"""Structured logging setup (structlog) shared by the CLI and the Python API."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False
_console: logging.Handler | None = None


def configure_logging(level: str = "INFO", *, json_lines: bool = False, force: bool = True) -> None:
    """Configure structlog + stdlib logging (level and console format).

    An explicit call always applies — it replaces the defaults :func:`get_logger` installs
    lazily (at import time, in practice), which is how an experiment's ``logging`` section
    takes effect. Reconfiguring swaps only the console handler installed here, so other
    handlers on the root logger (a session's ``logs/session.log``) are kept.

    Args:
        level: ``DEBUG`` … ``ERROR``.
        json_lines: Emit one JSON object per line on the console (for CI and log shippers).
        force: ``False`` makes the call a no-op once logging has been configured.
    """
    global _configured, _console
    if _configured and not force:
        return
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_lines
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    if _console is not None and _console in root.handlers:
        root.handlers[root.handlers.index(_console)] = handler
    elif not _configured:
        root.handlers[:] = [handler]
    else:
        root.addHandler(handler)
    _console = handler
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A bound logger for ``name`` (configures defaults lazily)."""
    if not _configured:
        configure_logging(force=False)
    return structlog.get_logger(name)  # type: ignore[no-any-return]
