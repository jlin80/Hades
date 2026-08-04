"""Structured logging. Import :func:`get_logger` — never use ``print``."""

from hades.shared_kernel.logging.errors import describe
from hades.shared_kernel.logging.ring_buffer import LogRingBuffer, get_log_buffer
from hades.shared_kernel.logging.setup import configure_logging, get_logger

__all__ = [
    "LogRingBuffer",
    "configure_logging",
    "describe",
    "get_log_buffer",
    "get_logger",
]
