from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Simple async token bucket. `rate` tokens per second, burst = max(1, rate)."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else max(1.0, float(rate))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self.capacity:
            raise ValueError("cost exceeds bucket capacity")
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(wait)
