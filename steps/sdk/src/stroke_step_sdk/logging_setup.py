"""Logging (contract section 3): UTF-8 lines that start with a level word."""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "step"


class _LevelWordFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = "ERROR" if record.levelno >= logging.ERROR else record.levelname
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{level} {message}"


class _FlushingHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def _utf8(stream):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    return stream


def setup(level: int = logging.INFO) -> logging.Logger:
    """Configure the ``step`` logger: INFO and below to stdout, WARNING and above to stderr."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    formatter = _LevelWordFormatter()

    out = _FlushingHandler(_utf8(sys.stdout))
    out.setFormatter(formatter)
    out.addFilter(lambda r: r.levelno < logging.WARNING)
    err = _FlushingHandler(_utf8(sys.stderr))
    err.setFormatter(formatter)
    err.setLevel(logging.WARNING)
    logger.addHandler(out)
    logger.addHandler(err)
    return logger
