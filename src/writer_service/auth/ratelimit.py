"""Login throttling.

Two budgets per attempt (both must have room):

* ``pair:<username>|<client>`` – guesses at one account from one client (default 5 / 15 min).
  Keyed per client so an attacker elsewhere can't lock the real admin out.
* ``client:<client>`` – all guesses from one client, across usernames (default 20 / 15 min),
  against password spraying.

``attempt()`` reserves a slot *before* the slow password check, under a lock, so parallel
requests can't exceed the budget; a successful login releases its pair budget.

Memory is bounded: lookups never create keys, empty keys are pruned, and at most
``max_keys`` keys are kept (oldest activity evicted first).

In-memory and per process: fine for a single-instance admin service. Behind several workers
or instances, use a shared store (e.g. Redis) with the same interface. Behind a reverse proxy,
run uvicorn with ``--proxy-headers --forwarded-allow-ips=<proxy>`` so ``client`` is the real
client address; otherwise every user shares the proxy's address and client budget.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable


def username_key(username: str) -> str:
    """Case- and accent-insensitive form, matching MySQL's default ``*_ai_ci`` collation (which
    treats 'admin', 'ADMIN' and 'ädmin' as the same username)."""
    decomposed = unicodedata.normalize("NFKD", username)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


class LoginLimiter:
    def __init__(
        self,
        max_per_pair: int = 5,
        max_per_client: int = 20,
        window_seconds: float = 15 * 60,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = {"pair": max_per_pair, "client": max_per_client}
        self.window = window_seconds
        self.max_keys = max_keys
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @staticmethod
    def keys(username: str, client: str) -> tuple[str, str]:
        return f"pair:{username_key(username)}|{client}", f"client:{client}"

    def _count(self, key: str, now: float) -> int:
        q = self._hits.get(key)
        if q is None:
            return 0
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._hits[key]
            return 0
        return len(q)

    def _limit(self, key: str) -> int:
        return self.limits[key.split(":", 1)[0]]

    def attempt(self, *keys: str) -> bool:
        """Reserve one attempt on every key; False (and nothing reserved) if any is full."""
        with self._lock:
            now = self.clock()
            if any(self._count(k, now) >= self._limit(k) for k in keys):
                return False
            for k in keys:
                self._hits.setdefault(k, deque()).append(now)
                self._hits.move_to_end(k)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def size(self) -> int:
        return len(self._hits)
