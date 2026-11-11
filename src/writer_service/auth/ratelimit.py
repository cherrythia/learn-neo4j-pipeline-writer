"""Login throttling: too many failures for a username (or from one client) locks it briefly.

In-memory and per process: enough for a single-instance admin service. Behind several workers
or instances, swap in a shared store (e.g. Redis) with the same interface.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class LoginLimiter:
    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self.clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._recent(k)) >= self.max_failures for k in keys)

    def record_failure(self, *keys: str) -> None:
        now = self.clock()
        for k in keys:
            self._recent(k).append(now)

    def reset(self, *keys: str) -> None:
        for k in keys:
            self._failures.pop(k, None)
