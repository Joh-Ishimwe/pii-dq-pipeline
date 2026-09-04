"""
Retry with exponential backoff.

The only interesting decision in retry logic is WHAT NOT TO RETRY.

  TRANSIENT  - the world was briefly broken. Timeouts, connection resets,
               HTTP 500/502/503/504, a locked file, a rate limit.
               -> retry. It will probably work in 4 seconds.

  PERMANENT  - the request itself is wrong. HTTP 404, 400, 401, 403,
               a missing file, a malformed CSV, a bad schema.
               -> do NOT retry. Retrying a 404 five times just turns one
                  fast failure into a slow one, and buries the real error.

Backoff is exponential (1s, 2s, 4s, 8s) with JITTER - a small random offset.
Without jitter, 200 pipelines that all failed at 03:00 all retry at 03:01
simultaneously and knock the recovering service straight back over. This is
called a thundering herd, and jitter is the one-line fix.
"""

from __future__ import annotations

import functools
import random
import time
from typing import Callable, TypeVar

from src.monitoring.logging import get_logger

log = get_logger("utils.retry")

T = TypeVar("T")


class TransientError(Exception):
    """Worth retrying: the operation may succeed if repeated."""


class PermanentError(Exception):
    """Not worth retrying: the request itself is wrong."""


def retry(
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[Exception], ...] = (TransientError, TimeoutError, ConnectionError),
) -> Callable:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except PermanentError:
                    log.error("%s failed permanently - not retrying", fn.__name__)
                    raise
                except retry_on as exc:
                    last = exc
                    if attempt == attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay += random.uniform(0, delay * 0.25)   # jitter
                    log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                                fn.__name__, attempt, attempts, type(exc).__name__, delay)
                    time.sleep(delay)
            log.error("%s exhausted %d attempts", fn.__name__, attempts)
            raise TransientError(
                f"{fn.__name__} failed after {attempts} attempts: {last}") from last
        return wrapper
    return decorator
