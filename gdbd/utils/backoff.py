"""Exponential backoff with jitter, shared by every network retry path."""

from __future__ import annotations

import random

from ..constants import RETRY_INITIAL_DELAY, RETRY_MAX_DELAY, RETRY_MULTIPLIER


def backoff_delay(
    attempt: int,
    initial: float = RETRY_INITIAL_DELAY,
    multiplier: float = RETRY_MULTIPLIER,
    maximum: float = RETRY_MAX_DELAY,
    jitter: bool = True,
) -> float:
    """Delay in seconds before retry number ``attempt`` (1-based).

    Follows ``2s, 4s, 8s, 16s, ...`` capped at :data:`RETRY_MAX_DELAY`, with a
    small amount of random jitter so concurrent workers do not all retry in
    lockstep after a rate-limit response.
    """
    if attempt < 1:
        attempt = 1
    delay = initial * (multiplier ** (attempt - 1))
    delay = min(delay, maximum)
    if jitter:
        delay *= random.uniform(0.8, 1.2)
    return round(min(delay, maximum), 3)
