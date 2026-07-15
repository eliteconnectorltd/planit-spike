"""Per-host request pacing.

Serializes requests to the same host behind a per-host lock and enforces a
minimum delay between them, using the event loop's monotonic clock.
"""

from __future__ import annotations

import asyncio


class HostThrottle:
    """Enforce a minimum delay between consecutive requests to the same host."""

    def __init__(self, delay: float):
        self.delay = delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def wait(self, host: str) -> None:
        """Block until the per-host minimum delay has elapsed, then stamp 'now'."""
        async with self._lock(host):
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last.get(host, 0.0)
            wait_for = last + self.delay - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last[host] = loop.time()
