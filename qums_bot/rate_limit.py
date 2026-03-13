from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import time


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, *, bucket: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time()
        with self._lock:
            queue = self._events[bucket]
            cutoff = now - window_seconds
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= limit:
                retry_after = max(1, int(queue[0] + window_seconds - now))
                return RateLimitResult(allowed=False, retry_after_seconds=retry_after)
            queue.append(now)
            return RateLimitResult(allowed=True, retry_after_seconds=0)
