"""Limit how often each caller may send messages to a hosted agent.

Each caller has a token bucket holding up to ``RateLimit.requests`` tokens,
refilled evenly over ``RateLimit.per_seconds``. A ``SendMessage`` takes one
token; with none left it is refused with HTTP 429 and a ``Retry-After`` of
the whole seconds until the next token. Callers can burst up to the full
allowance, then continue at the steady rate.

Buckets live in this process's memory, so each host instance limits callers
on its own: behind a load balancer with N instances, a caller may get up to N
times the allowance. For one limit across instances, rate-limit at the
gateway in front of the agents (for example Azure API Management).

Everything runs on one event loop, so the buckets need no lock.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from agentframe.errors import DefinitionError


@dataclass(frozen=True)
class RateLimit:
    """How many messages one caller may send in a period.

    Args:
        requests: How many ``SendMessage`` requests a caller may make in
            ``per_seconds``, and also how many it may make at once in a burst.
        per_seconds: The period ``requests`` is spread over. Defaults to 60.

    Raises:
        DefinitionError: ``requests`` is less than 1 or ``per_seconds`` is
            not positive and finite.
    """

    requests: int
    per_seconds: float = 60

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise DefinitionError("RateLimit requests must be at least 1")
        # An infinite period would never refill, leaving no Retry-After to give.
        if not (self.per_seconds > 0 and math.isfinite(self.per_seconds)):
            raise DefinitionError("RateLimit per_seconds must be positive and finite")


class CallerRateLimiter:
    """Applies one ``RateLimit`` to every caller separately.

    Args:
        limit: The allowance each caller gets.
        clock: Returns the current time in seconds; tests pass a fake clock.
    """

    def __init__(
        self, limit: RateLimit, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._capacity = float(limit.requests)
        self._refill_per_second = limit.requests / limit.per_seconds
        self._clock = clock
        # Caller ID -> (tokens left, time they were counted).
        self._buckets: dict[str | None, tuple[float, float]] = {}
        self._last_prune = clock()
        self._prune_every = limit.per_seconds

    def try_acquire(self, caller_id: str | None) -> float | None:
        """Take one token for ``caller_id``.

        Args:
            caller_id: The verified caller's ID, or ``None`` for an anonymous
                caller. Anonymous callers share one allowance.

        Returns:
            ``None`` when the request may go ahead; otherwise how many seconds
            the caller should wait before its next token.
        """
        now = self._clock()
        self._prune(now)
        tokens = self._tokens(caller_id, now)
        if tokens < 1:
            self._buckets[caller_id] = (tokens, now)
            return (1 - tokens) / self._refill_per_second
        self._buckets[caller_id] = (tokens - 1, now)
        return None

    def _tokens(self, caller_id: str | None, now: float) -> float:
        tokens, counted_at = self._buckets.get(caller_id, (self._capacity, now))
        refilled = tokens + (now - counted_at) * self._refill_per_second
        return min(self._capacity, refilled)

    def _prune(self, now: float) -> None:
        # A full bucket is the same as no bucket, so forget it; this keeps
        # memory bounded by the callers active in the last period.
        if now - self._last_prune < self._prune_every:
            return
        self._last_prune = now
        full = [
            caller
            for caller in self._buckets
            if self._tokens(caller, now) >= self._capacity
        ]
        for caller in full:
            del self._buckets[caller]


def retry_after_header(wait_seconds: float) -> str:
    """Format a wait as a ``Retry-After`` value: whole seconds, at least 1."""
    return str(max(1, math.ceil(wait_seconds)))
