"""Watch for the SignalRGB PC so the coordinator knows when to leave the Govee lights alone.

SignalRGB's Govee addon drives the lights directly and starts before
SignalRGB's first WLED request reaches the coordinator, so WLED traffic
alone always leaves a window where both drive the lights.

Two signals show the PC is up:

- Ping replies. Pings run only while the PC is not known to be on. While
  a SignalRGB session is active, the session itself shows the PC is on, so
  no pings go out and the PC counts as present.
- Any Ethernet frame from the PC's MAC (optional, needs a NIC on the PC's
  LAN). After a wake, Windows can drop ping until it has classified the
  network, and SignalRGB does not wait for that (2026-11-21 23:23: the
  lights strobed before the first ping reply). The PC's ARP, DHCP and
  IPv6 neighbor discovery broadcasts go out as soon as its link is up.
  A kernel BPF filter passes only frames with that source MAC.

The PC counts as present until neither signal is seen for absent_after
seconds.
"""

import asyncio
import ctypes
import logging
import socket
import struct
import time
from typing import Callable

log = logging.getLogger(__name__)

ETH_P_ALL = 0x0003
SO_ATTACH_FILTER = 26


def _src_mac_filter(mac: str) -> tuple[bytes, ctypes.Array]:
    """Classic BPF program that keeps frames whose Ethernet source is mac."""
    b = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(b) != 6:
        raise ValueError(f"bad MAC: {mac}")
    hi = int.from_bytes(b[:2], "big")
    lo = int.from_bytes(b[2:], "big")
    prog = [
        (0x28, 0, 0, 6),      # ldh [6]         source MAC, first 2 bytes
        (0x15, 0, 3, hi),     # jeq hi, else drop
        (0x20, 0, 0, 8),      # ld  [8]         source MAC, last 4 bytes
        (0x15, 0, 1, lo),     # jeq lo, else drop
        (0x06, 0, 0, 64),     # ret 64          keep, first 64 bytes
        (0x06, 0, 0, 0),      # ret 0           drop
    ]
    buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *i) for i in prog))
    fprog = struct.pack("HL", len(prog), ctypes.addressof(buf))
    return fprog, buf


class PingPresence:
    def __init__(self, host: str, interval: float, absent_after: float,
                 known_on: Callable[[], bool] = lambda: False,
                 mac: str | None = None, iface: str | None = None):
        self._host = host
        self._interval = interval
        self._absent_after = absent_after
        self._known_on = known_on
        self._mac = mac
        self._iface = iface
        self._last_reply = 0.0
        self._logged: bool | None = None
        self._pinging: bool | None = None
        self._sock: socket.socket | None = None
        self._filter_buf = None

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

    def _on_frame(self):
        try:
            while True:
                self._sock.recv(64)
                self._last_reply = time.monotonic()
        except BlockingIOError:
            pass
        except OSError as e:
            log.warning("Frame watch on %s failed: %s", self._iface, e)

    def _start_frame_watch(self):
        if not (self._mac and self._iface):
            return
        try:
            fprog, self._filter_buf = _src_mac_filter(self._mac)
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            s.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)
            s.bind((self._iface, 0))
            s.setblocking(False)
            # Frames queued before the filter was attached are not filtered.
            try:
                while True:
                    s.recv(65535)
            except BlockingIOError:
                pass
        except (OSError, ValueError) as e:
            log.warning("Frame watch for %s on %s unavailable: %s", self._mac, self._iface, e)
            return
        self._sock = s
        asyncio.get_running_loop().add_reader(s.fileno(), self._on_frame)
        log.info("Frame watch: any frame from %s on %s counts as up", self._mac, self._iface)

    async def run(self):
        log.info("Ping presence: %s every %.2fs, absent after %.0fs",
                 self._host, self._interval, self._absent_after)
        self._start_frame_watch()
        try:
            while True:
                start = time.monotonic()
                pinging = not self._known_on()
                if pinging != self._pinging:
                    self._pinging = pinging
                    log.info("Ping presence: %s", "pinging %s" % self._host if pinging
                             else "SignalRGB session active, pings paused")
                if not pinging:
                    self._last_reply = start
                elif not self.present or time.monotonic() - self._last_reply > self._interval:
                    try:
                        if await self._ping():
                            self._last_reply = time.monotonic()
                    except OSError as e:
                        log.warning("Ping %s failed to run: %s", self._host, e)
                if self.present != self._logged:
                    self._logged = self.present
                    log.info("Ping presence: %s %s", self._host, "up" if self._logged else "down")
                await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - start)))
        finally:
            if self._sock:
                asyncio.get_running_loop().remove_reader(self._sock.fileno())
                self._sock.close()
