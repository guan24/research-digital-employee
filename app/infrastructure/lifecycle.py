from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_callbacks: list[Callable[[], None]] = []


def register_shutdown_callback(callback: Callable[[], None]) -> None:
    """Register a process-scoped resource closer without coupling runtime to a provider."""
    with _lock:
        if callback not in _callbacks:
            _callbacks.append(callback)


def close_registered_resources() -> None:
    """Close registered process resources in reverse registration order."""
    with _lock:
        callbacks = list(reversed(_callbacks))
        _callbacks.clear()
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.exception("process resource shutdown callback failed")


atexit.register(close_registered_resources)
