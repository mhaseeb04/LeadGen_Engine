"""
ratelimit.py — Thread-safe token-bucket rate limiter.

The old code paced AI calls with a blanket ``time.sleep(4)`` after every
single request. That's simple but wastefully serial: it forces a 4-second
gap even while a response is still in flight, so 60 calls take at least
240s of pure sleeping regardless of how fast the API actually responds.

A token bucket lets several worker threads run concurrently while still
never exceeding the account's real requests-per-minute (RPM) limit. Each
worker calls :meth:`RateLimiter.acquire` before an API call; it returns
immediately if a token is available and otherwise blocks only as long as
strictly necessary. Network latency of overlapping calls then hides
inside the RPM budget instead of stacking on top of fixed sleeps.

This is shared by enricher.py and email_generator.py so both honour one
consistent, configurable limit. Bump ``rpm`` (or set a paid-tier value
in .env) and everything scales up automatically — no code changes.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """A simple, thread-safe token-bucket limiter.

    Args:
        rpm: Maximum requests per minute across all threads sharing this
            instance. The bucket refills continuously at ``rpm/60`` tokens
            per second, so short bursts up to ``capacity`` are allowed and
            the long-run average never exceeds ``rpm``.
        capacity: Max tokens the bucket can hold (burst size). Defaults to
            ``rpm`` — i.e. up to a full minute's worth can burst at once
            after an idle period, which is the desired behaviour here.
    """

    def __init__(self, rpm: float, capacity: float | None = None) -> None:
        self.rate_per_sec = max(rpm, 1) / 60.0
        self.capacity = float(capacity if capacity is not None else max(rpm, 1))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them.

        Safe to call from many threads at once. The sleep is computed
        while holding the lock but performed outside it, so waiting
        threads don't serialise on each other's sleeps.
        """
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # How long until enough tokens accrue?
                deficit = tokens - self._tokens
                wait = deficit / self.rate_per_sec
            time.sleep(min(wait, 1.0))  # cap per-iteration sleep so refill stays responsive
