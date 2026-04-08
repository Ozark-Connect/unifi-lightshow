"""Switch transport — sends color frames via UDP to on-switch daemons.

Also handles SSH bootstrap: ensures the etherlightd_udp daemon is running
on each switch by SSHing once on startup to kick off the persistent script.
"""

import asyncio
import json
import logging
import socket

import asyncssh

from config import Config, SwitchConfig

log = logging.getLogger(__name__)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"{r:02x}{g:02x}{b:02x}"


class SwitchConnection:
    """Manages UDP communication to one switch's etherlightd_udp daemon."""

    def __init__(self, switch: SwitchConfig, config: Config):
        self.switch = switch
        self._config = config
        self._sock: socket.socket | None = None
        self._addr: tuple[str, int] = (switch.host, 9200)
        self._connected = False
        self._last_colors: list[str | None] = [None] * switch.num_ports

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self):
        """Bootstrap the on-switch daemon via SSH, then open UDP socket."""
        # SSH once to ensure the daemon is running
        try:
            conn = await asyncssh.connect(
                self.switch.host,
                username=self.switch.user,
                known_hosts=self._config.ssh_known_hosts,
                keepalive_interval=15,
                keepalive_count_max=3,
            )
            # Run the persistent startup script
            await conn.run(
                "nohup /etc/persistent/start_etherlightd_udp.sh > /dev/null 2>&1 &",
                timeout=10,
            )
            log.info("[%s] Bootstrap: daemon started via SSH", self.switch.name)
            conn.close()
        except Exception as e:
            log.warning("[%s] SSH bootstrap failed (daemon may already be running): %s", self.switch.name, e)

        # Open UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._connected = True
        log.info("[%s] UDP transport ready → %s:%d", self.switch.name, *self._addr)

    async def send_frame(self, colors: list[tuple[int, int, int, int]], brightness: int = 100):
        """Send a color frame via UDP. The on-switch daemon handles ubus calls."""
        if not self._sock:
            return

        ports = []
        for r, g, b, w in colors[:self.switch.num_ports]:
            ports.append([r, g, b, w])

        frame = json.dumps({"ports": ports, "brightness": brightness})
        try:
            self._sock.sendto(frame.encode(), self._addr)
        except OSError as e:
            log.warning("[%s] UDP send failed: %s", self.switch.name, e)


    async def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None
            self._connected = False


class SSHTransport:
    """Manages UDP connections to all switches in the fleet."""

    def __init__(self, config: Config):
        self._config = config
        self._connections: dict[str, SwitchConnection] = {}
        for sw in config.switches:
            self._connections[sw.name] = SwitchConnection(sw, config)

    @property
    def connections(self) -> dict[str, SwitchConnection]:
        return self._connections

    def all_connected(self) -> bool:
        return all(c.connected for c in self._connections.values())

    async def connect_all(self):
        await asyncio.gather(*(c.connect() for c in self._connections.values()))

    async def send_frames(self, frames: dict[str, list[tuple[int, int, int, int]]], brightness: int = 100):
        tasks = []
        for name, colors in frames.items():
            conn = self._connections.get(name)
            if conn:
                tasks.append(conn.send_frame(colors, brightness))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all(self):
        await asyncio.gather(*(c.close() for c in self._connections.values()))
