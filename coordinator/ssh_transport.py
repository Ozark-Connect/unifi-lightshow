"""Switch transport — sends color frames via UDP to on-switch daemons.

Also handles SSH bootstrap: ensures the etherlightd_udp daemon is running
on each switch by SSHing once on startup to kick off the persistent script.
"""

import asyncio
import json
import logging
import pathlib
import socket

import asyncssh

from config import Config, SwitchConfig

log = logging.getLogger(__name__)

SWITCH_DAEMON_DIR = "/etc/persistent"
SWITCH_BINARY = f"{SWITCH_DAEMON_DIR}/etherlightd_udp"
SWITCH_START_SCRIPT = f"{SWITCH_DAEMON_DIR}/start_etherlightd_udp.sh"
LOCAL_DAEMON_DIR = pathlib.Path("/app/switch-daemon")


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
        # Called with (switch name, packet) for each port stats packet the
        # daemon sends back on this socket
        self.on_stats = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self):
        """Bootstrap the on-switch daemon via SSH, then open UDP socket.

        Auto-deploys the binary and start script if not present on the switch.
        """
        try:
            conn = await asyncssh.connect(
                self.switch.host,
                username=self.switch.user,
                known_hosts=self._config.ssh_known_hosts,
                keepalive_interval=15,
                keepalive_count_max=3,
            )

            result = await conn.run(
                f"test -f {SWITCH_BINARY}", check=False, timeout=5,
            )
            if result.exit_status != 0:
                log.info("[%s] Daemon not found, deploying...", self.switch.name)
                await asyncssh.scp(
                    LOCAL_DAEMON_DIR / "etherlightd_udp",
                    (conn, SWITCH_BINARY),
                )
                await asyncssh.scp(
                    LOCAL_DAEMON_DIR / "start_etherlightd_udp.sh",
                    (conn, SWITCH_START_SCRIPT),
                )
                await conn.run(
                    f"chmod +x {SWITCH_BINARY} {SWITCH_START_SCRIPT}",
                    timeout=10,
                )
                log.info("[%s] Deploy complete", self.switch.name)

            await conn.run(
                f"nohup {SWITCH_START_SCRIPT} > /dev/null 2>&1 &",
                timeout=10,
            )
            log.info("[%s] Bootstrap: daemon started via SSH", self.switch.name)
            conn.close()
        except Exception as e:
            log.warning("[%s] SSH bootstrap failed (daemon may already be running): %s", self.switch.name, e)

        # Open UDP socket. The daemon sends port stats back to the address
        # and port our frames come from, so the same socket receives them.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        asyncio.get_running_loop().add_reader(self._sock.fileno(), self._on_readable)
        self._connected = True
        log.info("[%s] UDP transport ready → %s:%d", self.switch.name, *self._addr)

    def _on_readable(self):
        while self._sock:
            try:
                data, _ = self._sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                # ICMP port unreachable from a stopped daemon lands here
                log.debug("[%s] UDP recv failed: %s", self.switch.name, e)
                return
            if self.on_stats:
                try:
                    self.on_stats(self.switch.name, data)
                except Exception as e:
                    log.warning("[%s] Stats handler failed: %s", self.switch.name, e)

    async def send_frame(self, colors: list[tuple[int, int, int, int]], brightness: int = 100,
                         force: bool = False):
        """Send a color frame via UDP. The on-switch daemon handles ubus calls.

        force makes the daemon write every port, including ports it thinks
        are unchanged.
        """
        if not self._sock:
            return

        ports = []
        for r, g, b, w in colors[:self.switch.num_ports]:
            ports.append([r, g, b, w])

        payload = {"ports": ports, "brightness": brightness}
        if force:
            payload["force"] = True
        frame = json.dumps(payload)
        try:
            self._sock.sendto(frame.encode(), self._addr)
        except OSError as e:
            log.warning("[%s] UDP send failed: %s", self.switch.name, e)


    async def close(self):
        if self._sock:
            asyncio.get_running_loop().remove_reader(self._sock.fileno())
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

    def set_stats_handler(self, handler):
        """handler(switch name, packet) receives each port stats packet."""
        for c in self._connections.values():
            c.on_stats = handler

    async def connect_all(self):
        await asyncio.gather(*(c.connect() for c in self._connections.values()))

    async def send_frames(self, frames: dict[str, list[tuple[int, int, int, int]]], brightness: int = 100,
                          force: bool = False):
        tasks = []
        for name, colors in frames.items():
            conn = self._connections.get(name)
            if conn:
                tasks.append(conn.send_frame(colors, brightness, force))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all(self):
        await asyncio.gather(*(c.close() for c in self._connections.values()))
