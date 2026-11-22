"""Ping the SignalRGB PC so the coordinator knows when to leave the Govee lights alone.

SignalRGB's Govee addon drives the lights directly and starts before
SignalRGB's first WLED request reaches the coordinator, so WLED traffic
alone always leaves a window where both drive the lights. The PC answers
ping before SignalRGB can send to the lights, so while it answers, the
coordinator yields.

Pings run only while the PC is not known to be on. While a SignalRGB
session is active, the session itself shows the PC is on, so no pings go
out and the PC counts as present. When the session ends, pinging resumes,
and the PC stays present until it misses replies for absent_after seconds.
"""

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


class PingPresence:
    def __init__(self, host: str, interval: float, absent_after: float,
                 known_on: Callable[[], bool] = lambda: False):
        self._host = host
        self._interval = interval
        self._absent_after = absent_after
        self._known_on = known_on
        self._last_reply = 0.0
        self._logged: bool | None = None
        self._pinging: bool | None = None

    @property
    def present(self) -> bool:
        return time.monotonic() - self._last_reply < self._absent_after

    async def _ping(self) -> bool:
        # Timeout = interval. With a 1 s timeout, each ping to a sleeping PC
        # waits 1 s, so a wake is seen up to 1 s late (a 1 s strobe on
        # 2026-11-21). iputils 20240905 accepts a fractional -W.
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", f"{self._interval:g}", self._host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    async def run(self):
        log.info("Ping presence: %s every %.2fs, absent after %.0fs",
                 self._host, self._interval, self._absent_after)
        while True:
            start = time.monotonic()
            pinging = not self._known_on()
            if pinging != self._pinging:
                self._pinging = pinging
                log.info("Ping presence: %s", "pinging %s" % self._host if pinging
                         else "SignalRGB session active, pings paused")
            if not pinging:
                self._last_reply = start
            else:
                try:
                    if await self._ping():
                        self._last_reply = time.monotonic()
                except OSError as e:
                    log.warning("Ping %s failed to run: %s", self._host, e)
            if self.present != self._logged:
                self._logged = self.present
                log.info("Ping presence: %s %s", self._host, "up" if self._logged else "down")
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - start)))
