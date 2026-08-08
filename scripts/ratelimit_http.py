"""
ratelimit_http.py — per-client rate limiting for public HTTP endpoints.

``/api/track`` and ``/api/contact`` must stay unauthenticated — the demo
landing page calls them from a prospect's browser with no API key. That
makes them the app's only open attack surface: without limits, anyone who
finds the URL can flood them, and ``/api/track`` both writes to disk and
triggers SMTP alert emails. This module caps how often a single client
(by IP) may hit those endpoints.

Design:
  • Fixed-window counter per (client_ip, bucket): simple, O(1), and needs
    no external store (no Redis) — correct for a single Render instance.
  • Windows are keyed by wall-clock second/minute buckets so old state
    ages out naturally; a tiny sweep keeps memory bounded even under a
    flood of distinct spoofed IPs.
  • Thread-safe (a Flask request may be handled on any worker thread).

Not a distributed limiter — if you later scale to multiple instances,
swap the store for Redis behind the same ``allow()`` interface.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class RateLimiter:
    """Fixed-window per-key limiter. ``allow(key)`` returns True if the
    call is within budget for the current window, else False.

    Args:
        max_requests: Allowed requests per window.
        window_seconds: Window length in seconds.
        max_keys: Hard cap on tracked keys; when exceeded, the oldest
            windows are swept so a flood of unique IPs can't grow memory
            without bound.
    """

    def __init__(self, max_requests: int, window_seconds: int, max_keys: int = 10_000) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self.max_keys = max_keys
        self._counts: dict[tuple[str, int], int] = defaultdict(int)
        self._lock = threading.Lock()

    def _current_window(self) -> int:
        return int(time.time() // self.window)

    def allow(self, key: str) -> bool:
        win = self._current_window()
        composite = (key, win)
        with self._lock:
            # Opportunistic sweep of stale windows to bound memory.
            if len(self._counts) > self.max_keys:
                self._counts = defaultdict(
                    int,
                    {k: v for k, v in self._counts.items() if k[1] == win},
                )
            count = self._counts[composite]
            if count >= self.max_requests:
                return False
            self._counts[composite] = count + 1
            return True

    def retry_after(self) -> int:
        """Seconds until the current window rolls over (for Retry-After)."""
        return int(self.window - (time.time() % self.window)) or 1
