"""Govee output — drives Govee lights over the LAN API (UDP 4003).

Uses the same streaming mode the SignalRGB Govee addon uses: "razer" mode
is switched on, then each frame is a binary packet, base64 encoded inside a
JSON "razer" command. Devices stretch the segment count they receive across
their physical LEDs.

Only sends. Govee devices reply to UDP 4002 on the sender, but nothing here
needs a reply, and 4002 is often held by another integration on the same
host (govee2mqtt), so this never binds it.
"""

import asyncio
import base64
import json
import logging

from config import Config, GoveeDeviceConfig

log = logging.getLogger(__name__)

GOVEE_CMD_PORT = 4003
RAZER_ON = "uwABsQEK"
RAZER_OFF = "uwABsQAL"


def _frame_packet(protocol: str, colors: list[tuple[int, int, int]]) -> bytes:
    """Build a streaming frame. DreamView carries an XOR checksum, Razer a zero byte."""
    n = len(colors)
    if protocol == "dreamview":
        packet = [0xBB, 0x00, 0x20, 0xB0, 0x01, n]
    else:
        packet = [0xBB, 0x00, 0x0E, 0xB0, 0x01, n]
    for rgb in colors:
        packet.extend(rgb)
    if protocol == "dreamview":
        checksum = 0
        for b in packet:
            checksum ^= b
        packet.append(checksum)
    else:
        packet.append(0)
    return bytes(packet)


class GoveeOutput:
    """Sends power, streaming-mode, and frame commands to Govee devices."""

    def __init__(self, config: Config):
        self._config = config
        self._devices: dict[str, GoveeDeviceConfig] = {d.name: d for d in config.govee}
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def device_names(self) -> list[str]:
        return list(self._devices)

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0),
        )
        log.info("Govee output: %s", ", ".join(f"{d.name}@{d.ip}" for d in self._devices.values()))

    def _send(self, dev: GoveeDeviceConfig, msg: dict):
        if not self._transport:
            return
        try:
            self._transport.sendto(json.dumps({"msg": msg}).encode(), (dev.ip, GOVEE_CMD_PORT))
        except OSError as e:
            log.warning("Govee send to %s failed: %s", dev.name, e)

    async def activate(self):
        """Power on and enter streaming mode (sent twice, as the SignalRGB addon does)."""
        for dev in self._devices.values():
            self._send(dev, {"cmd": "turn", "data": {"value": 1}})
        await asyncio.sleep(0.3)
        for _ in range(2):
            for dev in self._devices.values():
                self._send(dev, {"cmd": "razer", "data": {"pt": RAZER_ON}})
            await asyncio.sleep(0.1)
        log.info("Govee activated")

    async def deactivate(self):
        """Power off.

        Deliberately no streaming-off (RAZER_OFF): leaving streaming mode makes
        the device show its stored scene until the power-off lands, which reads
        as a flash. activate() re-enters streaming mode on the next power-on.
        """
        for dev in self._devices.values():
            self._send(dev, {"cmd": "turn", "data": {"value": 0}})
        log.debug("Govee deactivated")

    def send_frames(self, frames: dict[str, list[tuple[int, int, int, int]]], brightness: int = 100):
        """Push one frame to each Govee device present in frames."""
        for name, colors in frames.items():
            dev = self._devices.get(name)
            if dev is None:
                continue
            scale = brightness / 100.0 * dev.brightness / 100.0
            rgb = [(int(r * scale), int(g * scale), int(b * scale)) for r, g, b, _w in colors]
            pt = base64.b64encode(_frame_packet(dev.protocol, rgb)).decode()
            self._send(dev, {"cmd": "razer", "data": {"pt": pt}})

    def close(self):
        if self._transport:
            self._transport.close()
            self._transport = None
